"""The build job: admission, the pinned build, the atomic install and the history.

Everything runs against a temporary store, temporary XDG data and config roots and
fake `systemd-run`, `systemctl`, `git`, `cmake` and `hipconfig` on PATH. The fakes
are the host evidence the issue asks for: a fixture bare repository stands in for
the canonical origin, a fake cmake compiles the tree the installation check then really inspects, and
the fake units really run the job's argv, so a unit can be stopped, can finish and
can lose its manager.
"""

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission  # noqa: E402
from matuto import installation, recipes, registry, runner as runner_module  # noqa: E402
from matuto import runtime_build, store as store_module, versions  # noqa: E402
from test_runner import RunnerFixture

REAL_GIT = shutil.which("git")

FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
"""A fake systemd-run: it creates the unit and hands the command to a watcher.

The watcher is what makes the unit real enough for this issue: it runs the job's
argv with the runner's own hand-off in the environment, keeps the unit's log, and
leaves the unit loaded the way RemainAfterExit does once the command ended. The
unit exists before the command starts, so a second request really finds the slot
held, and the caller never waits for the build.
"""
import os
import pathlib
import sqlite3
import subprocess
import sys

STATE = pathlib.Path(os.environ["FAKE_SYSTEMD"])
arguments = sys.argv[1:]
with (STATE / "calls.log").open("a") as log:
    log.write("systemd-run " + " ".join(arguments) + "\\n")
unit = next(a.split("=", 1)[1] for a in arguments if a.startswith("--unit="))

connection = sqlite3.connect(os.environ["FAKE_STORE"])
row = connection.execute("SELECT job_id FROM jobs WHERE unit_name = ?", (unit,)).fetchone()
with (STATE / "starts.log").open("a") as log:
    log.write("{} | {}\\n".format(unit, row[0] if row else "-"))

environment = dict(os.environ)
for argument in arguments:
    if argument.startswith("--setenv="):
        name, _, value = argument[len("--setenv="):].partition("=")
        environment[name] = value

log = next((a.split(":", 1)[1] for a in arguments
            if a.startswith("--property=StandardOutput=append:")), "")
command = arguments[arguments.index("--") + 1:]
watcher = [sys.executable, str(pathlib.Path(sys.argv[0]).with_name("watch-unit.py")),
           str(STATE), unit, log, "--", *command]
units = STATE / "units"
units.mkdir(exist_ok=True)
(units / unit).write_text("active")
subprocess.Popen(watcher, env=environment, start_new_session=True,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
sys.exit(0)
'''

FAKE_WATCHER = '''#!/usr/bin/env python3
"""RemainAfterExit: run the unit's command, then leave the unit loaded and ended."""
import os
import pathlib
import subprocess
import sys
import time

STATE = pathlib.Path(sys.argv[1])
UNIT, LOG = sys.argv[2], sys.argv[3]
command = sys.argv[sys.argv.index("--") + 1:]
UNITS = STATE / "units"
UNITS.mkdir(parents=True, exist_ok=True)
with open(LOG, "ab") if LOG else open(os.devnull, "ab") as output:
    started = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                               start_new_session=True)
(UNITS / (UNIT + ".pid")).write_text(str(started.pid))
status = started.wait()
if (STATE / "keep-gone").exists():
    # A user manager that forgot the unit at logout leaves no state behind at all.
    sys.exit(0)
# Publish the outcome before making the unit observable as ended.
(UNITS / (UNIT + ".result")).write_text("success" if status == 0 else "exit-code")
(UNITS / (UNIT + ".status")).write_text(str(status))
(UNITS / (UNIT + ".state-monotonic")).write_text(
    str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000))
finished = UNITS / (UNIT + ".next")
finished.write_text("active" if status == 0 else "failed")
finished.replace(UNITS / UNIT)
(UNITS / (UNIT + ".substate")).write_text("exited" if status == 0 else "failed")
'''

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
"""A fake systemctl: unit properties, and a kill that ends the real process group."""
import os
import pathlib
import signal
import sys
import time

STATE = pathlib.Path(os.environ["FAKE_SYSTEMD"])
UNITS = STATE / "units"


def _read(path, default):
    return path.read_text().strip() if path.exists() else default


with (STATE / "calls.log").open("a") as log:
    log.write("systemctl " + " ".join(sys.argv[1:]) + "\\n")

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
    wanted = [a.split("=", 1)[1] for a in arguments if a.startswith("--property=")]
    if (UNITS / unit).exists():
        properties = {
            "LoadState": "loaded",
            "ActiveState": (UNITS / unit).read_text().strip(),
            "SubState": _read(UNITS / (unit + ".substate"), "running"),
            "Result": _read(UNITS / (unit + ".result"), "success"),
            "ExecMainStatus": _read(UNITS / (unit + ".status"), "0"),
            "StateChangeTimestampMonotonic": _read(UNITS / (unit + ".state-monotonic"), "")}
    else:
        properties = {"LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
                      "Result": "success", "ExecMainStatus": "0"}
    print("\\n".join("{}={}".format(key, properties.get(key, "")) for key in wanted))
    sys.exit(0)
if names[0] == "kill":
    unit = names[-1]
    pid_file = UNITS / (unit + ".pid")
    if pid_file.exists():
        number = signal.SIGKILL if options.get("--signal") == "SIGKILL" else signal.SIGTERM
        try:
            os.killpg(os.getpgid(int(pid_file.read_text().strip())), number)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.05)
    sys.exit(0)
sys.exit(2)
'''

