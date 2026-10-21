"""Start, Stop serving and Restart, over the manager's HTTP interface.

Every transition is asserted from what the routes answer and from what systemd was
asked to do. The fixture is a temporary home and store with a runtime selection
over a real fixture llama-server, the generated preset and discovery directory,
the existing API key at the path CUT-4 keeps, a fake systemctl with per-unit state
that logs every call, and a fake router standing in for llama-server's own server.

The launcher is exercised as the three callers reach it: the routes, the argv
`systemctl --user start` runs, and the crash restart that runs the same argv. Its
own refusals, the lease, the launch check and the exact argv it would exec are
read from the command itself, so a refusal a terminal start would produce is the
same one the routes produce.
"""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))
if str(AGENTS / "tests") not in sys.path:
    sys.path.insert(0, str(AGENTS / "tests"))

import matuto_runtime  # noqa: E402
import runtime_fixture  # noqa: E402
from matuto import (admission, login, router, runner, serving, serving_actions, sessions,  # noqa: E402
                    serving_state, store, voice, web)

KEY = "t" * 40
PCI = "0000:03:00.0"
NOW = "2026-10-11T00:00:00+00:00"
ROUTER_UNIT = serving_state.ROUTER_UNIT
# The lifecycle calls that change what systemd runs, as opposed to reading it.
LIFECYCLE = ("start", "stop", "restart", "daemon-reload")
PATHS = {serving_actions.START_AGENTIC: "/api/serving/start",
         serving_actions.STOP_SERVING: "/api/serving/stop",
         serving_actions.RESTART: "/api/serving/restart"}
VOICE_PATHS = {serving.SERVE_VOICE: "/api/serving/voice",
               serving.STOP_SERVING: "/api/serving/voice/stop",
               serving.SWITCH_TO_AGENTIC: "/api/serving/voice/switch"}

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
"""A fake systemctl: per-unit state, every call logged, refusals on request."""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_SYSTEMCTL_STATE"])
raw = sys.argv[1:]
if raw[:1] == ["--user"]:
    raw = raw[1:]
