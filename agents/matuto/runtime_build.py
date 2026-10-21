"""Build one pinned recipe as a job and install it atomically. See README.md.

A build is work, not a page. It is submitted to `matuto.runner` as one `build` job
under the shared admission slot, so one build at a time, a refusal while voice
serving runs, and Stop, interruption and progress all come from the one runner
rather than from a second set of rules here. The job's unit runs this file as
`build <job id>` and works through fixed phases, each reported through
`matuto.progress` — the progress interface shared by every unit — and each echoed into the
unit's own log, which the runner keeps capped and redacted.

The phases fail closed:

* the resolved version pin is taken only while it still names exactly these inputs, so a
  pin made for another recipe, ref or target never reaches a unit;
* the recipe prerequisites are checked before anything is fetched;
* the only fetch is `git clone` of the recipe's canonical origin, into clean
  source under Matuto's own data directory — never an operator checkout and never
  the tree `MATUTO_LLAMA_SOURCE` names;
* the checked-out commit, and Breeze's submodule commit, are confirmed against the
  pin before anything is configured;
* configure and build run `matuto.recipes`' argv, never through a shell;
* the whole staged tree is renamed in one step to a unique final path, which does
  not exist before the rename and never replaces an existing installation;
* the installation check then runs on that final path, and only a tree that passed it is
  recorded as installed.

Nothing here registers, selects, qualifies or starts serving. A completed build
records its installation and nothing else; the registry, the runtime selection and
the card belong to registry and runtime selection (RT-7).
"""

import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matuto import admission, installation, progress, recipes, registry, runner
from matuto import runtime_builds, store, versions

# The phases this job reports through the progress interface, in the order it works through them.
CHECKING = "checking the pin"
PREREQUISITES = "checking prerequisites"
FETCHING = "fetching the canonical source"
VERIFYING = "verifying the pinned commit"
CONFIGURING = "configuring the recipe"
BUILDING = "building the recipe"
INSTALLING = "installing the staged tree"
INSPECTING = "checking the installation"
RECORDING = "recording the installation"
PHASES = (CHECKING, PREREQUISITES, FETCHING, VERIFYING, CONFIGURING, BUILDING,
          INSTALLING, INSPECTING, RECORDING)

# Beside agentic serving a build is admitted, but it competes for the same CPU, RAM
# and disk. The warning is advisory: nothing here stops, starts or restarts serving.
CONTENTION_WARNING = ("Serving stays up while this build runs; CPU, RAM and disk "
                      "contention may slow it.")

# Breeze's only submodule. Its pin is not supplied by anyone: it is the gitlink the
# pinned commit itself names, so a submodule that moved under the clone is caught.
GGML_SUBMODULE = "third_party/ggml"

JOB_ID = re.compile(r"build-[0-9a-f]{16}")

# The project and backend each recipe builds, for provenance an operator reads back.
PROJECTS = {recipes.LLAMA_HIP: "llama.cpp", recipes.LLAMA_VULKAN: "llama.cpp",
            recipes.BREEZE: "Breeze-TTS-2.cpp", recipes.WHISPER: "whisper.cpp"}
BACKENDS = {recipes.LLAMA_HIP: "hip", recipes.LLAMA_VULKAN: "vulkan",
            recipes.BREEZE: "vulkan", recipes.WHISPER: "cpu"}


class BuildRefused(ValueError):
    """The build cannot start: a refused input, a missing prerequisite, a bad host."""


class BuildFailure(Exception):
    """A phase did not finish; its reason is the job's recorded failure reason."""


@dataclass(frozen=True)
class Submission:
    """One answered build request: the runner's answer and any contention warning."""

    start: object
    warning: str = ""


@dataclass(frozen=True)
class Paths:
    """The three directories one attempt owns, all outside the checkout.

    `final` is the only one discovery scans. Staging sits beside it rather than
    under it, so a partial or unverified staging tree can never appear installed.
    """

    final: Path
    staging: Path
    source: Path


@dataclass
class Attempt:
    """One attempt's state as its phases leave it."""

    job_id: str
    pinned: dict
    paths: Paths
    matuto: str = ""
    connection: object = None
    phases: list = field(default_factory=list)
    submodules: dict = field(default_factory=dict)
    toolchain: dict = field(default_factory=dict)
    reason: str = ""
    check: object = None
    installed: str = ""


# ---- where a build lives -------------------------------------------------