FAKE_GIT = '''#!/usr/bin/env python3
"""A fake git: it records the argv the build job chose, then does the work.

The canonical origin the job named is written to the log before anything is
redirected, so the test can prove the only origin fetched is the recipe's own. The
checkout's own origin is set back to that canonical URL afterwards, so the job's
check for it is answered by the URL the recipe names rather than a fixture path.
Breeze's two gitlink answers come from the fixture rather than a real submodule,
which is what lets the pinned submodule commit be moved under a passing clone.
"""
import json
import os
import subprocess
import sys

with open(os.environ["FAKE_GIT_STATE"] + "/calls.log", "a") as log:
    log.write("git " + " ".join(sys.argv[1:]) + "\\n")

REAL = os.environ["FAKE_REAL_GIT"]
argv = list(sys.argv[1:])
if argv[:1] == ["clone"]:
    origins = json.loads(os.environ["FAKE_ORIGINS"])
    rest = argv[1:]
    # The fixture origin carries no submodules, and this sandbox's brokered git
    # cannot run git-submodule, so the recursion is recorded but not delegated.
    flags = [item for item in rest if item.startswith("-") and item != "--recursive"]
    origin, destination = [item for item in rest if not item.startswith("-")][:2]
    if origin not in origins:
        print("fatal: not a fixture origin: " + origin, file=sys.stderr)
        sys.exit(128)
    done = subprocess.run([REAL, "clone", *flags, origins[origin], destination], check=False)
    if done.returncode == 0:
        subprocess.run([REAL, "-C", destination, "remote", "set-url", "origin", origin],
                       check=False)
    sys.exit(done.returncode)

index = argv.index("-C") if "-C" in argv else -1
directory = argv[index + 1] if index >= 0 else ""
tail = argv[index + 2:] if index >= 0 else argv
if tail[:1] == ["fetch"]:
    # The clone's origin was rewritten to the canonical URL the job must see, so a
    # later fetch is routed to the fixture origin that stands in for it.
    origins = json.loads(os.environ["FAKE_ORIGINS"])
    arguments = [origins.get(item, item) for item in tail]
    sys.exit(subprocess.run([REAL, "-C", directory, *arguments], check=False).returncode)
if tail[:1] == ["checkout"] and os.environ.get("FAKE_NO_CHECKOUT"):
    # A fixture that refuses to move the checkout, so a mismatch between the clean
    # checkout and the pin can be shown to fail the job before it configures.
    sys.exit(0)
if tail[:1] == ["submodule"]:
    # The fixture origin has no submodules; the job's refresh of Breeze's gitlink is
    # recorded above and its result is read through the faked rev-parse answers.
    sys.exit(0)
if tail == ["rev-parse", "HEAD"] and directory.endswith("third_party/ggml"):
    print(os.environ["FAKE_SUBMODULE_COMMIT"])
    sys.exit(0)
if tail == ["rev-parse", "HEAD:third_party/ggml"]:
    print(os.environ["FAKE_GITLINK_COMMIT"])
    sys.exit(0)
sys.exit(subprocess.run([REAL, *argv], check=False).returncode)
'''

FAKE_CMAKE = '''#!/usr/bin/env python3
"""A fake cmake: it records the argv, then compiles the tree the check will inspect.

The compile waits for a gate when one is named, so a unit can be stopped while the
build is genuinely in flight, and the server waits again when the check runs, so a
unit can lose its manager between the rename and the record. The compiled server
takes its reported commit from the clean checkout it was asked to build.
"""
import json
import os
import subprocess
import sys
import time

STATE = os.environ["FAKE_CMAKE_STATE"]
with open(STATE + "/calls.log", "a") as log:
    log.write("cmake " + " ".join(sys.argv[1:]) + "\\n")

blocking = ["-DFAKE_BLOCK_SERVER"] if os.environ.get("FAKE_BLOCK_SERVER") else []

arguments = sys.argv[1:]
if arguments[:1] == ["--version"]:
    print("cmake version 3.28.3")
    sys.exit(0)

if "--build" not in arguments:
    # The configure step: create the staging tree and record the source it was
    # configured from, which is where the build step reads the pinned commit.
    source = arguments[arguments.index("-S") + 1] if "-S" in arguments else ""
    staging = arguments[arguments.index("-B") + 1] if "-B" in arguments else None
    if staging:
        os.makedirs(staging, exist_ok=True)
        with open(os.path.join(staging, "CMakeCache.txt"), "a") as cache:
            cache.write("CMAKE_HOME_DIRECTORY:INTERNAL=" + source + "\\n")
    sys.exit(0)

staging = arguments[arguments.index("--build") + 1]
target = arguments[arguments.index("--target") + 1] if "--target" in arguments else ""
gate = os.environ.get("FAKE_BUILD_GATE")
if gate:
    open(STATE + "/staged", "a").close()
    while not os.path.exists(gate):
        time.sleep(0.02)

source = ""
cache = os.path.join(staging, "CMakeCache.txt")
if os.path.exists(cache):
    with open(cache) as handle:
        for line in handle:
            if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL="):
                source = line.split("=", 1)[1].strip()

revision = subprocess.run([REAL_GIT := os.environ["FAKE_REAL_GIT"], "-C", source,
                           "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
directory = staging if target == "breeze-server" else os.path.join(staging, "bin")
os.makedirs(directory, exist_ok=True)
subprocess.run(["cc", "-shared", "-fPIC", "-Wl,-soname,libggml-base.so", "-xc", "-",
                "-o", os.path.join(directory, "libggml-base.so")],
               input=open(STATE + "/library.c").read(), text=True, check=True)
# An empty RUNPATH entry is what the install probe refuses, so the fixture can
# produce the tree the installation check rejects without touching the check itself.
rpath = "$ORIGIN:" if os.environ.get("FAKE_EMPTY_RPATH") else "$ORIGIN"
if not os.environ.get("FAKE_NO_SERVER"):
    subprocess.run(["cc", "-o", os.path.join(directory, target), STATE + "/server.c",
                    "-L" + directory, "-lggml-base", "-Wl,-rpath," + rpath,
                    "-Wl,--enable-new-dtags",
                    "-DBLOCK_MARKER=" + json.dumps(os.environ["FAKE_BLOCK_MARKER"]),
                    "-DREPORTED_COMMIT=" + json.dumps(revision[:9])] + blocking, check=True)
'''

