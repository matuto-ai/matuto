"""Store launch specs are complete, current and fail closed."""
import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock, patch

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(AGENTS / "tests"))
sys.path.insert(0, str(AGENTS / "tests" / "app"))

from matuto import discovery, recipes, store, store_lab  # noqa: E402
from matuto.procedures import wq as wq_procedure  # noqa: E402
from matuto.routes import runs as runs_route  # noqa: E402
import runtime_fixture  # noqa: E402

NOW = "2026-10-14T00:00:00+00:00"
PCI = "0000:03:00.0"


class LaunchSpecTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="store-launch-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.opened = store.open_store(self.home / "config")
        self.addCleanup(self.opened.close)
        self.connection = self.opened.connection()
        self.build = self.home / "build"
        (self.build / "bin").mkdir(parents=True)
        for name in ("llama-server", "llama-cli", "llama-bench"):
            executable = self.build / "bin" / name
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        self.connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,"
            "notes,created_at) VALUES ('card','GPU',?,32000000000,42,16,512,128,3,'',?)",
            (PCI, NOW))
        self.connection.execute(
            "INSERT INTO builds (build_id,label,kind,location,backend,source_revision,target,"
            "recipe,toolchain,state,notes) VALUES ('llama','fixture','llama-hip',?,'hip',"
            "'revision','gfx1201',?,'fixture','registered','')",
            (str(self.build), recipes.LLAMA_HIP))
        self.connection.execute(
            "INSERT INTO runtime_selection VALUES (1,'llama','card','hip',?)", (NOW,))
        self.connection.execute("INSERT INTO lab_port VALUES (1,18890,?)", (NOW,))
        self.connection.execute(
            "INSERT INTO artifact_locations VALUES ('files',?,'',?)", (str(self.home), NOW))
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,checksum_status,"
            "location_id,verified_at,created_at) VALUES ('acq','fixture','adoption',"
            "'locally-hashed','files',?,?)", (NOW, NOW))
        for name, body in (("model.gguf", b"model"), ("speech.bin", b"weights")):
            path = self.home / name
            path.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            self.connection.execute(
                "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,"
                "computed_sha256,verified_at) VALUES ('acq',?,?,?,?)",
                (name, len(body), digest, NOW))
        for role in ("synthesis", "recognition"):
            executable = self.home / f"{role}-fixture"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
            backend, recipe = (("vulkan", recipes.BREEZE) if role == "synthesis"
                               else ("cpu", recipes.WHISPER))
            self.connection.execute(
                "INSERT INTO speech_executables VALUES (?,?,?,?,?,?,?,?)",
                (f"exe-{role}", role, "files", str(executable),
                 hashlib.sha256(executable.read_bytes()).hexdigest(), "fixture",
                 json.dumps({"backend": backend, "recipe": recipe}), NOW))
        for identifier, kind, exec_id, card in (
                ("lang", "language", None, "card"),
                ("synth", "synthesis", "exe-synthesis", "card"),
                ("recognize", "recognition", "exe-recognition", None)):
            self.connection.execute(
                "INSERT INTO model_profiles (profile_id,kind,name,alias,context_size,thinking,"
                "card_id,speech_executable_id,published,notes,created_at)"
                " VALUES (?,?,?, ?,4096,'high',?,?,0,'',?)",
                (identifier, kind, identifier, "fixture" if kind == "language" else "",
                 card, exec_id, NOW))
            self.connection.execute(
                "INSERT INTO profile_artifacts VALUES (?,?, 'acq','model.gguf')" if kind == "language"
                else "INSERT INTO profile_artifacts VALUES (?,?, 'acq','speech.bin')",
                (identifier, "model" if kind == "language" else "weights"))
        self.connection.execute(
            "INSERT INTO profile_arguments VALUES ('lang','server',0,NULL,'--parallel','1')")
        self.connection.execute(
            "INSERT INTO profile_arguments VALUES ('lang','server',1,NULL,'--gpu-layers=42',NULL)")
        self.connection.execute(
            "INSERT INTO profile_arguments VALUES ('lang','smoke',0,NULL,'--custom',?)",
            ('spaces \' " ; $(false)',))
        self.connection.execute(
            "INSERT INTO bundles (bundle_id,name,load_tokens,load_prompt,created_at)"
            " VALUES ('bundle','voice',128,'prompt',?)", (NOW,))
        for role, profile in (("language", "lang"), ("synthesis", "synth"), ("recognition", "recognize")):
            self.connection.execute("INSERT INTO bundle_members VALUES ('bundle',?,?)", (role, profile))

    def spec(self, kind, identifier):
        return store.launch_spec(self.opened, kind, identifier, detected_gpus=[PCI])

    def test_each_record_kind_emits_a_spec_with_recorded_paths_and_identity(self):
        language = self.spec("language", "lang")["record"]
        synthesis = self.spec("synthesis", "synth")["record"]
        recognition = self.spec("recognition", "recognize")["record"]
        bundle = self.spec("bundle", "bundle")["record"]

        self.assertEqual(language["fingerprint"], store.fingerprint(self.opened, store.read_record(self.opened, "language", "lang")))
        self.assertEqual(language["files"]["model"]["path"], str((self.home / "model.gguf").resolve()))
        self.assertEqual(language["files"]["model"]["acquisition_files"][0]["sha256"],
                         hashlib.sha256(b"model").hexdigest())
        self.assertEqual(language["arguments"]["smoke"][-1], 'spaces \' " ; $(false)')
        self.assertEqual(language["arguments"]["server"], ["--parallel", "1", "--gpu-layers=42"])
        self.assertEqual(store_lab._server_arguments(language)[:3],
                         ["--parallel", "1", "--gpu-layers=42"])
        without_override = dict(language, argument_rows=[
            row for row in language["argument_rows"] if row["option"] != "--gpu-layers=42"])
        self.assertEqual(store_lab._server_arguments(without_override)[:4],
                         ["--parallel", "1", "-ngl", "42"])
        self.assertEqual(language["gpu_layers"], 42)
        self.assertEqual(synthesis["executable"]["backend"], "Vulkan")
        self.assertEqual(recognition["executable"]["backend"], "CPU")
        self.assertNotEqual(bundle["members"]["synthesis"]["port"], bundle["members"]["recognition"]["port"])
        self.assertNotIn(18890, (bundle["members"]["synthesis"]["port"], bundle["members"]["recognition"]["port"]))
        self.assertEqual(bundle["members"]["language"]["server_port"], 18890)

    def test_matuto_lab_store_dispatch_never_sources_profiles_or_defaults(self):
        with tempfile.TemporaryDirectory(prefix="store-lab-dispatch-") as temp:
            root = Path(temp)
            config = root / "config/ai-agents-sandbox/matuto-lab"
            (config / "profiles").mkdir(parents=True)
            marker = root / "sourced"
            payload = f"touch {marker}\n"
            (config / "defaults.sh").write_text(payload)
            (config / "profiles/lang.sh").write_text(payload)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture = root / "python-args"
            python = fake_bin / "python3"
            python.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$CAPTURE\"\n")
            python.chmod(0o755)
            environment = dict(os.environ, HOME=temp, XDG_CONFIG_HOME=str(root / "config"),
                               XDG_STATE_HOME=str(root / "state"), PATH=f"{fake_bin}:/usr/bin:/bin",
                               CAPTURE=str(capture), MATUTO_LAB_STORE_BACKED="1")
            subprocess.run(["bash", str(AGENTS / "matuto-lab"), "lang", "serve"],
                           env=environment, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            self.assertFalse(marker.exists())
            self.assertIn("matuto/store_lab.py run language lang serve", capture.read_text())

    def test_wq_pins_the_launch_spec_and_card_limit_at_start(self):
        with patch("matuto_runtime.pci_devices", return_value={"state": "completed", "devices": [PCI]}):
            pinned = wq_procedure.prepare(self.opened, SimpleNamespace(id="lang"))
        self.assertEqual(pinned["record"]["context_size"], 4096)
        self.assertEqual(pinned["record"]["vram_ceiling_bytes"], 32_000_000_000)
        self.assertEqual(pinned["wq_limits"]["vram_mb"], 32000)
        self.connection.execute("UPDATE cards SET vram_ceiling_bytes = 16000000000")
        self.assertEqual(pinned["record"]["vram_ceiling_bytes"], 32_000_000_000)
        with patch("matuto_runtime.pci_devices", return_value={"state": "completed", "devices": [PCI]}):
            changed = wq_procedure.prepare(self.opened, SimpleNamespace(id="lang"))
            self.assertEqual(changed["wq_limits"]["vram_mb"], 16000)
            overridden = wq_procedure.prepare(self.opened, SimpleNamespace(id="lang"),
                                              {"vram_mb": 12000, "retrieval_samples": 4})
        self.assertEqual((overridden["wq_limits"]["vram_mb"],
                          overridden["wq_limits"]["retrieval_samples"]), (12000, 4))
        with patch("matuto_runtime.pci_devices", return_value={"state": "completed", "devices": [PCI]}):
            with self.assertRaisesRegex(store.StoreError, "whole number") as refusal:
                wq_procedure.prepare(self.opened, SimpleNamespace(id="lang"), {"retrieval_samples": 1.5})
        self.assertEqual(refusal.exception.field, "retrieval_samples")
        with patch("matuto_runtime.pci_devices", return_value={"state": "completed", "devices": [PCI]}), \
             self.assertRaisesRegex(store.StoreError, "supported limits"):
            wq_procedure.prepare(self.opened, SimpleNamespace(id="lang"), [])

    def test_runs_list_exposes_wq_defaults_from_each_language_profiles_card(self):
        self.connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,"
            "notes,created_at) VALUES ('card-2','Second GPU','0000:04:00.0',16000000000,42,16,512,128,3,'',?)",
            (NOW,))
        self.connection.execute(
            "INSERT INTO model_profiles (profile_id,kind,name,alias,context_size,thinking,"
            "card_id,published,notes,created_at) VALUES ('lang-2','language','Second profile',"
            "'second',4096,'high','card-2',0,'',?)", (NOW,))

        class EmptyRunner:
            store = self.opened

            def works(_self):
                return []

        data = runs_route._serialize(self.opened, EmptyRunner())
        records = {item["id"]: item for item in data["records"]}
        self.assertEqual(records["lang"]["wq_limits"]["vram_mb"], 32000)
        self.assertEqual(records["lang-2"]["wq_limits"]["vram_mb"], 16000)
        self.assertEqual(records["lang"]["wq_limits"]["retrieval_samples"], 3)

    def test_stored_wq_record_reaches_the_real_runtime_with_private_selection_and_sidecars(self):
        for role, filename, contents in (("mmproj", "projector.gguf", b"projector"),
                                         ("mtp", "draft.gguf", b"draft")):
            path = self.home / filename
            path.write_bytes(contents)
            digest = hashlib.sha256(contents).hexdigest()
            self.connection.execute(
                "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
                " VALUES ('acq',?,?,?,?)", (filename, len(contents), digest, NOW))
            self.connection.execute("INSERT INTO profile_artifacts VALUES ('lang',?,'acq',?)",
                                    (role, filename))
        spec = self.spec("language", "lang")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        spec["lab_port"] = spec["record"]["server_port"] = port
        root = Path(tempfile.mkdtemp(prefix="stored-wq-runtime-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        agents, build = runtime_fixture.create(root / "runtime")
        shutil.copy2(AGENTS.parent / "wq.py", root / "runtime/wq.py")
        spec["runtime"]["location"] = str(build.parent)
        private_home = root / "private-home"
        private_home.mkdir(mode=0o700)
        env = dict(os.environ, HOME=str(private_home), XDG_CONFIG_HOME=str(self.home / "config"),
                   MATUTO_WQ_LAUNCH_SPEC=json.dumps(spec), PYTHONPATH=str(agents))
        # The run takes only the store's launch spec: it freezes a private copy,
        # installs its own runtime selection and launches the record's argv. No
        # profile file is written, read or sourced.
        source = ("import json, os, subprocess, sys; from pathlib import Path; "
                  f"sys.path.insert(0, {str(agents)!r}); "
                  f"sys.path.insert(0, {str(root / 'runtime')!r}); import wq; "
                  "spec=json.loads(os.environ['MATUTO_WQ_LAUNCH_SPEC']); "
                  "profile=wq.apply_launch_spec(spec); "
                  f"server=wq.Server(Path({str(root / 'run')!r}), profile, spec['lab_port'], {{}}, None); "
                  "server.verify_files(); "
                  "print(server.target); "
                  "raise SystemExit(subprocess.run(server.runtime_command('serve', 'llama-server',"
                  " server.expected['GPU_LAYERS'], server.server_arguments(False)), env=server.env).returncode)")
        generated = subprocess.run([sys.executable, "-B", "-c", source], env=env,
                                   capture_output=True, text=True, check=False)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        profile = Path(generated.stdout.splitlines()[0])
        self.assertTrue(profile.is_file())
        self.assertEqual(json.loads(profile.read_text()), spec)
        self.assertFalse(list(profile.parent.glob("*.sh")))
        runtime_selection = private_home / ".config/ai-agents-sandbox/matuto-runtime/selection.json"
        self.assertTrue(runtime_selection.is_file())
        self.assertFalse((self.home / ".config/ai-agents-sandbox/matuto-runtime/selection.json").exists())
        received = json.loads((root / "runtime/received.json").read_text())
        args = received["args"]
        self.assertEqual(args[args.index("-m") + 1], spec["record"]["files"]["model"]["path"])
        self.assertEqual(args[args.index("-c") + 1], "4096")
        self.assertEqual(args[args.index("--port") + 1], str(port))
        self.assertEqual(args[args.index("--mmproj") + 1], spec["record"]["files"]["mmproj"]["path"])
        self.assertEqual(args[args.index("--spec-draft-model") + 1],
                         spec["record"]["files"]["mtp"]["path"])
        # The verified digests, not only the paths, reach the runtime the run
        # launched: the retained identity states which files were served.
        identity = json.loads((root / "run/runtime.json").read_text())
        self.assertEqual(identity["request"]["model_sha256"],
                         spec["record"]["files"]["model"]["sha256"])
        self.assertEqual(identity["request"]["mmproj_sha256"],
                         spec["record"]["files"]["mmproj"]["sha256"])
        self.assertEqual(identity["request"]["draft_sha256"],
                         spec["record"]["files"]["mtp"]["sha256"])

    def test_store_profile_wq_submits_through_the_runner_cli(self):
        with tempfile.TemporaryDirectory(prefix="store-wq-submit-") as temp:
            root = Path(temp)
            binary = root / "bin"
            binary.mkdir()
            capture = root / "args"
            command = binary / "matuto-manager"
            command.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$CAPTURE\"\n")
            command.chmod(0o755)
            environment = dict(os.environ, PATH=f"{binary}:/usr/bin:/bin", CAPTURE=str(capture))
            result = subprocess.run(["bash", str(AGENTS / "matuto-lab"), "stored-profile", "wq"],
                                    env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text().strip(), "run wq stored-profile")

    def test_completed_wq_check_failure_is_recorded_as_a_check_not_run_error(self):
        spec = {"record": {"kind": "language", "id": "lang"}, "runtime": {"fixture": True},
                "lab_port": 18890}
        with tempfile.TemporaryDirectory(prefix="store-wq-procedure-") as temp, \
             tempfile.TemporaryDirectory(prefix="store-wq-home-") as private_home:
            evidence = Path(temp)
            record = {"kind": "language", "profile_id": "lang"}
            environment = dict(os.environ, HOME=private_home)
            home = SimpleNamespace(cleanup=Mock())

            def execute(command, *, env, check):
                self.assertEqual(json.loads(env["MATUTO_WQ_LAUNCH_SPEC"]), spec)
                self.assertEqual(env["HOME"], private_home)
                self.assertEqual(command[command.index("--port") + 1], "18890")
                report = Path(command[command.index("--run-dir") + 1])
                report.mkdir()
                (report / "report.json").write_text(json.dumps({
                    "interrupted": False,
                    "results": [{"id": "WQ-01", "status": "FAIL", "reason": "fixture"}],
                    "counts": {"PASS": 0, "FAIL": 1, "ERROR": 0, "SKIP": 0}}))
                return SimpleNamespace(returncode=1)

            with patch.dict(os.environ, {"MATUTO_RUN_EVIDENCE_DIR": str(evidence)}), \
                 patch.object(wq_procedure, "release_root", return_value=AGENTS.parent), \
                 patch.object(wq_procedure, "revision", return_value="fixture-release"), \
                 patch.object(wq_procedure, "setup", return_value=(spec, environment, home)) as prepare_run, \
                 patch.object(wq_procedure.subprocess, "run", side_effect=execute):
                status = wq_procedure.run(self.opened, record, spec)

            prepare_run.assert_called_once_with(self.opened, record, spec)
            self.assertEqual(status, 0)
            home.cleanup.assert_called_once_with()
            self.assertEqual(json.loads((evidence / "checks.json").read_text())[0]["status"], "FAIL")
            self.assertIn("FAIL: 1", (evidence / "summary.txt").read_text())

    def test_changed_recorded_model_and_sidecar_bytes_refuse_without_a_spec(self):
        model = self.home / "model.gguf"
        model.write_bytes(model.read_bytes() + b"changed")
        with self.assertRaisesRegex(store.StoreError, "does not match the SHA-256 recorded"):
            self.spec("language", "lang")
        model.write_bytes(b"model")
        sidecar = self.home / "projector.gguf"
        sidecar.write_bytes(b"projector")
        self.connection.execute(
            "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
            " VALUES ('acq','projector.gguf',9,?,?)", (hashlib.sha256(b"projector").hexdigest(), NOW))
        self.connection.execute(
            "INSERT INTO profile_artifacts VALUES ('lang','mmproj','acq','projector.gguf')")
        sidecar.write_bytes(b"altered")
        with self.assertRaisesRegex(store.StoreError, "mmproj file 'projector.gguf'.*does not match"):
            self.spec("language", "lang")

    def test_unfingerprintable_record_refuses_without_a_spec(self):
        self.connection.execute(
            "UPDATE acquisition_files SET computed_sha256 = '' WHERE acquisition_id = 'acq' AND file_name = 'model.gguf'")
        with self.assertRaisesRegex(store.StoreError, "holds no computed SHA-256"):
            self.spec("language", "lang")

    def test_missing_selection_unknown_and_unavailable_records_refuse_without_a_spec(self):
        with self.assertRaisesRegex(store.StoreError, "card .* is not detected"):
            # The language profile has the selected card; removing detection is refusal.
            store.launch_spec(self.opened, "language", "lang", detected_gpus=[])
        with self.assertRaisesRegex(store.StoreError, "no model profile"):
            self.spec("language", "unknown")
        self.connection.execute("DELETE FROM runtime_selection")
        with self.assertRaisesRegex(store.StoreError, "No runtime selection"):
            self.spec("language", "lang")

    def test_store_backed_procedures_reach_fixture_runtimes_with_verbatim_argv(self):
        fixture_root = (self.home / "runtime-fixture").resolve()

        def assert_fixture_processes_stopped():
            for process in Path("/proc").iterdir():
                if not process.name.isdigit():
                    continue
                try:
                    executable = Path(os.readlink(process / "exe").removesuffix(" (deleted)"))
                except OSError:
                    continue
                if executable.is_relative_to(fixture_root) and executable.name in {
                    "llama-server", "breeze-server", "whisper-server"
                }:
                    self.fail(f"live fixture executable {executable} (PID {process.name})")
                try:
                    command = (process / "cmdline").read_bytes()
                except OSError:
                    continue
                self.assertFalse(str(fixture_root).encode() in command and b"matuto_runtime.py" in command,
                                 f"live fixture runtime (PID {process.name})")

        fixture_agents, fixture_build = runtime_fixture.create(fixture_root)
        inventory_path = fixture_root / "inventory.json"
        inventory = json.loads(inventory_path.read_text())
        second_pci = "0000:04:00.0"
        inventory["host"].append(dict(runtime_fixture.HOST, pciAddress=second_pci,
                                      subsystem_device="0x0e1c"))
        inventory["HIP/ROCm"].append({"pciAddress": second_pci, "uuid": "GPU-abcdef0123456789"})
        inventory_path.write_text(json.dumps(inventory))
        (fixture_root / "multiple-hip").touch()
        self.connection.execute("UPDATE builds SET location = ? WHERE build_id = 'llama'",
                                (str(fixture_build.parent),))
        for role, filename in (("synthesis", "breeze-server"), ("recognition", "whisper-server")):
            executable = fixture_build / filename
            self.connection.execute(
                "UPDATE speech_executables SET path = ?, sha256 = ? WHERE executable_id = ?",
                (str(executable), hashlib.sha256(executable.read_bytes()).hexdigest(), f"exe-{role}"))
        shutil.copytree(AGENTS / "matuto", fixture_agents / "matuto", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("matuto_runtime.py", "matuto_bench.py"))
        for marker in ("http", "bench-json"):
            (fixture_root / marker).touch()
        spec = store.launch_spec(self.opened, "language", "lang", detected_gpus=[PCI])
        self.assertEqual(spec["record"]["arguments"]["smoke"][-1], 'spaces \' " ; $(false)')

        old_paths = (store_lab.RUNTIME, store_lab.BENCH, store_lab.VOICE, store_lab.LAB)
        store_lab.RUNTIME = fixture_agents / "matuto" / "matuto_runtime.py"
        bench_wrapper = fixture_agents / "matuto_bench-test.py"
        bench_wrapper.write_text(
            f"import sys\nsys.path.insert(0, {str(fixture_agents)!r})\nimport matuto_runtime\n"
            f"import matuto_bench\nmatuto_bench.__file__ = {str(fixture_agents / 'matuto_bench.py')!r}\n"
            "matuto_bench.other_servers = lambda: []\n"
            "from matuto import store_bench\n"
            "raise SystemExit(store_bench.main(sys.argv[1:]))\n")
        store_lab.BENCH = bench_wrapper
        store_lab.VOICE = fixture_agents / "matuto" / "store_voice.py"
        store_lab.LAB = fixture_agents / "matuto-lab"
        self.addCleanup(setattr, store_lab, "RUNTIME", old_paths[0])
        self.addCleanup(setattr, store_lab, "BENCH", old_paths[1])
        self.addCleanup(setattr, store_lab, "VOICE", old_paths[2])
        self.addCleanup(setattr, store_lab, "LAB", old_paths[3])

        with tempfile.TemporaryDirectory(prefix="store-lab-runtime-") as home:
            env = store_lab._environment(home)
            env["MATUTO_STORE_CONFIG_ROOT"] = str(self.home / "config")
            store_lab._prepare_runtime(spec, env)
            self.assertEqual(store_lab._run_language(spec, "smoke", env), 0)
            received = json.loads((fixture_root / "received.json").read_text())
            self.assertEqual(received["hip"], runtime_fixture.HIP_UUID)
            self.assertEqual(received["args"].count('spaces \' " ; $(false)'), 1)
            self.assertEqual(received["args"][received["args"].index("--custom") + 1],
                             'spaces \' " ; $(false)')
            self.assertEqual(store_lab._run_language(spec, "verify", env), 0)
            (fixture_root / "received.json").unlink()
            unsupported = dict(spec)
            unsupported["record"] = dict(spec["record"])
            unsupported["record"]["argument_rows"] = [*spec["record"]["argument_rows"],
                {"tool": "smoke", "option": "--not-advertised", "value": None}]
            self.assertEqual(store_lab._run_language(unsupported, "smoke", env), 1)
            self.assertFalse((fixture_root / "received.json").exists())
            unsupported_server = dict(spec)
            unsupported_server["record"] = dict(spec["record"])
            unsupported_server["record"]["argument_rows"] = [*spec["record"]["argument_rows"],
                {"tool": "server", "option": "--not-advertised", "value": None}]
            diagnostic = io.StringIO()
            with contextlib.redirect_stderr(diagnostic):
                self.assertEqual(store_lab._run_language(unsupported_server, "bench", env), 1)
            self.assertIn("--not-advertised", diagnostic.getvalue())
            self.assertFalse((fixture_root / "received.json").exists())
            (fixture_root / "unreadable-help").touch()
            self.assertNotEqual(store_lab._run_language(spec, "smoke", env), 0)
            self.assertFalse((fixture_root / "received.json").exists())
            (fixture_root / "unreadable-help").unlink()
            env.update({"MODEL_MMPROJ": "/unrecorded/projector.gguf", "MODEL_MTP": "/unrecorded/draft.gguf",
                        "MATUTO_RUNTIME_MMPROJ_SHA256": "unrecorded", "MATUTO_RUNTIME_MTP_SHA256": "unrecorded"})
            bench_result = store_lab._run_language(spec, "bench", env)
            unconfigured_bench_args = json.loads((fixture_root / "received.json").read_text())["args"]
            self.assertNotIn("--mmproj", unconfigured_bench_args)
            self.assertNotIn("--spec-draft-model", unconfigured_bench_args)
            results = Path(env["XDG_STATE_HOME"]) / "ai-agents-sandbox/matuto-lab/results/lang"
            latest_result = max(results.iterdir(), key=lambda path: path.name)
            readiness_args = shlex.split((latest_result / "readiness-command.txt").read_text())
            self.assertEqual(readiness_args.count("--gpu-layers=42"), 1)
            self.assertNotIn("-ngl", readiness_args)
            diagnostics = {str(path): path.read_text(errors="replace") for path in results.rglob("*.log")}
            self.assertEqual(bench_result, 0, json.dumps({"received": (fixture_root / "received.json").read_text()
                             if (fixture_root / "received.json").exists() else "no fixture argv",
                             "logs": diagnostics}))
            custom_value_spec = dict(spec, record=dict(spec["record"]))
            custom_value_spec["record"]["argument_rows"] = [
                row for row in spec["record"]["argument_rows"]
                if not (row["tool"] == "server" and row["option"] == "--gpu-layers=42")]
            custom_value_spec["record"]["argument_rows"].append(
                {"tool": "server", "option": "--custom", "value": "--gpu-layers"})
            self.assertEqual(store_lab._run_language(custom_value_spec, "bench", env), 0)
            results = Path(env["XDG_STATE_HOME"]) / "ai-agents-sandbox/matuto-lab/results/lang"
            latest_result = max(results.iterdir(), key=lambda path: path.name)
            readiness_args = shlex.split((latest_result / "readiness-command.txt").read_text())
            self.assertEqual(readiness_args[readiness_args.index("--custom") + 1], "--gpu-layers")
            self.assertEqual(readiness_args.count("-ngl"), 1)
            self.assertEqual(readiness_args[readiness_args.index("-ngl") + 1], "42")
            for role, filename, content in (("mmproj", "projector.gguf", b"projector"),
                                            ("mtp", "draft.gguf", b"draft")):
                path = self.home / filename
                path.write_bytes(content)
                self.connection.execute(
                    "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256,verified_at)"
                    " VALUES ('acq',?,?,?,?)",
                    (filename, len(content), hashlib.sha256(content).hexdigest(), NOW))
                self.connection.execute(
                    "INSERT INTO profile_artifacts VALUES ('lang',?, 'acq',?)", (role, filename))
            spec = self.spec("language", "lang")
            self.assertEqual(store_lab._run_language(spec, "bench", env), 0)
            benchmark_args = json.loads((fixture_root / "received.json").read_text())["args"]
            self.assertEqual(benchmark_args[benchmark_args.index("--mmproj") + 1],
                             str((self.home / "projector.gguf").resolve()))
            self.assertEqual(benchmark_args[benchmark_args.index("--spec-draft-model") + 1],
                             str((self.home / "draft.gguf").resolve()))

            (fixture_root / "received.json").unlink(missing_ok=True)
            code = (
                "import json, os, sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); from matuto import store_lab as lab; "
                "lab.RUNTIME=Path(sys.argv[1])/'matuto'/'matuto_runtime.py'; "
                "spec=json.loads(os.environ['STORE_LAUNCH_SPEC']); "
                "lab._prepare_runtime(spec, os.environ); "
                "sys.exit(lab._run_language(spec, 'serve', os.environ))"
            )
            serving = subprocess.Popen([sys.executable, "-B", "-c", code, str(fixture_agents)],
                                       env=dict(env, STORE_LAUNCH_SPEC=json.dumps(spec)),
                                       start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", spec["lab_port"]), timeout=.1):
                            break
                    except OSError:
                        if serving.poll() is not None:
                            self.fail("store-backed serve exited before its fixture listener started")
                        time.sleep(.02)
                else:
                    self.fail("store-backed serve did not start the fixture listener")
                served_args = json.loads((fixture_root / "received.json").read_text())["args"]
                self.assertEqual(served_args.count("--gpu-layers=42"), 1)
                self.assertNotIn("-ngl", served_args)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(serving.pid, 15)
                serving.wait(timeout=10)

            for kind, identifier in (("synthesis", "synth"), ("recognition", "recognize")):
                speech_spec = self.spec(kind, identifier)
                self.assertEqual(store_lab._run_speech(speech_spec, "qualify", env), 0)

            bundle = store.launch_spec(self.opened, "bundle", "bundle", detected_gpus=[PCI])
            self.assertEqual(bundle["record"]["members"]["synthesis"]["executable"]["backend"], "Vulkan")
            self.assertEqual(bundle["record"]["members"]["recognition"]["executable"]["backend"], "CPU")
            self.assertNotEqual(bundle["record"]["members"]["synthesis"]["port"], bundle["lab_port"])
            self.connection.execute("UPDATE model_profiles SET context_size = 8192 WHERE profile_id = 'lang'")
            env.pop("MATUTO_RUN_EVIDENCE_DIR", None)
            env.pop("MATUTO_RUN_RESULTS_DIR", None)
            bundle_result = store_lab._bundle_environment(bundle, env)
            bundle_root = Path(env["MATUTO_RUN_RESULTS_DIR"])
            bundle_logs = {str(path): path.read_text(errors="replace") for path in bundle_root.rglob("*.log")}
            self.assertEqual(bundle_result, 0, json.dumps(bundle_logs))
            assert_fixture_processes_stopped()
            summary = json.loads((bundle_root / "summary.json").read_text())
            recognition = summary["decision"]["recognition"]
            self.assertEqual(recognition["status"], "measured")
            self.assertTrue(recognition["passed"])
            self.assertGreater(recognition["word_count"], 0)
            self.assertTrue(bundle_root.is_relative_to(Path(env["XDG_STATE_HOME"])))
            model_identity = json.loads(next(bundle_root.rglob("runtime-identity-model.json")).read_text())
            self.assertEqual(model_identity["request"]["context"], "4096")

            for marker, check, expected in (("recognition-empty", "recognition", False),
                                           ("recognition-slow", "recognition", False),
                                           ("speech-slow", "real-time", False)):
                (fixture_root / "vram-used").write_text("20000000000")
                (fixture_root / marker).touch()
                self.assertNotEqual(store_lab._bundle_environment(bundle, env), 0, marker)
                assert_fixture_processes_stopped()
                result = json.loads((bundle_root / "summary.json").read_text())
                if check == "recognition":
                    self.assertEqual(result["decision"]["recognition"]["passed"], expected, marker)
                else:
                    self.assertLessEqual(result["decision"]["numbers"]["real_time_factor"], 1.0)
                (fixture_root / marker).unlink()

            (fixture_root / "vram-used").write_text("20000000000")
            (fixture_root / "vram-speech").write_text("33000000000")
            self.assertNotEqual(store_lab._bundle_environment(bundle, env), 0, "over-capacity occupancy")
            assert_fixture_processes_stopped()
            result = json.loads((bundle_root / "summary.json").read_text())
            self.assertEqual(result["decision"]["outcome"], "refused")
            self.assertGreater(result["combined_occupancy_bytes"], result["card"]["capacity_bytes"])
            (fixture_root / "vram-speech").unlink()
            self.connection.execute("UPDATE model_profiles SET context_size = 4096 WHERE profile_id = 'lang'")

            lab_config = Path(env["XDG_CONFIG_HOME"]) / "ai-agents-sandbox/matuto-lab"
            (lab_config / "profiles").mkdir(parents=True)
            marker = Path(home) / "profile-was-sourced"
            malicious = f"touch {marker}\n"
            (lab_config / "defaults.sh").write_text(malicious)
            (lab_config / "profiles/lang.sh").write_text(malicious)
            launched = subprocess.run(
                [str(fixture_agents / "matuto-lab"), "--store", "run", "bundle", "bundle", "bundle"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
            self.assertEqual(launched.returncode, 0, launched.stderr)
            assert_fixture_processes_stopped()
            self.assertFalse(marker.exists())

            from matuto import admission, runner
            from test_runner import FAKE_SCRIPT, FAKE_SYSTEMCTL, FAKE_SYSTEMD_RUN
            fake_bin = self.home / "runner-bin"
            fake_bin.mkdir()
            systemd = self.home / "runner-systemd"
            systemd.mkdir()
            for name, body in (("systemctl", FAKE_SYSTEMCTL), ("systemd-run", FAKE_SYSTEMD_RUN),
                               ("script", FAKE_SCRIPT)):
                path = fake_bin / name
                path.write_text(body)
                path.chmod(0o755)
            (systemd / "run-command").touch()
            key = store.store_directory(self.home / "config").parent / "pi-local/api-key"
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text("fixture-router-key")
            key.chmod(0o600)
            runner_env = dict(env, XDG_CONFIG_HOME=str(self.home / "config"),
                              MATUTO_STORE_CONFIG_ROOT=str(self.home / "config"),
                              RUNNER_STORE=str(store.store_path(self.home / "config")),
                              RUNNER_SYSTEMD=str(systemd),
                              PATH=f"{fake_bin}{os.pathsep}{env['PATH']}")
            fingerprint = store.fingerprint(self.opened, store.read_record(self.opened, "bundle", "bundle"))
            procedure = fixture_agents / "matuto" / "procedure_runner.py"
            refused = subprocess.run([sys.executable, str(procedure), "speech", "synthesis", "synth",
                                      store.fingerprint(self.opened, store.read_record(
                                          self.opened, "synthesis", "synth"))],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("unknown procedure 'speech'", refused.stderr)
            request = runner.Request.run(
                "bundle", "bundle for voice",
                (sys.executable, str(procedure), "bundle", "bundle", "bundle", fingerprint),
                runner.Target("bundle", "bundle", "voice", fingerprint), "bundle-v1")
            with mock.patch.dict(os.environ, runner_env):
                active = runner.Runner(self.home / "config",
                                       serving_state=lambda: admission.STOPPED,
                                       card_holder=lambda: admission.Card(admission.CARD_FREE))
                self.addCleanup(active.close)
                connection = active.store.connection()

                def run_bundle(outcome, expected_checks):
                    started = active.start(request)
                    self.assertEqual(started.decision, admission.ADMIT, started.reason)
                    work = next(item for item in active.works()
                                if item.work_id == started.work.work_id)
                    self.assertEqual(work.outcome, outcome)
                    checks = [tuple(row) for row in connection.execute(
                        "SELECT name, status FROM checks WHERE run_id = ? ORDER BY sequence",
                        (work.work_id,))]
                    if checks != expected_checks:
                        evidence = store.read_run_evidence(active.store, work.work_id)
                        artifacts = {item["name"]: item["body"].decode(errors="replace")
                                     for item in evidence["artifacts"]}
                        self.fail(f"checks {checks!r}; log {active.output(work)!r}; artifacts {artifacts!r}")
                    return work

                work = run_bundle("completed", [("fit", "PASS"), ("real-time", "PASS"),
                                                 ("recognition", "PASS")])
                names = {row[0] for row in connection.execute(
                    "SELECT name FROM run_evidence WHERE run_id = ?", (work.work_id,))}
                self.assertIn("bundle-measurement/summary.json", names)
                self.assertFalse((active._work_directory(work.work_id)).exists())
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM qualification_records WHERE record_kind = 'bundle'").fetchone()[0], 0)
                self.assertNotIn("speech", discovery.item_names("procedures"))
                report_path = active._progress_path(work.work_id)
                self.assertTrue(report_path.exists(), f"missing progress report {report_path}")
                self.assertEqual(active.progress(work).phase, "bundle checks",
                                 report_path.read_text() if report_path.exists() else "")

                for marker_name, checks in (
                        ("recognition-empty", [("fit", "PASS"), ("real-time", "PASS"),
                                               ("recognition", "FAIL")]),
                        ("recognition-slow", [("fit", "PASS"), ("real-time", "PASS"),
                                              ("recognition", "FAIL")]),
                        ("speech-slow", [("fit", "PASS"), ("real-time", "FAIL"),
                                         ("recognition", "PASS")]),
                        ("vram-speech", [("fit", "FAIL"), ("real-time", "PASS"),
                                         ("recognition", "PASS")]),
                        ("vram-missing", [("fit", "ERROR"), ("real-time", "SKIP"),
                                          ("recognition", "SKIP")])):
                    (fixture_root / "vram-used").write_text("20000000000")
                    marker_path = fixture_root / marker_name
                    marker_path.write_text("33000000000" if marker_name == "vram-speech" else "")
                    run_bundle("completed", checks)
                    marker_path.unlink()

                (fixture_root / "vram-used").write_text("20000000000")
                (fixture_root / "speech-lag").touch()
                (fixture_root / "generation-fast").touch()
                run_bundle("completed", [("fit", "PASS"), ("real-time", "ERROR"),
                                          ("recognition", "PASS")])
                (fixture_root / "speech-lag").unlink()
                (fixture_root / "generation-fast").unlink()

                (fixture_root / "vram-used").write_text("20000000000")
                (fixture_root / "speech-fail-start").touch()
                run_bundle("errored", [("fit", "SKIP"), ("real-time", "SKIP"),
                                        ("recognition", "SKIP")])
                (fixture_root / "speech-fail-start").unlink()
                assert_fixture_processes_stopped()

    def test_bundle_run_uses_the_admitted_store_snapshot(self):
        from matuto.procedures import bundle as bundle_procedure

        snapshot = store.snapshot(self.opened)
        self.addCleanup(snapshot.close)
        admitted = store.read_record(snapshot, "bundle", "bundle")
        evidence = self.home / "snapshot-evidence"
        evidence.mkdir(mode=0o700)
        self.connection.execute("UPDATE bundles SET load_tokens = 256 WHERE bundle_id = 'bundle'")

        def capture(spec, environment):
            self.assertEqual(spec["record"]["load_tokens"], 128)
            parts = [{"role": role, "profile": part["id"]}
                     for role, part in spec["record"]["members"].items()
                     if role in ("synthesis", "recognition")]
            summary = {
                "outcome": "qualified", "parts": parts,
                "performed": ["model_launch", "synthesis_launch", "recognition_launch"],
                "decision": {"numbers": {
                    "combined_occupancy_bytes": 1, "capacity_bytes": 2,
                    "audio_duration_nanoseconds": 2, "synthesis_wall_nanoseconds": 1},
                    "recognition": {"status": "measured", "text": "hello", "passed": True,
                                    "word_count": 1}},
                "generation": {"spanned_synthesis": True},
            }
            (Path(environment["MATUTO_RUN_RESULTS_DIR"]) / "summary.json").write_text(
                json.dumps(summary))
            return 0

        runner_env = {"MATUTO_RUN_EVIDENCE_DIR": str(evidence),
                      "MATUTO_STORE_CONFIG_ROOT": str(self.home / "config"),
                      "XDG_CONFIG_HOME": str(self.home / "config")}
        with (mock.patch.dict(os.environ, runner_env, clear=False),
              mock.patch.object(bundle_procedure.matuto_runtime, "pci_devices",
                                return_value={"state": "completed", "devices": [PCI]}),
              mock.patch.object(store_lab, "_prepare_runtime"),
              mock.patch.object(store_lab, "_bundle_environment", side_effect=capture)):
            self.assertEqual(bundle_procedure.run(snapshot, admitted), 0)

        self.assertEqual(self.connection.execute(
            "SELECT load_tokens FROM bundles WHERE bundle_id = 'bundle'").fetchone()[0], 256)
        checks = json.loads((evidence / "checks.json").read_text())
        self.assertEqual([(check["name"], check["status"]) for check in checks],
                         [("fit", "PASS"), ("real-time", "PASS"),
                          ("recognition", "PASS")])
        partial = {
            "parts": [{"role": "synthesis", "profile": "synth"},
                      {"role": "recognition", "profile": "recognize"}],
            "decision": {"numbers": {"combined_occupancy_bytes": 1,
                                       "capacity_bytes": 2,
                                       "audio_duration_nanoseconds": 2,
                                       "synthesis_wall_nanoseconds": 1},
                         "recognition": {"status": "measured", "passed": False}},
            "generation": {"spanned_synthesis": True},
        }
        checks, all_started = bundle_procedure._checks(
            partial, "recognition round trip", evidence / "bundle-measurement")
        self.assertFalse(all_started)
        self.assertEqual([check["status"] for check in checks], ["SKIP", "SKIP", "SKIP"])

    def test_all_gpu_default_reaches_smoke_and_serve_as_99(self):
        self.connection.execute("UPDATE cards SET default_gpu_layers = -1 WHERE card_id = 'card'")
        self.connection.execute(
            "DELETE FROM profile_arguments WHERE profile_id = 'lang' AND tool = 'server'"
            " AND flag = '--gpu-layers=42'")
        fixture_root = self.home / "runtime-all-fixture"
        fixture_agents, fixture_build = runtime_fixture.create(fixture_root)
        (fixture_root / "http").touch()
        self.connection.execute("UPDATE builds SET location = ? WHERE build_id = 'llama'",
                                (str(fixture_build.parent),))
        shutil.copytree(AGENTS / "matuto", fixture_agents / "matuto", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("matuto_runtime.py", "matuto_bench.py"))
        spec = self.spec("language", "lang")
        self.assertEqual(spec["record"]["gpu_layers"], -1)
        old_runtime, old_lab = store_lab.RUNTIME, store_lab.LAB
        store_lab.RUNTIME = fixture_agents / "matuto" / "matuto_runtime.py"
        store_lab.LAB = fixture_agents / "matuto-lab"
        self.addCleanup(setattr, store_lab, "RUNTIME", old_runtime)
        self.addCleanup(setattr, store_lab, "LAB", old_lab)

        with tempfile.TemporaryDirectory(prefix="store-lab-all-") as home:
            env = store_lab._environment(home)
            env["MATUTO_STORE_CONFIG_ROOT"] = str(self.home / "config")
            store_lab._prepare_runtime(spec, env)
            self.assertEqual(store_lab._run_language(spec, "smoke", env), 0)
            smoke_args = json.loads((fixture_root / "received.json").read_text())["args"]
            self.assertEqual(smoke_args[smoke_args.index("-ngl") + 1], "99")

            env["MATUTO_RUNTIME_IDENTITY_PATH"] = str(fixture_root / "server-identity.json")
            code = (
                "import json, os, sys; from pathlib import Path; "
                "sys.path.insert(0, sys.argv[1]); from matuto import store_lab as lab; "
                "lab.RUNTIME=Path(sys.argv[1])/'matuto'/'matuto_runtime.py'; "
                "spec=json.loads(os.environ['STORE_LAUNCH_SPEC']); "
                "sys.exit(lab._run_language(spec, 'serve', os.environ))"
            )
            serving = subprocess.Popen([sys.executable, "-B", "-c", code, str(fixture_agents)],
                                       env=dict(env, STORE_LAUNCH_SPEC=json.dumps(spec)),
                                       start_new_session=True, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", spec["lab_port"]), timeout=.1):
                            break
                    except OSError:
                        if serving.poll() is not None:
                            self.fail("store-backed serve exited before its fixture listener started")
                        time.sleep(.02)
                else:
                    self.fail("store-backed serve did not start the fixture listener")
                server_args = json.loads((fixture_root / "received.json").read_text())["args"]
                self.assertEqual(server_args[server_args.index("-ngl") + 1], "99")
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(serving.pid, 15)
                serving.wait(timeout=10)

    def test_missing_speech_backend_or_recipe_identity_refuses(self):
        self.connection.execute("UPDATE speech_executables SET notes='{}' WHERE executable_id='exe-synthesis'")
        with self.assertRaisesRegex(store.StoreError, "missing or inconsistent recorded backend/recipe identity"):
            self.spec("synthesis", "synth")


if __name__ == "__main__":
    unittest.main()
