"""The serving-state reader, through a fake systemctl and a fake router.

Every state is asserted from what the reader reports: not serving, healthy
agentic with its loaded model, unhealthy agentic when the health or the model
list does not answer, voice serving with its bundle, parts and addresses, a
partly running voice, a speech unit no record names, units in transition, and
unknown when systemd or the router does not answer or answers with something
else. The fake systemctl also logs every call, so reading is seen to make no
lifecycle call, and the status provider reports the matching header item.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, serving_state, store as store_module  # noqa: E402
from matuto.discovery import load_item  # noqa: E402

KEY = "t" * 40
BUNDLE = "voice-conversation"
PROFILES = {"synthesis": "breeze-tts-2", "recognition": "whisper-large-v3-turbo"}
PORTS = {"synthesis": 8810, "recognition": 8811}
FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
"""A fake systemctl: answers per unit from a file and logs every call."""
import json
import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_SYSTEMCTL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
answers = json.loads(Path(os.environ["FAKE_SYSTEMCTL_UNITS"]).read_text(encoding="utf-8"))
if "list-units" in sys.argv:
    listed = answers.get("*", "")
    if listed == "exit-1":
        sys.exit(1)
    if listed == "banana":
        sys.stdout.write("banana\\n")
        sys.exit(0)
    sys.stdout.write("".join(f"{unit} loaded active running\\n" for unit in listed.split()))
else:
    sys.stdout.write(answers.get(sys.argv[-1], ""))
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
            self.answer(RouterHandler.health, RouterHandler.health_body)
        elif self.path == "/models":
            self.answer(RouterHandler.models, json.dumps(RouterHandler.listed).encode())
        else:
            self.send_error(404)

    def answer(self, status, body):
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *arguments):
        pass


class ServingStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RouterHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        RouterHandler.health = 200
        RouterHandler.health_body = b'{"status": "ok"}'
        RouterHandler.models = 200
        RouterHandler.listed = {"data": [{"id": "qwen3.8-27b-ud-q4_k_xl", "status": {"value": "loaded"}}]}
        RouterHandler.seen = []
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.units = self.home / "systemd" / "user"
        self.units.mkdir(parents=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        os.makedirs(store_module.store_directory(self.config), mode=0o700, exist_ok=True)
        os.chmod(store_module.store_directory(self.config), 0o700)
        self.log = self.home / "systemctl.log"
        self.answers = self.home / "units.json"
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        os.chmod(self.bin / "systemctl", 0o755)
        self.key = self.home / store_module.CONFIG_ROOT_NAME / "pi-local" / "api-key"
        os.makedirs(self.key.parent, mode=0o700, exist_ok=True)
        self.key.write_text(f"{KEY}\n", encoding="utf-8")
        os.chmod(self.key, 0o600)
        patch = mock.patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "XDG_CONFIG_HOME": str(self.home),
            "FAKE_SYSTEMCTL_LOG": str(self.log),
            "FAKE_SYSTEMCTL_UNITS": str(self.answers),
        })
        patch.start()
        self.addCleanup(patch.stop)

    # Fixtures ---------------------------------------------------------------

    def units_answer(self, *, listed=(), **states):
        names = listed if isinstance(listed, str) else " ".join(listed)
        self.answers.write_text(json.dumps({"*": names, **states}), encoding="utf-8")

    def stopped(self):
        self.units_answer(**{unit: "inactive" for unit in
                             (serving_state.ROUTER_UNIT, serving_state.VOICE_ROUTER_UNIT)})

    def voice_record(self, rows=PROFILES.items()):
        """A store with a bundle, its speech profiles and the voice record."""
        opened = store_module.open_store(self.config)
        self.addCleanup(opened.close)
        connection = opened.connection()
        connection.execute("INSERT INTO artifact_locations VALUES ('loc', '/srv/speech', '', '')")
        for index, (role, port) in enumerate(PORTS.items()):
            connection.execute("INSERT INTO speech_executables VALUES (?, ?, 'loc', ?, ?, ?, '', '')",
                               (f"exe-{index}", role, f"/opt/{PROFILES[role]}", f"{role}-sha", "1.0"))
        for role, profile in rows:
            connection.execute(
                "INSERT INTO model_profiles (profile_id, kind, name, speech_executable_id, created_at) "
                "VALUES (?, ?, ?, ?, '')", (profile, role, profile, f"exe-{list(PORTS).index(role)}"))
        connection.execute(
            "INSERT INTO bundles (bundle_id, name, notes, load_tokens, load_prompt, created_at) "
            "VALUES (?, 'Voice conversation', '', NULL, NULL, '')", (BUNDLE,))
        connection.execute("INSERT INTO voice_serving VALUES (1, ?)", (BUNDLE,))
        for role, profile in rows:
            connection.execute("INSERT INTO bundle_members VALUES (?, ?, ?)", (BUNDLE, role, profile))
            connection.execute("INSERT INTO voice_serving_parts VALUES (?, ?, ?)",
                               (role, BUNDLE, PORTS[role]))
        connection.commit()
        return connection

    def read(self, **kwargs):
        return serving_state.read(config_root=self.config, router_port=self.port, **kwargs)

    def generated_unit(self, profile):
        (self.units / serving_state.part_unit(profile)).write_text("[Unit]\n", encoding="utf-8")

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    # Not serving and agentic serving ---------------------------------------

    def test_an_inactive_router_reports_not_serving(self):
        self.stopped()
        state = self.read()
        self.assertEqual((state.state, state.reason), (serving_state.STOPPED, ""))
        self.assertEqual((state.model, state.bundle, state.address), ("", "", ""))
        self.assertFalse(store_module.store_path(self.config).exists(), "reading created a store")

    def test_a_failed_router_unit_reports_not_serving_with_the_reason(self):
        self.stopped()
        self.units_answer(**{serving_state.ROUTER_UNIT: "failed", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertIn("failed", self.read().reason)

    def test_a_healthy_agentic_router_reports_its_loaded_model_and_address(self):
        self.stopped()
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.AGENTIC, True))
        self.assertEqual(state.reason, "")
        self.assertEqual(state.model, "qwen3.8-27b-ud-q4_k_xl")
        self.assertEqual(state.address, f"http://127.0.0.1:{self.port}")
        self.assertIn(("/health", f"Bearer {KEY}"), RouterHandler.seen)
        self.assertNotIn(KEY, repr(state))

    def test_a_router_whose_health_does_not_answer_is_unhealthy(self):
        self.stopped()
        RouterHandler.health = 503
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.AGENTIC, False))
        self.assertIn("health", state.reason)
        self.assertEqual(state.model, "qwen3.8-27b-ud-q4_k_xl")

    def test_a_health_answer_that_is_not_the_router_json_is_unhealthy(self):
        self.stopped()
        RouterHandler.health_body = b"banana"
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_router_whose_model_list_does_not_answer_is_unhealthy_without_a_model(self):
        self.stopped()
        RouterHandler.models = 500
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.AGENTIC, False))
        self.assertIn("model list", state.reason)
        self.assertEqual(state.model, "")

    def test_a_model_list_this_reader_does_not_recognize_is_unhealthy(self):
        self.stopped()
        RouterHandler.listed = {"unexpected": True}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_router_without_a_readable_credential_is_unhealthy(self):
        self.stopped()
        self.key.unlink()
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.AGENTIC, False))
        self.assertEqual(RouterHandler.seen, [])

    def test_a_model_the_router_does_not_report_as_loaded_is_not_the_loaded_model(self):
        self.stopped()
        RouterHandler.listed = {"data": [{"id": "loaded-one", "status": {"value": "loaded"}},
                                         {"id": "idle-one", "status": {"value": "unloaded"}},
                                         {"id": "idle-two", "status": {"value": "unloaded"}}]}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().model, "loaded-one")

    def test_a_model_list_entry_that_is_not_a_model_is_unhealthy(self):
        self.stopped()
        RouterHandler.listed = {"data": [42]}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_health_object_without_a_status_is_unhealthy(self):
        self.stopped()
        RouterHandler.health_body = b'{"unexpected": true}'
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_model_entry_with_an_unrecognized_load_status_is_unhealthy(self):
        self.stopped()
        RouterHandler.listed = {"data": [{"id": "model-a", "status": 42}]}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_load_status_that_is_not_one_the_router_reports_is_unhealthy(self):
        self.stopped()
        RouterHandler.listed = {"data": [{"id": "model-a", "status": {"value": "banana"}}]}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_an_empty_catalogue_and_an_unloaded_model_stay_healthy(self):
        self.stopped()
        RouterHandler.listed = {"data": []}
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual((self.read().healthy, self.read().model), (True, ""))
        RouterHandler.listed = {"data": [{"id": "idle", "status": {"value": "unloaded"}}]}
        self.assertEqual((self.read().healthy, self.read().model), (True, ""))

    def test_a_health_status_that_is_not_the_routers_own_is_unhealthy(self):
        self.stopped()
        RouterHandler.health_body = b'{"status": ""}'
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        self.assertEqual(self.read().healthy, False)

    def test_a_credential_that_is_not_a_router_token_is_never_put_in_a_request(self):
        self.stopped()
        self.key.write_text(f"{KEY}\nsecond line\n", encoding="utf-8")
        self.units_answer(**{serving_state.ROUTER_UNIT: "active", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.AGENTIC, False))
        self.assertEqual(RouterHandler.seen, [])

    def test_the_credential_is_the_one_key_path_the_cutover_keeps(self):
        self.assertEqual(serving_state.router_key_path().relative_to(self.home),
                         Path(store_module.CONFIG_ROOT_NAME) / "pi-local" / "api-key")

    # Voice serving ----------------------------------------------------------

    def test_voice_serving_reports_its_bundle_parts_and_addresses(self):
        self.voice_record()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active",
                             serving_state.part_unit("breeze-tts-2"): "active",
                             serving_state.part_unit("whisper-large-v3-turbo"): "active"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.VOICE, True))
        self.assertEqual(state.reason, "")
        self.assertEqual([(part.profile, part.role, part.state, part.address) for part in state.parts],
                         [("whisper-large-v3-turbo", "recognition", "active", "http://127.0.0.1:8811"),
                          ("breeze-tts-2", "synthesis", "active", "http://127.0.0.1:8810")])
        self.assertEqual(state.bundle, BUNDLE)
        self.assertEqual(state.address, f"http://127.0.0.1:{serving_state.VOICE_ROUTER_PORT}")

    def test_a_partly_running_voice_is_reported_with_the_unit_that_is_not_running(self):
        self.voice_record()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active",
                             serving_state.part_unit("breeze-tts-2"): "active",
                             serving_state.part_unit("whisper-large-v3-turbo"): "inactive"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.VOICE, False))
        self.assertIn(f"{serving_state.part_unit('whisper-large-v3-turbo')} is not running", state.reason)

    def test_a_speech_unit_no_voice_record_names_is_reported(self):
        self.voice_record(rows=[("synthesis", "breeze-tts-2")])
        self.generated_unit("whisper-large-v3-turbo")
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active",
                             serving_state.part_unit("breeze-tts-2"): "active",
                             serving_state.part_unit("whisper-large-v3-turbo"): "active"})
        state = self.read()
        stray = [part for part in state.parts if part.stray]
        self.assertEqual([(part.profile, part.state) for part in stray], [("whisper-large-v3-turbo", "active")])
        self.assertIn(f"no voice record names {serving_state.part_unit('whisper-large-v3-turbo')}", state.reason)

    def test_a_voice_record_that_cannot_be_read_leaves_the_bundle_unknown(self):
        self.voice_record()
        os.chmod(store_module.store_directory(self.config), 0o755)
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active"})
        state = self.read()
        self.assertEqual((state.state, state.bundle), (serving_state.VOICE, ""))
        self.assertIn("voice record cannot be read", state.reason)

    def test_voice_parts_without_the_record_they_belong_to_leave_the_bundle_unknown(self):
        connection = self.voice_record()
        # A store written outside the store module, which the module's foreign
        # keys would not have allowed.
        raw = sqlite3.connect(store_module.store_path(self.config))
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DELETE FROM voice_serving")
        raw.commit()
        raw.close()
        del connection
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active"})
        state = self.read()
        self.assertEqual((state.state, state.bundle), (serving_state.VOICE, ""))
        self.assertIn("voice record cannot be read", state.reason)

    def test_voice_units_with_no_voice_record_are_not_healthy_voice(self):
        self.stopped()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.VOICE, False))
        self.assertEqual(state.bundle, "")
        self.assertIn("no voice record names the bundle", state.reason)

    def test_an_agentic_router_systemd_does_not_report_leaves_the_state_unknown(self):
        self.voice_record()
        self.units_answer(**{serving_state.VOICE_ROUTER_UNIT: "active",
                             serving_state.part_unit("breeze-tts-2"): "active",
                             serving_state.part_unit("whisper-large-v3-turbo"): "active"})
        state = self.read()
        self.assertEqual((state.state, state.healthy, state.bundle),
                         (serving_state.UNKNOWN_STATE, False, BUNDLE))
        self.assertIn(serving_state.ROUTER_UNIT, state.reason)
        self.assertEqual([part.profile for part in state.parts],
                         ["whisper-large-v3-turbo", "breeze-tts-2"])
        refused = admission.decide(admission.START_AGENTIC, "probe", None, state.state,
                                   admission.Card(admission.CARD_FREE))
        self.assertEqual(refused.outcome, admission.REFUSE)

    def test_an_interrupted_store_is_left_alone_and_its_record_is_unknown(self):
        path = store_module.store_path(self.config)
        os.makedirs(path.parent, mode=0o700, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.touch()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active"})
        state = self.read()
        self.assertEqual((state.state, state.bundle), (serving_state.VOICE, ""))
        self.assertIn("voice record cannot be read", state.reason)
        self.assertEqual(path.stat().st_size, 0)

    def test_an_unreachable_store_file_leaves_the_voice_record_unknown_without_raising(self):
        self.voice_record()
        path = store_module.store_path(self.config)
        exists = Path.exists
        lookups = 0

        def inaccessible(candidate):
            nonlocal lookups
            if candidate == path:
                lookups += 1
                if lookups == 2:
                    raise PermissionError("store file is unreachable")
            return exists(candidate)

        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active"})
        with mock.patch.object(Path, "exists", inaccessible):
            state = self.read()
        self.assertEqual((state.state, state.bundle), (serving_state.VOICE, ""))
        self.assertIn("voice record cannot be read", state.reason)

    # Transitions and unknown reads ------------------------------------------

    def test_a_starting_router_is_reported_in_transition_and_unknown(self):
        self.units_answer(**{serving_state.ROUTER_UNIT: "activating", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertIn("activating", state.reason)

    def test_a_speech_unit_systemd_still_has_after_its_file_went_is_reported(self):
        self.voice_record(rows=[("synthesis", "breeze-tts-2")])
        unit = serving_state.part_unit("whisper-large-v3-turbo")
        self.units_answer(listed=(unit,), **{serving_state.ROUTER_UNIT: "inactive",
                                             serving_state.VOICE_ROUTER_UNIT: "active",
                                             serving_state.part_unit("breeze-tts-2"): "active",
                                             unit: "active"})
        state = self.read()
        self.assertEqual([(part.profile, part.stray, part.state) for part in state.parts
                          if part.stray], [("whisper-large-v3-turbo", True, "active")])
        self.assertIn(f"no voice record names {unit}", state.reason)

    def test_a_voice_part_no_bundle_contains_cannot_be_recorded(self):
        connection = self.voice_record(rows=[("synthesis", "breeze-tts-2")])
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO voice_serving_parts VALUES ('recognition', ?, 8811)",
                               (BUNDLE,))
            connection.commit()

    def test_a_stopping_voice_part_is_reported_in_transition(self):
        self.voice_record()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive",
                             serving_state.VOICE_ROUTER_UNIT: "active",
                             serving_state.part_unit("breeze-tts-2"): "deactivating",
                             serving_state.part_unit("whisper-large-v3-turbo"): "active"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.VOICE, False))
        self.assertIn(f"{serving_state.part_unit('breeze-tts-2')} is deactivating", state.reason)
        self.assertIn("in transition", state.reason)

    def test_a_voice_unit_systemd_does_not_report_is_unknown_not_not_serving(self):
        self.voice_record()
        self.units_answer(**{serving_state.ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertIn(serving_state.VOICE_ROUTER_UNIT, state.reason)

    def test_a_voice_unit_systemd_does_not_report_is_unknown_not_healthy(self):
        self.stopped()
        self.units_answer(**{serving_state.ROUTER_UNIT: "active"})
        state = self.read()
        self.assertEqual((state.state, state.healthy), (serving_state.UNKNOWN_STATE, False))
        self.assertIn(serving_state.VOICE_ROUTER_UNIT, state.reason)

    def test_speech_units_neither_systemd_nor_the_unit_directory_can_list_are_unknown(self):
        self.stopped()
        shutil.rmtree(self.units)
        self.units.write_text("not a directory\n", encoding="utf-8")
        self.units_answer(listed="exit-1", **{serving_state.ROUTER_UNIT: "inactive",
                                              serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertIn("could not be listed", state.reason)

    def test_a_speech_unit_systemd_lists_is_reported_even_when_the_file_scan_fails(self):
        self.stopped()
        unit = serving_state.part_unit("whisper-large-v3-turbo")
        shutil.rmtree(self.units)
        self.units.write_text("not a directory\n", encoding="utf-8")
        self.units_answer(listed=(unit,), **{serving_state.ROUTER_UNIT: "inactive",
                                             serving_state.VOICE_ROUTER_UNIT: "active",
                                             unit: "active"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertEqual([(part.profile, part.stray, part.state) for part in state.parts],
                         [("whisper-large-v3-turbo", True, "active")])

    def test_a_unit_listing_this_reader_does_not_recognize_is_unknown(self):
        self.stopped()
        self.units_answer(listed="banana", **{serving_state.ROUTER_UNIT: "inactive",
                                              serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertIn("could not be listed", state.reason)

    def test_a_systemctl_that_does_not_answer_reports_unknown_not_stopped(self):
        self.stopped()
        with mock.patch.dict(os.environ, {"PATH": str(self.home / "empty")}):
            state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertIn("systemd did not report", state.reason)

    def test_an_unexpected_systemctl_answer_reports_unknown_not_inactive(self):
        self.stopped()
        self.units_answer(**{serving_state.ROUTER_UNIT: "banana", serving_state.VOICE_ROUTER_UNIT: "inactive"})
        state = self.read()
        self.assertEqual(state.state, serving_state.UNKNOWN_STATE)
        self.assertEqual(dict(state.units)[serving_state.ROUTER_UNIT], serving_state.UNKNOWN)

    def test_a_store_another_writer_has_open_is_left_alone(self):
        self.stopped()
        path = store_module.store_path(self.config)
        opened = store_module.open_store(self.config)
        opened.connection().execute(
            "INSERT INTO bundles (bundle_id, name, notes, load_tokens, load_prompt, created_at) "
            "VALUES ('bundle', 'Bundle', '', NULL, NULL, '')")
        opened.connection().commit()
        opened.close()
        child = ('import os, sqlite3, sys\n'
                 'connection = sqlite3.connect(sys.argv[1])\n'
                 'connection.execute("PRAGMA cache_size = 1")\n'
                 'connection.execute("BEGIN IMMEDIATE")\n'
                 "connection.execute('UPDATE bundles SET notes = ?', ('x' * 1000000,))\n"
                 'os._exit(0)\n')
        subprocess.run([sys.executable, "-B", "-c", child, str(path)], check=True)
        journal = Path(f"{path}-journal")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        state = self.read()
        self.assertEqual(state.state, serving_state.STOPPED)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertTrue(journal.exists(), "the read removed another writer's journal")

    # Reading changes nothing ------------------------------------------------

    def test_reading_makes_no_lifecycle_call(self):
        self.voice_record()
        self.stopped()
        self.read()
        self.read()
        calls = self.calls()
        self.assertTrue(calls)
        verbs = {call.split()[1] for call in calls}
        self.assertEqual(verbs, {"is-active", "list-units"})
        self.assertNotIn(verbs & {"start", "stop", "restart", "enable", "disable"}, set())
        self.assertEqual({call.split()[0] for call in calls}, {"--user"})
        self.assertEqual(sorted({call.split()[2] for call in calls if call.split()[1] == "is-active"}),
                         sorted([serving_state.ROUTER_UNIT, serving_state.VOICE_ROUTER_UNIT,
                                 serving_state.part_unit("breeze-tts-2"),
                                 serving_state.part_unit("whisper-large-v3-turbo")]))

    def test_the_unit_names_differ_from_the_checkout_install_units(self):
        checkout = {"local-llama-router.service", "matuto-voice-router.service"}
        self.assertTrue(checkout.isdisjoint({serving_state.ROUTER_UNIT, serving_state.VOICE_ROUTER_UNIT}))
        self.assertFalse(serving_state.part_unit("x").startswith("matuto-voice-part-"))


class StatusProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = load_item("status", "serving")

    def test_each_state_reports_its_header_item(self):
        reported = {state: self.provider.item(serving_state.State(state))["label"]
                    for state in (serving_state.STOPPED, serving_state.AGENTIC, serving_state.VOICE)}
        self.assertEqual(reported, {"stopped": "Not serving", "agentic": "Agentic serving",
                                    "voice": "Voice serving"})

    def test_every_item_links_to_overviews_serving(self):
        for state in (serving_state.STOPPED, serving_state.AGENTIC, serving_state.VOICE,
                      serving_state.UNKNOWN_STATE):
            self.assertEqual(self.provider.item(serving_state.State(state))["href"], "#/overview/serving")

    def test_an_unknown_state_is_named_not_reported_as_not_serving(self):
        self.assertEqual(self.provider.item(serving_state.State(serving_state.UNKNOWN_STATE))["label"],
                         "Serving state unknown")


if __name__ == "__main__":
    unittest.main()