def work_root():
    """Matuto's own build area in the data directory, beside the build location."""
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data) / "ai-agents-sandbox"


def paths(job_id, *, final_root=None):
    """The three directories for one attempt; every name carries the job's own token.

    The job's token is in every name, so a retry of the same commit, recipe and
    target gets its own staging tree and its own final path and can reuse neither.
    """
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise BuildRefused(f"{job_id!r} is not a build job id")
    token = job_id.split("-", 1)[1]
    return Paths(final=Path(registry.build_root() if final_root is None else final_root),
                 staging=work_root() / "build-staging" / token,
                 source=work_root() / "build-source" / token)


def final_path(attempt_paths, *, recipe, commit, target=None):
    """The unique installation path this attempt renames its staged tree to.

    The name carries the recipe, the commit, the HIP target where there is one and
    the attempt's own token, so it names the tree an operator is looking at and no
    two attempts can propose the same path.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", recipe.lower()).strip("-")
    parts = [slug, commit[:12]] + ([target] if target else []) + [attempt_paths.staging.name]
    return attempt_paths.final / "-".join(parts)


# ---- submitting one build ------------------------------------------------


def submit(running, pin_value, *, recipe, ref, target=None, argv=None):
    """Take only a pin that still names these inputs, and start one build job.

    `pin_value` is the resolved pin shown by the install page; it is refused unless the
    recipe, the ref and the physical target being built are the ones it was made
    for. Admission, the shared slot and the unit are the runner's; this only builds
    the request and adds the contention warning beside agentic serving. Nothing is
    stopped, started or restarted here.
    """
    pinned = versions.retained(pin_value, recipe, ref, target)
    if pinned is None:
        raise BuildRefused(
            f"no current pin names {recipe} at {ref} for target {target or 'no target'}; "
            "check the official version again before building")
    request = runner_request(pinned, argv=argv)
    started = running.start(request)
    warning = (CONTENTION_WARNING if started.decision == admission.ADMIT and
               running.serving_state() == admission.AGENTIC else "")
    return Submission(started, warning)


def runner_request(pinned, *, argv=None):
    """The runner request for one pinned build: its pinned request and its unit argv.

    The pinned request is the whole request as JSON, so the unit reads its recipe,
    ref, full commit and target from the row this very call recorded rather than
    from anything a caller could change afterwards.
    """
    pinned_request = json.dumps(
        {"recipe": pinned.recipe, "ref": pinned.ref, "commit": pinned.commit,
         "target": pinned.target}, sort_keys=True)
    purpose = f"{pinned.recipe} {pinned.commit[:12]}" + (
        f" for {pinned.target}" if pinned.target else "")
    command = tuple(argv) if argv is not None else (
        sys.executable, str(Path(__file__).absolute()), "build")
    return runner.Request.job(admission.BUILD, purpose, command,
                              pinned_request=pinned_request)


# ---- the unit's own entry point ------------------------------------------


def run(argv):
    """Build one recorded job, as its unit runs this file. Answers the exit status."""
    if len(argv) not in (1, 2):
        print("usage: runtime_build.py build [job-id]", file=sys.stderr)
        return 2
    opened = None
    try:
        job_id = argv[1] if len(argv) == 2 else job_id_from_environment()
        opened = store.open_store()
        # The unit writes its own phase rows while the manager may be settling other
        # work, so it waits for the store rather than failing on a held write lock.
        opened.connection().execute(f"PRAGMA busy_timeout = {20 * 1000}")
        return execute(opened.connection(), job_id)
    except (OSError, sqlite3.Error, store.StoreError, BuildRefused, BuildFailure) as error:
        print(f"matuto: {error}", file=sys.stderr)
        return 1
    finally:
        if opened is not None:
            opened.close()


def job_id_from_environment(environment=None):
    """The build job this unit was started for, from the runner's own hand-off.

    The runner gives every unit a private evidence directory named for its work, so
    the unit reads its own job id from there instead of being told it twice. A job
    the store does not record as a build is refused, so a hand-off naming anything
    else never runs one.
    """
    environment = os.environ if environment is None else environment
    directory = environment.get(runner.EVIDENCE_ENV)
    if not directory:
        raise BuildRefused(
            "this process was not started by the runner, so it does not know its build job")
    job_id = Path(directory).parent.name
    opened = store.open_store()
    try:
        row = opened.connection().execute(
            "SELECT job_id FROM jobs WHERE job_id = ? AND kind = ?",
            (job_id, admission.BUILD)).fetchone()
    finally:
        opened.close()
    if row is None:
        raise BuildRefused(f"the store records no build job {job_id!r}")
    return job_id


# ---- the phases ----------------------------------------------------------


def execute(connection, job_id, *, command=subprocess.run, host=None,
            report=progress.report, echo=print):
    """Run every phase of one attempt and record what it ended with.

    Answers 0 only when the installation was checked and recorded. A refusal or a
    failed phase is reported through the progress interface and into the unit's log, recorded with
    its reason, and answered with a nonzero status, which the runner keeps as
    `failed`.
    """
    attempt = _attempt(connection, job_id)
    try:
        _phase(attempt, CHECKING, report, echo)
        _check_pin(attempt)
        _phase(attempt, PREREQUISITES, report, echo)
        runtime_builds.require_prerequisites(attempt.pinned["recipe"], **(host or {}))
        _phase(attempt, FETCHING, report, echo)
        _fetch(attempt, command)
        _phase(attempt, VERIFYING, report, echo)
        _verify(attempt, command)
        chosen = recipes.plan(attempt.pinned["recipe"], attempt.pinned["commit"],
                              attempt.pinned["target"], source=str(attempt.paths.source),
                              staging=str(attempt.paths.staging))
        _phase(attempt, CONFIGURING, report, echo)
        _toolchain(attempt, command)
        _run_argv(chosen.configure, command, echo)
        _phase(attempt, BUILDING, report, echo)
        _run_argv(chosen.build, command, echo)
        _phase(attempt, INSTALLING, report, echo)
        _install(attempt)
        _phase(attempt, INSPECTING, report, echo)
        _inspect(attempt)
        if attempt.check.state != installation.STATE_CHECKED:
            # The renamed tree stays recorded, unchecked, with the check's reason;
            # only a passed check is an installation, so the job did not complete.
            raise BuildFailure(attempt.check.reason or "the installation check did not pass")
    except (BuildRefused, BuildFailure, store.StoreError, OSError, ValueError) as error:
        attempt.reason = str(error)
        _record(connection, attempt)
        echo(f"build {attempt.job_id} failed: {attempt.reason}")
        return 1
    _phase(attempt, RECORDING, report, echo)
    _record(connection, attempt)
    echo(f"build {attempt.job_id} completed at {attempt.installed} "
         f"({attempt.check.state})")
    return 0


def _attempt(connection, job_id):
    """The recorded job's pinned request, its own directories and its Matuto version."""
    row = connection.execute(
        "SELECT job_id, pinned_request FROM jobs WHERE job_id = ? AND kind = ?",
        (job_id, admission.BUILD)).fetchone()
    if row is None:
        raise BuildRefused(f"the store records no build job {job_id!r}")
    try:
        pinned = json.loads(row["pinned_request"])
    except (TypeError, ValueError):
        raise BuildRefused(f"the pinned request of {job_id} cannot be read") from None
    for key in ("recipe", "ref", "commit", "target"):
        if key not in pinned:
            raise BuildRefused(f"the pinned request of {job_id} names no {key}")
    return Attempt(job_id, pinned, paths(job_id), matuto=_matuto_version(connection),
                   connection=connection)


