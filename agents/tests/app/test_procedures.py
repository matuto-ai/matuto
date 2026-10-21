"""Verify, smoke and bench run through the shared runner against fixture runtimes."""

import hashlib
import json
import os
import shutil
import socket
import struct
import sys
import threading
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(AGENTS / "tests"))
sys.path.insert(0, str(AGENTS / "tests" / "app"))

import runtime_fixture  # noqa: E402
from matuto import admission, discovery, runner, store  # noqa: E402
from test_runner import RunnerFixture  # noqa: E402

NOW = "2026-10-14T00:00:00+00:00"
PCI = runtime_fixture.PCI


def gguf_fixture(context):
    def text(value):
        encoded = value.encode()
        return struct.pack("<Q", len(encoded)) + encoded
    metadata = (text("general.architecture") + struct.pack("<I", 8) + text("qwen3")
                + text("qwen3.context_length") + struct.pack("<II", 4, context))
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + metadata


class ProcedureRunTest(RunnerFixture):
    def setUp(self):
        super().setUp()
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        connection = self.opened.connection()
        fixture_root = self.root / "fixture"
        self.fixture_agents, fixture_build = runtime_fixture.create(fixture_root)
        shutil.copytree(AGENTS / "matuto", self.fixture_agents / "matuto", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("matuto_runtime.py", "matuto_bench.py"))
        shutil.copy2(AGENTS / "matuto" / "procedure_runner.py",
                     self.fixture_agents / "procedure_runner.py")
        self.connection = connection
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.connection.executescript(f"""
            INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,
                smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,
                notes,created_at) VALUES ('card','GPU','{PCI}',32000000000,42,16,512,128,3,'',
                '2026-10-14T00:00:00+00:00');
            INSERT INTO builds (build_id,label,kind,location,backend,source_revision,target,
                recipe,toolchain,state,notes) VALUES ('llama','fixture','llama-hip','', 'hip',
                'revision','gfx1201','llama-hip','fixture','registered','');
            INSERT INTO runtime_selection VALUES (1,'llama','card','hip','2026-10-14T00:00:00+00:00');
            INSERT INTO lab_port VALUES (1,{port},'2026-10-14T00:00:00+00:00');
            INSERT INTO artifact_locations VALUES ('files','{self.root}','','2026-10-14T00:00:00+00:00');
            INSERT INTO acquisition_records (acquisition_id,name,source,checksum_status,
                location_id,verified_at,created_at) VALUES ('acq','fixture','adoption',
                'locally-hashed','files','2026-10-14T00:00:00+00:00','2026-10-14T00:00:00+00:00');
        """)
        model = self.root / "model.gguf"
        model.write_bytes(gguf_fixture(262144))
        digest = hashlib.sha256(model.read_bytes()).hexdigest()
        self.connection.execute(
            "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
            " VALUES ('acq','model.gguf',?,?,?)", (model.stat().st_size, digest, NOW))
        self.connection.execute(
            "INSERT INTO model_profiles (profile_id,kind,name,alias,context_size,thinking,card_id,"
            "published,notes,created_at) VALUES ('lang','language','fixture profile','fixture',"
            "4096,'high','card',0,'',?)", (NOW,))
        self.connection.execute("INSERT INTO profile_artifacts VALUES ('lang','model','acq','model.gguf')")
        self.connection.execute("INSERT INTO profile_arguments VALUES ('lang','server',0,NULL,'--parallel','1')")
        self.connection.execute("INSERT INTO profile_arguments VALUES ('lang','smoke',0,NULL,'--custom','one value')")
        self.connection.execute("UPDATE builds SET location=? WHERE build_id='llama'",
                                (str(fixture_build.parent),))
        (fixture_root / "bench-json").touch()
        (fixture_root / "http").touch()
        bench_wrapper = self.fixture_agents / "matuto_bench-test.py"
        bench_wrapper.write_text(
            f"import sys\nsys.path.insert(0, {str(self.fixture_agents)!r})\nimport matuto_runtime\n"
            f"import matuto_bench\nmatuto_bench.__file__ = {str(self.fixture_agents / 'matuto_bench.py')!r}\n"
            "matuto_bench.other_servers = lambda: []\n"
            "from matuto import store_bench\nraise SystemExit(store_bench.main(sys.argv[1:]))\n")
        # The copied store-lab entry is imported by procedure modules in the unit.
        (self.fixture_agents / "matuto" / "store_lab.py").write_text(
            (AGENTS / "matuto" / "store_lab.py").read_text().replace(
                'BENCH = Path(__file__).with_name("store_bench.py")',
                f'BENCH = Path({str(bench_wrapper)!r})'))
        self.set_router_key()
        self.connection.commit()
        (self.systemd / "run-command").touch()

    def procedure_request(self, name):
        target_record = store.read_record(self.opened, "language", "lang")
        fingerprint = store.fingerprint(self.opened, target_record)
        target = runner.Target("language", "lang", target_record["name"], fingerprint)
        module = discovery.load_item("procedures", name, root=self.fixture_agents / "matuto")
        command = (sys.executable, str(self.fixture_agents / "procedure_runner.py"), name,
                   "language", "lang", fingerprint)
        return runner.Request.run(name, f"{name} for {target.label}", command, target, "",
                                  pty=bool(getattr(module, "PTY", False)))

    def start_procedure(self, name, *, serving=admission.STOPPED):
        request = self.procedure_request(name)
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            running = self.runner(serving_state=lambda: serving,
                                  card_holder=lambda: admission.Card(admission.CARD_FREE))
            started = running.start(request)
            self.assertEqual(started.decision, admission.ADMIT, started.reason)
            completed = next(work for work in running.works() if work.work_id == started.work.work_id)
            return running, completed, store.read_run_evidence(self.opened, completed.work_id)

    def set_trained_context(self, trained_context):
        model = self.root / "model.gguf"
        model.write_bytes(gguf_fixture(trained_context))
        self.connection.execute(
            "UPDATE acquisition_files SET size_bytes=?,computed_sha256=?"
            " WHERE acquisition_id='acq' AND file_name='model.gguf'",
            (model.stat().st_size, hashlib.sha256(model.read_bytes()).hexdigest()))
        self.connection.commit()

    def run_context_search(self, trained_context, start_rung, failing_contexts=(), prepare_error=False):
        self.set_trained_context(trained_context)
        self.connection.execute("UPDATE model_profiles SET context_size=65536 WHERE profile_id='lang'")
        self.connection.commit()
        failures = sorted(failing_contexts)
        (self.fixture_agents / "matuto" / "procedures" / "wq.py").write_text(f'''
import json, os
from pathlib import Path

FAILURES = {failures!r}
PREPARE_ERROR = {prepare_error!r}

def prepare(opened, target):
    if PREPARE_ERROR:
        raise RuntimeError("fixture refused")
    return {{}}

def run(opened, record, spec=None):
    root = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    fail = record["context_size"] in FAILURES
    checks = [{{"name": "WQ-06", "status": "FAIL" if fail else "PASS", "detail": "fixture"}},
              {{"name": "WQ-10", "status": "PASS", "detail": "fixture"}}]
    (root / "checks.json").write_text(json.dumps(checks))
    (root / "summary.txt").write_text("fixture result\\n")
    report = root / "wq"
    report.mkdir()
    (report / "report.json").write_text(json.dumps({{"results": [
        {{"id": "SETUP", "status": "PASS", "observations": {{}}}},
        {{"id": "WQ-06", "status": checks[0]["status"], "observations": {{
            "sampled_peak_vram_bytes": 28000000000}}}},
        {{"id": "WQ-10", "status": "PASS", "observations": {{}}}}]}}))
    return 0
''')
        record = store.read_record(self.opened, "language", "lang")
        fingerprint = store.fingerprint(self.opened, record)
        target = runner.Target("language", "lang", record["name"], fingerprint)
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        pinned = procedure.prepare(self.opened, target, start_rung)
        command = [sys.executable, str(self.fixture_agents / "procedure_runner.py"),
                   "context_search", "language", "lang", fingerprint,
                   json.dumps(pinned, separators=(",", ":"))]
        request = runner.Request.run("context_search", "context search fixture", command,
                                     target, procedure.REVISION)
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            running = self.runner(serving_state=lambda: admission.STOPPED,
                                  card_holder=lambda: admission.Card(admission.CARD_FREE))
            started = running.start(request)
        self.assertEqual(started.decision, admission.ADMIT, started.reason)
        completed = next(work for work in running.works() if work.work_id == started.work.work_id)
        evidence = store.read_run_evidence(self.opened, completed.work_id)
        self.assertEqual(completed.outcome, runner.COMPLETED, completed.log)
        summary = json.loads(next(item["body"] for item in evidence["artifacts"]
                                  if item["name"] == "context-search.json"))
        return completed, summary

    def test_context_search_failed_consistency_check_names_no_winner(self):
        completed, summary = self.run_context_search(196608, 196608, (131072,))
        self.assertEqual(completed.outcome, runner.COMPLETED)
        self.assertEqual((summary["outcome"], summary["winner"], summary["checked"]),
                         ("no winner", None, False))
        self.assertEqual([row["context"] for row in summary["rungs"]], [196608, 131072])
        self.assertEqual([row["outcome"] for row in summary["rungs"]], ["completed", "completed"])

    def test_context_search_96k_winner_needs_no_consistency_check(self):
        completed, summary = self.run_context_search(98304, 98304)
        self.assertEqual(completed.outcome, runner.COMPLETED)
        self.assertEqual((summary["outcome"], summary["winner"], summary["checked"]),
                         ("winner", 98304, False))
        self.assertEqual([row["context"] for row in summary["rungs"]], [98304])

    def test_context_search_records_a_rung_that_fails_before_running(self):
        completed, summary = self.run_context_search(98304, 98304, prepare_error=True)
        self.assertEqual(completed.outcome, runner.COMPLETED, completed.log)
        self.assertEqual((summary["outcome"], summary["winner"]), ("no winner", None))
        self.assertEqual(summary["rungs"][0]["outcome"], "errored")
        self.assertEqual(summary["rungs"][0]["checks"][0]["status"], "ERROR")

    def test_context_search_crash_records_child_and_excludes_private_wq_data(self):
        self.connection.execute("UPDATE model_profiles SET context_size=98304 WHERE profile_id='lang'")
        self.connection.commit()
        (self.fixture_agents / "matuto" / "procedures" / "wq.py").write_text('''
import os, signal
from pathlib import Path

def prepare(opened, target):
    return {}

def run(opened, record, spec=None):
    raw = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"]) / "wq" / "private-fixture"
    raw.mkdir(parents=True)
    (raw / "request.json").write_text("SYNTHETIC_PRIVATE_REQUEST_BODY")
    os.kill(os.getpid(), signal.SIGKILL)
''')
        record = store.read_record(self.opened, "language", "lang")
        fingerprint = store.fingerprint(self.opened, record)
        target = runner.Target("language", "lang", record["name"], fingerprint)
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        pinned = procedure.prepare(self.opened, target, 98304)
        command = [sys.executable, str(self.fixture_agents / "procedure_runner.py"),
                   "context_search", "language", "lang", fingerprint,
                   json.dumps(pinned, separators=(",", ":"))]
        request = runner.Request.run("context_search", "interrupted context-search fixture", command,
                                     target, procedure.REVISION)
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            running = self.runner(serving_state=lambda: admission.STOPPED,
                                  card_holder=lambda: admission.Card(admission.CARD_FREE))
            started = running.start(request)
            parent = next(work for work in running.works() if work.work_id == started.work.work_id)
        self.assertEqual(parent.outcome, "errored", parent.log)
        child = self.connection.execute(
            "SELECT run_id, outcome FROM runs WHERE procedure='wq'").fetchone()
        self.assertIsNotNone(child)
        self.assertEqual(child["outcome"], runner.INTERRUPTED)
        parent_evidence = store.read_run_evidence(self.opened, parent.work_id)
        child_evidence = store.read_run_evidence(self.opened, child["run_id"])
        summary = json.loads(next(item["body"] for item in parent_evidence["artifacts"]
                                  if item["name"] == "context-search.json"))
        self.assertEqual((summary["outcome"], summary["winner"]), ("no winner", None))
        self.assertEqual(summary["rungs"][0]["outcome"], runner.INTERRUPTED)
        for item in parent_evidence["artifacts"] + child_evidence["artifacts"]:
            self.assertNotIn("private-fixture/request.json", item["name"])
            self.assertNotIn(b"SYNTHETIC_PRIVATE_REQUEST_BODY", item["body"])

    def test_context_search_refuses_unoffered_start_and_context_below_floor(self):
        self.set_trained_context(262144)
        self.connection.execute("UPDATE model_profiles SET context_size=262144 WHERE profile_id='lang'")
        self.connection.commit()
        record = store.read_record(self.opened, "language", "lang")
        target = runner.Target("language", "lang", record["name"], store.fingerprint(self.opened, record))
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        with self.assertRaisesRegex(store.StoreError, "not offered"):
            procedure.prepare(self.opened, target, 65536)
        self.set_trained_context(65536)
        self.connection.execute("UPDATE model_profiles SET context_size=262144 WHERE profile_id='lang'")
        self.connection.commit()
        record = store.read_record(self.opened, "language", "lang")
        target = runner.Target("language", "lang", record["name"], store.fingerprint(self.opened, record))
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        with self.assertRaisesRegex(store.StoreError, "below the 96K floor"):
            procedure.prepare(self.opened, target, 65536)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_smoke_reports_current_phase_and_step_through_runner_progress(self):
        hold = self.root / "fixture" / "hold-smoke"
        hold.touch()
        answers = []
        request = self.procedure_request("smoke")

        def launch():
            with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
                opened = runner.Runner(self.config, serving_state=lambda: admission.STOPPED,
                                       card_holder=lambda: admission.Card(admission.CARD_FREE))
                try:
                    answers.append(opened.start(request))
                finally:
                    opened.close()

        thread = threading.Thread(target=launch)
        thread.start()
        self.wait_for(self.root / "fixture" / "received.json")
        monitor = self.runner()
        try:
            work = monitor.works()[0]
            progress = monitor.progress(work)
            self.assertEqual((progress.state, progress.phase, progress.step),
                             (runner.RUNNING, "smoke", "running the exact-reply check"))
            self.connection.execute("UPDATE model_profiles SET context_size=8192 WHERE profile_id='lang'")
            self.connection.commit()
        finally:
            hold.unlink()
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(answers[0].decision, admission.ADMIT)
        completed = next(work for work in monitor.works() if work.work_id == answers[0].work.work_id)
        self.assertEqual(completed.fingerprint, request.fingerprint)
        self.assertNotEqual(store.fingerprint(self.opened,
                         store.read_record(self.opened, "language", "lang")), request.fingerprint)
        args = json.loads((self.root / "fixture" / "received.json").read_text())["args"]
        self.assertEqual(args[args.index("-c") + 1], "4096")

    def test_smoke_captures_cli_output_with_a_real_controlling_terminal(self):
        real_script = shutil.which("script", path="/usr/bin:/bin")
        self.assertIsNotNone(real_script)
        (self.bin / "script").unlink()
        (self.bin / "script").symlink_to(real_script)
        _, smoke, evidence = self.start_procedure("smoke")
        self.assertEqual(smoke.outcome, runner.COMPLETED, smoke.log)
        self.assertEqual(evidence["checks"][0]["status"], "PASS")
        output = next(item for item in evidence["artifacts"]
                      if item["name"] == "smoke-output.txt")["body"]
        self.assertIn(b"fixture CLI banner", output)

    def test_context_search_records_child_runs_at_only_the_temporary_context(self):
        self.connection.execute("UPDATE model_profiles SET context_size=262144 WHERE profile_id='lang'")
        self.connection.commit()
        (self.fixture_agents / "matuto" / "procedures" / "wq.py").write_text('''
import json, os
from pathlib import Path

def prepare(opened, target):
    return {}

def run(opened, record, spec=None):
    root = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    fail = record["context_size"] == 262144
    checks = [{"name": "WQ-06", "status": "FAIL" if fail else "PASS", "detail": "fixture"},
              {"name": "WQ-10", "status": "PASS", "detail": "fixture"}]
    (root / "record.json").write_text(json.dumps({"fingerprint": "fixture"}))
    (root / "checks.json").write_text(json.dumps(checks))
    (root / "summary.txt").write_text("fixture WQ result\\n")
    report = root / "wq"
    report.mkdir()
    (report / "report.json").write_text(json.dumps({
        "limits": {"vram_mb": 32000},
        "results": [{"id": "SETUP", "status": "PASS", "observations": {}},
                    {"id": "WQ-06", "status": checks[0]["status"],
                     "observations": {"sampled_peak_vram_bytes": 28000000000}},
                    {"id": "WQ-10", "status": "PASS", "observations": {}}]}))
    return 0
''')
        source = store.read_record(self.opened, "language", "lang")
        original_fingerprint = store.fingerprint(self.opened, source)
        target = runner.Target("language", "lang", source["name"], original_fingerprint)
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        pinned = procedure.prepare(self.opened, target, 262144)
        command = [sys.executable, str(self.fixture_agents / "procedure_runner.py"),
                   "context_search", "language", "lang", original_fingerprint,
                   json.dumps(pinned, separators=(",", ":"))]
        request = runner.Request.run("context_search", "search for fixture profile", command,
                                     target, procedure.REVISION)
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            started = self.runner(serving_state=lambda: admission.STOPPED,
                                  card_holder=lambda: admission.Card(admission.CARD_FREE)).start(request)
        self.assertEqual(started.decision, admission.ADMIT, started.reason)
        work = next(item for item in self.runner().works() if item.work_id == started.work.work_id)
        self.assertEqual(work.outcome, runner.COMPLETED, work.log)
        evidence = store.read_run_evidence(self.opened, work.work_id)
        summary = json.loads(next(item["body"] for item in evidence["artifacts"]
                                  if item["name"] == "context-search.json"))
        self.assertEqual((summary["outcome"], summary["winner"], summary["checked"]),
                         ("winner", 196608, True))
        self.assertEqual([row["context"] for row in summary["rungs"]], [262144, 196608, 131072])
        self.assertEqual([row["outcome"] for row in summary["rungs"]],
                         ["completed", "completed", "completed"])
        self.assertEqual(len(set(row["fingerprint"] for row in summary["rungs"])), 3)
        self.assertEqual(self.connection.execute(
            "SELECT context_size FROM model_profiles WHERE profile_id='lang'").fetchone()[0], 262144)
        self.assertEqual(store.fingerprint(self.opened,
                         store.read_record(self.opened, "language", "lang")), original_fingerprint)
        child_runs = self.connection.execute(
            "SELECT run_id, fingerprint, outcome FROM runs WHERE procedure='wq' ORDER BY started_at, rowid").fetchall()
        self.assertEqual(len(child_runs), 3)
        self.assertEqual({row["fingerprint"] for row in child_runs},
                         {row["fingerprint"] for row in summary["rungs"]})
        updated = store.read_record(self.opened, "language", "lang")
        updated["context_size"] = 196608
        store.save_record(self.opened, updated, record_id="lang")
        current = store.read_record(self.opened, "language", "lang")
        self.assertEqual(store.fingerprint(self.opened, current), summary["rungs"][1]["fingerprint"])
        cited = self.connection.execute(
            "SELECT outcome FROM runs WHERE procedure='wq' AND fingerprint=?",
            (summary["rungs"][1]["fingerprint"],)).fetchone()
        self.assertEqual(cited["outcome"], "completed")

    def test_context_search_bundle_runs_each_rung_at_its_language_member_context(self):
        for role, profile, executable, acquisition, filename, digest in (
                ("synthesis", "synth", "synth-exe", "synth-acq", "synth.gguf", "a" * 64),
                ("recognition", "recognizer", "recognizer-exe", "recognizer-acq", "recognizer.bin", "b" * 64)):
            path = self.root / filename
            path.write_bytes(b"fixture speech weights")
            self.connection.execute(
                "INSERT INTO acquisition_records (acquisition_id,name,source,location_id,created_at)"
                " VALUES (?, ?, 'adoption', 'files', ?)", (acquisition, filename, NOW))
            self.connection.execute(
                "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256)"
                " VALUES (?, ?, ?, ?)", (acquisition, filename, path.stat().st_size, digest))
            executable_path = self.root / f"{executable}.sh"
            executable_path.write_text("#!/bin/sh\n")
            self.connection.execute(
                "INSERT INTO speech_executables (executable_id,kind,location_id,path,sha256,installed_at)"
                " VALUES (?, ?, 'files', ?, ?, ?)",
                (executable, role, str(executable_path), digest, NOW))
            self.connection.execute(
                "INSERT INTO model_profiles (profile_id,kind,name,card_id,speech_executable_id,created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (profile, role, profile, "card" if role == "synthesis" else None, executable, NOW))
            self.connection.execute("INSERT INTO profile_artifacts VALUES (?, 'weights', ?, ?)",
                                    (profile, acquisition, filename))
        self.connection.execute(
            "INSERT INTO bundles (bundle_id,name,load_tokens,load_prompt,created_at)"
            " VALUES ('bundle','fixture voice',128,'hello',?)", (NOW,))
        self.connection.executemany(
            "INSERT INTO bundle_members VALUES ('bundle', ?, ?)",
            [("language", "lang"), ("synthesis", "synth"), ("recognition", "recognizer")])
        self.connection.execute("UPDATE model_profiles SET context_size=262144 WHERE profile_id='lang'")
        self.connection.commit()
        (self.fixture_agents / "matuto" / "procedures" / "bundle.py").write_text('''
import json, os
from pathlib import Path
from matuto import store

def run(opened, record):
    root = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    language = store.read_record(opened, "language", record["members"]["language"])
    fail = language["context_size"] == 262144
    checks = [{"name": name, "status": "FAIL" if fail and name == "fit" else "PASS",
               "detail": "fixture"} for name in ("fit", "real-time", "recognition")]
    (root / "record.json").write_text(json.dumps({"fingerprint": store.fingerprint(opened, record)}))
    (root / "checks.json").write_text(json.dumps(checks))
    (root / "summary.txt").write_text("fixture bundle result\\n")
    return 0
''')
        bundle = store.read_record(self.opened, "bundle", "bundle")
        fingerprint = store.fingerprint(self.opened, bundle)
        target = runner.Target("bundle", "bundle", bundle["name"], fingerprint)
        procedure = discovery.load_item("procedures", "context_search", root=self.fixture_agents / "matuto")
        pinned = procedure.prepare(self.opened, target, 262144)
        command = [sys.executable, str(self.fixture_agents / "procedure_runner.py"),
                   "context_search", "bundle", "bundle", fingerprint,
                   json.dumps(pinned, separators=(",", ":"))]
        request = runner.Request.run("context_search", "search fixture bundle", command,
                                     target, procedure.REVISION)
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            started = self.runner(serving_state=lambda: admission.STOPPED,
                                  card_holder=lambda: admission.Card(admission.CARD_FREE)).start(request)
        self.assertEqual(started.decision, admission.ADMIT, started.reason)
        work = next(item for item in self.runner().works() if item.work_id == started.work.work_id)
        self.assertEqual(work.outcome, runner.COMPLETED, work.log)
        summary = json.loads(next(item["body"] for item in
                                  store.read_run_evidence(self.opened, work.work_id)["artifacts"]
                                  if item["name"] == "context-search.json"))
        self.assertEqual((summary["winner"], summary["checked"]), (196608, True))
        self.assertEqual([row["context"] for row in summary["rungs"]], [262144, 196608, 131072])
        child_runs = self.connection.execute(
            "SELECT fingerprint, outcome FROM runs WHERE procedure='bundle'").fetchall()
        self.assertEqual(len(child_runs), 3)
        self.assertEqual({row["fingerprint"] for row in child_runs},
                         {row["fingerprint"] for row in summary["rungs"]})
        self.assertEqual(store.read_record(self.opened, "language", "lang")["context_size"], 262144)

        (self.fixture_agents / "matuto" / "procedures" / "bundle.py").write_text('''
import json, os, signal
from pathlib import Path

def run(opened, record):
    root = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    (root / "checks.json").write_text(json.dumps([
        {"name": name, "status": status, "detail": "interrupted fixture"}
        for name, status in (("fit", "PASS"), ("real-time", "SKIP"),
                             ("recognition", "SKIP"))]))
    (root / "summary.txt").write_text("interrupted bundle rung\\n")
    (root / "measurement.txt").write_text("measurement evidence")
    os.kill(os.getpid(), signal.SIGKILL)
''')
        with mock.patch.dict(os.environ, {"MATUTO_STORE_CONFIG_ROOT": str(self.config)}):
            interrupted = self.runner(serving_state=lambda: admission.STOPPED,
                                      card_holder=lambda: admission.Card(admission.CARD_FREE))
            started = interrupted.start(request)
            parent = next(work for work in interrupted.works() if work.work_id == started.work.work_id)
        self.assertEqual(parent.outcome, "errored", parent.log)
        child = self.connection.execute(
            "SELECT run_id, outcome FROM runs WHERE procedure='bundle' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(child["outcome"], runner.INTERRUPTED)
        evidence = store.read_run_evidence(self.opened, child["run_id"])
        self.assertEqual([(item["name"], item["status"]) for item in evidence["checks"]],
                         [("fit", "PASS"), ("real-time", "SKIP"), ("recognition", "SKIP")])
        self.assertIn("interrupted bundle rung", evidence["summary"])
        self.assertIn("measurement.txt", [item["name"] for item in evidence["artifacts"]])

    def test_three_procedures_run_store_backed_evidence_via_runner(self):
        before = store.fingerprint(self.opened, store.read_record(self.opened, "language", "lang"))

        model = self.root / "model.gguf"
        original = model.read_bytes()
        model.write_bytes(original + b"altered")
        try:
            _, refused, _ = self.start_procedure("verify")
        finally:
            model.write_bytes(original)
        self.assertEqual(refused.outcome, "errored")
        self.assertIn("does not match the SHA-256 recorded", refused.log)
        self.assertFalse((self.root / "fixture" / "received.json").exists())

        (self.root / "fixture" / "record-device-args").touch()
        _, verify, verify_evidence = self.start_procedure("verify", serving=admission.AGENTIC)
        self.assertEqual(verify.outcome, runner.COMPLETED, verify.log)
        self.assertEqual(verify_evidence["checks"][0]["status"], "PASS")
        launched = json.loads((self.root / "fixture" / "received.json").read_text())
        self.assertIn("--list-devices", launched["args"])
        self.assertNotIn("-m", launched["args"])
        self.connection.executemany(
            "INSERT INTO profile_arguments VALUES ('lang','smoke',?,NULL,?,NULL)",
            [(1, "--simple-io"), (2, "--single-turn")])
        self.connection.commit()

        _, smoke, smoke_evidence = self.start_procedure("smoke")
        self.assertEqual(smoke.outcome, runner.COMPLETED)
        self.assertEqual(smoke_evidence["checks"][0]["status"], "PASS",
                         (smoke_evidence["checks"], smoke_evidence["artifacts"]))
        artifact_names = {item["name"] for item in smoke_evidence["artifacts"]}
        self.assertIn("smoke-output.txt", artifact_names)
        self.assertIn("smoke-conversation.txt", artifact_names)
        self.assertIn("smoke-stderr.txt", artifact_names)
        launched = json.loads((self.root / "fixture" / "received.json").read_text())
        self.assertEqual(launched["args"].count("--simple-io"), 1)
        self.assertEqual(launched["args"].count("--single-turn"), 1)

        self.connection.execute(
            "UPDATE profile_arguments SET value='--single-turn' WHERE profile_id='lang' AND tool='smoke' AND position=0")
        self.connection.execute(
            "DELETE FROM profile_arguments WHERE profile_id='lang' AND tool='smoke' AND position=2")
        self.connection.commit()
        _, operand_smoke, operand_evidence = self.start_procedure("smoke")
        self.assertEqual(operand_smoke.outcome, runner.COMPLETED)
        self.assertEqual(operand_evidence["checks"][0]["status"], "PASS")
        launched = json.loads((self.root / "fixture" / "received.json").read_text())
        self.assertEqual(launched["args"][:2], ["--custom", "--single-turn"])
        self.assertEqual(launched["args"].count("--single-turn"), 2)
        self.connection.execute(
            "UPDATE profile_arguments SET value='one value' WHERE profile_id='lang' AND tool='smoke' AND position=0")
        self.connection.execute(
            "INSERT INTO profile_arguments VALUES ('lang','smoke',2,NULL,'--single-turn',NULL)")
        self.connection.commit()

        stderr_artifact = next(item for item in smoke_evidence["artifacts"]
                               if item["name"] == "smoke-stderr.txt")
        self.assertIn(b"matuto-runtime observation", stderr_artifact["body"])
        extra = self.root / "fixture" / "smoke-extra"
        extra.touch()
        _, additional, additional_evidence = self.start_procedure("smoke")
        extra.unlink()
        self.assertEqual(additional.outcome, runner.COMPLETED)
        self.assertEqual(additional_evidence["checks"][0]["status"], "FAIL")
        empty_reply = self.root / "fixture" / "smoke-empty-reply"
        empty_reply.touch()
        _, empty, empty_evidence = self.start_procedure("smoke")
        empty_reply.unlink()
        self.assertEqual(empty.outcome, runner.COMPLETED)
        self.assertEqual(empty_evidence["checks"][0]["status"], "ERROR")
        overflow = self.root / "fixture" / "smoke-overflow"
        overflow.touch()
        _, oversized, oversized_evidence = self.start_procedure("smoke")
        overflow.unlink()
        self.assertEqual(oversized.outcome, runner.COMPLETED)
        self.assertEqual(oversized_evidence["checks"][0]["status"], "ERROR")
        wrong = self.root / "fixture" / "smoke-wrong"
        wrong.touch()
        _, mismatch, mismatch_evidence = self.start_procedure("smoke")
        wrong.unlink()
        self.assertEqual(mismatch.outcome, runner.COMPLETED)
        self.assertEqual(mismatch_evidence["checks"][0]["status"], "FAIL")
        no_reply = self.root / "fixture" / "smoke-no-reply"
        no_reply.touch()
        _, absent, absent_evidence = self.start_procedure("smoke")
        no_reply.unlink()
        self.assertEqual(absent.outcome, runner.COMPLETED)
        self.assertEqual(absent_evidence["checks"][0]["status"], "ERROR")
        failed = self.root / "fixture" / "smoke-fails"
        failed.touch()
        _, nonzero, nonzero_evidence = self.start_procedure("smoke")
        failed.unlink()
        self.assertEqual(nonzero.outcome, runner.COMPLETED)
        self.assertEqual(nonzero_evidence["checks"][0]["status"], "ERROR")
        self.assertIn("matuto-runtime observation", nonzero.log)

        self.connection.execute(
            "UPDATE profile_arguments SET flag='--unadvertised' WHERE profile_id='lang' AND tool='smoke' AND position=0")
        self.connection.commit()
        _, refused_smoke, refused_smoke_evidence = self.start_procedure("smoke")
        self.assertEqual(refused_smoke.outcome, "errored")
        self.assertEqual(refused_smoke_evidence["checks"][0]["status"], "SKIP")
        self.connection.execute(
            "UPDATE profile_arguments SET flag='--custom' WHERE profile_id='lang' AND tool='smoke' AND position=0")
        self.connection.execute(
            "UPDATE profile_arguments SET flag='--unadvertised' WHERE profile_id='lang' AND tool='server'")
        self.connection.commit()
        _, refused_verify, refused_verify_evidence = self.start_procedure("verify")
        self.assertEqual(refused_verify.outcome, "errored")
        self.assertEqual(refused_verify_evidence["checks"][0]["status"], "SKIP")
        self.connection.execute(
            "UPDATE profile_arguments SET flag='--parallel' WHERE profile_id='lang' AND tool='server'")
        self.connection.commit()

        _, bench, bench_evidence = self.start_procedure("bench")
        self.assertEqual(bench.outcome, runner.COMPLETED, bench.log)
        self.assertEqual(bench_evidence["checks"], [])
        summary_artifact = next(item for item in bench_evidence["artifacts"]
                                if item["name"] == "bench/summary.json")
        self.assertEqual(json.loads(summary_artifact["body"])["outcome"], "completed")
        self.assertEqual(store.fingerprint(self.opened,
                         store.read_record(self.opened, "language", "lang")), before)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM acceptances").fetchone()[0], 0)
