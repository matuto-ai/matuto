"""Serve a stored profile through the runner, including its Stop lifecycle."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

APP_TESTS = Path(__file__).resolve().parent
AGENTS = APP_TESTS.parents[1]
sys.path[:0] = [str(AGENTS), str(APP_TESTS.parent), str(APP_TESTS)]

from matuto import admission, runner, store  # noqa: E402
from matuto.procedures import serve  # noqa: E402
from test_record_validation import RecordTestCase, digest_of  # noqa: E402
import runtime_fixture  # noqa: E402


FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
args = sys.argv[1:]
unit = next(value.split("=", 1)[1] for value in args if value.startswith("--unit="))
environment = dict(os.environ)
for value in args:
    if value.startswith("--setenv="):
        name, _, content = value[len("--setenv="):].partition("=")
        environment[name] = content
command = args[args.index("--") + 1:]
log = next(value.split(":", 1)[1] for value in args
            if value.startswith("--property=StandardOutput=append:"))
(state / "units").mkdir(exist_ok=True)
with open(log, "a") as output:
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                               env=environment, start_new_session=True)
(state / "units" / unit).write_text("active")
(state / (unit + ".pid")).write_text(str(process.pid))
'''

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import os, pathlib, signal, sys, time
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
args = sys.argv[1:]
if args[:1] == ["--user"]: args = args[1:]
if args[0] == "list-units": raise SystemExit(0)
unit = args[1] if args[0] == "show" else args[-1]
unit_state = state / "units" / unit
if args[0] == "show":
    self_exited = (state / "serve-self-exited").exists()
    active = unit_state.read_text() if unit_state.exists() else "not-found"
    main_pid = state / (unit + ".pid")
    if active == "active" and main_pid.exists():
        try:
            process_state = pathlib.Path("/proc", main_pid.read_text(), "stat").read_text().rsplit(")", 1)[1].split()[0]
            self_exited = self_exited or process_state == "Z"
        except FileNotFoundError:
            self_exited = True
    if self_exited: active = "inactive"
    values = {"LoadState": "loaded", "ActiveState": active,
              "SubState": "running" if active == "active" else "dead",
              "Result": "exit-code" if self_exited else "signal",
              "ExecMainStatus": "1" if self_exited else "143",
              "StateChangeTimestampMonotonic": str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)}
    print("\\n".join(value.split("=", 1)[1] + "=" + values[value.split("=", 1)[1]]
                     for value in args if value.startswith("--property=")))
    raise SystemExit(0)
if args[0] == "kill":
    pid_path = state / (unit + ".pid")
    if pid_path.exists():
        root = int(pid_path.read_text())
        parents = {}
        for entry in pathlib.Path("/proc").iterdir():
            if entry.name.isdigit():
                try: parents[int(entry.name)] = int(entry.joinpath("stat").read_text().rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError): pass
        descendants, pending = set(), [root]
        while pending:
            parent = pending.pop()
            children = [pid for pid, ppid in parents.items() if ppid == parent and pid not in descendants]
            descendants.update(children)
            pending.extend(children)
        for pid in descendants:
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        try: os.kill(root, signal.SIGTERM)
        except ProcessLookupError: pass
    unit_state.write_text("inactive")
    raise SystemExit(0)
raise SystemExit(2)
'''


class ServeProcedureTest(unittest.TestCase):
    def setUp(self):
        self.records = RecordTestCase("run")
        self.records.setUp()
        self.addCleanup(self.records.doCleanups)
        self.records.insert_language_profile()
        detected = {"state": "completed", "devices": [{"pciAddress": self.records.detected[0]}]}
        self.detected_patch = mock.patch.object(serve.matuto_runtime, "pci_devices",
                                                return_value=detected)
        self.detected_patch.start()
        self.addCleanup(self.detected_patch.stop)
        router_key = self.records.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        router_key.parent.mkdir(parents=True, exist_ok=True)
        router_key.write_text("fixture-router-key")
        for name in ("Qwen3.6-35B-A3B-Q4_K_M.gguf", "mmproj-f16.gguf", "mtp-head.gguf"):
            path = self.records.models / name
            self.records.connection.execute(
                "UPDATE acquisition_files SET size_bytes = ?, computed_sha256 = ? WHERE file_name = ?",
                (path.stat().st_size, digest_of(path), name))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.records.connection.execute("INSERT INTO lab_port VALUES (1, ?, ?)", (self.port, "now"))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture_root = self.root / "runtime-fixture"
        self.fixture_agents, self.fixture_build = runtime_fixture.create(self.fixture_root)
        runtime = self.fixture_agents / "matuto" / "matuto_runtime.py"
        wrapper = runtime.read_text()
        observation = ('r.pci_devices = lambda root=None: _real_pci_devices(root) if root is not None else '
                       '{"state":"completed", "devices":inventory()["host"]}')
        gated = (
            "def fixture_pci_devices(root=None):\n"
            "    if root is not None: return _real_pci_devices(root)\n"
            "    gate = os.environ.get('FIXTURE_GPU_GATE')\n"
            "    if gate:\n"
            "        import time\n"
            "        Path(gate).write_text('ready')\n"
            "        while not Path(os.environ['FIXTURE_GPU_RELEASE']).exists(): time.sleep(0.01)\n"
            "    return {\"state\":\"completed\", \"devices\":inventory()[\"host\"]}\n"
            "r.pci_devices = fixture_pci_devices")
        self.assertEqual(wrapper.count(observation), 1)
        runtime.write_text(wrapper.replace(observation, gated))
        shutil.copytree(AGENTS / "matuto", self.fixture_agents / "matuto", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("matuto_runtime.py", "matuto_bench.py"))
        self.records.connection.execute("UPDATE builds SET location = ? WHERE build_id = 'build-hip'",
                                        (str(self.fixture_build.parent),))
        (self.fixture_root / "http").touch()
        self.records.connection.commit()
        self.spec = store.launch_spec(self.records.store, "language", "prof-coder",
                                      detected_gpus=self.records.detected)
        self.assertEqual(self.spec["record"]["server_host"], "127.0.0.1")
        self.assertEqual(self.spec["record"]["server_port"], self.port)
        self.systemd, self.binaries = self.root / "systemd", self.root / "bin"
        self.systemd.mkdir()
        self.binaries.mkdir()
        for name, source in (("systemctl", FAKE_SYSTEMCTL), ("systemd-run", FAKE_SYSTEMD_RUN)):
            executable = self.binaries / name
            executable.write_text(source)
            executable.chmod(0o700)
        self.environment = {
            **os.environ, "XDG_CONFIG_HOME": str(self.records.config),
            "MATUTO_STORE_CONFIG_ROOT": str(self.records.config),
            "RUNNER_SYSTEMD": str(self.systemd),
            "RUNNER_STORE": str(store.store_path(self.records.config)),
            "PYTHONPATH": os.pathsep.join((str(self.fixture_agents), str(AGENTS))),
            "PATH": f"{self.binaries}{os.pathsep}{os.environ['PATH']}",
        }
        code = ("import sys; from matuto.procedure_runner import run; "
                "raise SystemExit(run(['serve','language','prof-coder',sys.argv[1]]))")
        self.target = runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K",
                                    self.spec["record"]["fingerprint"])
        self.request = runner.Request.run("serve", "serve for Qwen3.6 Coder 192K",
                                          (sys.executable, "-c", code,
                                           self.spec["record"]["fingerprint"]), self.target, "")
        self.running = None

    def make_runner(self, state=admission.STOPPED):
        return runner.Runner(self.records.config, serving_state=lambda: state,
                             card_holder=lambda: admission.Card(admission.CARD_FREE))

    def _wait_for_server(self, running, work):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=0.2)
            except OSError:
                time.sleep(0.02)
        self.fail(f"fixture server did not start: {running._log_path(work.work_id).read_text()}")

    def _wait_for_address(self, running, work):
        address = f"Temporary server: http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if address in running.progress(work).log:
                return
            time.sleep(0.02)
        self.fail(f"runner log did not publish {address}")

    def _wait_for_port_release(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                try:
                    probe.connect(("127.0.0.1", self.port))
                except OSError:
                    return
            time.sleep(0.02)
        self.fail(f"server still holds 127.0.0.1:{self.port}")

    def test_runner_keeps_server_live_until_stop_and_records_cancellation(self):
        with mock.patch.dict(os.environ, self.environment):
            self.running = self.make_runner()
            self.addCleanup(self.running.close)
            for serving in (admission.AGENTIC, admission.VOICE):
                offered = self.make_runner(serving)
                self.addCleanup(offered.close)
                result = offered.start(self.request)
                self.assertEqual(result.decision, admission.STOP_SERVING_AND_RUN)
                self.assertIn("Stop serving and run", result.reason)
                self.assertEqual(self.records.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

            started = self.running.start(self.request)
            self.assertEqual(started.decision, admission.ADMIT)
            self.assertEqual(started.work.state, runner.RUNNING)
            connection = self._wait_for_server(self.running, started.work)
            with connection:
                connection.sendall(b"GET /health HTTP/1.0\r\n\r\n")
                self.assertIn(b"200 OK", connection.recv(256))

            deadline = time.monotonic() + 2
            while True:
                progress = self.running.progress(started.work)
                if f"Temporary server: http://127.0.0.1:{self.port}" in progress.log:
                    break
                if time.monotonic() >= deadline:
                    self.fail(f"runner log did not publish the address: {progress.log!r}; "
                              f"file={self.running._log_path(started.work.work_id).read_text()!r}; "
                              f"calls={(self.systemd / 'calls.log').read_text() if (self.systemd / 'calls.log').exists() else ''}")
                time.sleep(0.02)

            address = f"http://127.0.0.1:{self.port}"
            self.assertEqual(progress.step, address)
            with self.running._log_path(started.work.work_id).open("a") as output:
                output.write("\n".join(f"server output {line}" for line in range(61)) + "\n")
            progressed = self.running.progress(started.work)
            self.assertNotIn(address, progressed.log)
            self.assertEqual(progressed.step, address)

            refused = self.running.start(self.request)
            self.assertEqual(refused.decision, admission.REFUSE)
            self.assertIn("serve for Qwen3.6 Coder 192K", refused.reason)
            self.assertEqual(self.records.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

            stopped = self.running.stop(started.work.work_id)
            self.assertEqual(stopped.outcome, runner.CANCELLED)
            self.assertEqual(stopped.fingerprint, self.spec["record"]["fingerprint"])
            self._wait_for_port_release()

    def test_runner_uses_the_admitted_profile_snapshot_after_a_live_edit(self):
        gate, release = self.root / "gpu-gate", self.root / "gpu-release"
        environment = dict(self.environment, FIXTURE_GPU_GATE=str(gate),
                           FIXTURE_GPU_RELEASE=str(release))
        with mock.patch.dict(os.environ, environment):
            running = self.make_runner()
            self.addCleanup(running.close)
            started = running.start(self.request)
            deadline = time.monotonic() + 5
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(gate.exists(), "procedure did not reach the frozen launch-spec boundary")
            self.records.connection.execute(
                "UPDATE model_profiles SET context_size = 65536 WHERE profile_id = 'prof-coder'")
            self.records.connection.commit()
            release.touch()
            with self._wait_for_server(running, started.work):
                pass
            args = json.loads((self.fixture_root / "received.json").read_text())["args"]
            self.assertEqual(args[args.index("-c") + 1], "131072")
            self.assertEqual(started.work.fingerprint, self.spec["record"]["fingerprint"])
            stopped = running.stop(started.work.work_id)
            self.assertEqual(stopped.outcome, runner.CANCELLED)
            self._wait_for_port_release()

    def test_runner_records_unexpected_server_exit_and_its_reason(self):
        with mock.patch.dict(os.environ, self.environment):
            running = self.make_runner()
            self.addCleanup(running.close)
            started = running.start(self.request)
            self.assertEqual(started.decision, admission.ADMIT)
            with self._wait_for_server(running, started.work):
                pass
            self._wait_for_address(running, started.work)
            pid = json.loads((self.fixture_root / "received.json").read_text())["pid"]
            os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                ended = next(work for work in running.works() if work.work_id == started.work.work_id)
                if ended.outcome and "exited before Stop" in ended.log:
                    break
                time.sleep(0.02)
            self.assertEqual(ended.outcome, "errored")
            self.assertNotEqual(ended.outcome, runner.COMPLETED)
            self.assertIn(f"Temporary server: http://127.0.0.1:{self.port}", ended.log)
            self.assertIn("exited before Stop", ended.log)
            self._wait_for_port_release()

    def test_runtime_startup_failure_errors_the_run_with_a_reason(self):
        (self.fixture_root / "unreadable-help").touch()
        with mock.patch.dict(os.environ, self.environment):
            running = self.make_runner()
            self.addCleanup(running.close)
            started = running.start(self.request)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                ended = next(work for work in running.works() if work.work_id == started.work.work_id)
                if ended.outcome:
                    break
                time.sleep(0.02)
            self.assertEqual(ended.outcome, "errored", ended.log)
            self.assertIn("matuto-runtime:", ended.log)
            self.assertFalse((self.fixture_root / "received.json").exists())

    def test_port_collision_refuses_before_start(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.records.connection.execute("UPDATE lab_port SET port = ?",
                                            (listener.getsockname()[1],))
            self.records.connection.commit()
            with mock.patch.object(serve.subprocess, "Popen") as launch:
                with self.assertRaisesRegex(store.StoreError, "already in use"):
                    serve.run(self.records.store,
                              store.read_record(self.records.store, "language", "prof-coder"))
            launch.assert_not_called()

    def test_no_port_refusal_names_settings_lab_port_without_launching(self):
        self.records.connection.execute("DELETE FROM lab_port")
        self.records.connection.commit()
        with mock.patch.object(serve.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(store.StoreError, "Settings › Lab port"):
                serve.run(self.records.store,
                          store.read_record(self.records.store, "language", "prof-coder"))
        launch.assert_not_called()

    def test_non_language_record_is_refused(self):
        with self.assertRaisesRegex(store.StoreError, "language profile"):
            serve.run(None, {"kind": "bundle"})


if __name__ == "__main__":
    unittest.main()