FAKE_HIPCONFIG = '''#!/usr/bin/env python3
"""A fake hipconfig: it names the fixture ROCm prefixes the prerequisite check reads."""
import os
import sys

with open(os.environ["FAKE_CMAKE_STATE"] + "/calls.log", "a") as log:
    log.write("hipconfig " + " ".join(sys.argv[1:]) + "\\n")
print(os.environ["FAKE_HIP_BIN"] if sys.argv[1:2] == ["-l"] else os.environ["FAKE_HIP_ROOT"])
'''

STUB = "#!/bin/sh\nexit 0\n"

SERVER_SOURCE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern int fixture_value(void);

static const char *HELP = HELP_TEXT_VALUE;
static const char *BLOCK = BLOCK_MARKER;

int main(int argc, char **argv) {
#ifdef FAKE_BLOCK_SERVER
    // This fixture build's server blocks when it is probed; the check's sandboxed
    // environment never passes the test's variables through, so the switch is
    // compiled in while the test's build env carries FAKE_BLOCK_SERVER.
    FILE *marker = fopen(BLOCK, "a");
    if (marker) { fprintf(marker, "blocking\n"); fclose(marker); }
    for (;;) sleep(1);
#endif
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--help") == 0) { printf("%s", HELP); return 0; }
        if (strcmp(argv[index], "--version") == 0) {
            printf("version: 4096 (build 10218, commit %s)\n", REPORTED_COMMIT);
            return 0;
        }
        if (strcmp(argv[index], "--list-devices") == 0) {
            printf("Available devices:\n  HIP0 AMD Radeon (1000 MiB, 900 MiB free)\n");
            return 0;
        }
    }
    return fixture_value() == 7 ? 0 : 1;
}
"""

HELP = ("usage: server [options]\n  -m FNAME, --model FNAME   model path\n"
        "  -h, --help                show this help message and exit\n"
        "  --version                 show version and build info\n"
        "  --list-devices            list devices\n")

STOPPED = lambda: admission.STOPPED
FREE = lambda: admission.Card(admission.CARD_FREE)


class BuildJobTest(unittest.TestCase):
    """A temporary config root, a temporary data root and fake units on PATH."""

    @classmethod
    def setUpClass(cls):
        origins = tempfile.TemporaryDirectory(prefix="build-job-origins-")
        cls.addClassCleanup(origins.cleanup)
        cls.origin_root = Path(origins.name)
        cls.first_commits = {}
        cls.llama = cls.fixture_origin("llama.cpp")
        cls.breeze = cls.fixture_origin("breeze")
        cls.whisper = cls.fixture_origin("whisper")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="build-job-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.config = self.root / "config"
        self.data = self.root / "data"
        self.state = self.root / "systemd"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.gate = self.root / "gate"
        self.blocked = self.root / "blocked"
        self.run_command = self.state / "run-command"
        self.keep_gone = self.state / "keep-gone"
        self.gate_state = {"FAKE_BUILD_GATE": "", "FAKE_BLOCK_SERVER": "",
                           "FAKE_EMPTY_RPATH": "", "FAKE_NO_SERVER": "",
                           "FAKE_NO_CHECKOUT": "", "FAKE_BLOCK_MARKER": str(self.blocked)}
        self.hip = self.fixture_rocm()
        self.vulkan = self.fixture_vulkan()
        # Progress shows a job's tail only when the key it must never carry was read.
        key = self.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("fixture-key\n")
        (self.state / "server.c").write_text(
            SERVER_SOURCE.replace("HELP_TEXT_VALUE", json.dumps(HELP)))
        (self.state / "library.c").write_text("int fixture_value(void) { return 7; }\n")
        for name, body in (("systemd-run", FAKE_SYSTEMD_RUN), ("systemctl", FAKE_SYSTEMCTL),
                           ("watch-unit.py", FAKE_WATCHER), ("git", FAKE_GIT),
                           ("cmake", FAKE_CMAKE), ("hipconfig", FAKE_HIPCONFIG)):
            self.write(name, body)
        for name in ("ninja", "glslc", "rocminfo", "c++"):
            self.write(name, STUB)
        self.gate_patch = mock.patch.dict(os.environ, dict(self.gate_state, **{
            "FAKE_SYSTEMD": str(self.state),
            "FAKE_STORE": str(store_module.store_path(self.config)),
            "FAKE_CMAKE_STATE": str(self.state), "FAKE_GIT_STATE": str(self.state),
            "FAKE_REAL_GIT": REAL_GIT,
            "FAKE_ORIGINS": json.dumps({recipes.LLAMA_ORIGIN: str(self.llama),
                                        recipes.BREEZE_ORIGIN: str(self.breeze),
                                        recipes.WHISPER_ORIGIN: str(self.whisper)}),
            "FAKE_HIP_ROOT": str(self.hip), "FAKE_HIP_BIN": str(self.hip / "bin"),
            "FAKE_GITLINK_COMMIT": "c" * 40, "FAKE_SUBMODULE_COMMIT": "c" * 40,
            "VULKAN_SDK": str(self.vulkan),
            "XDG_CONFIG_HOME": str(self.config), "XDG_DATA_HOME": str(self.data),
            # An operator checkout the build must never fetch from or build.
            "MATUTO_LLAMA_SOURCE": str(self.root / "operator-checkout"),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}))
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)
        self.runners = []
        self.addCleanup(self.close_runners)

    # ---- the fixture ------------------------------------------------------

    def write(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    @classmethod
    def fixture_origin(cls, name):
        """A bare repository standing in for one canonical origin."""
        work = cls.origin_root / f"source-{name}"
        work.mkdir()
        cls.git(work, "init", "-q")
        cls.git(work, "config", "user.email", "build@example.invalid")
        cls.git(work, "config", "user.name", "Fixture")
        (work / "README").write_text(f"{name} fixture source\n")
        cls.git(work, "add", "README")
        cls.git(work, "commit", "-q", "-m", "fixture source")
        first = cls.git(work, "rev-parse", "HEAD").stdout.strip()
        # A second commit makes the default branch's tip differ from the first, so a
        # pin for an older official commit is not the clone's checked-out commit.
        (work / "README").write_text(f"{name} fixture source, revised\n")
        cls.git(work, "add", "README")
        cls.git(work, "commit", "-q", "-m", "fixture source moves on")
        cls.first_commits[name] = first
        bare = cls.origin_root / f"{name}.git"
        cls.git(cls.origin_root, "clone", "-q", "--bare", str(work), str(bare))
        return bare

    def fixture_rocm(self):
        """The HIP compiler, header, CMake package and library the recipe requires."""
        root = self.root / "rocm"
        (root / "bin").mkdir(parents=True)
        self.executable(root / "bin" / "clang", STUB)
        for package, header, library in (
                ("hip", "hip/hip_runtime.h", "libamdhip64.so"),
                ("hipblas", "hipblas/hipblas.h", "libhipblas.so"),
                ("rocblas", "rocblas/rocblas.h", "librocblas.so")):
            header_path = root / "include" / header
            header_path.parent.mkdir(parents=True, exist_ok=True)
            header_path.write_text("/* fixture */\n")
            (root / "lib" / "cmake" / package).mkdir(parents=True, exist_ok=True)
            (root / "lib" / "cmake" / package / f"{package}-config.cmake").write_text("#\n")
            (root / "lib" / library).write_text("fixture\n")
        return root

    def fixture_vulkan(self):
        """The Vulkan SDK the recipe requires through VULKAN_SDK."""
        root = self.root / "vulkan-sdk"
        for relative in ("include/vulkan/vulkan.h", "include/spirv/unified1/spirv.hpp",
                         "lib/libvulkan.so"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/* fixture */\n")
        return root

    def executable(self, path, body):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    @staticmethod
    def git(directory, *arguments):
        return subprocess.run([REAL_GIT, "-C", str(directory), *arguments],
                              capture_output=True, text=True, check=True)

    def calls(self, prefix):
        path = self.state / "calls.log"
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def commit_of(self, recipe):
        origin = {recipes.BREEZE: self.breeze, recipes.WHISPER: self.whisper}.get(
            recipe, self.llama)
        return self.git(origin, "rev-parse", "HEAD").stdout.strip()

    # ---- the runner -------------------------------------------------------

    def runner(self, serving=STOPPED):
        running = runner_module.Runner(self.config, serving_state=serving, card_holder=FREE)
        self.runners.append(running)
        return running

    def close_runners(self):
        for running in self.runners:
            running.close()

    def pin(self, recipe=recipes.LLAMA_HIP, ref="main", target="gfx1036"):
        chosen = target if recipes.RECIPES[recipe].takes_gfx else None
        return versions.pin(recipe, ref, self.commit_of(recipe), chosen)

    def submit(self, pinned=None, serving=STOPPED, running=None, **keywords):
        """Start one build through the runner and answer the submission."""
        pinned = self.pin() if pinned is None else pinned
        running = self.runner(serving) if running is None else running
        keywords.setdefault("recipe", pinned.recipe)
        keywords.setdefault("ref", pinned.ref)
        keywords.setdefault("target", pinned.target)
        return running, runtime_build.submit(running, pinned, **keywords), pinned

    def start(self, pinned=None, **keywords):
        """Admit one build whose unit really runs, and hand back the running work."""
        running, submitted, _ = self.submit(pinned=pinned, **keywords)
        self.assertEqual(submitted.start.decision, admission.ADMIT, submitted.start.reason)
        self.run_command.touch()
        return running, submitted.start.work

    def settle(self, running, work_id, seconds=10):
        """Wait for one work's recorded end and answer the work."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            running.works()
            work = running.work(work_id)
            if work is not None and work.outcome:
                return work
            time.sleep(0.02)
        self.fail(f"{work_id} never recorded an end")

    def gate_on(self, **environment):
        """Change what the fixture tools do for the next build."""
        self.gate_state.update(environment)
        os.environ.update(environment)

    def installations(self, running):
        return running.store.connection().execute(
            "SELECT * FROM installations ORDER BY location").fetchall()

    def entry(self, running, work_id):
        return runtime_build.history(running.store.connection(), work_id)

    def seed_speech_records(self, running):
        """Existing profiles and qualification history must survive installs unchanged."""
        connection = running.store.connection()
        location = self.root / "previous-speech"
        location.mkdir()
        connection.execute(
            "INSERT INTO artifact_locations VALUES ('loc-previous-speech', ?, '', 'created')",
            (str(location),))
        for executable_id, kind, filename in (
                ("old-synthesis", "synthesis", "breeze-server"),
                ("old-recognition", "recognition", "whisper-server")):
            executable = location / filename
            executable.write_text(executable_id)
            executable.chmod(0o700)
            connection.execute(
                "INSERT INTO speech_executables VALUES (?, ?, 'loc-previous-speech', ?, ?, NULL, '', 'created')",
                (executable_id, kind, str(executable), self.digest(executable)))
        connection.execute(
            "INSERT INTO model_profiles (profile_id, kind, name, alias, speech_executable_id, created_at)"
            " VALUES ('profile-language', 'language', 'Language', 'language', NULL, 'created'),"
            " ('profile-synthesis', 'synthesis', 'Synthesis', '', 'old-synthesis', 'created'),"
            " ('profile-recognition', 'recognition', 'Recognition', '', 'old-recognition', 'created')")
        connection.execute("INSERT INTO bundles (bundle_id, name, created_at) VALUES ('bundle', 'Bundle', 'created')")
        for role, profile in (("language", "profile-language"),
                              ("synthesis", "profile-synthesis"),
                              ("recognition", "profile-recognition")):
            connection.execute("INSERT INTO bundle_members VALUES ('bundle', ?, ?)",
                               (role, profile))
        for kind, record in (("language", "profile-language"), ("bundle", "bundle")):
            connection.execute(
                "INSERT INTO qualification_records VALUES (?, ?, ?, ?, ?, NULL, 'created')",
                (f"qualification-{kind}", kind, record, record, f"fingerprint-{kind}"))

    def speech_snapshot(self, connection):
        tables = ("model_profiles", "bundles", "bundle_members", "qualification_records", "acceptances")
        return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
                for table in tables}

    def installed_at(self, work_id, pinned):
        return runtime_build.final_path(runtime_build.paths(work_id),
                                        recipe=pinned.recipe, commit=pinned.commit,
                                        target=pinned.target)

    def digest(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SubmissionTest(RunnerFixture):
    """The build-specific hand-off, without running a compiler."""

    def test_the_pinned_build_request_reaches_the_runner(self):
        pinned = versions.pin(recipes.LLAMA_HIP, "main", "a" * 40, "gfx1036")
        running = self.runner()
        submitted = runtime_build.submit(running, pinned, recipe=pinned.recipe,
                                         ref=pinned.ref, target=pinned.target)
        self.assertEqual(submitted.start.decision, admission.ADMIT, submitted.start.reason)
        row = self.rows("jobs")[0]
        self.assertEqual(row["kind"], "build")
        self.assertEqual(json.loads(row["pinned_request"]), {
            "recipe": pinned.recipe, "ref": pinned.ref, "commit": pinned.commit,
            "target": pinned.target})

    def test_a_build_beside_agentic_serving_warns_and_never_touches_serving(self):
        pinned = versions.pin(recipes.LLAMA_HIP, "main", "a" * 40, "gfx1036")
        running = self.runner(serving_state=lambda: admission.AGENTIC)
        submitted = runtime_build.submit(running, pinned, recipe=pinned.recipe,
                                         ref=pinned.ref, target=pinned.target)
        self.assertEqual(submitted.start.decision, admission.ADMIT, submitted.start.reason)
        self.assertIn("CPU, RAM and disk contention", submitted.warning)
        issued = [line for line in self.calls() if line.startswith("systemctl") and
                  not line.startswith(("systemctl --user show", "systemctl --user list-units"))]
        self.assertEqual(issued, [], "a build never stops, starts or restarts serving")

    def test_a_pin_made_for_other_inputs_is_refused_before_any_unit(self):
        pinned = versions.pin(recipes.LLAMA_HIP, "main", "a" * 40, "gfx1036")
        running = self.runner()
        for recipe, ref, target in ((recipes.LLAMA_VULKAN, "main", None),
                                    (recipes.LLAMA_HIP, "other", "gfx1036"),
                                    (recipes.LLAMA_HIP, "main", "gfx1201")):
            with self.assertRaises(runtime_build.BuildRefused):
                runtime_build.submit(running, pinned, recipe=recipe, ref=ref, target=target)
        self.assertEqual(self.rows("jobs"), [])
        self.assertEqual(self.calls(), [])


class PinnedBuildTest(BuildJobTest):
    """The build itself: clean source, the recipe's argv, the rename and the check."""

    def test_checked_breeze_and_whisper_builds_are_listed_with_identity_and_digest(self):
        running = self.runner()
        self.seed_speech_records(running)
        before = self.speech_snapshot(running.store.connection())
        breeze = self.pin(recipe=recipes.BREEZE, target=None)
        whisper = self.pin(recipe=recipes.WHISPER, target=None)
        earlier_whisper = versions.pin(recipes.WHISPER, "earlier",
                                       self.first_commits["whisper"], None)
        first_whisper_path = None
        first_whisper_digest = None

        for pinned in (breeze, whisper, earlier_whisper):
            with self.subTest(recipe=pinned.recipe):
                current, work = self.start(pinned=pinned, running=running)
                self.assertIs(current, running)
                self.assertEqual(self.settle(running, work.work_id).outcome, "completed")
                records = store_module.list_speech_executables(running.store.connection())
                executable = next(record for record in records
                                  if record["executable_id"] == f"speech-{work.work_id}")
                expected_server = installation.SERVER_PATHS[pinned.recipe]
                self.assertEqual(executable["kind"],
                                 "synthesis" if pinned.recipe == recipes.BREEZE else "recognition")
                self.assertEqual(executable["path"], expected_server)
                self.assertEqual(executable["root"], str(self.installed_at(work.work_id, pinned)))
                self.assertEqual(executable["sha256"],
                                 self.entry(running, work.work_id)["outputDigests"][expected_server]["sha256"])
                self.assertEqual(executable["version"], pinned.commit)
                self.assertEqual(executable["provenance"]["project"],
                                 runtime_build.PROJECTS[pinned.recipe])
                self.assertEqual(executable["provenance"]["recipe"], pinned.recipe)
                self.assertEqual(executable["provenance"]["backend"],
                                 runtime_build.BACKENDS[pinned.recipe])
                self.assertEqual(executable["provenance"]["commit"], pinned.commit)
                self.assertEqual(len([item for item in records
                                     if item["kind"] == "recognition"]),
                                 1 if pinned is breeze else
                                 2 + (pinned is earlier_whisper))
                if pinned.recipe == recipes.WHISPER:
                    if first_whisper_path is None:
                        first_whisper_path = self.installed_at(work.work_id, pinned) / expected_server
                        first_whisper_digest = self.digest(first_whisper_path)
                    else:
                        self.assertNotEqual(self.installed_at(work.work_id, pinned),
                                            first_whisper_path.parent.parent)
                        self.assertEqual(self.digest(first_whisper_path), first_whisper_digest)
                if pinned.recipe == recipes.BREEZE:
                    self.assertEqual(executable["provenance"]["submodules"],
                                     {runtime_build.GGML_SUBMODULE: "c" * 40})
                self.assertEqual(len(registry.listing(running.store.connection())), 0,
                                 "speech installs never enter the llama.cpp registry")
                self.assertEqual(self.speech_snapshot(running.store.connection()), before)

    def test_a_failed_speech_install_check_records_no_speech_executable(self):
        running = self.runner()
        self.gate_on(FAKE_EMPTY_RPATH="1")
        pinned = self.pin(recipe=recipes.WHISPER, target=None)

        work = self.start(pinned=pinned, running=running)[1]
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        self.assertEqual(store_module.list_speech_executables(running.store.connection()), [])

    def test_a_hip_build_lands_at_a_unique_final_path_checked_and_recorded(self):
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        installed = self.installed_at(work.work_id, pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "completed")
        progress = running.progress(running.work(work.work_id))

        self.assertTrue(installed.is_dir(), f"{installed} was not installed")
        self.assertEqual(installed.parent, registry.build_root())
        row = self.installations(running)[0]
        self.assertEqual(row["location"], str(installed))
        self.assertEqual(row["check_state"], installation.STATE_CHECKED, row["check_reason"])
        self.assertEqual(store_module.list_speech_executables(running.store.connection()), [])
        self.assertEqual(row["source_revision"], pinned.commit)
        self.assertEqual(row["target"], pinned.target)
        self.assertEqual(row["recipe"], pinned.recipe)
        self.assertIn("cmake", row["toolchain"])
        self.assertIn(progress.phase, runtime_build.PHASES,
                      "the job reports progress as it goes")
        entry = self.entry(running, work.work_id)
        self.assertEqual(entry["outcome"], "completed")
        self.assertEqual(entry["phases"], list(runtime_build.PHASES))
        self.assertEqual(entry["outputDigests"]["bin/llama-server"]["sha256"],
                         self.digest(installed / "bin" / "llama-server"))
        self.assertIn("bin/libggml-base.so", entry["outputDigests"])
        self.assertEqual(entry["provenance"]["commit"], pinned.commit)
        self.assertEqual(entry["provenance"]["origin"], recipes.LLAMA_ORIGIN)
        self.assertEqual(entry["provenance"]["recipe"], recipes.LLAMA_HIP)
        self.assertEqual(entry["provenance"]["backend"], "hip")
        self.assertTrue(entry["provenance"]["matuto"])
        self.assertEqual(entry["reason"], "")

    def test_a_rebuild_of_the_same_commit_lands_at_a_new_path_and_changes_nothing(self):
        pinned = self.pin()
        running, first = self.start(pinned=pinned)
        first_path = self.installed_at(first.work_id, pinned)
        self.assertEqual(self.settle(running, first.work_id).outcome, "completed")
        before = self.digest(first_path / "bin" / "llama-server")

        again, second = self.start(pinned=pinned)
        second_path = self.installed_at(second.work_id, pinned)
        self.assertEqual(self.settle(again, second.work_id).outcome, "completed")

        self.assertNotEqual(second_path, first_path)
        self.assertTrue(second_path.is_dir())
        self.assertEqual(self.digest(first_path / "bin" / "llama-server"), before,
                         "a rebuild never overwrites or moves the first installation")
        self.assertEqual(len(self.installations(again)), 2)
        self.assertEqual(self.entry(again, second.work_id)["pinnedRequest"],
                         self.entry(running, first.work_id)["pinnedRequest"])

    def test_a_missing_prerequisite_refuses_before_anything_is_fetched(self):
        (self.bin / "hipconfig").unlink()
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        entry = self.entry(running, work.work_id)
        self.assertIn("hipconfig", entry["reason"])
        self.assertIn(runtime_build.PREREQUISITES, entry["phases"])
        self.assertNotIn(runtime_build.FETCHING, entry["phases"])
        self.assertEqual(self.calls("git clone"), [])
        self.assertEqual(self.calls("cmake -S"), [])
        self.assertEqual(self.installations(running), [])

    def test_a_pin_for_an_older_official_commit_is_checked_out_and_built(self):
        pinned = versions.pin(recipes.LLAMA_HIP, "main", self.first_commits["llama.cpp"],
                              "gfx1036")

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "completed")

        row = self.installations(running)[0]
        self.assertEqual(row["source_revision"], pinned.commit)
        self.assertIn(f"checkout --quiet --detach {pinned.commit}",
                      " ".join(self.calls("git -C")))

    def test_a_pinned_breeze_commit_refreshes_the_submodule_it_names(self):
        pinned = versions.pin(recipes.BREEZE, "main", self.first_commits["breeze"], None)

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "completed")

        self.assertEqual(self.installations(running)[0]["source_revision"], pinned.commit)
        self.assertIn("submodule update --init --recursive", " ".join(self.calls("git -C")))

    def test_a_clean_checkout_that_does_not_hold_the_pin_fails_the_job(self):
        self.gate_on(FAKE_NO_CHECKOUT="1")
        pinned = versions.pin(recipes.LLAMA_HIP, "main", self.first_commits["llama.cpp"],
                              "gfx1036")

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        entry = self.entry(running, work.work_id)
        self.assertIn("clean checkout holds", entry["reason"])
        self.assertIn(pinned.commit, entry["reason"])
        self.assertIn(runtime_build.VERIFYING, entry["phases"])
        self.assertEqual(self.calls("cmake -S"), [], "a mismatch fails before configuring")

    def test_a_pinned_commit_the_origin_does_not_hold_fails_the_job(self):
        pinned = versions.pin(recipes.LLAMA_HIP, "main", "b" * 40, "gfx1036")

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        entry = self.entry(running, work.work_id)
        self.assertIn("b" * 40, entry["reason"])
        self.assertIn(runtime_build.FETCHING, entry["phases"])
        self.assertNotIn(runtime_build.VERIFYING, entry["phases"])
        self.assertEqual(self.calls("cmake -S"), [], "nothing is configured from a missing commit")

    def test_breeze_submodule_commit_must_be_the_one_the_pinned_commit_names(self):
        self.gate_on(FAKE_SUBMODULE_COMMIT="d" * 40)
        pinned = self.pin(recipe=recipes.BREEZE, target=None)

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        entry = self.entry(running, work.work_id)
        self.assertIn("third_party/ggml holds", entry["reason"])
        self.assertIn("c" * 40, entry["reason"])
        self.assertEqual(self.installations(running), [])
        self.assertEqual(self.calls("cmake -S"), [])

    def test_a_tree_failing_the_installation_check_is_recorded_unchecked(self):
        self.gate_on(FAKE_EMPTY_RPATH="1")
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        installed = self.installed_at(work.work_id, pinned)
        row = self.installations(running)[0]
        self.assertEqual(row["location"], str(installed), "the renamed tree is recorded")
        self.assertEqual(row["check_state"], installation.STATE_UNCHECKED)
        self.assertIn("empty", row["check_reason"])
        self.assertIn(row["check_reason"], self.entry(running, work.work_id)["log"])
        found = {item["location"]: item for item in registry.listing(running.store.connection())}
        self.assertEqual(sorted(found), [str(installed)])
        self.assertNotEqual(found[str(installed)]["status"], "discovered",
                            "an unchecked tree is never an installed one")

    def test_a_staged_tree_without_the_server_is_never_renamed_into_place(self):
        self.gate_on(FAKE_NO_SERVER="1")
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.assertEqual(self.settle(running, work.work_id).outcome, "failed")

        self.assertEqual(self.installations(running), [])
        self.assertFalse(self.installed_at(work.work_id, pinned).exists(),
                         "an incomplete tree is never renamed into place")

    def test_staging_is_never_offered_as_an_installation(self):
        pinned = self.pin()
        self.gate_on(FAKE_BUILD_GATE=str(self.gate))
        running, work = self.start(pinned=pinned)
        self.wait_for(self.state / "staged")

        staged = runtime_build.paths(work.work_id).staging
        found = [item["location"] for item in registry.listing(running.store.connection())]

        self.gate.touch()
        self.assertEqual(self.settle(running, work.work_id).outcome, "completed")
        self.assertEqual(found, [], "a staging tree is outside the scanned location")
        self.assertFalse(staged.parent == registry.build_root())
        self.assertEqual(len(self.installations(running)), 1)

    def wait_for(self, path, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"the fixture never reached {path}")