def _phase(attempt, phase, report, echo):
    """Report one phase through the progress interface, into the unit's log, and into the job's history.

    The phase is written as it is reported, so an attempt that is cancelled or
    interrupted still keeps every phase it had reached; a unit killed before its
    final record would otherwise leave no history at all.
    """
    attempt.phases.append(phase)
    report(phase, f"{len(attempt.phases)} of {len(PHASES)}")
    echo(f"[{phase}]")
    attempt.connection.execute("UPDATE jobs SET phase = ? WHERE job_id = ?",
                               (json.dumps(attempt.phases), attempt.job_id))


def _check_pin(attempt):
    """Refuse a pinned request that is not a buildable fixed recipe, before any fetch.

    Version resolution keeps a pin only while its own inputs match and the recipe module owns the recipe's
    argv, so what is left to refuse is a recorded request that names a recipe, a
    full commit and a target the recipe would not build.
    """
    pinned = attempt.pinned
    recipes.plan(pinned["recipe"], pinned["commit"], pinned["target"],
                 source=str(attempt.paths.source), staging=str(attempt.paths.staging))


def _fetch(attempt, command):
    """Clone only the recipe's canonical origin into clean source Matuto owns.

    The origin is the recipe's own and no argument here could name another, so an
    operator checkout and the tree `MATUTO_LLAMA_SOURCE` names are never fetched
    from. The source and staging directories are cleared first: an attempt starts
    from clean source or not at all.
    """
    pinned = attempt.pinned
    chosen = recipes.plan(pinned["recipe"], pinned["commit"], pinned["target"],
                          source=str(attempt.paths.source),
                          staging=str(attempt.paths.staging))
    for directory in (attempt.paths.source, attempt.paths.staging):
        _private_directory(directory.parent)
        _clear(directory)
    _run_argv(chosen.clone, command, echo=None)
    _confirm_origin(attempt, recipes.RECIPES[pinned["recipe"]].origin, command)
    _checkout(attempt, command)


