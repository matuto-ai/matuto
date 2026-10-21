"""The one router launcher: every start of the agentic serving unit runs through it.

`matuto-app-router.service` is the unit the serving status names, and its ExecStart is this
package's `router` command rather than llama-server itself. A start from the
Overview routes, a terminal `systemctl --user start` and systemd's own crash
restart therefore run the same launcher, and the card-lease rules apply to all
three (RUN-4). Nothing here is a second way to serve: the unit is generated with
no `[Install]` section and no dependency on the manager's unit, so nothing starts
it at login or sign-in, an operator's Stop is not undone by a restart, and a
manager failure leaves the router running (SERVE-5, START-3).

The launcher's order is the rules' order. It takes the card-admission decision first, under
the same store write transaction the runner's admission takes, so a run admitted first
refuses the start and a start that came first is what a later admission reads as
serving up. Then the launch-time validation runs over each published profile's
server rows against the active build's advertised options, and only after both
does the router process start. Every refusal is the operator's own reason, and
nothing is started or left half-started after one.

The launch inputs are the ones the store holds: the runtime selection of build,
backend and active card, and the generated preset, discovery directory and
the one API key at the path CUT-4 keeps. The key is handed to llama-server by
path and never read into a unit, an answer or a message.
"""

import contextlib
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from matuto import matuto_runtime
from matuto import (admission, arguments, installation, recipes, registry, runner,
                    serving_state, store)
from matuto.matuto_runtime import InspectionError

# The unit and the fixed loopback address consumers use today (Compatibility).
UNIT = serving_state.ROUTER_UNIT
HOST = serving_state.HOST
PORT = serving_state.ROUTER_PORT
# The router keeps one loaded model; each preset's own layers decide what is offloaded.
MODELS_MAX = "1"
# The backend names the store records and the names the runtime's own APIs use.
BACKENDS = {"hip": "HIP/ROCm", "vulkan": "Vulkan"}
# The command systemd runs. It is this package's own entry point, so the unit names
# the launcher rather than a server this module would have to keep in step.
COMMAND = "router"
CLI = Path(__file__).resolve().parent / "cli.py"
LIFECYCLE_TIMEOUT = 30
# The process name a llama-server runs under, read from /proc. Every one of them
# except the router unit's own process is a foreign holder of the card.
ROUTER_PROCESS = "llama-server"
# The busy timeout a launcher waits for the runner's admission to release the store.
STORE_TIMEOUT_MS = 60_000


class LauncherRefused(Exception):
    """The launcher refuses; the reason is the operator's, not a traceback's."""


@dataclass
class Plan:
    """One launch as it will run: its argv, its environment and the paths in them.

    `directory` and `descriptor` stay open so the caller can confine and exec the
    server it opened; a caller that only checks the launch closes them.
    """

    argv: tuple
    environment: dict
    selector: str
    model_directory: str
    presets: str
    key_path: str
    server: str
    directory: int = None
    descriptor: int = None

    def close(self):
        """Release the opened server. A caller that execs never reaches this."""
        for held in (self.descriptor, self.directory):
            if held is not None:
                with contextlib.suppress(OSError):
                    os.close(held)
        self.descriptor = self.directory = None


# ---- the unit systemd runs -------------------------------------------------

def launcher_command():
    """The argv the unit's ExecStart runs: this package's CLI, not a shell line."""
    return [sys.executable, "-B", str(CLI), COMMAND]


def unit_text():
    """The generated router unit.

    There is no `[Install]` section, so systemd has nothing to enable and login
    never starts the router (SERVE-5), and no `Requires=` or `After=` on the
    manager's unit, so the manager stopping, crashing or restarting leaves the
    router running (START-3). `Restart=on-failure` restarts a router that exited
    unexpectedly, and that start runs this launcher and its lease check again.
    """
    return f"""[Unit]
Description=Matuto agentic serving router

[Service]
Type=simple
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=control-group
TimeoutStopSec=20
UnsetEnvironment=ROCM_PATH
ExecStart={shlex.join(launcher_command())}
Restart=on-failure
RestartSec=2

"""


def unit_path():
    return serving_state.unit_directory() / UNIT


def write_unit():
    """Write the router's own unit file and let systemd read it again."""
    path = unit_path()
    text = unit_text()
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        return path
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise LauncherRefused(f"the router unit could not be written at {path}: {error}") from None
    _systemctl("daemon-reload")
    return path


# ---- the card lease (RUN-4) -----------------------------------------

