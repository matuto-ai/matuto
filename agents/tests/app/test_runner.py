"""The one runner: the store it keeps, the units it starts and the ends it records.

Everything here runs the real runner through its Python API against a temporary
store, with a fake `systemd-run`, `systemctl` and `script` on PATH that record every
call and simulate unit states. The fakes are the host evidence the issue asks for:
a unit that ignores SIGTERM, one that is still stopping, one that never started, one
that vanished, a kill that does not work, and a systemd that will not answer at all.
"""

import datetime
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission  # noqa: E402  the package is not on the path yet
from matuto import progress as progress_module  # noqa: E402
from matuto import runner as runner_module  # noqa: E402
from matuto import storage  # noqa: E402
from matuto import store as store_module  # noqa: E402

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
"""A fake systemctl: per-unit state, a call log, and the failure modes under test.

State is one file per unit holding its ActiveState, beside optional files for its
SubState, its Result, its ExecMainStatus and a `.stubborn` marker. Markers in the
state directory choose a failure: `silent` never answers, `hang` never returns,
`name-only` names the unit instead of answering about it, `kill-fails` makes the
forced kill fail, `clean-exit` lets SIGTERM end the unit successfully and
`slow-kill` ends it and then takes its time returning.
"""
import datetime
import pathlib
import subprocess
import sys
import time

STATE = pathlib.Path(__import__("os").environ["RUNNER_SYSTEMD"])
UNITS = STATE / "units"
with (STATE / "calls.log").open("a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\\n")


def read(name, default=None):
    path = UNITS / name
    return path.read_text().strip() if path.exists() else default


def mark(name):
    (STATE / name).touch()


def moment():
    """The unit's monotonic microsecond transition time, as systemd exposes it."""
    return str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)


if (STATE / "silent").exists():
    # The user manager never answers: no properties, no state, no exit status.
    sys.exit(1)
if (STATE / "hang").exists():
    time.sleep(30)
    sys.exit(1)

arguments = sys.argv[1:]
if arguments[:1] == ["--user"]:
    arguments = arguments[1:]
options = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in arguments
          if a.startswith("--") and "=" in a}
names = [a for a in arguments if not a.startswith("-")]
if not names:
    sys.exit(2)

if names[0] == "list-units":
    print("local-llama-router.service loaded active running Local Router")
    sys.exit(0)

if names[0] == "show":
    unit = names[1]
    if (STATE / "name-only").exists():
        # systemd before 230 names the unit instead of answering about it.
        print(f"Failed to get properties: Unit {unit} could not be found.", file=sys.stderr)
        sys.exit(4)
    wanted = [a.split("=", 1)[1] for a in arguments if a.startswith("--property=")]
    if (UNITS / unit).exists():
        state = read(unit)
        properties = {"LoadState": "loaded", "ActiveState": state,
                      "SubState": read(unit + ".substate", "dead"),
                      "Result": read(unit + ".result", "success"),
                      "ExecMainStatus": read(unit + ".status", "0"),
                      "StateChangeTimestampMonotonic": read(unit + ".state-monotonic", "")}
    else:
        properties = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
                      "Result": "success", "ExecMainStatus": "0"}
    lines = [f"{key}={properties.get(key, '')}" for key in wanted]
    if (STATE / "slow-show").exists():
        # One observation taken before whatever is about to happen to the unit.
        (STATE / "slow-show").unlink()
        mark("showing")
        time.sleep(1)
    print("\\n".join(lines))
    sys.exit(0)

