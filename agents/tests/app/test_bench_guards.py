"""Keep benchmark allocation, telemetry and process-reaping failures observable."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(AGENTS), str(AGENTS / "tests")]

import runtime_fixture  # noqa: E402
from matuto import matuto_bench  # noqa: E402


class BenchGuardsTest(unittest.TestCase):
    def environment(self, **values):
        return {"CONTEXT_SIZE": "98304", "BENCH_GENERATION_TOKENS": "512",
                "BENCH_PROMPT_TOKENS": "4096", "BENCH_REPETITIONS": "5",
                "BENCH_VRAM_LIMIT_MB": "30000", **values}

    def test_context_block_rounding_refuses_plan_that_exceeds_declared_context(self):
        with self.assertRaisesRegex(matuto_bench.BenchError, "above the profile's declared context"):
            matuto_bench.build_plan(self.environment(CONTEXT_SIZE="5000"), [])

    def test_missing_vram_telemetry_is_not_treated_as_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(matuto_bench.BenchError, "missing or invalid"):
                matuto_bench.Vram.read_device(Path(directory) / "missing")

    def test_pre_run_vram_breach_refuses_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            reading = Path(directory) / "vram-used"
            reading.write_text("31000000000")
            monitor = matuto_bench.Vram(reading, 31_000_000_000, 30_000)
            with self.assertRaisesRegex(matuto_bench.BenchError, "above the declared ceiling"):
                monitor.check()

    def test_interrupted_shutdown_escalates_and_reaps_an_owned_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, text=True, start_new_session=True)

        class InterruptedWait:
            pid = process.pid

            def __init__(self):
                self.interrupted = False

            def poll(self):
                return process.poll()

            def wait(self, timeout):
                if not self.interrupted:
                    self.interrupted = True
                    raise matuto_bench.BenchError("interrupted while waiting for shutdown")
                return process.wait(timeout)

        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with mock.patch.object(matuto_bench, "STOP_SECONDS", 0.1):
                with self.assertRaisesRegex(matuto_bench.BenchError, "interrupted while waiting"):
                    matuto_bench.stop(InterruptedWait())
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


class BenchCallsiteFailureTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="matuto-bench-callsite-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fixture = self.root / "fixture"
        self.agents, self.build = runtime_fixture.create(self.fixture)
        bench = self.agents / "matuto/matuto_bench.py"
        source = bench.read_text()
        self.assertEqual(source.count("READY_SECONDS = 300"), 1)
        self.assertEqual(source.count("STOP_SECONDS = 15"), 1)
        source = source.replace("READY_SECONDS = 300", "READY_SECONDS = 0.2")
        bench.write_text(source.replace("STOP_SECONDS = 15", "STOP_SECONDS = 2"))
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.root / "config"
        self.config.mkdir(mode=0o700)
        self.state = self.root / "state"
        self.model = self.root / "model.gguf"
        self.model.write_bytes(b"measured fixture model")
        self.digest = hashlib.sha256(self.model.read_bytes()).hexdigest()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.environment = {**os.environ, "HOME": str(self.home),
                            "XDG_CONFIG_HOME": str(self.config),
                            "XDG_DATA_HOME": str(self.root / "data"),
                            "XDG_STATE_HOME": str(self.state),
                            "MODEL_PATH": str(self.model), "MODEL_SHA256": self.digest,
                            "MODEL_ALIAS": "fixture-model", "GPU_LAYERS": "99",
                            "CONTEXT_SIZE": "98304", "SERVER_OUTPUT_TOKENS": "512",
                            "SERVER_HOST": "127.0.0.1", "SERVER_PORT": str(port),
                            "BACKEND_VERSION": "fixture",
                            "BENCH_PROMPT_TOKENS": "4096",
                            "BENCH_GENERATION_TOKENS": "512",
                            "BENCH_REPETITIONS": "5", "BENCH_VRAM_LIMIT_MB": "30000",
                            "MATUTO_RUNTIME_MODEL_SHA256": self.digest,
                            "PATH": os.environ["PATH"]}
        runtime_fixture.configure(self.agents, self.build, self.environment)
        self.bench_script = self.agents / "matuto_bench.py"

    def start_bench(self, name, *, environment=None):
        results = self.root / name
        results.mkdir(mode=0o700)
        env = dict(self.environment)
        env.update(environment or {})
        process = subprocess.Popen(
            [sys.executable, "-B", str(self.bench_script), str(results),
             "--bench", "--server"], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        return process, results

    def test_unuseful_depth_override_and_invalid_ceiling_fail_before_any_measurement(self):
        for name, changes, bench_args in (
                ("shallow", {"CONTEXT_SIZE": "4096"}, ()),
                ("owned", {}, ("-d", "1024")),
                ("ceiling", {"BENCH_VRAM_LIMIT_MB": "not-a-number"}, ())):
            results = self.root / name
            results.mkdir(mode=0o700)
            env = {**self.environment, **changes}
            result = subprocess.run(
                [sys.executable, "-B", str(self.bench_script), str(results),
                 "--bench", *bench_args, "--server"],
                env=env, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            summary = json.loads((results / "summary.json").read_text())
            self.assertEqual(summary["outcome"], "error")
            self.assertEqual(summary["performed"], [])
            self.assertFalse((results / "bench.json").exists())

    def test_unready_server_and_malformed_bench_json_are_errors_not_measurements(self):
        (self.fixture / "bench-json").touch()
        unready, results = self.start_bench("unready")
        try:
            self.assertEqual(unready.wait(timeout=10), 1)
        finally:
            if unready.poll() is None:
                unready.kill()
                unready.wait()
            unready.stdout.close()
            unready.stderr.close()
        summary = json.loads((results / "summary.json").read_text())
        self.assertEqual(summary["outcome"], "error")
        self.assertEqual(summary["performed"], ["benchmark"])
        self.assertTrue((results / "readiness-command.txt").is_file())

        (self.fixture / "http").touch()
        (self.fixture / "bench-json").unlink()
        malformed, results = self.start_bench("malformed")
        try:
            self.assertEqual(malformed.wait(timeout=10), 1)
        finally:
            if malformed.poll() is None:
                malformed.kill()
                malformed.wait()
            malformed.stdout.close()
            malformed.stderr.close()
        summary = json.loads((results / "summary.json").read_text())
        self.assertEqual(summary["outcome"], "error")
        self.assertEqual(summary["performed"], ["benchmark", "readiness"])

    def test_second_termination_during_shutdown_reaps_the_benchmark_child(self):
        for marker in ("http", "bench-json", "hold-depth", "ignore-term"):
            (self.fixture / marker).touch()
        (self.fixture / "received.json").unlink(missing_ok=True)
        process, results = self.start_bench("interrupted")
        child = None
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    received = json.loads((self.fixture / "received.json").read_text())
                except (OSError, ValueError):
                    time.sleep(0.02)
                    continue
                if "-d" in received["args"]:
                    child = received["pid"]
                    break
                time.sleep(0.02)
            self.assertIsNotNone(child, "the depth benchmark did not start")
            process.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 5
            shutdown_wait = self.fixture / "shutdown-wait"
            while not shutdown_wait.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(shutdown_wait.exists(), "cleanup did not reach its protected wait")
            self.assertEqual(int(shutdown_wait.read_text()), child)
            os.kill(child, 0)
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=10), 1)
            deadline = time.monotonic() + 5
            while Path(f"/proc/{child}").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(Path(f"/proc/{child}").exists(), "interrupted cleanup left the benchmark process running")
            summary = json.loads((results / "summary.json").read_text())
            self.assertEqual(summary["outcome"], "error")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
            if child is not None and Path(f"/proc/{child}").exists():
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child, 9)


if __name__ == "__main__":
    unittest.main()