def card_holder(config_root=None, connection=None):
    """Who holds the card, from the runner's records first and the host second.

    A run recorded in the store holds the card under the card-admission rules, and the rows
    read here are the ones the runner's admission writes. A llama-server this account
    runs that is not the router unit's own process is a foreign holder. A store
    or a host that cannot be read is an unknown holder, which refuses.
    """
    if connection is not None:
        return _card_holder(connection)
    opened = store.read_store(config_root)
    if opened is None:
        return admission.Card(admission.CARD_UNKNOWN)
    try:
        return _card_holder(opened.connection())
    finally:
        opened.close()


def _card_holder(connection):
    """The holder, given the caller's own connection inside its own transaction."""
    work = runner._current_work(connection)
    if work is not None and work.kind in admission.RUN_KINDS \
            and work.kind not in admission.NO_CARD_RUNS:
        return admission.Card(admission.CARD_RUN, work.purpose)
    own = router_pid()
    voice_own = _unit_pid(serving_state.VOICE_ROUTER_UNIT)
    if own is None or voice_own is None:
        return admission.Card(admission.CARD_UNKNOWN)
    foreign = foreign_holders({own, voice_own})
    if foreign is None:
        return admission.Card(admission.CARD_UNKNOWN)
    return admission.Card(admission.CARD_FOREIGN, foreign[0]) if foreign \
        else admission.Card(admission.CARD_FREE)


def foreign_holders(own_pids):
    """The llama-server processes this account runs that are not the router unit's.

    None when the process list cannot be read at all, which is an unknown holder
    rather than a free card.
    """
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit() or int(entry) in own_pids:
            continue
        try:
            with open(f"/proc/{entry}/comm", encoding="utf-8") as handle:
                if handle.read().strip() != ROUTER_PROCESS:
                    continue
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                command = handle.read().split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            continue
        found.append(f"{ROUTER_PROCESS} process {entry} ({command})")
    return found


def router_pid():
    """The router unit's own main process, or None when systemd does not answer."""
    return _unit_pid(UNIT)


def _unit_pid(unit):
    answer = _systemctl("show", unit, "--property=MainPID")
    if not answer.answered or answer.returncode != 0:
        if unit == serving_state.VOICE_ROUTER_UNIT:
            state = _systemctl("is-active", unit)
            if state.answered and state.stdout.strip() in ("inactive", "failed"):
                return 0
        return None
    try:
        return int(answer.stdout.strip().partition("=")[2])
    except ValueError:
        return None


@contextlib.contextmanager
def lease(config_root=None, *, state=None):
    """The card-admission decision for this start, taken under the admission lock.

    The serving state is read before the write transaction opens, because a
    reader must not wait on this launcher. Inside the transaction the current
    work and the card's holder are read and the caller holds it until it has
    finished, so an admission racing this start is ordered after it and reads the
    unit this launcher left up.
    """
    if state is None:
        state = serving_state.read(config_root=config_root).state
    opened = store.open_store(config_root)
    try:
        connection = opened.connection()
        connection.execute(f"PRAGMA busy_timeout = {STORE_TIMEOUT_MS}")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield admission.decide(admission.START_AGENTIC, "to start agentic serving",
                                   runner._current_work(connection), state,
                                   card_holder(config_root, connection))
        finally:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
    finally:
        opened.close()


def check_lease(config_root=None, *, state=None):
    """Whether this start is admitted now, and why it is not when it is refused."""
    with lease(config_root, state=state) as decision:
        return decision


# ---- the launch itself -----------------------------------------------------

