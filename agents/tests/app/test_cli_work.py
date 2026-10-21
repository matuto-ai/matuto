"""Exercise the work commands as subprocesses against the shared runner."""

import json
import os
import select
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

APP_TESTS = Path(__file__).resolve().parent
AGENTS = APP_TESTS.parents[1]
sys.path[:0] = [str(AGENTS), str(APP_TESTS)]

from matuto import admission, progress, runner, serving_state, store  # noqa: E402
from test_record_validation import RecordTestCase  # noqa: E402
from test_runner import FAKE_SCRIPT, FAKE_SYSTEMCTL, FAKE_SYSTEMD_RUN  # noqa: E402


class CliWorkTest(unittest.TestCase):
    def setUp(self):
        self.records = RecordTestCase("run")
        self.records.setUp()
        self.records.insert_language_profile()
        self.addCleanup(self.records.doCleanups)
        self.app = Path(tempfile.mkdtemp(prefix="matuto-cli-app-"))
        self.addCleanup(shutil.rmtree, self.app, ignore_errors=True)
        shutil.copytree(AGENTS / "matuto", self.app / "matuto")
        shutil.copy2(AGENTS / "matuto_runtime.py", self.app / "matuto_runtime.py")
        self.cli_path = self.app / "matuto" / "cli.py"
        self.bin = Path(tempfile.mkdtemp(prefix="matuto-cli-bin-"))
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.systemd = Path(tempfile.mkdtemp(prefix="matuto-cli-systemd-"))
        self.addCleanup(shutil.rmtree, self.systemd, ignore_errors=True)
        self.environment = {
            **os.environ,
            "XDG_CONFIG_HOME": str(self.records.config),
            "RUNNER_SYSTEMD": str(self.systemd),
            "RUNNER_STORE": str(store.store_path(self.records.config)),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }
        self.fake("systemctl", FAKE_SYSTEMCTL)
        self.fake("systemd-run", FAKE_SYSTEMD_RUN)
        self.fake("script", FAKE_SCRIPT)
        self.procedure("verify")
        self.procedure("smoke", pty=True)
        self.environment_patch = mock.patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.runners = []
        self.addCleanup(self.close_runners)

    def procedure(self, name, *, pty=False):
        path = self.app / "matuto" / "procedures" / f"{name}.py"
        path.write_text(
            "import os\n"
            f"PTY = {pty!r}\n"
            f"PROCEDURE = {name!r}\n\n"
            "def run(opened, record):\n"
            "    print('procedure output: ' + PROCEDURE + ' ' + record['name'], flush=True)\n"
            "    if 'context_size' in record:\n"
            "        print('CTX=' + str(record['context_size']), flush=True)\n"
            "    if os.environ.get('PROCEDURE_LARGE'):\n"
            "        print('payload:' + 'x' * 150000, flush=True)\n"
            "    return 7 if os.environ.get('PROCEDURE_FAIL') else 0\n")

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def cli(self, *args, **keywords):
        environment = dict(self.environment)
        environment.update(keywords.pop("env", {}))
        return subprocess.run([sys.executable, str(self.cli_path), *args], env=environment,
                              capture_output=True, text=True, **keywords)

    def make_runner(self):
        instance = runner.Runner(self.records.config,
                                 serving_state=lambda: admission.STOPPED,
                                 card_holder=lambda: admission.Card(admission.CARD_FREE))
        self.runners.append(instance)
        return instance

    def close_runners(self):
        for instance in self.runners:
            instance.close()

    def wait_for(self, path, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {path}")

    def test_legacy_profile_commands_refuse_without_executing_a_profile(self):
        marker = self.app / "sourced"
        profile = self.app / "example.sh"
        profile.write_text(f"touch {marker}\n")
        for argv in ([str(profile), "serve"], ["example", "serve"], ["speech", "qualify"],
                     ["speech", "list"], ["bundle", "qualify"], ["list"]):
            result = subprocess.run(["bash", str(AGENTS / "matuto-lab"), *argv],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, argv)
            self.assertIn("Error:", result.stderr, argv)
        self.assertFalse(marker.exists())

    def test_wq_procedure_runner_survives_unit_sigterm_to_collect_child_results(self):
        started = self.app / "wq-wrapper-started"
        finished = self.app / "wq-wrapper-finished"
        code = f'''\nimport os, sys, time\nsys.path.insert(0, {str(self.app)!r})\nfrom pathlib import Path\nfrom matuto import procedure_runner as p\nclass Store:\n    def close(self): pass\np.item_names = lambda _kind: ["wq"]\np.store.open_store = lambda *_args: Store()\np.store.snapshot = lambda _opened: Store()\np.store.read_record = lambda _opened, kind, _id: {{"kind": kind}}\np.store.fingerprint = lambda *_args: "fingerprint"\nclass Procedure:\n    def run(self, _opened, _record):\n        Path({str(started)!r}).touch()\n        time.sleep(0.3)\n        Path({str(finished)!r}).touch()\n        return 0\np.load_item = lambda *_args: Procedure()\nsys.exit(p.run(["wq", "language", "profile", "fingerprint"]))\n'''
        process = subprocess.Popen([sys.executable, "-c", code], env=self.environment)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.wait_for(started)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertTrue(finished.exists())

    def test_matuto_lab_wq_runs_as_a_pinned_runner_unit_and_cleans_private_rows_on_completion(self):
        import hashlib

        for name in ("Qwen3.6-35B-A3B-Q4_K_M.gguf", "mmproj-f16.gguf", "mtp-head.gguf"):
            path = self.records.models / name
            self.records.connection.execute(
                "UPDATE acquisition_files SET computed_sha256 = ? WHERE file_name = ?",
                (hashlib.sha256(path.read_bytes()).hexdigest(), name))
        self.records.connection.execute("INSERT INTO lab_port VALUES (1, 18890, 'now')")
        self.records.connection.commit()

        runtime_path = self.app / "matuto" / "matuto_runtime.py"
        runtime_source = runtime_path.read_text()
        runtime_path.write_text(runtime_source.replace(
            'def pci_devices(root=Path("/sys/bus/pci/devices")):\n',
            'def pci_devices(root=Path("/sys/bus/pci/devices")):\n'
            '    if os.environ.get("MATUTO_FIXTURE_PCI"):\n'
            '        return {"state": "completed", "devices": '
            '[{"pciAddress": os.environ["MATUTO_FIXTURE_PCI"]}]}\n'))
        (self.app / ".git").mkdir()
        shutil.copy2(AGENTS.parent / "wq.py", self.app / "wq.py")
        support = self.app / "matuto" / "procedure_support.py"
        support_source = support.read_text()
        self.assertIn("        store_lab._prepare_runtime(spec, environment)\n", support_source)
        support.write_text(support_source.replace(
            "        store_lab._prepare_runtime(spec, environment)\n",
            "        if not os.environ.get('MATUTO_FIXTURE_PCI'):\n"
            "            store_lab._prepare_runtime(spec, environment)\n"))
        data = self.app / "data" / "matuto" / "releases"
        release = data / "fixture-1.2.3"
        release.mkdir(parents=True)
        (data / "current").symlink_to(release.name)
        self.fake("matuto-manager", "#!/usr/bin/env python3\n"
            "import sys\n"
            f"sys.path.insert(0, {str(self.app)!r})\n"
            "from matuto import admission, runner, work_cli\n"
            "work_cli.open_runner = lambda: runner.Runner("
            "serving_state=lambda: admission.STOPPED, "
            "card_holder=lambda: admission.Card(admission.CARD_FREE))\n"
            "from matuto.cli import main\n"
            "sys.exit(main(['matuto-manager', *sys.argv[1:]]))\n")
        driver = (AGENTS.parent / "wq.py").read_text()
        old_entrypoint = '''if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, Finding, ValueError):
        print("WQ could not create or complete its report. Check output-directory permissions and run prerequisites; no private exception text is printed.", file=sys.stderr)
        sys.exit(2)
'''
        fixture_entrypoint = '''if __name__ == "__main__":
    import json
    state = Path(os.environ["RUNNER_SYSTEMD"])
    spec = json.loads(os.environ["MATUTO_WQ_LAUNCH_SPEC"])
    output = Path(sys.argv[sys.argv.index("--run-dir") + 1])
    output.mkdir(parents=True, exist_ok=True)
    private = output / "private-request"
    private.mkdir()
    (private / "request.body").write_text("raw WQ request and response")
    report = {"interrupted": False, "results": [
        {"id": "WQ-01", "status": "PASS", "reason": "completed"},
        {"id": "WQ-02", "status": "SKIP", "reason": "not reached"}],
        "counts": {"PASS": 1, "FAIL": 0, "ERROR": 0, "SKIP": 1}}
    (output / "report.json").write_text(json.dumps(report))
    (state / "wq-spec.json").write_text(json.dumps(spec))
    matuto_progress.report("worker qualification", "WQ-01")
    (state / "wq-started").touch()
    while not (state / "wq-continue").exists(): time.sleep(0.01)
    matuto_progress.report("worker qualification", "WQ-02")
    sys.exit(0)
'''
        self.assertIn(old_entrypoint, driver)
        (self.app / "wq.py").write_text(driver.replace(old_entrypoint, fixture_entrypoint))
        (self.systemd / "run-command").touch()
        key = self.records.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("fixture-router-key-0123456789012345\n")
        key.chmod(0o600)
        env = dict(self.environment, XDG_DATA_HOME=str(self.app / "data"),
                   MATUTO_FIXTURE_PCI="0000:03:00.0")
        run = subprocess.Popen(["bash", str(AGENTS / "matuto-lab"), "prof-coder", "wq"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def complete_child():
            if run.poll() is None:
                (self.systemd / "wq-continue").touch()
                try:
                    run.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    run.kill()
                    run.communicate()
        self.addCleanup(complete_child)
        try:
            self.wait_for(self.systemd / "wq-started")
        except AssertionError:
            (self.systemd / "wq-continue").touch()
            stdout, stderr = run.communicate(timeout=10)
            self.fail(f"WQ did not start; stdout={stdout!r}, stderr={stderr!r}")
        connection = self.records.store.connection()
        row = connection.execute("SELECT * FROM runs WHERE procedure = 'wq'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["record_id"], row["dirty_checkout"], row["procedure_revision"]),
                         ("prof-coder", 1, "fixture-1.2.3"))
        self.assertTrue(row["fingerprint"].startswith("sha256:"))
        pinned = json.loads((self.systemd / "wq-spec.json").read_text())
        self.assertEqual(pinned["record"]["vram_ceiling_bytes"], 34_359_738_368)
        self.assertEqual(pinned["wq_limits"]["vram_mb"], 34_359.738368)
        connection.execute("UPDATE cards SET vram_ceiling_bytes = 16000000000")
        connection.commit()
        self.assertEqual(pinned["wq_limits"]["vram_mb"], 34_359.738368)

        (self.systemd / "wq-continue").touch()
        stdout, stderr = run.communicate(timeout=10)
        running = self.make_runner()
        stopped = running.work(row["run_id"])
        self.assertEqual(stopped.outcome, runner.COMPLETED, stdout + stderr)
        with self.assertRaisesRegex(store.StoreError, "dirty checkout"):
            store.accept(self.records.store, "language", "prof-coder", [row["run_id"]],
                         "worker evidence", {}, "", "reviewer")
        checks = [tuple(result) for result in connection.execute(
            "SELECT name, status FROM checks WHERE run_id = ? ORDER BY sequence", (row["run_id"],))]
        self.assertEqual(checks, [("WQ-01", "PASS"), ("WQ-02", "SKIP")])
        entries = connection.execute("SELECT name, body FROM run_evidence WHERE run_id = ?",
                                     (row["run_id"],)).fetchall()
        self.assertEqual([entry["name"] for entry in entries], ["wq/report.json"])
        self.assertNotIn(b"raw WQ request and response", b"".join(entry["body"] for entry in entries))
        progress = running.progress(running.work(row["run_id"]))
        self.assertEqual((progress.phase, progress.step), ("worker qualification", "WQ-02"))
        self.assertFalse(running._evidence_dir(row["run_id"]).exists())
        self.assertEqual(run.returncode, 0)

    def test_run_starts_through_runner_follows_output_and_returns_procedure_status(self):
        (self.systemd / "run-command").touch()
        done = self.cli("run", "verify", "Qwen3.6 Coder 192K")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("procedure output: verify Qwen3.6 Coder 192K", done.stdout)
        connection = self.records.store.connection()
        row = connection.execute("SELECT procedure, record_id, outcome, fingerprint FROM runs").fetchone()
        self.assertEqual((row["procedure"], row["record_id"], row["outcome"]),
                         ("verify", "prof-coder", "completed"))
        self.assertTrue(row["fingerprint"].startswith("sha256:"))
        self.assertIn("systemd-run --user --unit=matuto-verify-", "\n".join(
            (self.systemd / "calls.log").read_text().splitlines()))

        failed = self.cli("run", "verify", "Qwen3.6 Coder 192K",
                          env={"PROCEDURE_FAIL": "1"})
        self.assertEqual(failed.returncode, 1)
        self.assertIn("errored", failed.stdout)
        self.assertEqual(connection.execute(
            "SELECT outcome FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()[0], "errored")

    def test_run_refuses_a_record_edited_after_admission_before_procedure_execution(self):
        (self.systemd / "run-command").touch()
        (self.systemd / "slow-ack").touch()
        systemd_run = self.bin / "systemd-run"
        source = systemd_run.read_text().replace(
            "    time.sleep(30)\n",
            "    while not (STATE / 'release-launch').exists():\n"
            "        time.sleep(0.01)\n")
        systemd_run.write_text(source)
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "run", "verify",
                                 "Qwen3.6 Coder 192K"], env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for(self.systemd / "acked")
        connection = self.records.store.connection()
        expected = connection.execute("SELECT fingerprint FROM runs").fetchone()[0]
        connection.execute("UPDATE model_profiles SET context_size = 32768"
                           " WHERE profile_id = 'prof-coder'")
        connection.commit()
        (self.systemd / "release-launch").touch()
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, stderr)
        self.assertIn("changed after admission", stdout + stderr)
        self.assertNotIn("procedure output", stdout)
        row = connection.execute("SELECT fingerprint, outcome FROM runs").fetchone()
        self.assertEqual(row["fingerprint"], expected)
        self.assertEqual(row["outcome"], "errored")

    def test_procedure_uses_the_admitted_snapshot_of_dependent_card_values(self):
        procedure = self.app / "matuto" / "procedures" / "verify.py"
        procedure.write_text(
            "import os, time\n"
            "from pathlib import Path\n"
            "from matuto import store\n"
            "state = Path(os.environ['RUNNER_SYSTEMD'])\n"
            "def run(opened, record):\n"
            "    state.joinpath('procedure-loading').touch()\n"
            "    while not state.joinpath('release-procedure').exists(): time.sleep(0.01)\n"
            "    config = store.served_configuration(opened, record)\n"
            "    print('GPU layers: ' + str(config['gpu_layers']), flush=True)\n"
            "    return 0\n")
        (self.systemd / "run-command").touch()
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "run", "verify",
                                 "Qwen3.6 Coder 192K"], env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.wait_for(self.systemd / "procedure-loading")
        connection = self.records.store.connection()
        expected = connection.execute("SELECT fingerprint FROM runs").fetchone()[0]
        connection.execute("UPDATE cards SET default_gpu_layers = 12 WHERE card_id = 'card-r9'")
        connection.commit()
        (self.systemd / "release-procedure").touch()
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn("GPU layers: 99", stdout)
        self.assertNotIn("GPU layers: 12", stdout)
        row = connection.execute("SELECT fingerprint, outcome FROM runs").fetchone()
        self.assertEqual(row["fingerprint"], expected)
        self.assertEqual(row["outcome"], "completed")

    def test_a_new_procedure_file_is_discovered_without_changing_the_cli(self):
        self.procedure("future_probe")
        admission_path = self.app / "matuto" / "admission.py"
        source = admission_path.read_text()
        source = source.replace('"context_search")', '"context_search", "future_probe")', 1)
        source = source.replace('frozenset({"verify"})', 'frozenset({"verify", "future_probe"})', 1)
        admission_path.write_text(source)
        (self.systemd / "run-command").touch()
        done = self.cli("run", "future_probe", "Qwen3.6 Coder 192K")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("procedure output: future_probe Qwen3.6 Coder 192K", done.stdout)
        self.assertEqual(tuple(self.records.connection.execute(
            "SELECT procedure, outcome FROM runs").fetchone()), ("future_probe", "completed"))

    def test_follow_drops_an_oversized_unbroken_log_line_and_reports_completion(self):
        (self.systemd / "run-command").touch()
        done = self.cli("run", "verify", "Qwen3.6 Coder 192K",
                        env={"PROCEDURE_LARGE": "1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("x" * 100, done.stdout)
        self.assertIn("completed", done.stdout)

    def test_ctrl_c_during_launch_stops_the_recorded_unit(self):
        (self.systemd / "slow-ack").touch()
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "run", "verify",
                                 "Qwen3.6 Coder 192K"], env=self.environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.wait_for(self.systemd / "acked")
        os.killpg(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, stderr)
        self.assertIn("cancelled", stdout)
        row = self.records.connection.execute("SELECT outcome, unit_name FROM runs").fetchone()
        self.assertEqual(row["outcome"], "cancelled")
        self.assertNotEqual((self.systemd / "units" / row["unit_name"]).read_text(), "active")

    def test_start_refusal_names_the_current_work_and_records_nothing(self):
        running = self.make_runner()
        current = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        done = self.cli("run", "verify", "Qwen3.6 Coder 192K")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("verify", done.stderr)
        self.assertIn("Qwen3.6 Coder 192K", done.stderr)
        self.assertEqual(self.records.store.connection().execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(len([line for line in (self.systemd / "calls.log").read_text().splitlines()
                              if line.startswith("systemd-run ")]), 1)
        self.assertEqual(running.work(current.work.work_id).outcome, "")

    def test_stop_serving_offer_exits_without_a_run_and_names_the_consequence(self):
        body = FAKE_SYSTEMCTL.replace(
            'if names[0] == "list-units":\n'
            '    print("local-llama-router.service loaded active running Local Router")\n'
            '    sys.exit(0)\n',
            'if names[0] == "list-units":\n'
            '    if "--all" in arguments:\n'
            '        print("matuto-app-voice-part-prof-breeze.service loaded active running Voice")\n'
            '        print("1 unit files listed.")\n'
            '    else:\n'
            '        print("local-llama-router.service loaded active running Local Router")\n'
            '    sys.exit(0)\n'
            'if names[0] == "is-active":\n'
            '    if names[1] == "matuto-app-voice-part-prof-breeze.service":\n'
            '        print("active")\n'
            '        sys.exit(0)\n'
            '    print("inactive")\n'
            '    sys.exit(3)\n')
        self.fake("systemctl", body)
        self.records.connection.execute(
            "INSERT INTO bundles (bundle_id, name, created_at, load_tokens, load_prompt)"
            " VALUES ('bundle-voice', 'Voice 192K', '2026-10-09T16:00:00+00:00', 128, 'hello')")
        self.records.connection.executemany(
            "INSERT INTO bundle_members (bundle_id, role, profile_id) VALUES ('bundle-voice', ?, ?)",
            [("language", "prof-coder"), ("synthesis", "prof-breeze"),
             ("recognition", "prof-whisper")])
        self.records.connection.execute(
            "INSERT INTO voice_serving (voice_serving_id, bundle_id) VALUES (1, 'bundle-voice')")
        self.records.connection.execute(
            "INSERT INTO voice_serving_parts (part_role, bundle_id, port)"
            " VALUES ('synthesis', 'bundle-voice', 8886)")
        observed = serving_state.read(config_root=self.records.config)
        self.assertEqual(observed.state, admission.VOICE, observed)
        done = self.cli("run", "smoke", "Qwen3.6 Coder 192K")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Stop serving first", done.stderr, (self.systemd / "calls.log").read_text())
        self.assertIn("consumers lose the model", done.stderr.lower())
        self.assertEqual(self.records.connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertNotIn("systemd-run", (self.systemd / "calls.log").read_text())

    def test_unknown_card_holder_refuses_card_run_without_recording_or_launching(self):
        done = self.cli("run", "smoke", "Qwen3.6 Coder 192K")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("serving state is unknown", done.stderr)
        self.assertEqual(self.records.store.connection().execute("SELECT count(*) FROM runs").fetchone()[0], 0)
        self.assertNotIn("systemd-run", (self.systemd / "calls.log").read_text())

    def test_cli_lists_runs_and_shared_jobs_from_the_same_store(self):
        running = self.make_runner()
        start = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        connection = self.records.store.connection()
        connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at, outcome)"
            " VALUES ('build-1', 'build', 'matuto-build-one.service', 'llama.cpp HIP', ?, 'completed')",
            (start.work.started_at,))
        done = self.cli("list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"{start.work.work_id}\tverify\tverify for Qwen3.6 Coder 192K\trunning\t",
                      done.stdout)
        self.assertIn("build-1\tbuild\tllama.cpp HIP\tcompleted\t", done.stdout)

    def test_cli_list_includes_the_live_serve_address(self):
        router_key = self.records.config / "ai-agents-sandbox" / "pi-local" / "api-key"
        router_key.parent.mkdir(parents=True, exist_ok=True)
        router_key.write_text("fixture-router-key")
        running = self.make_runner()
        target = runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc")
        started = running.start(runner.Request.run(
            "serve", "serve for Qwen3.6 Coder 192K", ("/bin/true",), target, ""))
        log = running._log_path(started.work.work_id)
        log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        address = "http://127.0.0.1:18890"
        progress.report("Serving", address, path=running._progress_path(started.work.work_id))
        log.write_text("Temporary server: " + address + "\n" +
                        "\n".join(f"server output {line}" for line in range(61)) + "\n")
        listed = self.cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(f"{started.work.work_id}\tserve\tserve for Qwen3.6 Coder 192K\trunning\t"
                      f"{started.work.started_at}\thttp://127.0.0.1:18890", listed.stdout)

    def test_follow_can_be_interrupted_and_cancels_through_the_runner(self):
        running = self.make_runner()
        started = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        running._log_path(started.work.work_id).write_text("follow-ready\n")
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "follow", started.work.work_id],
                                env=self.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.communicate, timeout=5)
        self.addCleanup(proc.kill)
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        self.assertTrue(ready, "follow did not produce output")
        self.assertEqual(proc.stdout.readline(), "follow-ready\n")
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, stderr)
        self.assertIn("cancelled", stdout)
        self.assertEqual(running.work(started.work.work_id).outcome, "cancelled")

    def test_browser_runner_can_stop_a_cli_started_run(self):
        running = self.make_runner()
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "run", "verify", "Qwen3.6 Coder 192K"],
                                env=self.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.wait_for(self.systemd / "starts.log")
        connection = self.records.store.connection()
        row = connection.execute("SELECT run_id FROM runs").fetchone()
        self.assertIsNotNone(row)
        stopped = running.stop(row["run_id"])
        self.assertEqual(stopped.outcome, "cancelled")
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1, stderr)
        self.assertIn("cancelled", stdout)

    def test_ctrl_c_reports_completion_when_the_run_finished_before_stop(self):
        running = self.make_runner()
        started = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        proc = subprocess.Popen([sys.executable, str(self.cli_path), "follow", started.work.work_id],
                                env=self.environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.wait_for(self.systemd / "calls.log")
        deadline = time.time() + 5
        while time.time() < deadline:
            calls = (self.systemd / "calls.log").read_text()
            if f"systemctl --user show {started.work.unit}" in calls:
                break
            time.sleep(0.02)
        else:
            proc.kill()
            self.fail("follow never read the run")
        time.sleep(0.3)
        unit = self.systemd / "units" / started.work.unit
        unit.write_text("active")
        (self.systemd / "units" / f"{started.work.unit}.substate").write_text("exited")
        (self.systemd / "units" / f"{started.work.unit}.result").write_text("success")
        (self.systemd / "units" / f"{started.work.unit}.status").write_text("0")
        (self.systemd / "units" / f"{started.work.unit}.state-monotonic").write_text(
            str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000 - 1_000_000))
        proc.send_signal(signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn("completed", stdout)
        self.assertEqual(running.work(started.work.work_id).outcome, "completed")

    def test_cancel_accepts_a_completion_that_won_before_stop(self):
        running = self.make_runner()
        started = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        work = started.work
        unit = self.systemd / "units" / work.unit
        unit.write_text("active")
        (self.systemd / "units" / f"{work.unit}.substate").write_text("exited")
        (self.systemd / "units" / f"{work.unit}.result").write_text("success")
        (self.systemd / "units" / f"{work.unit}.status").write_text("0")
        (self.systemd / "units" / f"{work.unit}.state-monotonic").write_text(
            str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000 - 1_000_000))
        done = self.cli("cancel", work.work_id)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("completed", done.stdout)
        self.assertEqual(running.work(work.work_id).outcome, "completed")

    def test_interrupted_runs_list_and_follow_as_interrupted(self):
        running = self.make_runner()
        started = running.start(runner.Request.run(
            "verify", "verify for Qwen3.6 Coder 192K", ("/bin/true",),
            runner.Target("language", "prof-coder", "Qwen3.6 Coder 192K", "sha256:abc"), ""))
        (self.systemd / "units" / started.work.unit).unlink()
        listed = self.cli("list")
        self.assertIn(f"{started.work.work_id}\tverify\tverify for Qwen3.6 Coder 192K\tinterrupted\t",
                      listed.stdout)
        followed = self.cli("follow", started.work.work_id)
        self.assertEqual(followed.returncode, 1)
        self.assertIn("interrupted", followed.stdout)


if __name__ == "__main__":
    unittest.main()