if names[0] == "kill":
    unit = names[-1]
    signal = options.get("--signal", "SIGTERM")
    if not (UNITS / unit).exists():
        print(f"Failed to stop {unit}: Unit {unit} could not be found.", file=sys.stderr)
        sys.exit(4)
    if read(unit) != "active" or read(unit + ".substate", "running") == "exited":
        if options.get("--job-mode") == "fail":
            if (STATE / "slow-no-process").exists():
                mark("refused-kill")
                time.sleep(1)
            print("Failed to kill unit: No matching processes to kill", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if signal == "SIGTERM" and (UNITS / (unit + ".stubborn")).exists():
        sys.exit(0)          # it ignores the graceful signal.
    if signal == "SIGKILL" and (STATE / "kill-fails").exists():
        print("Bus error", file=sys.stderr)
        sys.exit(1)
    # RemainAfterExit keeps a unit that exited cleanly loaded and active (exited).
    (UNITS / (unit + ".state-monotonic")).write_text(moment())
    if signal == "SIGTERM" and not (STATE / "clean-exit").exists():
        (UNITS / unit).write_text("failed")
        (UNITS / (unit + ".substate")).write_text("failed")
        (UNITS / (unit + ".result")).write_text("signal")
        (UNITS / (unit + ".status")).write_text("15")
    else:
        (UNITS / unit).write_text("failed" if signal == "SIGKILL" else "active")
        (UNITS / (unit + ".substate")).write_text("failed" if signal == "SIGKILL" else "exited")
        (UNITS / (unit + ".result")).write_text("signal" if signal == "SIGKILL" else "success")
        (UNITS / (unit + ".status")).write_text("9" if signal == "SIGKILL" else "0")
    if (STATE / "slow-kill").exists():
        mark("signalled")
        time.sleep(1)
    sys.exit(0)

sys.exit(2)
'''

FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
"""A fake systemd-run: it reads the store first, so the row is provably there.

A `run-command` marker makes the fake unit really run its command, with the
command's own output and exit status, which is how the pseudo-terminal and the
redirected log are exercised. A `slow-start` marker holds the unit back, which is
how the window between the recorded row and the unit that carries it is exercised.
"""
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

STATE = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
arguments = sys.argv[1:]
with (STATE / "calls.log").open("a") as log:
    log.write("systemd-run " + " ".join(arguments) + "\\n")
unit = next(a.split("=", 1)[1] for a in arguments if a.startswith("--unit="))

connection = sqlite3.connect(os.environ["RUNNER_STORE"])
row = connection.execute("SELECT run_id, started_at, outcome FROM runs WHERE unit_name = ?",
                         (unit,)).fetchone()
table = "runs"
if row is None:
    row = connection.execute("SELECT job_id, started_at, outcome FROM jobs WHERE unit_name = ?",
                             (unit,)).fetchone()
    table = "jobs"
with (STATE / "starts.log").open("a") as log:
    log.write("{} | {} | {} | {}\\n".format(unit, table, row[0] if row else "-",
                                            " | ".join(str(part) if part else "-" for part in row[1:])))

if (STATE / "refuse-start").exists():
    sys.exit(1)

def moment():
    """The unit's monotonic microsecond transition time, as systemd exposes it."""
    return str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)


UNITS = STATE / "units"
UNITS.mkdir(parents=True, exist_ok=True)
if (STATE / "slow-start").exists():
    (STATE / "starting").touch()
    time.sleep(0.5)
for argument in arguments:
    # Real systemd-run exports every --setenv name into the unit's environment, so
    # the fake does too: that is how a unit reaches the runner's progress interface.
    if argument.startswith("--setenv="):
        name, _, value = argument.split("=", 1)[1].partition("=")
        os.environ[name] = value
# Publish a complete state; an empty file is not a real systemd observation.
pending = UNITS / (unit + ".pending")
pending.write_text("active")
pending.replace(UNITS / unit)
if (STATE / "slow-ack").exists():
    # The unit is created and the user manager never answers the launch.
    (STATE / "acked").touch()
    time.sleep(30)
log = next((a.split(":", 1)[1] for a in arguments
            if a.startswith("--property=StandardOutput=append:")), "")
if not (STATE / "run-command").exists():
    sys.exit(0)

# The runner hands the unit its private evidence directory with `--setenv`, so the
# fake passes every such variable on exactly as systemd would.
environment = dict(os.environ)
for argument in arguments:
    if argument.startswith("--setenv="):
        name, _, value = argument[len("--setenv="):].partition("=")
        environment[name] = value

command = arguments[arguments.index("--") + 1:]
with open(log, "a") if log else open(os.devnull, "a") as output:
    finished = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, check=False,
                              env=environment)
status = finished.returncode
# RemainAfterExit: a clean exit stays loaded as active (exited); a failure fails.
(UNITS / unit).write_text("active" if status == 0 else "failed")
(UNITS / (unit + ".substate")).write_text("exited" if status == 0 else "failed")
(UNITS / (unit + ".result")).write_text("success" if status == 0 else "exit-code")
(UNITS / (unit + ".status")).write_text(str(status))
(UNITS / (unit + ".state-monotonic")).write_text(moment())
sys.exit(status)
'''

FAKE_PROCEDURE = '''#!/usr/bin/env python3
"""A fake qualification procedure: it writes its evidence where the runner says.

It writes a record, a checks list, a summary and an artifact into the private
`MATUTO_RUN_EVIDENCE_DIR` it was handed, and prints a long log with a credential in
it. Its first argument is the status it exits with, so one fixture proves both a
completed run and an errored one.
"""
import os
import sys
from pathlib import Path

evidence = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
(evidence / "record.json").write_text(
    '{"schema": "matuto-run-record/1", "performed": ["smoke"]}\\n')
(evidence / "checks.json").write_text(
    '[{"name": "exact reply", "status": "PASS", "detail": "the reply matched"}]\\n')
(evidence / "summary.txt").write_text("one passed smoke\\n")
(evidence / "artifact.txt").write_text("artifact body\\n")
print("load_tensors: layer 1 assigned")
print("x" * 70000)
print("api_key=supersecret")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''

FAKE_SCRIPT = '''#!/usr/bin/env python3
"""A fake script: it runs the command, keeps its output, and passes on its status.

`script` without `-e` exits 0 whatever the command did, which is why the runner
never leaves `-e` out.
"""
import pathlib
import subprocess
import sys

STATE = pathlib.Path(__import__("os").environ["RUNNER_SYSTEMD"])
arguments = sys.argv[1:]
with (STATE / "calls.log").open("a") as log:
    log.write("script " + " ".join(arguments) + "\\n")
with open(arguments[-1], "a") as output:
    finished = subprocess.run(["/bin/sh", "-c", arguments[arguments.index("-c") + 1]],
                              stdout=output, stderr=subprocess.STDOUT, check=False)
sys.exit(finished.returncode if "-e" in arguments else 0)
'''

TARGET = runner_module.Target("language", "p-1", "llama-3.1-8b", "sha256:abc")
STOPPED = lambda: admission.STOPPED
FREE = lambda: admission.Card(admission.CARD_FREE)
REPOSITORY = AGENTS.parent


def repository_files():
    """Every path under the checkout outside .git, so a write into it is visible."""
    return sorted(path.relative_to(REPOSITORY).as_posix() for path in REPOSITORY.rglob("*")
                  if ".git" not in path.parts)


def smoke_request(pty=False):
    return runner_module.Request.run("smoke", "smoke for llama-3.1-8b",
                                     ("/opt/matuto/bin/matuto-lab", "llama-3.1-8b", "smoke"),
                                     TARGET, "2026.10.9", pty=pty)


def bench_request():
    return runner_module.Request.run("bench", "bench for llama-3.1-8b",
                                     ("/opt/matuto/bin/matuto-lab", "bench"), TARGET, "2026.10.9")


class RunnerFixture(unittest.TestCase):
    """A temporary config root, a temporary store and fake systemd on PATH."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="runner-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = self.root / "config"
        self.systemd = self.root / "systemd"
        self.systemd.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("systemctl", FAKE_SYSTEMCTL), ("systemd-run", FAKE_SYSTEMD_RUN),
                           ("script", FAKE_SCRIPT), ("matuto-procedure", FAKE_PROCEDURE)):
            path = self.bin / name
            path.write_text(body)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.runners = []
        self.addCleanup(self.close_runners)
        environment = mock.patch.dict(os.environ, {
            "RUNNER_SYSTEMD": str(self.systemd),
            "RUNNER_STORE": str(store_module.store_path(self.config)),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        environment.start()
        self.addCleanup(environment.stop)

    def close_runners(self):
        for runner in self.runners:
            runner.close()

    def runner(self, **keywords):
        """A runner with the fakes' standing state: serving stopped, the card free."""
        runner = runner_module.Runner(self.config, **{"serving_state": STOPPED,
                                                       "card_holder": FREE, **keywords})
        self.runners.append(runner)
        return runner

    def in_a_thread(self, work):
        """Run `work` in its own thread with its own runner, as a second launcher is."""
        answers, failures = [], []

        def submit():
            runner = runner_module.Runner(self.config, serving_state=STOPPED, card_holder=FREE)
            try:
                answers.append(work(runner))
            except BaseException as failure:      # a thread must not fail silently
                failures.append(failure)
            finally:
                runner.close()

        thread = threading.Thread(target=submit)
        thread.start()
        return thread, answers, failures

    def wait_for(self, path, seconds=10):
        """Wait until the fake reports it reached a step, or fail the test."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"the fake never reached {path.name}")

    # ---- the fixture's own state -----------------------------------------

    def calls(self):
        return (self.systemd / "calls.log").read_text().splitlines() if (
            self.systemd / "calls.log").exists() else []

    def starts(self):
        return (self.systemd / "starts.log").read_text().splitlines() if (
            self.systemd / "starts.log").exists() else []

    def unit_of(self, work):
        return self.systemd / "units" / work.unit

    def unit_state(self, work, active, substate, result="success", status="0"):
        """The unit states systemd writes for this work."""
        self.unit_of(work).write_text(active)
        self.systemd.joinpath("units", f"{work.unit}.substate").write_text(substate)
        self.systemd.joinpath("units", f"{work.unit}.result").write_text(result)
        self.systemd.joinpath("units", f"{work.unit}.status").write_text(status)

    def end_the_unit(self, work, result="success", status="0", changed=None):
        """A unit that finished on its own, retained loaded by RemainAfterExit.

        `changed` is when the unit last changed state, which is what tells a whole
        unit that finished before a Stop's first signal from one that signal finished.
        """
        state, substate = ("active", "exited") if result == "success" else ("failed", "failed")
        self.unit_state(work, state, substate, result, status)
        stamp = changed or str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)
        self.systemd.joinpath("units", f"{work.unit}.state-monotonic").write_text(stamp)

    def rows(self, table="runs"):
        connection = store_module.open_store(self.config).connection()
        return connection.execute(f"SELECT * FROM {table}").fetchall()

    def units(self):
        return sorted(path.name for path in (self.systemd / "units").glob("*.service"))

    def pending_work(self):
        """The work an in-flight request has recorded, read the way a launcher reads it."""
        store = store_module.open_store(self.config)
        self.addCleanup(store.close)
        row = store.connection().execute(runner_module._RUN_SELECT).fetchone()
        return runner_module._work_from(row)

    KEY = "9f3c2b7a1d8e4f6a0b5c9d2e7f1a3b6c"

    def set_router_key(self, key=KEY):
        """The router key at the path CUT-4 keeps, so the runner can redact it."""
        path = self.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key)

    def reporting_command(self):
        """A unit that reports two phases through the real interface and writes log lines."""
        script = "\n".join([
            f"import sys; sys.path.insert(0, {str(AGENTS)!r})",
            "from matuto import progress",
            "progress.report('compile', 'step 1 of 3')",
            "print('load_tensors: layer 1 assigned')",
            "progress.report('bench', 'bench repetition 3 of 5')",
            f"print('Authorization: Bearer {self.KEY}')",
            f"print('PI_LOCAL_API_KEY={self.KEY}')",
            "print('token=fixture-token-value')",
            f"print('echoed value {self.KEY}')",
            "print('plain line kept')",
            "print('x' * 1000)",
            r"print('control\x00chars\x07here')",
        ])
        return (sys.executable, "-B", "-c", script)

    def start(self, runner=None, request=None, **keywords):
        runner = runner or self.runner(**keywords)
        return runner, runner.start(request or smoke_request())


class RunnerTest(RunnerFixture):
    # ---- the record ------------------------------------------------------

    def test_app_install_pending_refuses_new_runner_work(self):
        store_module.open_store(self.config).close()
        store_module.write_update_state(
            store_module.store_path(self.config), "2026.10.1", "complete",
            details={"app_install_pending": {"version": "2026.10.1", "list_number": 7}})
        before = self.starts()
        with self.assertRaisesRegex(store_module.StoreError, "Matuto update to 2026.10.1 has not recorded an outcome"):
            runner_module.Runner(self.config, serving_state=STOPPED, card_holder=FREE)
        self.assertEqual(self.starts(), before)

    def test_admitted_work_is_recorded_before_its_unit_starts(self):
        runner, started = self.start()
        self.assertEqual(started.decision, admission.ADMIT)
        work = started.work
        self.assertEqual(work.state, runner_module.RUNNING)
        self.assertEqual(work.outcome, "")
        # The fake systemd-run read the store before it created the unit, so its own
        # log is the ordering evidence: the row was there, with no end, at start time.
        self.assertEqual(self.starts(), [f"{work.unit} | runs | {work.work_id} | "
                                         f"{work.started_at} | -"])

    def test_the_record_names_kind_target_fingerprint_unit_and_start_time(self):
        _, started = self.start()
        work = started.work
        self.assertEqual(work.kind, "smoke")
        self.assertEqual(work.purpose, "smoke for llama-3.1-8b")
        self.assertEqual(work.record_kind, "language")
        self.assertEqual(work.record_id, "p-1")
        self.assertEqual(work.record_label, "llama-3.1-8b")
        self.assertEqual(work.fingerprint, "sha256:abc")
        self.assertEqual(work.procedure_revision, "2026.10.9")
        self.assertTrue(work.unit.startswith("matuto-smoke-"))
        self.assertTrue(work.unit.endswith(".service"))
        self.assertTrue(work.started_at)
        row = self.rows()[0]
        self.assertEqual((row["unit_name"], row["procedure"], row["started_at"]),
                         (work.unit, "smoke", work.started_at))

    def test_every_unit_is_the_user_managers_and_keeps_its_own_result(self):
        self.start()
        started_line = [line for line in self.calls() if line.startswith("systemd-run ")][0]
        self.assertTrue(started_line.startswith("systemd-run --user --unit=matuto-smoke-"))
        # The user manager's unit outlives the manager process and ends at logout;
        # the control group is what makes a stop reach every process of it, and
        # ExitType=cgroup is what keeps the unit running while any of them is left.
        self.assertIn("--property=KillMode=control-group", started_line)
        self.assertIn("--property=ExitType=cgroup", started_line)
        self.assertIn("--property=Type=exec", started_line)
        # A finished unit stays loaded, so a completed run is never read as an
        # interrupted one by a later reader, and a failed unit is never collected.
        self.assertIn("--property=RemainAfterExit=yes", started_line)
        self.assertIn("--property=CollectMode=inactive", started_line)
        self.assertTrue(all(line.startswith("systemctl --user ") for line in self.calls()
                            if line.startswith("systemctl")))

    def test_the_runner_is_the_only_way_the_package_starts_work(self):
        self.start()
        # The independent updater also uses a transient unit, but it does not start Matuto work.
        others = sorted(path.relative_to(AGENTS).as_posix()
                        for path in (AGENTS / "matuto").rglob("*.py")
                        if "systemd-run" in path.read_text()
                        and path.name not in ("runner.py", "updater.py", "removal.py", "maintenance.py"))
        self.assertEqual(others, [])

    def test_a_serving_start_is_refused_because_it_is_not_the_runners_work(self):
        answer = self.runner().start(runner_module.Request(
            admission.START_AGENTIC, "to start agentic serving", ("/opt/matuto/bin/router",)))
        self.assertEqual(answer.decision, admission.REFUSE)
        self.assertIn("not work this runner starts", answer.reason)
        self.assertEqual(self.rows(), [])

    def test_terminal_started_work_is_listed_and_stopped_like_any_other(self):
        runner, started = self.start()
        work = started.work
        # The browser reads the same list the terminal's request was recorded in.
        listed = [entry.work_id for entry in runner.works()]
        self.assertEqual(listed, [work.work_id])
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.state, runner_module.CANCELLED)
        self.assertEqual(stopped.work_id, work.work_id)

    def test_live_work_observation_does_not_settle_or_discard_ended_work(self):
        runner, started = self.start()
        work = started.work
        before = [(row["outcome"], row["ended_at"]) for row in self.rows()]
        self.unit_state(work, "inactive", "dead")

        self.assertEqual(runner.live_works(), [])
        self.assertEqual([(row["outcome"], row["ended_at"]) for row in self.rows()], before)
        self.assertTrue(self.unit_of(work).exists())
        self.assertTrue(runner._work_directory(work.work_id).exists())

    def test_a_run_records_the_active_build_at_admission(self):
        store = store_module.open_store(self.config)
        store.connection().executescript("""
            INSERT INTO cards VALUES ('card', 'GPU', 'gfx1201', 1, 1, 1, 1, 1, 1, '', '');
            INSERT INTO builds VALUES ('build-at-start', 'ROCm', 'llama-hip', '/build', 'hip',
                NULL, NULL, 'llama-hip', NULL, 'registered', '');
            INSERT INTO runtime_selection VALUES (1, 'build-at-start', 'card', 'hip', 'now');
        """)
        store.close()

        _runner, started = self.start()

        self.assertEqual(self.rows()[0]["build_id"], "build-at-start")
        self.assertEqual(started.work.outcome, "")

    def test_a_waiting_download_start_refuses_after_location_change_commits(self):
        old, new = self.root / "old-models", self.root / "new-models"
        old.mkdir(mode=0o700)
        new.mkdir(mode=0o700)
        opened = store_module.open_store(self.config)
        saved = storage.save_model_storage(opened.connection(), old)
        stale = runner_module.Request.job(
            admission.DOWNLOAD, "model weights", ("curl", "--output", str(old / "model.gguf")),
            pinned_request="model weights", destination=str(old))
        changing, ready, start_now, admitted, release = (threading.Event(), threading.Event(),
                                                           threading.Event(), threading.Event(),
                                                           threading.Event())
        answers, results = [], []
        original_availability = storage.availability

        def pause_change(path):
            if path == str(new) and not changing.is_set():
                changing.set()
                if not release.wait(3):
                    raise TimeoutError("location change was not released")
            return original_availability(path)

        def change():
            changing_store = store_module.open_store(self.config)
            try:
                storage.change_location(changing_store.connection(), saved["location_id"], new,
                                        serving_state=admission.STOPPED)
            finally:
                changing_store.close()

        def start():
            runner = runner_module.Runner(self.config)
            original_answers = runner._answers

            def observe_answers():
                result = original_answers()
                answers.append(result)
                admitted.set()
                return result

            runner._answers = observe_answers
            ready.set()
            if not start_now.wait(3):
                runner.close()
                return
            try:
                results.append(runner.start(stale))
            finally:
                runner.close()

        with mock.patch.object(storage, "availability", side_effect=pause_change):
            start_thread = threading.Thread(target=start)
            start_thread.start()
            self.assertTrue(ready.wait(2))
            change_thread = threading.Thread(target=change)
            change_thread.start()
            self.assertTrue(changing.wait(2))
            start_now.set()
            self.assertTrue(admitted.wait(2))
            time.sleep(0.05)
            self.assertEqual(results, [])
            release.set()
            change_thread.join(3)
            start_thread.join(3)
        final_location = storage.model_storage(opened.connection())
        opened.close()

        self.assertFalse(change_thread.is_alive())
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(answers, [True])
        self.assertEqual(results[0].decision, admission.REFUSE)
        self.assertIn("review the download again", results[0].reason)
        self.assertEqual(final_location, str(new))
        self.assertEqual(self.rows("jobs"), [])
        self.assertFalse(any(line.startswith("systemd-run ") for line in self.calls()))
        current = runner_module.Request.job(
            admission.DOWNLOAD, "model weights", ("curl", "--output", str(new / "model.gguf")),
            pinned_request="model weights", destination=str(new))
        runner, started = self.start(request=current)
        self.assertEqual(started.decision, admission.ADMIT)
        launch = next(line for line in self.calls() if line.startswith("systemd-run "))
        self.assertIn(str(new / "model.gguf"), launch)
        runner.stop(started.work.work_id)

    def test_a_build_job_records_its_pinned_request(self):
        runner, started = self.start(request=runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", ("/opt/matuto/bin/build-llama", "9f1c2d4"),
            pinned_request="llama.cpp@9f1c2d4 hip gfx1100"))
        self.assertEqual(started.work.kind, admission.BUILD)
        self.assertEqual(self.rows("jobs")[0]["pinned_request"], "llama.cpp@9f1c2d4 hip gfx1100")
        self.assertEqual(runner.works()[0].purpose, "llama.cpp@9f1c2d4 hip gfx1100")
        self.assertEqual(runner.stop(started.work.work_id).state, runner_module.CANCELLED)
        self.assertEqual(self.rows("jobs")[0]["outcome"], runner_module.CANCELLED)

    # ---- the pseudo-terminal ---------------------------------------------

    def test_a_pty_run_keeps_its_output_and_the_commands_own_exit_status(self):
        # The fake unit really runs its command, and the fake script really runs it
        # under a pseudo-terminal, so this is the whole path and not only its argv.
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.run(
            "smoke", "smoke for llama-3.1-8b",
            ("/bin/sh", "-c", "printf 'load_tensors: layer 1 assigned\\n'"), TARGET,
            "2026.10.9", pty=True))
        work = started.work
        script = [line for line in self.calls() if line.startswith("script ")]
        self.assertEqual(len(script), 1)
        self.assertIn(" -q ", script[0])
        self.assertIn(" -e ", script[0])
        self.assertTrue(script[0].endswith(str(runner.logs / work.work_id / runner_module.LOG_NAME)))
        finished = runner.works()[0]
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        self.assertEqual(finished.log, "load_tensors: layer 1 assigned\n")

    def test_a_failing_pty_run_never_reads_as_success(self):
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.run(
            "smoke", "smoke for llama-3.1-8b",
            ("/bin/sh", "-c", "printf 'no response\\n' >&2; exit 7"), TARGET, "2026.10.9",
            pty=True))
        finished = runner.works()[0]
        self.assertNotEqual(finished.outcome, runner_module.COMPLETED)
        self.assertEqual(finished.outcome, "errored")
        self.assertEqual(finished.state, "errored")
        self.assertEqual(finished.log, "no response\n")

    def test_a_run_without_a_terminal_its_output_is_redirected_to_its_log(self):
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.run(
            "bench", "bench for llama-3.1-8b",
            ("/bin/sh", "-c", "printf 'bench repetition 3 of 5\\n'"), TARGET, "2026.10.9"))
        started_line = [line for line in self.calls() if line.startswith("systemd-run ")][0]
        log = str(runner.logs / started.work.work_id / runner_module.LOG_NAME)
        self.assertIn(f"--property=StandardOutput=append:{log}", started_line)
        self.assertIn(f"--property=StandardError=append:{log}", started_line)
        finished = runner.works()[0]
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        self.assertEqual(finished.log, "bench repetition 3 of 5\n")

    def test_a_long_unit_output_is_bounded_and_marked_truncated(self):
        runner, started = self.start()
        # Whole lines beyond the cap, so the tail keeps its own lines and the mark
        # says the start of it was cut.
        (runner.logs / started.work.work_id / runner_module.LOG_NAME).write_text(
            "".join("x" * 1000 + "\n" for _ in range(100)))
        self.end_the_unit(started.work)
        finished = runner.works()[0]
        self.assertTrue(finished.log_truncated)
        self.assertLessEqual(len(finished.log), runner_module.LOG_LIMIT)
        self.assertTrue(finished.log.startswith("x"))
        self.assertEqual(self.rows()[0]["log_truncated"], 1)

    def test_following_a_long_output_reads_only_the_bounded_tail(self):
        runner, started = self.start()
        with (runner.logs / started.work.work_id / runner_module.LOG_NAME).open("wb") as body:
            body.truncate(32 * 1024 * 1024)      # a unit with no natural end
        tracemalloc.start()
        try:
            tail = runner.output(started.work)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLessEqual(len(tail), runner_module.LOG_LIMIT)
        self.assertLess(peak, 4 * 1024 * 1024)

    # ---- progress ---------------------------------------------------------

    def test_progress_reports_the_latest_phase_and_step_and_a_redacted_tail(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        finished = runner.works()[0]
        found = runner.progress(finished)
        # The last report the unit made is what a reader sees, not the first.
        self.assertEqual((found.phase, found.step), ("bench", "bench repetition 3 of 5"))
        self.assertEqual(found.state, runner_module.COMPLETED)
        self.assertFalse(found.unknown)
        # Its own log, cleaned and redacted: the key, a token assignment and a
        # bearer header are replaced whole, control characters removed and the
        # overlong line cut.
        self.assertNotIn(self.KEY, found.log)
        self.assertNotIn("fixture-token-value", found.log)
        self.assertIn("load_tensors: layer 1 assigned", found.log)
        self.assertIn("plain line kept", found.log)
        self.assertIn("control chars here", found.log)
        self.assertEqual(found.log.count(progress_module.REDACTED), 4)
        self.assertTrue(all(len(line) <= progress_module.TAIL_LINE
                            for line in found.log.splitlines()))
        # Elapsed and total duration come from the times the runner recorded.
        self.assertGreater(found.duration, 0.0)
        self.assertEqual(found.elapsed, found.duration)

    def test_progress_can_prefix_a_nested_procedure_step(self):
        path = self.root / "nested-progress"
        with mock.patch.dict(os.environ, {
                progress_module.FILE_VARIABLE: str(path),
                "MATUTO_PROGRESS_PREFIX": "rung 192K"}):
            progress_module.report("worker qualification", "WQ-06, 7 of 14")
        self.assertEqual(progress_module.latest(path),
                         ("worker qualification", "rung 192K, WQ-06, 7 of 14"))

    def test_progress_is_the_same_for_a_browser_or_terminal_start_and_after_a_restart(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        browser = self.runner()
        started = browser.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        browser.works()
        first = browser.progress(browser.work(started.work.work_id))
        # A second launcher is the terminal's or the restarted manager's: it finds
        # the same report and tail from what the job itself recorded.
        restarted = self.runner()
        again = restarted.progress(restarted.work(started.work.work_id))
        self.assertEqual((first.state, first.phase, first.step, first.log),
                         (again.state, again.phase, again.step, again.log))
        self.assertEqual(again.state, runner_module.COMPLETED)
        self.assertNotIn(self.KEY, again.log)

    def test_progress_shows_only_the_bounded_tail_of_the_units_own_log(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4",
            (sys.executable, "-B", "-c",
             "print('\\n'.join('log-line-%03d' % n for n in range(200)))")))
        runner.works()
        found = runner.progress(runner.work(started.work.work_id))
        lines = found.log.splitlines()
        self.assertEqual(len(lines), progress_module.TAIL_LINES)
        self.assertNotIn("log-line-000", found.log)
        self.assertEqual(lines[-1], "log-line-199")

    def test_latest_reads_only_the_bounded_tail_of_a_large_report(self):
        path = self.root / "progress-report"
        with path.open("wb") as body:
            for number in range(200_000):         # a report that grew without bound
                body.write(f"phase\tstep {number}\n".encode())
            body.write(b"bench\tbench repetition 3 of 5\n")
        tracemalloc.start()
        try:
            found = progress_module.latest(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(found, ("bench", "bench repetition 3 of 5"))
        self.assertLess(peak, 1024 * 1024)

    def test_progress_reads_unknown_when_systemd_does_not_answer(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        (self.systemd / "silent").write_text("")
        found = runner.progress(started.work)
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertIn("systemd did not answer", found.unknown)
        self.assertEqual((found.phase, found.step, found.log), ("", "", ""))
        # Reading changes nothing: no end was settled and no row was written.
        self.assertIsNone(self.rows("jobs")[0]["outcome"])

    def test_progress_reads_unknown_when_the_jobs_own_log_cannot_be_read(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        self.unit_state(started.work, "active", "running")
        log = runner._log_path(started.work.work_id)
        log.unlink()
        log.mkdir()                                      # a log that cannot be read
        found = runner.progress(runner.work(started.work.work_id))
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertIn("log could not be read", found.unknown)
        self.assertEqual(found.log, "")

    def test_progress_redacts_the_key_value_and_not_only_a_named_credential(self):
        # The key carries no secret word of its own, so only reading it can redact it.
        line = f"echoed value {self.KEY}"
        self.assertNotIn(self.KEY, progress_module.tail(line, self.KEY))
        self.assertIn(self.KEY, progress_module.tail(line, ""))

    def test_progress_says_unknown_rather_than_show_a_tail_without_the_key(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        runner.works()
        (self.config / "ai-agents-sandbox" / "pi-local" / "api-key").unlink()
        found = runner.progress(runner.work(started.work.work_id))
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertIn("router API key", found.unknown)
        self.assertEqual(found.log, "")

    def test_progress_hides_a_stale_phase_when_the_unit_is_gone_with_no_recorded_end(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        # No end is settled, and the unit has vanished: the last report is stale.
        self.unit_of(started.work).unlink()
        found = runner.progress(started.work)
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertEqual((found.phase, found.step, found.log), ("", "", ""))
        self.assertIn("no recorded end", found.unknown)

    def test_progress_never_reads_another_units_log_through_a_link(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        self.unit_state(started.work, "active", "running")
        other = self.systemd / "other-unit.log"
        other.write_text("BBBB-only-in-b\n")
        log = runner._log_path(started.work.work_id)
        log.unlink()
        log.symlink_to(other)
        found = runner.progress(runner.work(started.work.work_id))
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertIn("log could not be read", found.unknown)
        self.assertNotIn("BBBB-only-in-b", found.log)

    def test_progress_says_unknown_when_the_router_key_is_not_a_private_file(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        runner.works()
        key = self.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        other = self.systemd / "other-key"
        other.write_text("a-different-key-that-would-miss-the-real-one")
        key.unlink()
        key.symlink_to(other)
        found = runner.progress(runner.work(started.work.work_id))
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertIn("router API key", found.unknown)
        self.assertEqual(found.log, "")

    def test_progress_answers_unknown_for_a_non_regular_report_log_or_key(self):
        self.set_router_key()
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(runner_module.Request.job(
            admission.BUILD, "llama.cpp HIP 9f1c2d4", self.reporting_command()))
        self.unit_state(started.work, "active", "running")
        work_id = started.work.work_id
        work = runner.work(work_id)        # fetched before the readers run off-thread
        cases = {
            "report": runner._progress_path(work_id),
            "log": runner._log_path(work_id),
            "key": self.config / "ai-agents-sandbox" / "pi-local" / "api-key",
        }
        for name, path in cases.items():
            with self.subTest(name=name):
                saved = path.read_bytes()
                path.unlink()
                os.mkfifo(path)             # a reader that blocked would never answer
                try:
                    answers, failures = [], []

                    def read_progress():
                        try:
                            answers.append(runner.progress(work))
                        except BaseException as failure:      # a thread must not fail silently
                            failures.append(failure)

                    thread = threading.Thread(target=read_progress, daemon=True)
                    thread.start()
                    thread.join(2)
                    self.assertFalse(thread.is_alive(), f"progress blocked on a FIFO {name}")
                    self.assertEqual(failures, [])
                    self.assertEqual(answers[0].state, runner_module.UNKNOWN)
                    self.assertEqual((answers[0].phase, answers[0].step, answers[0].log),
                                     ("", "", ""))
                finally:
                    path.unlink()
                    path.write_bytes(saved)

    # ---- admission --------------------------------------------------------

    def test_a_second_request_is_refused_naming_the_current_work_and_records_nothing(self):
        runner, started = self.start()
        before = len(self.rows())
        answer = runner.start(bench_request())
        self.assertEqual(answer.decision, admission.REFUSE)
        self.assertIn("smoke", answer.reason)
        self.assertIn("llama-3.1-8b", answer.reason)
        self.assertIn("nothing is queued", answer.reason)
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(answer.work, None)

    def test_two_concurrent_starts_admit_exactly_one(self):
        answers, failures = [], []
        threads = [self.in_a_thread(lambda runner: runner.start(request))
                   for request in (smoke_request(), bench_request())]
        for thread, _answers, thread_failures in threads:
            thread.join()
            answers += _answers
            failures += thread_failures
        self.assertEqual(failures, [])
        admitted = [answer for answer in answers if answer.decision == admission.ADMIT]
        refused = [answer for answer in answers if answer.decision == admission.REFUSE]
        self.assertEqual((len(admitted), len(refused)), (1, 1))
        self.assertEqual(len(self.rows()), 1)

    def test_a_status_read_during_the_launch_window_neither_ends_nor_admits_again(self):
        (self.systemd / "slow-start").write_text("")
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.start(smoke_request()))
        self.wait_for(self.systemd / "starting")        # the row is there, the unit is not
        other = self.runner()
        during = other.works()
        self.assertEqual([entry.outcome for entry in during], [""])
        self.assertEqual(other.start(smoke_request()).decision, admission.REFUSE)
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(answers[0].decision, admission.ADMIT)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.units()), 1)

    def test_a_live_process_with_a_reused_number_does_not_hold_an_old_launch(self):
        runner, started = self.start(graceful=0)
        # A mark from a previous session: expired, and its number now belongs to a
        # process that did not and could not have launched this work.
        self.unit_of(started.work).unlink()
        marker = runner.logs / f"{started.work.work_id}.launching"
        marker.write_text(f"{runner_module._stamp()} {os.getpid()} 0 {runner_module._boot_id()}")
        os.utime(marker, (time.time() - runner_module.LAUNCH_PENDING_SECONDS - 1,) * 2)
        self.assertEqual(runner.works()[0].outcome, "errored")
        self.assertEqual(runner.start(bench_request()).decision, admission.ADMIT)

    def test_a_launch_from_another_boot_does_not_hold_the_slot(self):
        runner, started = self.start(graceful=0)
        self.unit_of(started.work).unlink()
        marker = runner.logs / f"{started.work.work_id}.launching"
        marker.write_text(f"{runner_module._stamp()} {os.getpid()} "
                          f"{runner_module._process_identity(os.getpid())} prior-boot")
        os.utime(marker, (time.time() - runner_module.LAUNCH_PENDING_SECONDS - 1,) * 2)
        self.assertEqual(runner.works()[0].outcome, "errored")
        self.assertEqual(runner.start(bench_request()).decision, admission.ADMIT)

    def test_stopping_a_unit_whose_child_is_left_is_a_cancellation(self):
        runner, started = self.start(graceful=0)
        work = started.work
        # The main process finished a while ago and left a child holding the card, so
        # the unit is still running and its own exit says nothing about this Stop.
        old = str(time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 1000 - 600_000_000)
        self.unit_state(work, "active", "running", "success", "0")
        self.systemd.joinpath("units", f"{work.unit}.state-monotonic").write_text(old)
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.outcome, runner_module.CANCELLED)

    def test_a_launch_whose_manager_died_records_errored_and_frees_the_slot(self):
        runner, started = self.start(graceful=0)
        # The recorded row is there, its unit never appeared, and the launch is older
        # than the runner waits for one and its launcher is gone: nothing ever ran, so
        # nothing may read as run.
        self.unit_of(started.work).unlink()
        marker = runner.logs / f"{started.work.work_id}.launching"
        marker.write_text(f"{runner_module._stamp()} 999999  {runner_module._boot_id()}")
        os.utime(marker, (time.time() - runner_module.LAUNCH_PENDING_SECONDS - 1,) * 2)
        found = runner.works()[0]
        self.assertEqual(found.outcome, "errored")
        self.assertEqual(runner.start(bench_request()).decision, admission.ADMIT)

    def test_a_stop_during_the_launch_window_ends_the_unit_that_appears(self):
        (self.systemd / "slow-start").write_text("")
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.start(smoke_request()))
        self.wait_for(self.systemd / "starting")        # the row is there, the unit is not
        stopper = self.runner()
        during = stopper.stop(self.pending_work().work_id)
        # Nothing is recorded for a unit that has not appeared, so the slot stays held
        # and the request to stop is kept for the launcher that is still in flight.
        self.assertEqual(during.outcome, "")
        self.assertEqual(during.state, runner_module.LAUNCHING)
        self.assertEqual(stopper.start(smoke_request()).decision, admission.REFUSE)
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.rows()[0]["outcome"], runner_module.CANCELLED)
        self.assertEqual(len(self.units()), 1)
        self.assertNotEqual(self.unit_of(during).read_text(), "active")

    def test_a_launch_the_user_manager_never_answered_keeps_the_slot(self):
        (self.systemd / "slow-ack").write_text("")
        runner = self.runner()
        with mock.patch.object(runner_module, "CALL_TIMEOUT", 0.3):
            started = runner.start(smoke_request())
        self.assertEqual(started.decision, admission.ADMIT)
        # The unit was created and the answer was lost, so it is running work that
        # still holds the slot, not a launch that never happened.
        self.assertEqual(started.work.outcome, "")
        self.assertEqual(started.work.state, runner_module.RUNNING)
        self.assertEqual(runner.start(bench_request()).decision, admission.REFUSE)

    def test_an_expired_launch_whose_launcher_is_alive_keeps_the_slot(self):
        runner, started = self.start(graceful=0)
        # The row is there, its unit never appeared, and the launcher is suspended
        # rather than dead: its slot is not reclaimed while it can still start it.
        self.unit_of(started.work).unlink()
        marker = runner.logs / f"{started.work.work_id}.launching"
        marker.write_text(f"{runner_module._stamp()} {os.getpid()} "
                          f"{runner_module._process_identity(os.getpid())} {runner_module._boot_id()}")
        os.utime(marker, (time.time() - runner_module.LAUNCH_PENDING_SECONDS - 1,) * 2)
        self.assertEqual(runner.works()[0].outcome, "")
        self.assertEqual(runner.start(bench_request()).decision, admission.REFUSE)

    def test_a_pending_stop_is_honoured_when_the_launch_is_never_answered(self):
        (self.systemd / "slow-start").write_text("")
        (self.systemd / "slow-ack").write_text("")
        with mock.patch.object(runner_module, "CALL_TIMEOUT", 2.0):
            thread, answers, failures = self.in_a_thread(
                lambda runner: runner.start(smoke_request()))
            self.wait_for(self.systemd / "starting")
            stopper = self.runner()
            pending = stopper.stop(self.pending_work().work_id)
            self.assertEqual(pending.state, runner_module.LAUNCHING)
            thread.join(30)
        self.assertEqual(failures, [])
        # The unit appeared after the Stop and the acknowledgement was lost, so the
        # stop that was left behind is carried out rather than lost with it.
        self.assertIn("--signal=SIGTERM", " ".join(self.calls()))
        self.assertEqual(self.rows()[0]["outcome"], runner_module.CANCELLED)
        self.assertEqual(answers[0].work.outcome, runner_module.CANCELLED)

    def test_a_same_microsecond_completion_before_signal_delivery_is_kept(self):
        (self.systemd / "slow-show").write_text("")
        runner, started = self.start(graceful=0)
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.stop(started.work.work_id))
        self.wait_for(self.systemd / "showing")
        # The live-state read is already in flight. Complete the unit at exactly the
        # monotonic instant the Stop records, so timestamp ordering alone cannot tell
        # who won; systemd's no-process signal refusal must preserve this completion.
        with mock.patch.object(runner_module.time, "clock_gettime_ns", return_value=123456000):
            self.end_the_unit(started.work, changed="123456")
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(answers[0].outcome, runner_module.COMPLETED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.COMPLETED)
        self.assertIn("--job-mode=fail", " ".join(self.calls()))

    def test_a_status_read_does_not_overwrite_a_completion_while_stop_is_resolving_signal_refusal(self):
        (self.systemd / "slow-show").write_text("")
        (self.systemd / "slow-no-process").write_text("")
        runner, started = self.start(graceful=0)
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.stop(started.work.work_id))
        self.wait_for(self.systemd / "showing")
        self.end_the_unit(started.work)
        self.wait_for(self.systemd / "refused-kill")
        during = self.runner().works()[0]
        self.assertEqual(during.outcome, "")
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(answers[0].outcome, runner_module.COMPLETED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.COMPLETED)

    def test_a_completion_that_finished_before_the_first_signal_is_kept(self):
        (self.systemd / "slow-show").write_text("")
        (self.systemd / "kill-fails").write_text("")
        runner, started = self.start(graceful=0)
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.stop(started.work.work_id))
        self.wait_for(self.systemd / "showing")        # this Stop's look is in flight
        self.end_the_unit(started.work)                # the run finishes on its own
        thread.join()
        self.assertEqual(failures, [])
        # A request to stop is not proof that the stop ended it: this unit's exit is
        # recorded before the signal, so the completion is what stands.
        self.assertEqual(answers[0].outcome, runner_module.COMPLETED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.COMPLETED)

    def test_a_card_run_while_serving_is_up_is_only_offered_stop_serving_and_run(self):
        runner = self.runner(serving_state=lambda: admission.AGENTIC)
        answer = runner.start(smoke_request())
        self.assertEqual(answer.decision, admission.STOP_SERVING_AND_RUN)
        self.assertEqual(self.rows(), [])

    def test_an_unknown_serving_state_or_card_holder_refuses(self):
        for keywords in ({"serving_state": lambda: "started"},
                         {"card_holder": lambda: admission.Card(admission.CARD_UNKNOWN)}):
            with self.subTest(state=keywords):
                runner = self.runner(**keywords)
                answer = runner.start(smoke_request())
                self.assertEqual(answer.decision, admission.REFUSE)
                self.assertEqual(self.rows(), [])

    def test_a_verify_run_needs_no_card_and_no_serving_state(self):
        runner = self.runner(serving_state=lambda: admission.SERVING_UNKNOWN,
                             card_holder=lambda: admission.Card(admission.CARD_UNKNOWN))
        answer = runner.start(runner_module.Request.run(
            "verify", "verify for llama-3.1-8b", ("/opt/matuto/bin/matuto-lab", "verify"),
            TARGET, "2026.10.9"))
        self.assertEqual(answer.decision, admission.ADMIT)

    def test_work_that_ended_on_its_own_frees_the_slot_for_the_next_request(self):
        runner, started = self.start()
        self.end_the_unit(started.work)
        self.assertEqual(runner.start(bench_request()).decision, admission.ADMIT)

    # ---- stopping ---------------------------------------------------------

    def test_stop_ends_the_whole_unit_gracefully_then_forcibly_after_a_bounded_wait(self):
        runner, started = self.start(graceful=0.5)
        work = started.work
        self.systemd.joinpath("units", f"{work.unit}.stubborn").write_text("")
        waits = []
        runner.sleep = waits.append
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.state, runner_module.CANCELLED)
        kills = [line for line in self.calls() if " kill " in line]
        self.assertEqual(len(kills), 2)
        self.assertIn("--kill-who=all", kills[0])
        self.assertIn("--job-mode=fail", kills[0])
        self.assertIn("--signal=SIGTERM", kills[0])
        self.assertIn("--kill-who=all", kills[1])
        self.assertIn("--job-mode=fail", kills[1])
        self.assertIn("--signal=SIGKILL", kills[1])
        # The force came after the wait, and the wait stayed bounded.
        self.assertGreater(len(waits), 0)
        self.assertLessEqual(len(waits), 5)

    def test_a_forced_kill_that_does_not_stop_the_unit_records_nothing(self):
        (self.systemd / "kill-fails").write_text("")
        runner, started = self.start(graceful=0)
        self.systemd.joinpath("units", f"{started.work.unit}.stubborn").write_text("")
        stopped = runner.stop(started.work.work_id)
        # The unit is still there, so it keeps the slot and no end is invented.
        self.assertEqual(stopped.outcome, "")
        self.assertEqual(stopped.state, runner_module.RUNNING)
        self.assertEqual(runner.start(bench_request()).decision, admission.REFUSE)

    def test_a_unit_still_stopping_keeps_the_slot_and_records_nothing(self):
        runner, started = self.start()
        self.unit_state(started.work, "deactivating", "stop", "success", "0")
        found = runner.works()[0]
        self.assertEqual(found.outcome, "")
        self.assertEqual(found.state, runner_module.RUNNING)
        self.assertEqual(runner.start(bench_request()).decision, admission.REFUSE)

    def test_a_run_that_exits_zero_only_because_of_stop_is_cancelled(self):
        (self.systemd / "clean-exit").write_text("")
        runner, started = self.start(graceful=0)
        stopped = runner.stop(started.work.work_id)
        self.assertEqual(stopped.outcome, runner_module.CANCELLED)

    def test_a_timed_out_kill_acknowledgement_does_not_turn_its_exit_into_completion(self):
        (self.systemd / "clean-exit").write_text("")
        runner, started = self.start(graceful=0)
        run = subprocess.run

        def lose_kill_acknowledgement(argv, **kwargs):
            result = run(argv, **kwargs)
            if argv[:3] == ["systemctl", "--user", "kill"]:
                self.assertEqual(result.returncode, 0)
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return result

        def suspended_clock(clock_id):
            return (1_000_000 if clock_id == time.CLOCK_MONOTONIC else 10**18)
        with mock.patch.object(runner_module.subprocess, "run", side_effect=lose_kill_acknowledgement), \
                mock.patch.object(runner_module.time, "clock_gettime_ns", side_effect=suspended_clock):
            stopped = runner.stop(started.work.work_id)
        self.assertEqual(stopped.outcome, runner_module.CANCELLED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.CANCELLED)

    def test_a_status_read_during_a_stop_does_not_record_the_signals_exit(self):
        (self.systemd / "slow-kill").write_text("")
        _, started = self.start()
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.stop(started.work.work_id))
        self.wait_for(self.systemd / "signalled")      # the unit ended; the Stop has not
        other = self.runner()
        during = other.works()[0]
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(during.outcome, "")
        self.assertEqual(answers[0].outcome, runner_module.CANCELLED)

    def test_a_second_stop_does_not_record_the_first_stops_exit_as_errored(self):
        (self.systemd / "slow-kill").write_text("")
        _, started = self.start()
        thread, answers, failures = self.in_a_thread(
            lambda runner: runner.stop(started.work.work_id))
        self.wait_for(self.systemd / "signalled")      # the unit ended; the Stop has not
        second = self.runner().stop(started.work.work_id)
        thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(second.outcome, runner_module.CANCELLED)
        self.assertEqual(answers[0].outcome, runner_module.CANCELLED)
        terms = [line for line in self.calls() if "--signal=SIGTERM" in line]
        self.assertEqual(len(terms), 1)

    def test_a_status_read_in_flight_cannot_erase_a_stops_own_reason(self):
        (self.systemd / "slow-show").write_text("")
        _, started = self.start()
        thread, answers, failures = self.in_a_thread(lambda runner: runner.works())
        self.wait_for(self.systemd / "showing")        # this reader's look is in flight
        stopped = self.runner().stop(started.work.work_id)
        thread.join()
        self.assertEqual(failures, [])
        # The in-flight reader either sees nothing to settle or reaches the same
        # conclusion the Stop did; it never turns this Stop into an error.
        self.assertIn(answers[0][0].outcome, ("", runner_module.CANCELLED))
        self.assertNotEqual(answers[0][0].outcome, "errored")
        self.assertEqual(stopped.outcome, runner_module.CANCELLED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.CANCELLED)

    def test_a_stop_racing_a_completion_keeps_the_completion_and_reports_it(self):
        runner, started = self.start()
        work = started.work
        self.end_the_unit(work)
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.outcome, runner_module.COMPLETED)
        self.assertEqual(stopped.state, runner_module.COMPLETED)
        self.assertEqual(self.rows()[0]["outcome"], runner_module.COMPLETED)

    def test_a_unit_that_already_failed_keeps_its_own_outcome_through_a_stop(self):
        runner, started = self.start()
        self.end_the_unit(started.work, result="exit-code", status="1")
        stopped = runner.stop(started.work.work_id)
        self.assertEqual(stopped.outcome, "errored")

    def test_completed_rows_survive_a_stop(self):
        runner, started = self.start()
        work = started.work
        connection = store_module.open_store(self.config).connection()
        connection.execute("INSERT INTO checks (run_id, sequence, name, status) VALUES (?, 0,"
                           " 'smoke', 'PASS')", (work.work_id,))
        self.end_the_unit(work)
        self.assertEqual(runner.works()[0].outcome, runner_module.COMPLETED)
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.outcome, runner_module.COMPLETED)
        self.assertEqual(tuple(connection.execute("SELECT name, status FROM checks WHERE run_id = ?",
                                                  (work.work_id,)).fetchone()), ("smoke", "PASS"))
        self.assertNotIn("kill", " ".join(self.calls()))

    # ---- reconnect and interruption ---------------------------------------

    def test_work_that_ends_while_the_manager_is_down_is_recorded_afterwards(self):
        runner, started = self.start()
        work = started.work
        self.end_the_unit(work)          # the unit finished while nothing was looking
        self.close_runners()             # the manager process is gone
        runner = self.runner()           # a restart finds the work in the store
        found = runner.works()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].outcome, runner_module.COMPLETED)
        self.assertTrue(found[0].ended_at)

    def test_a_running_unit_is_found_from_the_store_after_a_restart(self):
        runner, started = self.start()
        self.systemd.joinpath("units", f"{started.work.unit}.stubborn").write_text("")
        (runner.logs / started.work.work_id / runner_module.LOG_NAME).write_text("bench repetition 3 of 5\n")
        self.close_runners()
        runner = self.runner()           # a restart finds the work in the store
        found = runner.works()
        self.assertEqual(found[0].state, runner_module.RUNNING)
        # Its output is still reachable, because it is the unit's own log file.
        self.assertEqual(runner.output(found[0]), "bench repetition 3 of 5\n")

    def test_a_unit_gone_with_no_recorded_end_reads_interrupted_never_completed(self):
        runner, started = self.start()
        self.unit_of(started.work).unlink()      # logout or reboot ended the unit
        found = runner.works()[0]
        self.assertEqual(found.outcome, runner_module.INTERRUPTED)
        self.assertNotEqual(found.state, runner_module.COMPLETED)

    def test_a_systemd_that_names_the_unit_instead_of_answering_reads_it_as_gone(self):
        runner, started = self.start()
        self.end_the_unit(started.work)
        (self.systemd / "name-only").write_text("")
        found = runner.works()[0]
        self.assertEqual(found.outcome, runner_module.INTERRUPTED)
        self.assertNotEqual(found.state, runner_module.COMPLETED)

    def test_a_unit_that_appears_after_its_request_was_settled_is_stopped(self):
        runner = self.runner()
        launch = runner._start_unit

        def launch_then_settle(request, work_id, unit):
            answer = launch(request, work_id, unit)
            runner._settle_end(work_id, "errored")     # settled after the unit appeared
            return answer

        runner.launch = launch_then_settle
        started = runner.start(smoke_request())
        self.assertEqual(started.work.outcome, "errored")
        # The unit that appeared is not left running behind a request that is over.
        self.assertIn("--signal=SIGTERM", " ".join(self.calls()))
        self.assertNotEqual(self.unit_of(started.work).read_text(), "active")

    def test_a_unit_that_never_started_is_recorded_errored_and_frees_the_slot(self):
        (self.systemd / "refuse-start").write_text("")
        runner = self.runner()
        started = runner.start(smoke_request())
        self.assertEqual(started.decision, admission.ADMIT)
        self.assertEqual(started.work.outcome, "errored")
        self.assertEqual(started.work.state, "errored")
        self.assertTrue(started.work.ended_at)
        (self.systemd / "refuse-start").unlink()
        self.assertEqual(runner.start(bench_request()).decision, admission.ADMIT)

    # ---- the unknown state ------------------------------------------------

    def test_a_silent_systemd_admits_nothing(self):
        (self.systemd / "silent").write_text("")
        answer = self.runner().start(smoke_request())
        self.assertEqual(answer.decision, admission.REFUSE)
        self.assertIn("systemd did not answer", answer.reason)
        self.assertEqual(self.rows(), [])

    def test_a_silent_systemd_reports_the_work_as_unknown_and_ends_nothing(self):
        runner, started = self.start()
        (self.systemd / "silent").write_text("")
        found = runner.works()[0]
        self.assertEqual(found.state, runner_module.UNKNOWN)
        self.assertEqual(found.outcome, "")
        stopped = runner.stop(started.work.work_id)
        self.assertEqual(stopped.state, runner_module.UNKNOWN)
        self.assertEqual(stopped.outcome, "")
        self.assertEqual(self.rows()[0]["outcome"], None)

    def test_a_systemd_that_never_returns_is_refused_and_never_guesses(self):
        runner, started = self.start()
        (self.systemd / "hang").write_text("")
        with mock.patch.object(runner_module, "CALL_TIMEOUT", 0.05):
            self.assertEqual(runner.start(bench_request()).decision, admission.REFUSE)
            stopped = runner.stop(started.work.work_id)
            found = runner.works()[0]
        self.assertEqual((stopped.outcome, stopped.state), ("", runner_module.UNKNOWN))
        self.assertEqual((found.outcome, found.state), ("", runner_module.UNKNOWN))
        self.assertEqual(self.rows()[0]["outcome"], None)

    def test_a_systemd_that_is_not_there_at_all_admits_nothing(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            answer = self.runner().start(smoke_request())
        self.assertEqual(answer.decision, admission.REFUSE)
        self.assertIn("systemd did not answer", answer.reason)
        self.assertEqual(self.rows(), [])

    def test_the_runner_never_enables_lingering(self):
        runner, started = self.start()
        runner.stop(started.work.work_id)
        runner.works()
        self.assertNotIn("linger", " ".join(self.calls()))


class RunEvidenceTest(RunnerFixture):
    """A run's evidence: its private directory, the move into the store, its end.

    The fake procedure runs for real where the exit status and the log are the point,
    so the `MATUTO_RUN_EVIDENCE_DIR` hand-off is exercised end to end. The other cases
    write the same evidence files directly, which is how every end a run can have —
    completed, errored, cancelled, interrupted — is proven to move its evidence into
    the store and remove its private directory.
    """

    def procedure_request(self, status="0"):
        return runner_module.Request.run(
            "smoke", "smoke for llama-3.1-8b", (str(self.bin / "matuto-procedure"), status),
            TARGET, "2026.10.9")

    def start_procedure(self, status="0"):
        (self.systemd / "run-command").write_text("")
        runner = self.runner()
        started = runner.start(self.procedure_request(status))
        return runner, started.work

    def write_evidence(self, runner, work, checks='[{"name": "exact reply", "status": "PASS"}]\n'):
        evidence = runner._evidence_dir(work.work_id)
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / runner_module.SUMMARY_NAME).write_text("one passed smoke\n")
        (evidence / runner_module.CHECKS_NAME).write_text(checks)
        (evidence / "artifact.txt").write_text("artifact body\n")
        (runner.logs / work.work_id / runner_module.LOG_NAME).write_text(
            "load_tensors: layer 1 assigned\n")

    def evidence_rows(self, work_id):
        connection = store_module.open_store(self.config).connection()
        run = connection.execute("SELECT summary, log, log_truncated FROM runs WHERE run_id = ?",
                                 (work_id,)).fetchone()
        checks = [tuple(row) for row in connection.execute(
            "SELECT name, status, detail FROM checks WHERE run_id = ? ORDER BY sequence", (work_id,))]
        names = [row[0] for row in connection.execute(
            "SELECT name FROM run_evidence WHERE run_id = ? ORDER BY name", (work_id,))]
        return run, checks, names

    def store(self):
        store = store_module.open_store(self.config)
        self.addCleanup(store.close)
        return store

    def test_a_stored_log_never_keeps_the_router_key(self):
        key_file = self.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text("router-key-abc\n")
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        (runner.logs / work.work_id / runner_module.LOG_NAME).write_text(
            "curl -H 'X: router-key-abc'\n")
        self.end_the_unit(work)
        runner.works()
        self.assertEqual(self.evidence_rows(work.work_id)[0]["log"], "[redacted]\n")

    def test_a_completed_runs_evidence_is_moved_and_its_private_directory_removed(self):
        runner, work = self.start_procedure()
        finished = runner.works()[0]
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        run, checks, names = self.evidence_rows(work.work_id)
        self.assertEqual(run["summary"], "one passed smoke\n")
        # The log the run stored is bounded and marked: the cap cut the start of the
        # very line it begins in, and the credential line never survives the move.
        self.assertEqual(run["log_truncated"], 1)
        self.assertLessEqual(len(run["log"]), runner_module.LOG_LIMIT)
        # The cap cut the start of the long line, so that partial line is dropped and
        # the credential line that follows it is the whole stored tail.
        self.assertEqual(run["log"], "[redacted]\n")
        self.assertNotIn("supersecret", run["log"])
        self.assertEqual(checks, [("exact reply", "PASS", "the reply matched")])
        self.assertEqual(names, ["artifact.txt", "record.json"])
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_an_errored_runs_evidence_is_moved_too(self):
        runner, work = self.start_procedure("7")
        self.assertEqual(work.outcome, "errored")
        _, checks, names = self.evidence_rows(work.work_id)
        self.assertEqual(checks, [("exact reply", "PASS", "the reply matched")])
        self.assertEqual(names, ["artifact.txt", "record.json"])
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_wq_raw_requests_are_deleted_before_evidence_moves(self):
        request = runner_module.Request.run(
            "wq", "wq for llama-3.1-8b", (sys.executable, "-c", "import time; time.sleep(30)"),
            TARGET, "2026.10.15", dirty_checkout=True)
        runner, started = self.start(request=request, graceful=0)
        evidence = runner._evidence_dir(started.work.work_id)
        wq = evidence / "wq"
        raw = wq / "private-fixture"
        raw.mkdir(parents=True)
        (raw / "request.body").write_text("private prompt and response")
        private_home = evidence / "wq-private-home/.config/ai-agents-sandbox/matuto-runtime"
        private_home.mkdir(parents=True)
        (private_home / "selection.json").write_text("private runtime selection")
        (wq / "report.json").write_text(json.dumps({"results": [
            {"id": "WQ-01", "status": "PASS", "reason": "completed"},
            {"id": "WQ-02", "status": "SKIP", "reason": "not reached"}]}))
        (evidence / "summary.txt").write_text("WQ stopped\n")

        stopped = runner.stop(started.work.work_id)

        self.assertEqual(stopped.outcome, runner_module.CANCELLED)
        run = self.store().connection().execute(
            "SELECT procedure_revision, dirty_checkout FROM runs WHERE run_id = ?",
            (started.work.work_id,)).fetchone()
        self.assertEqual((run["procedure_revision"], run["dirty_checkout"]), ("2026.10.15", 1))
        _, checks, names = self.evidence_rows(started.work.work_id)
        self.assertEqual(checks, [("WQ-01", "PASS", "completed"),
                                  ("WQ-02", "SKIP", "not reached")])
        self.assertEqual(names, ["wq/report.json"])
        self.assertFalse((runner.logs / started.work.work_id).exists())

    def test_a_cancelled_runs_evidence_is_moved_and_its_directory_removed(self):
        runner, started = self.start(graceful=0)
        work = started.work
        self.write_evidence(runner, work)
        stopped = runner.stop(work.work_id)
        self.assertEqual(stopped.outcome, runner_module.CANCELLED)
        run, checks, names = self.evidence_rows(work.work_id)
        self.assertEqual(run["summary"], "one passed smoke\n")
        self.assertEqual(checks, [("exact reply", "PASS", "")])
        self.assertEqual(names, ["artifact.txt"])
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_an_interrupted_runs_evidence_is_moved_and_its_directory_removed(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        self.unit_of(work).unlink()          # logout or reboot ended the unit
        finished = runner.works()[0]
        self.assertEqual(finished.outcome, runner_module.INTERRUPTED)
        _, checks, names = self.evidence_rows(work.work_id)
        self.assertEqual(names, ["artifact.txt"])
        self.assertFalse((runner.logs / work.work_id).exists())
        # A run that can never be cited still downloads as one file naming itself.
        archive = store_module.run_archive(self.store(), work.work_id)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertEqual(json.loads(bundle.read("record.json"))["outcome"], "interrupted")

    def test_a_runs_private_directory_is_closed_to_group_and_others(self):
        runner, started = self.start()
        work = started.work
        for path in (runner.logs / work.work_id, runner._evidence_dir(work.work_id)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)

    def test_an_unbounded_artifact_is_capped_and_marked(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        with (runner._evidence_dir(work.work_id) / "big.bin").open("wb") as body:
            body.write(b"y" * (runner_module.EVIDENCE_LIMIT + 20))
        self.end_the_unit(work)
        runner.works()
        row = self.store().connection().execute(
            "SELECT size_bytes, truncated, length(body) AS kept FROM run_evidence"
            " WHERE run_id = ? AND name = 'big.bin'", (work.work_id,)).fetchone()
        self.assertEqual((row["size_bytes"], row["truncated"], row["kept"]),
                         (runner_module.EVIDENCE_LIMIT, 1, runner_module.EVIDENCE_LIMIT))

    def test_evidence_of_a_run_that_ended_while_the_manager_was_down_moves_after_restart(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        self.end_the_unit(work)              # the unit finished while nothing was looking
        self.close_runners()                 # the manager process is gone
        restarted = self.runner()            # a restart finds the work in the store
        finished = restarted.works()[0]
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        _, checks, names = self.evidence_rows(work.work_id)
        self.assertEqual(names, ["artifact.txt"])
        self.assertFalse((restarted.logs / work.work_id).exists())

    def test_a_move_that_fails_leaves_the_directory_and_an_unchanged_store(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        self.end_the_unit(work)
        with mock.patch.object(runner_module.store, "write_run_evidence",
                               side_effect=sqlite3.OperationalError("disk I/O error")):
            with self.assertRaises(runner_module.EvidenceError) as refusal:
                runner.works()
        self.assertIn(work.work_id, str(refusal.exception))
        self.assertIn(str(runner.logs / work.work_id), str(refusal.exception))
        # Nothing of this run's end reached the store, so no run is half-moved...
        self.assertIsNone(self.rows()[0]["outcome"])
        self.assertEqual(self.rows("checks"), [])
        self.assertEqual(self.rows("run_evidence"), [])
        self.assertTrue((runner.logs / work.work_id).is_dir())
        # ...and the next reader, with the failure gone, moves it.
        finished = runner.works()[0]
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_a_checks_document_the_store_cannot_read_is_refused_whole(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work, checks='[{"name": "exact reply", "status": "maybe"}]')
        self.end_the_unit(work)
        with self.assertRaises(runner_module.EvidenceError):
            runner.works()
        self.assertIsNone(self.rows()[0]["outcome"])
        self.assertTrue((runner.logs / work.work_id).is_dir())

    def test_a_run_downloads_as_one_file_holding_every_part(self):
        runner, work = self.start_procedure()
        runner.works()
        archive = store_module.run_archive(self.store(), work.work_id)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertEqual(set(bundle.namelist()),
                             {"record.json", "checks.json", "summary.txt", "log.txt",
                              "evidence/artifact.txt", "evidence/record.json"})
            # The archive names the run it holds, citable or not.
            self.assertEqual(json.loads(bundle.read("record.json"))["run_id"], work.work_id)
            self.assertEqual(bundle.read("summary.txt"), b"one passed smoke\n")
            self.assertEqual(json.loads(bundle.read("checks.json"))[0]["status"], "PASS")
        self.assertIsNone(store_module.run_archive(self.store(), "run-not-there"))

    def test_deleting_a_run_removes_its_evidence(self):
        runner, work = self.start_procedure()
        runner.works()
        store = self.store()
        store_module.delete_run(store, work.work_id)
        connection = store.connection()
        self.assertIsNone(connection.execute("SELECT 1 FROM runs WHERE run_id = ?",
                                             (work.work_id,)).fetchone())
        self.assertEqual(connection.execute("SELECT count(*) FROM checks WHERE run_id = ?",
                                            (work.work_id,)).fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT count(*) FROM run_evidence WHERE run_id = ?",
                                            (work.work_id,)).fetchone()[0], 0)

    def test_deleting_a_run_that_has_not_ended_is_refused(self):
        runner, started = self.start()
        work = started.work
        with self.assertRaises(store_module.ActiveRunRefusal) as refusal:
            store_module.delete_run(self.store(), work.work_id)
        self.assertIn(work.work_id, str(refusal.exception))
        # The run keeps its place: still recorded, still holding the slot.
        self.assertIsNone(self.rows()[0]["outcome"])
        self.assertEqual(len(self.units()), 1)
        self.assertEqual(runner.start(smoke_request()).decision, admission.REFUSE)

    def test_a_log_that_cannot_be_read_refuses_the_move(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        (runner.logs / work.work_id / runner_module.LOG_NAME).write_text(
            "load_tensors: layer 1 assigned\n")
        self.end_the_unit(work)
        real_open = Path.open

        def refuse_reads(self, *args, **kwargs):
            if (args[0] if args else kwargs.get("mode", "r")) == "rb":
                raise PermissionError("the log cannot be read")
            return real_open(self, *args, **kwargs)

        # Settlement opens the log for append to lock it; only the read fails here, the
        # way it does for a write-only file or an I/O error.
        with mock.patch.object(Path, "open", refuse_reads):
            with self.assertRaises(runner_module.EvidenceError) as refusal:
                runner.works()
        self.assertIn(work.work_id, str(refusal.exception))
        self.assertIn(str(runner.logs / work.work_id), str(refusal.exception))
        # No empty log was substituted and the directory that held the only copy stays.
        self.assertIsNone(self.rows()[0]["outcome"])
        self.assertEqual(self.rows("run_evidence"), [])
        self.assertTrue((runner.logs / work.work_id).is_dir())
        # The read works again, so the next reader moves the evidence.
        self.assertEqual(runner.works()[0].outcome, runner_module.COMPLETED)
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_an_artifact_below_a_directory_moves_too(self):
        runner, work = self.start_procedure()
        nested = runner._evidence_dir(work.work_id) / "metrics"
        nested.mkdir()
        (nested / "timings.json").write_text('{"seconds": 3}\n')
        runner.works()
        archive = store_module.run_archive(self.store(), work.work_id)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            self.assertEqual(bundle.read("evidence/metrics/timings.json"),
                             b'{"seconds": 3}\n')
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_a_log_that_redaction_expands_is_still_capped(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        # Every line is redacted whole, so the stored text is longer than the file.
        (runner.logs / work.work_id / runner_module.LOG_NAME).write_text("token\n" * 10000)
        self.end_the_unit(work)
        runner.works()
        run = self.evidence_rows(work.work_id)[0]
        self.assertTrue(run["log_truncated"])
        self.assertLessEqual(len(run["log"].encode("utf-8")), runner_module.LOG_LIMIT)
        self.assertEqual(run["log"].count("[redacted]"), run["log"].count("\n"))

    def test_the_capped_log_keeps_the_newest_lines(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        # Redaction expands the repeated lines past the cap, so the tail is cut.
        (runner.logs / work.work_id / runner_module.LOG_NAME).write_text(
            "token\n" * 10000 + "FINAL outcome: error\n")
        self.end_the_unit(work)
        runner.works()
        run = self.evidence_rows(work.work_id)[0]
        self.assertTrue(run["log_truncated"])
        self.assertLessEqual(len(run["log"].encode("utf-8")), runner_module.LOG_LIMIT)
        # The newest line, the one that says how the work ended, is the one kept.
        self.assertTrue(run["log"].endswith("FINAL outcome: error\n"))

    def test_a_directory_left_by_a_committed_run_is_removed_by_a_later_read(self):
        runner, started = self.start()
        work = started.work
        self.write_evidence(runner, work)
        self.end_the_unit(work)
        with mock.patch.object(runner, "_discard", lambda work_ids: None):
            finished = runner.works()[0]      # the end commits, the directory is left
        self.assertEqual(finished.outcome, runner_module.COMPLETED)
        self.assertTrue((runner.logs / work.work_id).is_dir())
        self.runner().works()
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_deleting_a_run_removes_a_directory_left_behind_too(self):
        runner, work = self.start_procedure()
        with mock.patch.object(runner, "_discard", lambda work_ids: None):
            runner.works()
        self.assertTrue((runner.logs / work.work_id).is_dir())
        store_module.delete_run(self.store(), work.work_id)
        runner.works()
        self.assertFalse((runner.logs / work.work_id).exists())

    def test_run_evidence_never_reaches_the_repository(self):
        before = repository_files()
        runner, work = self.start_procedure()
        runner.works()
        store_module.run_archive(self.store(), work.work_id)
        store_module.delete_run(self.store(), work.work_id)
        self.assertNotIn(REPOSITORY, runner.logs.resolve().parents)
        self.assertEqual(repository_files(), before)


if __name__ == "__main__":
    unittest.main()