def prepare(config_root=None):
    """Everything the router needs before its first process, or why it may not start.

    The selection, the generated files and launch-time validation over every published
    profile's server rows are all resolved here, so a refusal names the operator's
    blocker before systemd is asked to start anything.
    """
    opened = store.read_store(config_root)
    if opened is None:
        raise LauncherRefused("The Matuto store cannot be read, so agentic serving"
                              " cannot start.")
    directory = descriptor = None
    try:
        selection = _selection(opened)
        files = store.router_files(opened)
        for reason in (files["key_reason"], files["model_directory_reason"]):
            if reason:
                raise LauncherRefused(f"{reason}, so agentic serving cannot start.")
        environment = matuto_runtime.execution_environment()
        directory, descriptor = matuto_runtime._tool_descriptor(
            selection["location"], ROUTER_PROCESS)
        environment = _card_environment(selection, environment)
        help_text = matuto_runtime.probe(descriptor, directory, "--help", environment)
        if not all(flag in help_text for flag in ("--device", "--list-devices")):
            raise LauncherRefused(
                f"The active build {selection['build_id']!r} advertises no device selection,"
                " so the router cannot be placed on the selected card.")
        selector = _selector(descriptor, directory, environment, selection)
        _launch_check(opened, selector, help_text)
        argv = ["--host", HOST, "--port", str(PORT),
                "--api-key-file", files["key_path"],
                "--models-dir", files["model_directory"],
                "--models-preset", files["presets_path"],
                "--models-max", MODELS_MAX,
                "--device", selector]
        try:
            matuto_runtime.validate_arguments(argv, help_text, selector, router=True)
        except InspectionError as error:
            raise LauncherRefused(f"The router's own arguments are refused: {error}") from None
        server = Path(selection["location"]) / installation.SERVER_PATHS.get(
            selection["recipe"], installation.SERVER_PATHS[recipes.LLAMA_HIP])
        plan = Plan(tuple(argv), dict(environment), selector, files["model_directory"],
                    files["presets_path"], files["key_path"], str(server), directory, descriptor)
        directory = descriptor = None
        return plan
    except InspectionError as error:
        raise LauncherRefused(str(error)) from None
    finally:
        for held in (descriptor, directory):
            if held is not None:
                with contextlib.suppress(OSError):
                    os.close(held)


def _selection(opened):
    """The saved runtime selection with its build and card, or why it cannot serve."""
    row = opened.connection().execute(
        "SELECT b.build_id AS build_id, b.location AS location, b.recipe AS recipe,"
        " b.backend AS backend, b.state AS state, c.gpu_identity AS gpu_identity"
        " FROM runtime_selection AS s JOIN builds AS b ON b.build_id = s.build_id"
        " JOIN cards AS c ON c.card_id = s.card_id WHERE s.selection_id = 1").fetchone()
    if row is None:
        raise LauncherRefused("No runtime build and card are selected; choose one before"
                              " starting agentic serving.")
    available, reason = registry.availability(opened.connection(), row["build_id"])
    if not available:
        raise LauncherRefused(f"The runtime build {row['build_id']!r} is unavailable: {reason};"
                              " inspect it again before starting agentic serving.")
    backend = BACKENDS.get(row["backend"])
    if backend is None:
        raise LauncherRefused(f"The selected build uses the unknown backend"
                              f" {row['backend']!r}, so nothing can be started on it.")
    return dict(row, runtime_backend=backend)


def _selector(descriptor, directory, environment, selection):
    """The one device selector the selected backend exposes, named by the build itself."""
    try:
        listed = [(selector, kind) for selector, kind in matuto_runtime._device_listing(
            matuto_runtime.probe(descriptor, directory, "--list-devices", environment))
            if kind == selection["runtime_backend"]]
    except InspectionError as error:
        raise LauncherRefused(f"The active build did not list its devices: {error}") from None
    if len(listed) != 1:
        raise LauncherRefused(
            f"The active build {selection['build_id']!r} does not expose exactly one"
            f" {selection['runtime_backend']} device, so the selected card cannot be named.")
    return listed[0][0]


def _card_environment(selection, environment):
    """The environment that pins the router to the selected card, or why it cannot."""
    try:
        host = next((item for item in matuto_runtime.pci_devices()["devices"]
                     if item["pciAddress"] == selection["gpu_identity"]), None)
        identities = [item for item in matuto_runtime.bounded_identity_probe(
            selection["runtime_backend"], environment)
            if item.get("pciAddress") == selection["gpu_identity"]
            and isinstance(item.get("uuid"), str)]
    except (InspectionError, OSError) as error:
        raise LauncherRefused(f"The selected card could not be read: {error}") from None
    if host is None or len(identities) != 1:
        raise LauncherRefused(
            "The active build cannot corroborate the selected card, so agentic serving is"
            f" not started over unknown work ({selection['gpu_identity']}).")
    chosen = {"pci": selection["gpu_identity"], "backend": selection["runtime_backend"],
              "corroborator": {"uuid": identities[0]["uuid"], "host": host}}
    try:
        resolved, _identity = matuto_runtime.resolve_device(chosen, environment)
    except InspectionError as error:
        raise LauncherRefused(f"The selected card cannot be resolved for serving: {error}") from None
    return resolved