with Path(os.environ["FAKE_SYSTEMCTL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(raw) + "\\n")
answer = json.loads(STATE.read_text(encoding="utf-8"))
units = answer["units"]
arguments = raw
action = arguments[0] if arguments else ""
unit = arguments[1] if len(arguments) > 1 else ""
if action == "is-active":
    sys.stdout.write(units.get(unit, {}).get("ActiveState", "inactive"))
elif action == "list-units":
    listed = answer.get("list", "")
    if listed == "exit-1":
        sys.exit(1)
    sys.stdout.write("".join(f"{name} loaded active running\\n" for name in listed.split()))
elif action == "show":
    if answer.get("show") == "silent":
        sys.stderr.write("Failed to connect to bus\\n")
        sys.exit(1)
    properties = [name.partition("=")[2] for name in arguments if name.startswith("--property=")]
    values = [units.get(unit, {}).get(name, "0") for name in properties]
    sys.stdout.write("".join(f"{name}={value}\\n" for name, value in zip(properties, values)))
elif action in ("start", "stop", "restart"):
    if unit in answer.get(f"fail-{action}", []):
        sys.stderr.write(f"Job for {unit} failed\\n")
        sys.exit(1)
    state = dict(units.get(unit, {}))
    if action in ("start", "restart"):
        state.update(ActiveState="active", MainPID=str(900000 + len(units)))
    else:
        state.update(ActiveState="inactive", MainPID="0")
    units[unit] = state
    STATE.write_text(json.dumps(answer), encoding="utf-8")
elif action == "daemon-reload":
    pass
else:
    sys.stderr.write(f"Unknown systemctl action {action}\\n")
    sys.exit(1)
'''


class RouterHandler(BaseHTTPRequestHandler):
    health = 200
    health_body = b'{"status": "ok"}'
    models = 200
    listed = {"data": [{"id": "qwen3.8-27b-ud-q4_k_xl", "status": {"value": "loaded"}}]}
    seen = []

    def do_GET(self):
        RouterHandler.seen.append((self.path, self.headers.get("Authorization")))
        if self.path == "/health":
            self._answer(RouterHandler.health, RouterHandler.health_body)
        elif self.path == "/models":
            self._answer(RouterHandler.models, json.dumps(RouterHandler.listed).encode())
        else:
            self.send_error(404)

    def _answer(self, status, body):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_arguments):
        pass


class ServingActionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_root = Path(tempfile.mkdtemp(prefix="serving-fixture-"))
        _agents, fixture_bin = runtime_fixture.create(cls.fixture_root)
        cls.fixture_server = fixture_bin / "llama-server"
        cls.fake_router = ThreadingHTTPServer(("127.0.0.1", 0), RouterHandler)
        cls.router_thread = threading.Thread(target=cls.fake_router.serve_forever, daemon=True)
        cls.router_thread.start()
        cls.router_port = cls.fake_router.server_address[1]
        cls.manager = web.build_server(port=0)
        cls.csrf = cls.manager.RequestHandlerClass.csrf
        cls.manager_thread = threading.Thread(target=cls.manager.serve_forever, daemon=True)
        cls.manager_thread.start()
        cls.manager_port = cls.manager.server_address[1]

    @classmethod
    def tearDownClass(cls):
        for server in (cls.manager, cls.fake_router):
            server.shutdown()
            server.server_close()
        for thread in (cls.manager_thread, cls.router_thread):
            thread.join(timeout=5)
        shutil.rmtree(cls.fixture_root, ignore_errors=True)

    def setUp(self):
        RouterHandler.health = 200
        RouterHandler.health_body = b'{"status": "ok"}'
        RouterHandler.models = 200
        RouterHandler.listed = {"data": [{"id": "qwen3.8-27b-ud-q4_k_xl",
                                          "status": {"value": "loaded"}}]}
        RouterHandler.seen = []
        self.home = Path(tempfile.mkdtemp(prefix="serving-actions-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        os.chmod(self.bin / "systemctl", 0o755)
        self.state = self.home / "systemd.json"
        self.log = self.home / "systemctl.log"
        self.units(router="inactive")
        environment = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "XDG_CONFIG_HOME": str(self.config),
            "FAKE_SYSTEMCTL_STATE": str(self.state),
            "FAKE_SYSTEMCTL_LOG": str(self.log),
        })
        environment.start()
        self.addCleanup(environment.stop)
        # Serving status reads the router at its own address; the fixture answers on a free port.
        address = mock.patch.object(serving_state, "ROUTER_PORT", self.router_port)
        address.start()
        self.addCleanup(address.stop)
        hardware = mock.patch.multiple(matuto_runtime, pci_devices=mock.DEFAULT,
                                       bounded_identity_probe=mock.DEFAULT)
        hardware.start()
        self.addCleanup(hardware.stop)
        self.hardware()
        self.seed()

    # Fixtures ---------------------------------------------------------------

    def hardware(self, *, identities=None, hosts=None):
        """The card observations the fixture runtime cannot make on this host."""
        found = runtime_fixture.HOST if hosts is None else hosts
        matuto_runtime.pci_devices = lambda: {"state": "completed", "devices": [found]}

        def probed(_backend, environment):
            if identities is not None:
                return identities
            if environment.get("ROCR_VISIBLE_DEVICES") in (None, runtime_fixture.HIP_UUID):
                return [{"pciAddress": PCI, "uuid": runtime_fixture.HIP_UUID}]
            return []
        matuto_runtime.bounded_identity_probe = probed

    def units(self, *, router="inactive", voice="inactive", **extra):
        """The fake systemd's per-unit state, with the refusals it was asked for."""
        answer = {"units": {ROUTER_UNIT: {"ActiveState": router, "MainPID": "0"},
                            serving_state.VOICE_ROUTER_UNIT: {"ActiveState": voice,
                                                               "MainPID": "0"}},
                  "list": ""}
        answer.update(extra)
        self.state.write_text(json.dumps(answer), encoding="utf-8")

    def seed(self):
        """A store with runtime selection over a fixture build and generated router files."""
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO cards (card_id, name, gpu_identity, vram_ceiling_bytes,"
            " default_gpu_layers, smoke_output_tokens, bench_prompt_tokens,"
            " bench_generation_tokens, bench_repetitions, notes, created_at)"
            " VALUES ('card-a', 'GPU', ?, 34359738368, 99, 256, 512, 128, 3, '', ?)",
            (PCI, NOW))
        installation = self.home / "build"
        (installation / "bin").mkdir(parents=True)
        shutil.copy2(self.fixture_server, installation / "bin" / "llama-server")
        connection.execute(
            "INSERT INTO installations (location, recipe, source_revision, target, toolchain,"
            " check_state, check_reason, check_digests, recorded_at)"
            " VALUES (?, 'llama.cpp HIP', 'revision', 'gfx1201', 'toolchain', 'checked', '',"
            " '{\"bin/llama-server\": \"fixture\"}', ?)", (str(installation), NOW))
        connection.execute(
            "INSERT INTO builds (build_id, label, kind, location, backend, source_revision,"
            " target, recipe, toolchain, state, notes)"
            " VALUES ('hip', 'hip', 'llama-hip', ?, 'hip', 'revision', 'gfx1201',"
            " 'llama.cpp HIP', 'toolchain', 'registered', '')", (str(installation),))
        connection.execute(
            "INSERT INTO runtime_selection (selection_id, build_id, card_id, backend, saved_at)"
            " VALUES (1, 'hip', 'card-a', 'hip', ?)", (NOW,))
        models = self.home / "models"
        models.mkdir(mode=0o700)
        body = b"model"
        (models / "model.gguf").write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        connection.execute("INSERT INTO artifact_locations VALUES ('loc-models', ?, '', ?)",
                           (str(models), NOW))
        connection.execute("INSERT INTO model_storage VALUES (1, 'loc-models', ?)", (NOW,))
        connection.execute(
            "INSERT INTO acquisition_records VALUES ('acq', 'acq', 'adoption',"
            " 'locally-hashed', NULL, NULL, NULL, 'loc-models', ?, '', ?)",
            (NOW, NOW))
        connection.execute("INSERT INTO acquisition_files VALUES ('acq', 'model.gguf', ?,"
                           " NULL, ?, ?)", (len(body), digest, NOW))
        connection.execute(
            "INSERT INTO model_profiles VALUES ('alpha', 'language', 'Alpha', 'alpha', 8192,"
            " 'high', 'card-a', NULL, 1, '', ?)", (NOW,))
        connection.execute("INSERT INTO profile_artifacts VALUES ('alpha', 'model', 'acq',"
                           " 'model.gguf')")
        self.key = self.opened.path.parent.parent / "pi-local" / "api-key"
        self.key.parent.mkdir(mode=0o700, exist_ok=True)
        self.key.write_text(f"{KEY}\n", encoding="utf-8")
        os.chmod(self.key, 0o600)
        store.regenerate_router_files(self.opened)

    def arguments(self, *rows):
        """The published profile's server rows, as launch validation will read them."""
        connection = self.opened.connection()
        for option, value in rows:
            position = connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM profile_arguments"
            ).fetchone()["next"]
            connection.execute(
                "INSERT INTO profile_arguments VALUES ('alpha', 'server', ?, NULL, ?, ?)",
                (position, option, value))

    def seed_qualified_voice(self):
        connection = self.opened.connection()
        executable_paths = {}
        for role, executable_id, binary in (("synthesis", "exe-synthesis", "breeze-server"),
                                            ("recognition", "exe-recognition", "whisper-server")):
            executable = self.home / binary
            shutil.copy2(self.fixture_server, executable)
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            executable_paths[role] = str(executable)
            connection.execute("INSERT INTO speech_executables VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (executable_id, role, "loc-models", str(executable), digest, "1",
                                json.dumps({"backend": "vulkan" if role == "synthesis" else "cpu"}), NOW))
        for profile, role, executable_id in (("voice-synth", "synthesis", "exe-synthesis"),
                                             ("voice-recog", "recognition", "exe-recognition")):
            connection.execute("INSERT INTO model_profiles (profile_id, kind, name, card_id,"
                                " speech_executable_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (profile, role, profile, "card-a" if role == "synthesis" else None,
                                 executable_id, NOW))
            connection.execute("INSERT INTO profile_artifacts VALUES (?, 'weights', 'acq', 'model.gguf')",
                               (profile,))
        connection.execute("INSERT INTO bundles (bundle_id, name, load_tokens, load_prompt, created_at)"
                           " VALUES ('voice-bundle', 'Voice', 8, 'Hello', ?)", (NOW,))
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'language', 'alpha')")
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'synthesis', 'voice-synth')")
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'recognition', 'voice-recog')")
        record = store._read_record(self.opened, "bundle", "voice-bundle")
        fingerprint = store.fingerprint(self.opened, record)
        self.assertIsNotNone(fingerprint)
        connection.execute("INSERT INTO qualification_records VALUES (?, 'bundle', ?, ?, ?, ?, ?)",
                           ("qual-voice", "voice-bundle", "Voice", fingerprint, "hip", NOW))
        connection.execute("INSERT INTO acceptances VALUES (?, ?, ?, '', 'operator', ?, NULL)",
                           ("accept-voice", "qual-voice", "voice", NOW))
        return executable_paths

    def seed_voice_record(self):
        connection = self.opened.connection()
        connection.execute("INSERT INTO speech_executables VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                           ("exe-synthesis", "synthesis", "loc-models", str(self.fixture_server),
                            "fixture-synthesis", "1", NOW))
        connection.execute("INSERT INTO speech_executables VALUES (?, ?, ?, ?, ?, ?, '', ?)",
                           ("exe-recognition", "recognition", "loc-models", str(self.fixture_server),
                            "fixture-recognition", "1", NOW))
        for profile, kind, executable in (("voice-synth", "synthesis", "exe-synthesis"),
                                          ("voice-recog", "recognition", "exe-recognition")):
            card = "card-a" if kind == "synthesis" else None
            connection.execute("INSERT INTO model_profiles (profile_id, kind, name, card_id,"
                                " speech_executable_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (profile, kind, profile, card, executable, NOW))
        connection.execute("INSERT INTO bundles (bundle_id, name, created_at) VALUES ('voice-bundle', 'Voice', ?)",
                           (NOW,))
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'language', 'alpha')")
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'synthesis', 'voice-synth')")
        connection.execute("INSERT INTO bundle_members VALUES ('voice-bundle', 'recognition', 'voice-recog')")
        connection.execute("INSERT INTO voice_serving VALUES (1, 'voice-bundle')")
        connection.execute("INSERT INTO voice_serving_parts VALUES ('synthesis', 'voice-bundle', 18081)")
        connection.execute("INSERT INTO voice_serving_parts VALUES ('recognition', 'voice-bundle', 18082)")
        voice_units = [serving_state.VOICE_ROUTER_UNIT,
                       serving_state.part_unit("voice-synth"),
                       serving_state.part_unit("voice-recog")]
        self.units(router="inactive", voice="active", list=" ".join(voice_units[1:]))
        answer = json.loads(self.state.read_text())
        for unit in voice_units[1:]:
            answer["units"][unit] = {"ActiveState": "active", "MainPID": "901"}
        self.state.write_text(json.dumps(answer), encoding="utf-8")
        return voice_units

    def record_run(self, procedure="smoke", label="Alpha"):
        """A recorded unfinished run that holds the card under the admission rules."""
        self.opened.connection().execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
            " procedure_revision, unit_name, started_at)"
            " VALUES ('run-1', 'language', 'alpha', ?, ?, '1', 'matuto-smoke-1.service', ?)",
            (label, procedure, NOW))

    # Requests ---------------------------------------------------------------

    def get(self, path, *, token=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.manager_port}{path}")
        request.add_header("Host", web.ALLOWED_HOST)
        if token:
            request.add_header("Cookie", f"{web.SESSION_COOKIE}={token}")
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def post(self, path, body=None, *, token=None, origin=web.MANAGER_ORIGIN, csrf=None):
        """One request at the fixed address, with the Host and Origin it demands."""
        payload = json.dumps(body if body is not None else {}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.manager_port}{path}",
                                         data=payload, method="POST")
        request.add_header("Host", web.ALLOWED_HOST)
        if origin is not None:
            request.add_header("Origin", origin)
        request.add_header("X-CSRF-Token", self.csrf if csrf is None else csrf)
        if token:
            request.add_header("Cookie", f"{web.SESSION_COOKIE}={token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return answer.status, json.loads(answer.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def sign_in(self):
        login.create_login("operator", "first-secret", "first-secret")
        token, _expiry = sessions.start_session("operator", "first-secret")
        return token

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def lifecycle(self):
        """Only the calls that change what systemd runs."""
        return [call for call in self.calls() if call.split(" ", 1)[0] in LIFECYCLE]

    def launcher(self, *arguments):
        """What systemd's ExecStart runs, as a terminal start and a crash restart run it."""
        return subprocess.run([sys.executable, "-B", str(router.CLI), "router", *arguments],
                              cwd=AGENTS, capture_output=True, text=True, timeout=180,
                              env=os.environ.copy())

    # Overview ----------------------------------------------------------------

    def test_overview_reads_serving_runtime_profile_and_current_work_without_lifecycle_calls(self):
        token = self.sign_in()
        status, body = self.get("/api/overview/serving", token=token)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], admission.STOPPED)
        self.assertEqual(body["headline"], "Ready. Not serving.")
        self.assertEqual(body["runtime"], {"build": "hip", "backend": "hip", "card": "GPU"})
        self.assertEqual([(profile["name"], profile["qualification"]["state"])
                          for profile in body["profiles"]], [("Alpha", "candidate")])
        self.assertIsNone(body["currentWork"])
        self.assertNotIn(KEY, json.dumps(body))
        self.assertFalse(self.lifecycle(), "loading Overview only reads serving state")

    # Start ------------------------------------------------------------------

    def test_app_install_pending_refuses_router_start_before_systemd(self):
        store.write_update_state(
            store.store_path(self.config), "2026.10.1", "complete",
            details={"app_install_pending": {"version": "2026.10.1", "list_number": 7}})
        with self.assertRaisesRegex(store.StoreError, "Matuto update to 2026.10.1 has not recorded an outcome"):
            router.start_unit(self.config)
        self.assertFalse(any(line.startswith(f"start {ROUTER_UNIT}") for line in self.calls()))

    def test_start_from_stopped_launches_the_router_unit_and_reports_it_healthy(self):
        token = self.sign_in()

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual((status, body["acted"], body["state"], body["healthy"]),
                         (200, True, admission.AGENTIC, True))
        self.assertIn(f"start {ROUTER_UNIT}", self.lifecycle())
        self.assertCountEqual(RouterHandler.seen, [("/health", f"Bearer {KEY}"),
                                                   ("/models", f"Bearer {KEY}")])
        self.assertNotIn(KEY, json.dumps(body))
        self.assertEqual(router.unit_path().name, ROUTER_UNIT)
        self.assertIn(shlex.join(router.launcher_command()),
                      router.unit_path().read_text(encoding="utf-8"))

    def test_the_launcher_argv_is_the_loopback_consumer_address_with_the_store_files(self):
        plan = router.prepare(self.config)
        self.addCleanup(plan.close)

        self.assertEqual(plan.argv[:2], ("--host", "127.0.0.1"))
        self.assertEqual(plan.argv[plan.argv.index("--port") + 1], "8888")
        self.assertEqual(plan.argv[plan.argv.index("--device") + 1], "ROCm0")
        self.assertEqual(plan.argv[plan.argv.index("--models-max") + 1], "1")
        self.assertEqual(plan.argv[plan.argv.index("--api-key-file") + 1], str(self.key))
        self.assertEqual(plan.argv[plan.argv.index("--models-preset") + 1],
                         str(self.opened.path.parent / "router-presets.ini"))
        self.assertEqual(plan.argv[plan.argv.index("--models-dir") + 1],
                         str(self.home / "models"))
        self.assertEqual(plan.environment["ROCR_VISIBLE_DEVICES"], runtime_fixture.HIP_UUID)
        self.assertNotIn(KEY, json.dumps(list(plan.argv) + [str(plan.environment)]))

    def test_start_needs_no_confirmation_and_start_while_serving_is_up_is_refused(self):
        token = self.sign_in()
        self.units(router="active")

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("agentic serving", body["error"])
        self.assertEqual(self.lifecycle(), [])

        self.units(voice="active")
        status, voice = self.post(PATHS[serving_actions.START_AGENTIC], token=token)
        self.assertEqual(status, 409)
        self.assertIn("voice serving", voice["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_start_is_refused_with_the_reason_when_no_runtime_selection_is_saved(self):
        token = self.sign_in()
        self.opened.connection().execute("DELETE FROM runtime_selection")

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("No runtime build and card are selected", body["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_an_unadvertised_server_option_is_refused_before_the_router_starts(self):
        token = self.sign_in()
        self.arguments(("--temp", "0.7"), ("--banana", "1"))

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("'alpha'", body["error"])
        self.assertIn("--banana", body["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_non_server_profile_arguments_do_not_block_agentic_serving(self):
        token = self.sign_in()
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO profile_arguments VALUES ('alpha', 'smoke', 0, NULL, '--temp', '0.7')")
        connection.execute(
            "INSERT INTO profile_arguments VALUES ('alpha', 'bench', 0, NULL, '-n', '128')")

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual((status, body["acted"], body["healthy"]), (200, True, True))
        self.assertIn(f"start {ROUTER_UNIT}", self.lifecycle())

    def test_start_refuses_an_unchecked_installation_before_probing_it(self):
        token = self.sign_in()
        self.opened.connection().execute(
            "UPDATE installations SET check_state = 'unchecked', check_reason = ?"
            " WHERE location = ?", ("inspection failed: unsafe loader path",
                                     str(self.home / "build")))

        with mock.patch.object(matuto_runtime, "probe") as probe:
            status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("inspection failed: unsafe loader path", body["error"])
        probe.assert_not_called()
        self.assertEqual(self.lifecycle(), [])

    def test_device_listing_uses_the_selected_cards_visibility_environment(self):
        original_probe = matuto_runtime.probe
        listed_environments = []

        def probe(descriptor, directory, flag, environment):
            if flag == "--list-devices":
                listed_environments.append(dict(environment))
                devices = ["ROCm0: discrete fixture (8192 MiB, 4096 MiB free)"]
                if environment.get("ROCR_VISIBLE_DEVICES") != runtime_fixture.HIP_UUID:
                    devices.append("ROCm1: other fixture (8192 MiB, 4096 MiB free)")
                return "Available devices:\n" + "\n".join(devices)
            return original_probe(descriptor, directory, flag, environment)

        with mock.patch.object(matuto_runtime, "probe", side_effect=probe):
            plan = router.prepare(self.config)
        self.addCleanup(plan.close)

        self.assertEqual(plan.selector, "ROCm0")
        self.assertEqual(len(listed_environments), 1)
        self.assertEqual(listed_environments[0].get("ROCR_VISIBLE_DEVICES"),
                         runtime_fixture.HIP_UUID)

    def test_a_router_that_never_answers_healthy_is_reported_as_not_healthy(self):
        token = self.sign_in()
        RouterHandler.health = 503
        bound = mock.patch.object(serving_actions, "READY_SECONDS", 0.5)
        bound.start()
        self.addCleanup(bound.stop)

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual((status, body["acted"], body["state"], body["healthy"]),
                         (200, True, admission.AGENTIC, False))
        self.assertIn("health", body["reason"])
        self.assertEqual(body["state"], admission.AGENTIC)

    def test_a_unit_that_did_not_start_is_never_reported_as_started(self):
        token = self.sign_in()
        self.units(**{"fail-start": [ROUTER_UNIT]})

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("did not start", body["error"])
        self.assertNotIn("state", body)

    # Stop and Restart -------------------------------------------------------

    def test_stop_without_confirmation_returns_the_consequences_and_changes_nothing(self):
        token = self.sign_in()
        self.units(router="active")

        status, body = self.post(PATHS[serving_actions.STOP_SERVING], token=token)

        self.assertEqual((status, body["acted"]), (200, False))
        self.assertEqual(body["preview"]["stops"], ["Agentic serving; consumers lose access"])
        self.assertEqual(body["preview"]["starts"], ["Nothing"])
        self.assertEqual(body["preview"]["safe_choice"], "Cancel")
        self.assertEqual(self.lifecycle(), [])

    def test_restart_without_confirmation_returns_the_consequences_and_changes_nothing(self):
        token = self.sign_in()
        self.units(router="active")

        status, body = self.post(PATHS[serving_actions.RESTART], token=token)

        self.assertEqual((status, body["acted"]), (200, False))
        self.assertIn("the loaded model unloads", body["preview"]["stops"][0])
        self.assertEqual(body["preview"]["starts"],
                         ["Agentic serving with the published profiles"])
        self.assertEqual(self.lifecycle(), [])

    def test_a_confirmed_stop_stops_the_unit_and_a_confirmed_restart_starts_it_again(self):
        token = self.sign_in()
        self.units(router="active")

        status, body = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                 token=token)

        self.assertEqual((status, body["acted"], body["stopped"], body["state"]),
                         (200, True, True, admission.STOPPED))
        self.assertEqual(self.lifecycle(), [f"stop {ROUTER_UNIT}"])

        self.units(router="active")
        self.log.unlink()
        status, restarted = self.post(PATHS[serving_actions.RESTART], {"confirm": True},
                                      token=token)

        self.assertEqual((status, restarted["acted"], restarted["healthy"]), (200, True, True))
        self.assertEqual(self.lifecycle()[0], f"stop {ROUTER_UNIT}")
        self.assertIn(f"start {ROUTER_UNIT}", self.lifecycle())
        self.assertNotIn(KEY, json.dumps(restarted))

    def test_a_stop_that_fails_is_reported_and_never_presented_as_stopped(self):
        token = self.sign_in()
        self.units(router="active", **{"fail-stop": [ROUTER_UNIT]})

        status, body = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                 token=token)

        self.assertEqual((status, body["acted"], body["stopped"]), (200, True, False))
        self.assertIn("could not be stopped", body["reason"])
        self.assertEqual(body["state"], admission.AGENTIC)

    def test_stop_and_restart_are_refused_from_stopped(self):
        token = self.sign_in()
        for control in (serving_actions.STOP_SERVING, serving_actions.RESTART):
            with self.subTest(control=control):
                status, body = self.post(PATHS[control], {"confirm": True}, token=token)
                self.assertEqual(status, 409)
                self.assertIn("not serving", body["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_agentic_stop_is_refused_from_voice_without_stopping_the_router(self):
        token = self.sign_in()
        self.units(voice="active")

        status, body = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                 token=token)

        self.assertEqual(status, 409)
        self.assertIn("voice serving", body["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_serve_voice_previews_then_starts_and_records_one_offered_bundle(self):
        token = self.sign_in()
        self.seed_qualified_voice()

        status, preview = self.post(VOICE_PATHS[serving.SERVE_VOICE],
                                    {"bundle": "voice-bundle"}, token=token)

        self.assertEqual(status, 200, preview)
        self.assertFalse(preview["acted"])
        self.assertEqual(preview["preview"]["starts"],
                         ["Voice: language, synthesis and recognition"])
        self.assertEqual(self.lifecycle(), [])
        with mock.patch.object(voice, "_readiness_reason", return_value=""):
            status, started = self.post(VOICE_PATHS[serving.SERVE_VOICE],
                                        {"bundle": "voice-bundle", "confirm": True}, token=token)

        self.assertEqual(status, 200, started)
        self.assertEqual((started["acted"], started["state"]), (True, admission.VOICE))
        self.assertEqual(set(started["units"]), {
            serving_state.VOICE_ROUTER_UNIT,
            serving_state.part_unit("voice-synth"),
            serving_state.part_unit("voice-recog")})
        self.assertEqual(serving_state.read(config_root=self.config).address,
                         f"http://{serving_state.HOST}:{serving_state.VOICE_ROUTER_PORT}")
        rows = self.opened.connection().execute(
            "SELECT part_role, port FROM voice_serving_parts ORDER BY part_role").fetchall()
        self.assertEqual([(row["part_role"], row["port"]) for row in rows],
                         [("recognition", 18082), ("synthesis", 18081)])
        for path in self.config.joinpath("systemd", "user").glob("matuto-app-voice-*.service"):
            unit_text = path.read_text()
            self.assertNotIn("[Install]", unit_text)
            self.assertNotIn("matuto-app-manager.service", unit_text)
            command = shlex.split(next(line.removeprefix("ExecStart=") for line in unit_text.splitlines()
                                       if line.startswith("ExecStart=")))
            command_check = subprocess.run(command[:4], capture_output=True, text=True, check=False)
            expected = "voice-router" if path.name == serving_state.VOICE_ROUTER_UNIT else "voice-part"
            self.assertIn(f"usage: matuto {expected}", command_check.stderr)
        synthesis = voice.prepare_part("synthesis")
        recognition = voice.prepare_part("recognition")
        for spec, flag in ((synthesis, "--voices-dir"), (recognition, "--public")):
            index = spec["args"].index(flag)
            self.assertTrue(Path(spec["args"][index + 1]).is_absolute())

    def test_failed_voice_start_stops_even_a_unit_whose_start_command_failed(self):
        token = self.sign_in()
        self.seed_qualified_voice()
        start_unit = voice._start_unit

        def start_then_fail(unit):
            start_unit(unit)
            if unit == serving_state.part_unit("voice-recog"):
                raise voice.VoiceRefused("simulated start failure")

        with mock.patch.object(voice, "_start_unit", side_effect=start_then_fail):
            status, failure = self.post(VOICE_PATHS[serving.SERVE_VOICE],
                                        {"bundle": "voice-bundle", "confirm": True}, token=token)

        self.assertEqual(status, 409, failure)
        self.assertIn("simulated start failure", failure["error"])
        self.assertCountEqual(self.lifecycle(), [
            f"start {serving_state.VOICE_ROUTER_UNIT}",
            f"start {serving_state.part_unit('voice-synth')}",
            f"start {serving_state.part_unit('voice-recog')}",
            f"stop {serving_state.part_unit('voice-recog')}",
            f"stop {serving_state.part_unit('voice-synth')}",
            f"stop {serving_state.VOICE_ROUTER_UNIT}",
            "daemon-reload", "daemon-reload", "daemon-reload"])
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM voice_serving").fetchone()[0], 0)

    def test_failed_voice_start_reports_when_prior_agentic_serving_cannot_be_restored(self):
        self.seed_qualified_voice()
        self.units(router="active")

        with (mock.patch.object(voice, "_readiness_reason", return_value="fixture readiness failure"),
              mock.patch.object(router, "start_unit",
                                side_effect=router.LauncherRefused("restore refused"))):
            with self.assertRaisesRegex(voice.VoiceRefused,
                                        "Could not restore prior agentic serving: restore refused"):
                voice.start(self.config, "voice-bundle", timeout=0)

        self.assertIn(f"stop {ROUTER_UNIT}", self.lifecycle())
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM voice_serving").fetchone()[0], 0)

    def test_healthy_voice_offers_switch_despite_its_expected_speech_parts(self):
        state = serving_state.State(admission.VOICE, healthy=True, bundle="Voice",
                                    parts=(serving_state.Part("voice-synth", "synthesis", "active"),
                                           serving_state.Part("voice-recog", "recognition", "active")))

        controls = serving.controls(voice._view(state), bundles=())

        switch = next(item for item in controls if item.name == serving.SWITCH_TO_AGENTIC)
        self.assertTrue(switch.offered, switch.reasons)

    def test_synthesis_launcher_uses_store_selection_without_legacy_selection(self):
        def identities(backend, _environment):
            if backend == "Vulkan":
                return [{"pciAddress": PCI, "uuid": "f" * 32, "index": 0}]
            return [{"pciAddress": PCI, "uuid": runtime_fixture.HIP_UUID}]

        speech_exec = self.home / "breeze-server"
        shutil.copy2(self.fixture_server, speech_exec)
        with (mock.patch.object(matuto_runtime, "bounded_identity_probe", side_effect=identities),
              mock.patch.object(matuto_runtime, "read_selection",
                                side_effect=AssertionError("legacy selection read")),
              mock.patch.object(voice.os, "chdir"),
              mock.patch.object(voice.os, "execve") as execute):
            voice._exec_speech("synthesis", "Vulkan", str(speech_exec),
                               str(self.home / "models" / "model.gguf"), "127.0.0.1",
                               18081, ["--voices-dir", str(self.home / "voices")])

        argv, environment = execute.call_args.args[1:3]
        self.assertEqual(argv[0], "breeze-server")
        self.assertEqual(environment["GGML_VK_VISIBLE_DEVICES"], "0")

    def test_failed_readiness_names_part_and_failed_start_never_reads_healthy(self):
        token = self.sign_in()
        self.seed_qualified_voice()
        synth = serving_state.part_unit("voice-synth")
        recog = serving_state.part_unit("voice-recog")
        units = [serving_state.VOICE_ROUTER_UNIT, synth, recog]
        self.units(**{"fail-stop": units})
        with mock.patch.object(voice, "_readiness_reason",
                               side_effect=lambda unit, _parts:
                               "fixture HTTP 503" if unit == recog else ""):
            with self.assertRaisesRegex(voice.VoiceRefused,
                                        f"{recog}: fixture HTTP 503") as failure:
                voice.start(self.config, "voice-bundle", timeout=0.01)

        self.assertIn("remain running", str(failure.exception))
        state = serving_state.read(config_root=self.config)
        self.assertEqual(state.state, admission.VOICE)
        self.assertFalse(state.healthy, state.reason)
        self.assertIn("does not name every bundle part", state.reason)
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM voice_serving_parts").fetchone()[0], 1)

        status, retry = self.post(VOICE_PATHS[serving.STOP_SERVING], {"confirm": True}, token=token)
        self.assertEqual((status, retry["stopped"]), (200, False), retry)
        state = serving_state.read(config_root=self.config)
        self.assertFalse(state.healthy, state.reason)
        status, refused = self.post(VOICE_PATHS[serving.SWITCH_TO_AGENTIC],
                                    {"confirm": True}, token=token)
        self.assertEqual(status, 409)
        self.assertIn("speech part is still running", refused["error"])
        self.assertNotIn(f"start {ROUTER_UNIT}", self.lifecycle())

    def test_voice_router_checks_only_its_bundle_language_profile(self):
        self.seed_voice_record()
        connection = self.opened.connection()
        connection.execute("INSERT INTO model_profiles (profile_id, kind, name, alias, context_size,"
                           " thinking, card_id, published, created_at)"
                           " VALUES ('unrelated', 'language', 'Unrelated', 'unrelated', 8192,"
                           " 'high', 'card-a', 1, ?)", (NOW,))
        connection.execute("INSERT INTO profile_arguments VALUES ('unrelated', 'server', 0, NULL,"
                           " '--unsupported-voice-probe', NULL)")
        with (mock.patch.object(matuto_runtime, "_landlock_confine"),
              mock.patch.object(voice.os, "chdir"),
              mock.patch.object(voice.os, "execve", side_effect=RuntimeError("exec fixture"))):
            with self.assertRaisesRegex(RuntimeError, "exec fixture"):
                voice.launch_router("voice-bundle", self.config)

    def test_voice_unit_launcher_refuses_when_a_run_holds_the_card(self):
        self.seed_voice_record()
        self.record_run()

        with mock.patch.object(voice.os, "execve") as execute:
            with self.assertRaisesRegex(voice.VoiceRefused, "smoke for Alpha run"):
                voice.launch_part("synthesis", "Vulkan", "/not-used", "/not-used",
                                  "127.0.0.1", 18081, [])

        execute.assert_not_called()

    def test_switch_stops_healthy_voice_before_starting_agentic(self):
        token = self.sign_in()
        self.seed_voice_record()

        status, preview = self.post(VOICE_PATHS[serving.SWITCH_TO_AGENTIC], token=token)
        self.assertEqual((status, preview["acted"]), (200, False), preview)
        self.assertEqual(self.lifecycle(), [])

        status, switched = self.post(VOICE_PATHS[serving.SWITCH_TO_AGENTIC],
                                     {"confirm": True}, token=token)

        self.assertEqual((status, switched["state"], switched["healthy"]),
                         (200, admission.AGENTIC, True), switched)
        lifecycle = self.lifecycle()
        start_index = lifecycle.index(f"start {ROUTER_UNIT}")
        self.assertCountEqual([call for call in lifecycle[:start_index]
                               if call.startswith("stop ")], [
            f"stop {serving_state.VOICE_ROUTER_UNIT}",
            f"stop {serving_state.part_unit('voice-recog')}",
            f"stop {serving_state.part_unit('voice-synth')}"])

    def test_failed_voice_stop_remains_retryable_and_blocks_switch(self):
        token = self.sign_in()
        synth = serving_state.part_unit("voice-synth")
        self.seed_voice_record()
        self.units(router="inactive", voice="active", list=" ".join((
            serving_state.part_unit("voice-synth"), serving_state.part_unit("voice-recog"))),
            **{"fail-stop": [synth]})
        answer = json.loads(self.state.read_text())
        for unit in (serving_state.part_unit("voice-synth"), serving_state.part_unit("voice-recog")):
            answer["units"][unit] = {"ActiveState": "active", "MainPID": "901"}
        self.state.write_text(json.dumps(answer), encoding="utf-8")

        status, partial = self.post(VOICE_PATHS[serving.STOP_SERVING], {"confirm": True}, token=token)

        self.assertEqual(status, 200, partial)
        self.assertFalse(partial["stopped"])
        self.assertIn(synth, partial["remaining"])
        status, refused = self.post(VOICE_PATHS[serving.SWITCH_TO_AGENTIC],
                                    {"confirm": True}, token=token)
        self.assertEqual(status, 409)
        self.assertIn("speech part is still running", refused["error"])
        self.assertNotIn("start", [call.split(" ", 1)[0] for call in self.lifecycle()])

        self.units(router="inactive", voice="active", list=" ".join((
            serving_state.part_unit("voice-synth"), serving_state.part_unit("voice-recog"))))
        status, stopped = self.post(VOICE_PATHS[serving.STOP_SERVING], {"confirm": True}, token=token)
        self.assertEqual((status, stopped["stopped"]), (200, True), stopped)

    def test_voice_stop_requires_confirmation_then_stops_all_parts_and_starts_nothing(self):
        token = self.sign_in()
        units = self.seed_voice_record()
        current = serving_state.read(config_root=self.config)
        self.assertEqual(current.state, admission.VOICE, current.reason)

        status, preview = self.post(VOICE_PATHS[serving.STOP_SERVING], token=token)

        self.assertEqual(status, 200, preview)
        self.assertFalse(preview["acted"])
        self.assertIn("Every voice part", preview["preview"]["stops"][0])
        self.assertEqual(preview["preview"]["starts"], ["Nothing"])
        self.assertEqual(self.lifecycle(), [])

        status, stopped = self.post(VOICE_PATHS[serving.STOP_SERVING], {"confirm": True},
                                    token=token)

        self.assertEqual((status, stopped["acted"], stopped["stopped"]), (200, True, True))
        self.assertCountEqual(self.lifecycle(), [f"stop {unit}" for unit in units])
        self.assertNotIn("start", [call.split(" ", 1)[0] for call in self.lifecycle()])
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM voice_serving").fetchone()[0], 0)

    # The launcher every start runs ------------------------------------------

    def test_the_route_refuses_a_start_while_a_run_holds_the_card(self):
        token = self.sign_in()
        self.record_run(label="Alpha")

        status, body = self.post(PATHS[serving_actions.START_AGENTIC], token=token)

        self.assertEqual(status, 409)
        self.assertIn("held by the smoke run for Alpha", body["error"])
        self.assertEqual(self.lifecycle(), [])

    def test_the_launcher_a_terminal_start_and_a_crash_restart_run_refuses_the_same_way(self):
        self.record_run(label="Alpha")

        finished = self.launcher("--check")

        self.assertEqual(finished.returncode, 1)
        self.assertIn("held by the smoke for Alpha run", finished.stderr)
        self.assertEqual(self.lifecycle(), [])

    def test_the_launcher_refuses_while_the_card_holder_is_unknown(self):
        self.units(show="silent")

        finished = self.launcher("--check")

        self.assertEqual(finished.returncode, 1)
        self.assertIn("holder is unknown", finished.stderr)

    def test_the_launcher_admits_a_start_whose_checks_all_pass(self):
        self.arguments(("--temp", "0.7"))
        from matuto.commands import router as router_command

        self.assertEqual(router_command.run(["--check"]), 0)
        self.assertEqual(self.calls()[-2:], [f"show {ROUTER_UNIT} --property=MainPID",
                                             f"show {serving_state.VOICE_ROUTER_UNIT} --property=MainPID"])

    def test_a_run_admission_racing_a_start_never_leaves_both_holding_the_card(self):
        finished = threading.Event()
        outcome = {}
        asked = router._systemctl

        def admit():
            worker = runner.Runner(
                self.config,
                serving_state=lambda: serving_state.read(
                    config_root=self.config, router_port=self.router_port).state,
                card_holder=lambda: admission.Card(admission.CARD_FREE))
            worker._answers = lambda: True
            worker.launch = lambda *_arguments: SimpleNamespace(returncode=0)
            try:
                outcome["admission"] = worker.start(runner.Request.run(
                    "smoke", "Alpha", ("true",),
                    runner.Target("language", "alpha", "Alpha", "sha256:fixture"), "1"))
            except BaseException:
                outcome["error"] = traceback.format_exc()
            finally:
                worker.close()
                finished.set()

        def raced(*arguments):
            answer = asked(*arguments)
            if arguments[:2] == ("start", ROUTER_UNIT):
                threading.Thread(target=admit, daemon=True).start()
                self.assertFalse(finished.wait(0.5),
                                 "a run was admitted while the start held the card")
            return answer
        self.addCleanup(lambda: finished.wait(30))

        with mock.patch.object(router, "_systemctl", raced):
            serving_actions.activate(serving_actions.START_AGENTIC, confirmed=True,
                                     config_root=self.config)

        self.assertTrue(finished.wait(30), outcome.get("error", ""))
        self.assertEqual(outcome["admission"].decision, admission.STOP_SERVING_AND_RUN)
        self.assertIn("Stop serving and run", outcome["admission"].reason)

    # Nothing starts serving on its own --------------------------------------

    def test_the_router_unit_has_no_install_section_and_depends_on_no_manager_unit(self):
        text = router.unit_text()

        self.assertNotIn("[Install]", text)
        for clause in ("WantedBy", "RequiredBy", "Requires=", "After=", "BindsTo="):
            self.assertNotIn(clause, text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn(shlex.join(router.launcher_command()), text)
        self.assertEqual(router.unit_path().name, ROUTER_UNIT)

    def test_nothing_starts_the_unit_again_after_a_stop(self):
        token = self.sign_in()
        self.units(router="active")

        status, _body = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                  token=token)
        self.assertEqual(status, 200)
        self.log.unlink()

        for _attempt in range(3):
            self.assertEqual(serving_actions.read_state(self.config).state, admission.STOPPED)
        status, stopped = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                    token=token)

        self.assertEqual(status, 409)
        self.assertEqual(self.lifecycle(), [])

    def test_stopping_the_manager_leaves_the_router_running_and_calls_nothing(self):
        self.units(router="active")
        if self.log.exists():
            self.log.unlink()

        state = serving_actions.read_state(self.config)

        self.assertEqual((state.state, state.healthy), (admission.AGENTIC, True))
        self.assertEqual(self.lifecycle(), [])
        self.assertEqual(sorted(self.calls()),
                         sorted([f"is-active {ROUTER_UNIT}",
                                 f"is-active {serving_state.VOICE_ROUTER_UNIT}",
                                 "list-units --all --no-legend --plain"
                                 " matuto-app-voice-part-*.service"]))
        self.assertNotIn("matuto-app-manager.service", router.unit_text())

    # The session, Origin and CSRF checks ------------------------------------

    def test_every_route_refuses_without_a_session_origin_or_csrf_token(self):
        token = self.sign_in()
        self.units(router="active")
        for control, path in PATHS.items():
            for missing in ("session", "origin", "csrf"):
                with self.subTest(control=control, missing=missing):
                    supplied = {"token": token, "origin": web.MANAGER_ORIGIN, "csrf": self.csrf}
                    supplied["token" if missing == "session" else missing] = (
                        None if missing == "origin" else "")
                    status, body = self.post(path, {"confirm": True}, **supplied)
                    self.assertEqual(status, 401 if missing == "session" else 403)
                    self.assertNotIn(KEY, json.dumps(body))
                    self.assertNotIn("state", body)
        self.assertEqual(self.lifecycle(), [])

    def test_a_credit_shaped_token_that_is_not_a_session_is_refused(self):
        self.sign_in()

        status, body = self.post(PATHS[serving_actions.STOP_SERVING], {"confirm": True},
                                 token="z" * 40)

        self.assertEqual(status, 401)
        self.assertIn("sign-in", body["error"])


if __name__ == "__main__":
    unittest.main()