def _checkout(attempt, command):
    """Put the clean checkout on the pinned commit, fetched only from its origin.

    A clone checks out the origin's default branch, which need not be the pinned
    commit: version resolution keeps a pin for any official ref, and for an explicit commit the
    clone usually holds it already. A commit no ref reaches is fetched by its own
    name from the same canonical origin, which the check above has just confirmed.
    Breeze's submodule is brought to the gitlink the pinned commit names, which the
    recursive clone did only for the branch it first checked out.
    """
    commit = attempt.pinned["commit"]
    source = str(attempt.paths.source)
    if _git(attempt.paths.source, ["rev-parse", "HEAD"], command).strip() == commit:
        return
    checked = _run(command, ["git", "-C", source, "checkout", "--quiet", "--detach",
                             commit], capture=True)
    if checked.returncode != 0:
        # A commit no ref reaches is not in the clone; version resolution fetched exactly this
        # commit from the origin when it resolved the pin, so fetch it the same way.
        fetched = _run(command, ["git", "-C", source, "fetch", "--quiet", "--no-tags",
                                 "origin", commit], capture=True)
        if fetched.returncode != 0:
            detail = (fetched.stderr or "").strip() or f"git fetch exited {fetched.returncode}"
            raise BuildFailure(
                f"the pinned commit {commit} is not in the clean checkout and could not be "
                f"fetched from the canonical origin: {detail}")
        _run_argv(["git", "-C", source, "checkout", "--quiet", "--detach", commit],
                  command, echo=None)
    if recipes.RECIPES[attempt.pinned["recipe"]].recursive:
        _run_argv(["git", "-C", source, "submodule", "update", "--init", "--recursive",
                   "--quiet"], command, echo=None)


def _confirm_origin(attempt, origin, command):
    """The clean checkout's origin is the canonical one, so nothing else fed it."""
    reported = _git(attempt.paths.source, ["remote", "get-url", "origin"],
                    command).strip()
    if reported != origin:
        raise BuildFailure(
            f"the clean checkout's origin is {reported!r}, not the canonical {origin!r}")


def _verify(attempt, command):
    """Confirm the checked-out commit, and Breeze's submodule commit, are the pinned ones."""
    pinned = attempt.pinned
    source = attempt.paths.source
    head = _git(source, ["rev-parse", "HEAD"], command).strip()
    if head != pinned["commit"]:
        raise BuildFailure(
            f"the clean checkout holds {head}, not the pinned {pinned['commit']}")
    if pinned["recipe"] != recipes.BREEZE:
        return
    expected = _git(source, ["rev-parse", f"HEAD:{GGML_SUBMODULE}"], command).strip()
    found = _git(source / GGML_SUBMODULE, ["rev-parse", "HEAD"], command).strip()
    attempt.submodules = {GGML_SUBMODULE: expected}
    if found != expected:
        raise BuildFailure(
            f"{GGML_SUBMODULE} holds {found}, not the {expected} the pinned commit names")


def _toolchain(attempt, command):
    """The versions of the tools that built this tree, kept beside its provenance."""
    attempt.toolchain = {"git": _version(["git", "--version"], command),
                         "cmake": _version(["cmake", "--version"], command)}