class InterruptionTest(BuildJobTest):
    """Cancel, a vanished unit and interruption, all through the runner's own rules."""

    def test_cancel_mid_build_records_cancelled_with_nothing_installed(self):
        self.gate_on(FAKE_BUILD_GATE=str(self.gate))
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.wait_for(self.state / "staged")
        stopped = running.stop(work.work_id)

        self.assertEqual(stopped.outcome, "cancelled")
        self.assertEqual(self.installations(running), [])
        self.assertEqual(store_module.list_speech_executables(running.store.connection()), [])
        entry = self.entry(running, work.work_id)
        self.assertEqual(entry["outcome"], "cancelled")
        self.assertIn(runtime_build.BUILDING, entry["phases"])
        self.assertNotIn(runtime_build.RECORDING, entry["phases"])
        self.assertEqual([item for item in registry.listing(running.store.connection())
                          if item["location"].startswith(str(registry.build_root()))], [],
                         "a cancelled build leaves no installation behind")
        self.assertTrue(runtime_build.paths(work.work_id).staging.is_dir(),
                        "the staged tree stays staging")

    def test_a_stop_that_races_the_recorded_completion_keeps_completed(self):
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.wait_for_installation(running)
        self.wait_for_unit_end(work)
        stopped = running.stop(work.work_id)

        self.assertEqual(stopped.outcome, "completed",
                         "an installation that won the race stays completed")
        row = self.installations(running)[0]
        self.assertEqual(row["check_state"], installation.STATE_CHECKED)

    def test_a_unit_that_vanished_reads_interrupted_and_installs_nothing(self):
        self.gate_on(FAKE_BLOCK_SERVER="1")
        pinned = self.pin()

        running, work = self.start(pinned=pinned)
        self.wait_for(self.blocked)
        # Logout: the whole unit's control group is gone and systemd forgot it.
        self.keep_gone.touch()
        self.end_the_processes(work)
        for path in (self.state / "units").glob(work.unit + "*"):
            path.unlink()
        settled = self.settle(running, work.work_id)

        self.assertEqual(settled.outcome, "interrupted")
        self.assertEqual(self.installations(running), [],
                         "a tree at the final path without its recorded check is no proof")
        self.assertEqual(store_module.list_speech_executables(running.store.connection()), [])
        installed = self.installed_at(work.work_id, pinned)
        self.assertTrue(installed.is_dir(), "the tree really was renamed before the loss")
        found = {item["location"]: item for item in registry.listing(running.store.connection())}
        self.assertEqual(sorted(found), [str(installed)])
        self.assertNotEqual(found[str(installed)]["status"], "discovered")

    def test_the_interrupted_attempt_is_not_the_same_path_a_retry_takes(self):
        self.gate_on(FAKE_BUILD_GATE=str(self.gate))
        pinned = self.pin()
        running, first = self.start(pinned=pinned)
        self.wait_for(self.state / "staged")
        stopped = running.stop(first.work_id)
        self.assertEqual(stopped.outcome, "cancelled")
        # The first attempt was killed while its build waited; release the fixture
        # gate so the retry runs a whole build of its own.
        self.gate.touch()

        again, second = self.start(pinned=pinned)
        self.assertEqual(self.settle(again, second.work_id).outcome, "completed")

        self.assertNotEqual(self.installed_at(second.work_id, pinned),
                            self.installed_at(first.work_id, pinned))
        self.assertFalse(self.installed_at(first.work_id, pinned).exists())
        self.assertNotEqual(runtime_build.paths(second.work_id).source,
                            runtime_build.paths(first.work_id).source)
        self.assertEqual(len(self.installations(again)), 1)
        self.assertEqual(self.entry(again, second.work_id)["pinnedRequest"],
                         self.entry(running, first.work_id)["pinnedRequest"])

    # ---- waiting on the fixture -------------------------------------------

    def wait_for(self, path, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"the fixture never reached {path}")

    def wait_for_installation(self, running, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.installations(running):
                return
            time.sleep(0.02)
        self.fail("the attempt recorded no installation")

    def wait_for_unit_end(self, work, seconds=10):
        deadline = time.time() + seconds
        substate = self.state / "units" / f"{work.unit}.substate"
        while time.time() < deadline:
            if substate.exists():
                return
            time.sleep(0.02)
        self.fail("the unit never reported an end")

    def end_the_processes(self, work):
        """End this unit's whole control group, as a logout would."""
        pid_file = self.state / "units" / f"{work.unit}.pid"
        if pid_file.exists():
            try:
                os.killpg(os.getpgid(int(pid_file.read_text().strip())), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


class JobHistoryTest(BuildJobTest):
    """What history keeps, and what deleting it takes away."""

    def test_deleting_history_keeps_the_provenance_the_installation_needs(self):
        running, work = self.start()
        self.assertEqual(self.settle(running, work.work_id).outcome, "completed")
        self.assertTrue(self.entry(running, work.work_id)["log"])

        runtime_build.forget_history(running.store.connection(), work.work_id)
        entry = self.entry(running, work.work_id)

        self.assertEqual(entry["log"], "")
        self.assertFalse(entry["logTruncated"])
        self.assertEqual(entry["outcome"], "completed")
        self.assertEqual(entry["provenance"]["commit"], self.commit_of(recipes.LLAMA_HIP))
        self.assertEqual(entry["provenance"]["recipe"], recipes.LLAMA_HIP)
        self.assertEqual(entry["provenance"]["project"], "llama.cpp")
        self.assertIn("bin/llama-server", entry["outputDigests"])
        row = self.installations(running)[0]
        self.assertEqual(json.loads(row["check_digests"]), entry["outputDigests"])
        self.assertEqual(row["source_revision"], entry["provenance"]["commit"])
        found = registry.listing(running.store.connection())
        self.assertEqual(found[0]["status"], "discovered",
                         "the installation is still a usable one")

    def test_an_unknown_job_has_no_history_and_cannot_be_deleted(self):
        connection = self.runner().store.connection()

        self.assertIsNone(runtime_build.history(connection, "build-0123456789abcdef"))
        with self.assertRaises(store_module.StoreError):
            runtime_build.forget_history(connection, "build-0123456789abcdef")

    def test_paths_refuse_anything_that_is_not_a_build_job_id(self):
        for value in ("", "run-0123456789abcdef", "build-XYZ", "../escape"):
            with self.assertRaises(runtime_build.BuildRefused):
                runtime_build.paths(value)


if __name__ == "__main__":
    unittest.main()