def _launch_check(opened, selector, help_text):
    """Launch-time validation over every published profile's server rows (PROF-3).

    A profile carrying an option the active build does not advertise is refused by
    name before anything starts, so no consumer meets a router that would have
    dropped that profile.
    """
    connection = opened.connection()
    profiles = connection.execute(
        "SELECT profile_id, alias FROM model_profiles WHERE kind = 'language'"
        " AND published = 1 ORDER BY alias, profile_id").fetchall()
    for profile in profiles:
        rows = [{"tool": row["tool"], "option": row["flag"], "value": row["value"]}
                for row in connection.execute(
                     "SELECT tool, flag, value FROM profile_arguments"
                     " WHERE profile_id = ? AND tool = 'server' ORDER BY position",
                     (profile["profile_id"],))]
        try:
            arguments.launch_check(rows, selector=selector,
                                   help_text={"server": help_text})
        except arguments.Refusal as refusal:
            raise LauncherRefused(
                f"Published profile {profile['alias']!r} ({profile['profile_id']}) {refusal},"
                " so nothing is started.") from None


def launch(config_root=None):
    """What systemd's ExecStart runs: the lease, the checks, then the router itself.

    Every refusal returns without starting anything, which systemd records as a
    failed unit rather than a running router.
    """
    with lease(config_root) as decision:
        if decision.outcome != admission.ADMIT:
            raise LauncherRefused(decision.reason)
        plan = prepare(config_root)
    coordination = None
    try:
        coordination = matuto_runtime.runtime_lease()
        os.set_inheritable(plan.descriptor, True)
        os.set_inheritable(coordination, True)
        matuto_runtime._landlock_confine(plan.descriptor)
        os.chdir(plan.model_directory)
        os.execve(plan.descriptor, [ROUTER_PROCESS, *plan.argv], plan.environment)
    finally:
        if coordination is not None:
            with contextlib.suppress(OSError):
                os.close(coordination)


# ---- systemd ---------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    """What one host command said: its status, its output, and whether it said it."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    answered: bool = True


SILENT = Answer(1, "", "", answered=False)


def start_unit(config_root=None):
    """Ask systemd to start the router unit, and say why when it may not.

    The unit's ExecStart runs this launcher again, so the lease and the launch
    checks are the same ones a terminal start and a crash restart run; taking them
    here too means the operator reads the refusal instead of a failed unit.
    """
    with lease(config_root) as decision:
        if decision.outcome != admission.ADMIT:
            raise LauncherRefused(decision.reason)
        plan = prepare(config_root)
        plan.close()
        path = write_unit()
        answer = _systemctl("start", UNIT)
    if not answer.answered:
        raise LauncherRefused(f"systemd did not answer about {UNIT}, so nothing was started.")
    if answer.returncode != 0:
        raise LauncherRefused(f"{UNIT} did not start: {_detail(answer) or 'systemctl failed'}.")
    return path


def stop_unit():
    """Stop the router unit, answering whether it stopped.

    A stop that failed is answered as a failure and never as a return to not
    serving, which is what the state reader would then read.
    """
    answer = _systemctl("stop", UNIT)
    if not answer.answered:
        return False, f"systemd did not answer about {UNIT}, so its state is unknown."
    if answer.returncode != 0:
        return False, f"{UNIT} could not be stopped: {_detail(answer) or 'systemctl failed'}."
    return True, ""


def wait_ready(config_root=None, *, port=None, key_path=None, timeout=15.0,
               sleep=time.sleep, poll=0.2):
    """The serving state after a start, waiting out the bound for a healthy router.

    A router that never answers healthy ends the wait and is reported as the state
    the reader reads, which is never a started one; a unit that is not agentic
    serving cannot become so while the router is down, so the wait ends at once.
    """
    deadline = time.monotonic() + timeout
    while True:
        state = serving_state.read(config_root=config_root,
                                   router_port=port or serving_state.ROUTER_PORT,
                                   key_path=key_path)
        if state.state != serving_state.AGENTIC or state.healthy:
            return state
        if time.monotonic() >= deadline:
            return state
        sleep(poll)


def _systemctl(*arguments):
    """One user-manager call. A call that does not answer is answered=False."""
    try:
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        finished = subprocess.run(["systemctl", "--user", *arguments], capture_output=True,
                                  text=True, timeout=LIFECYCLE_TIMEOUT, check=False,
                                  env=environment)
    except (OSError, subprocess.SubprocessError):
        return SILENT
    return Answer(finished.returncode, finished.stdout, finished.stderr)


def _detail(answer, limit=200):
    """What systemd said, cleaned to one line and bounded like every log tail."""
    text = " ".join(part for part in (answer.stdout, answer.stderr) if part)
    return " ".join(text.split())[:limit]