def _install(attempt):
    """Rename the whole staged tree in one step to a unique final path.

    The path is refused outright when it already exists, which is the case a
    rebuild of the same commit must never reach; and renaming a directory only
    replaces an empty one, so an existing installation is never overwritten even if
    a name ever collided.
    """
    pinned = attempt.pinned
    destination = final_path(attempt.paths, recipe=pinned["recipe"],
                             commit=pinned["commit"], target=pinned["target"])
    if not attempt.paths.staging.is_dir():
        raise BuildFailure("the staged build tree is absent; nothing is installed")
    staged_server = attempt.paths.staging / installation.SERVER_PATHS[pinned["recipe"]]
    if not staged_server.is_file() or not os.access(staged_server, os.X_OK):
        # The build tools said they succeeded but the recipe's server is not there,
        # so the staged tree is not an installation and is never renamed into place.
        raise BuildFailure(
            f"the staged tree holds no runnable {staged_server.name}; nothing is installed")
    _private_directory(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise BuildFailure(f"{destination} already exists; no installation is replaced")
    os.rename(attempt.paths.staging, destination)
    attempt.installed = str(destination)


def _inspect(attempt):
    """Run the installation check on the renamed tree, from a directory unrelated to it."""
    with tempfile.TemporaryDirectory(prefix="matuto-build-check-") as working:
        attempt.check = installation.check(
            attempt.installed, attempt.pinned["recipe"],
            source_commit=attempt.pinned["commit"],
            submodules=attempt.submodules or None,
            # The only channel a renamed tree has for the pin this job confirmed in
            # its clean checkout before the rename.
            verified_checkout=True, working_directory=working)


def _record(connection, attempt):
    """Write the phases, provenance, digests and installation in one transaction.

    A failure anywhere leaves the store exactly as it was, so a half-written
    attempt is never read as an installed one. The provenance and the failure reason
    are kept whatever the outcome, because they are what an installation or a run
    that used one still needs once the log is gone.
    """
    pinned = attempt.pinned
    provenance = {
        "project": PROJECTS[pinned["recipe"]],
        "origin": recipes.RECIPES[pinned["recipe"]].origin,
        "commit": pinned["commit"],
        "submodules": attempt.submodules,
        "recipe": pinned["recipe"],
        "backend": BACKENDS[pinned["recipe"]],
        "target": pinned["target"],
        "toolchain": attempt.toolchain,
        "matuto": attempt.matuto,
        "reason": attempt.reason,
    }
    digests = {} if attempt.check is None else attempt.check.digests
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE jobs SET phase = ?, provenance = ?, output_digests = ? WHERE job_id = ?",
            (json.dumps(attempt.phases), json.dumps(provenance, sort_keys=True),
             json.dumps(digests, sort_keys=True), attempt.job_id))
        if attempt.check is not None:
            recorded_at = _now()
            connection.execute(
                "INSERT OR REPLACE INTO installations (location, recipe, source_revision,"
                " target, toolchain, check_state, check_reason, check_digests, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt.installed, pinned["recipe"], pinned["commit"], pinned["target"],
                 _toolchain_text(attempt.toolchain), attempt.check.state,
                 str(attempt.check.reason or ""), json.dumps(digests, sort_keys=True),
                 recorded_at))
            if attempt.check.state == installation.STATE_CHECKED and pinned["recipe"] in (
                    recipes.BREEZE, recipes.WHISPER):
                server = attempt.check.server
                digest = digests.get(server, {}).get("sha256")
                if not digest:
                    raise BuildFailure(
                        f"the checked {server} executable has no recorded SHA-256")
                kind = "synthesis" if pinned["recipe"] == recipes.BREEZE else "recognition"
                store.record_speech_executable(
                    connection, executable_id=f"speech-{attempt.job_id}", kind=kind,
                    root=attempt.installed, path=server, sha256=digest,
                    version=pinned["commit"], installed_at=recorded_at,
                    provenance={"project": PROJECTS[pinned["recipe"]],
                                "origin": recipes.RECIPES[pinned["recipe"]].origin,
                                "commit": pinned["commit"], "submodules": attempt.submodules,
                                "recipe": pinned["recipe"], "backend": BACKENDS[pinned["recipe"]],
                                "target": pinned["target"], "toolchain": attempt.toolchain,
                                "matuto": attempt.matuto, "installation": attempt.installed})
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


# ---- history -------------------------------------------------------------


def history(connection, job_id):
    """One attempt as job history keeps it, or None when no build job has that id.

    The pinned request, every phase the job reported, its capped redacted log, its
    outcome and failure reason, its provenance, and the SHA-256 digests of the
    executables and libraries the installation check named. Reading it writes nothing.
    """
    row = connection.execute(
        "SELECT job_id, kind, pinned_request, phase, outcome, provenance, output_digests,"
        " log, log_truncated, started_at, ended_at FROM jobs WHERE job_id = ? AND kind = ?",
        (job_id, admission.BUILD)).fetchone()
    if row is None:
        return None
    provenance = _object(row["provenance"])
    return {
        "jobId": row["job_id"], "kind": row["kind"], "pinnedRequest": row["pinned_request"],
        "phases": _object(row["phase"], default=[]), "outcome": row["outcome"] or "",
        "log": row["log"], "logTruncated": bool(row["log_truncated"]),
        "provenance": provenance, "reason": provenance.get("reason", ""),
        "outputDigests": _object(row["output_digests"]),
        "startedAt": row["started_at"], "endedAt": row["ended_at"] or "",
    }


def forget_history(connection, job_id):
    """Delete one attempt's log, keeping the provenance and digests it installed with.

    The record itself stays: an installation and any run that used it still need the
    commit, the recipe, the toolchain and the digests, and the installation row the
    check recorded keeps them as well.
    """
    row = connection.execute("SELECT job_id FROM jobs WHERE job_id = ? AND kind = ?",
                             (job_id, admission.BUILD)).fetchone()
    if row is None:
        raise store.StoreError(f"no build job has ID {job_id!r}")
    connection.execute("UPDATE jobs SET log = '', log_truncated = 0 WHERE job_id = ?",
                       (job_id,))


# ---- commands ------------------------------------------------------------


def _run_argv(argv, command, echo, *, capture=False):
    """Run one recipe argv: never a shell, and never a string where argv belongs."""
    if echo is not None:
        echo("$ " + " ".join(argv))
    done = _run(command, argv, capture=capture)
    if done.returncode != 0:
        raise BuildFailure(f"{' '.join(argv[:2])} exited {done.returncode}")
    return done


def _run(command, argv, *, capture=False):
    """Run one argv, keeping its output only when the caller reads it back.

    A recipe's configure and build inherit the unit's own stdout, so their output
    lands in the capped redacted log; a version or a git answer is captured because
    it is read rather than shown.
    """
    extra = {"capture_output": True, "text": True} if capture else {}
    try:
        return command(argv, env=_environment(argv[0]), check=False, **extra)
    except OSError as error:
        raise BuildFailure(f"{argv[0]} could not run: {error}") from None


def _git(directory, arguments, command):
    return _run_argv(["git", "-C", str(directory), *arguments], command, None,
                     capture=True).stdout


def _version(argv, command):
    """One tool's version line, or empty when it will not say."""
    try:
        done = _run(command, argv, capture=True)
    except BuildFailure:
        return ""
    return done.stdout.strip().splitlines()[0] if done.stdout.strip() else ""


def _environment(program):
    """The caller's environment with every inherited git variable dropped.

    `GIT_DIR` and `GIT_WORK_TREE` can point the clone at an operator checkout and
    `GIT_CONFIG_PARAMETERS` can rewrite the origin URL, so none of them is
    inherited; the two that disable the caller's own configuration are set
    explicitly. This is the version resolver's rule, applied to the build's own commands.
    """
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith("GIT_")}
    if program == "git":
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
    return environment


def _private_directory(directory):
    """Create one of Matuto's own build directories, closed to group and others."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)


def _clear(directory):
    """Empty one of this attempt's own directories, so an attempt starts clean."""
    try:
        if directory.is_symlink() or directory.is_file():
            directory.unlink()
        elif directory.is_dir():
            shutil.rmtree(directory)
    except OSError as error:
        raise BuildFailure(f"{directory} cannot be cleared: {error}") from None


def _matuto_version(connection):
    """The Matuto release this store records, or the store version when it records none."""
    row = connection.execute("SELECT release_version FROM update_state LIMIT 1").fetchone()
    if row is None or not row["release_version"]:
        return f"store schema {store.SCHEMA_VERSION}"
    return str(row["release_version"])


def _toolchain_text(toolchain):
    """The toolchain versions as one line the registry stores beside a tree."""
    return "; ".join(f"{name} {value}" for name, value in sorted(toolchain.items()) if value)


def _object(text, default=None):
    """One JSON column, or the empty value of its kind when it holds none."""
    empty = {} if default is None else default
    if not text:
        return empty
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return empty


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))