"""Runtime › Speech runtimes lists only installed speech executable records."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agents.tests.app.test_record_validation import RecordTestCase

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions
from matuto.page import speech_runtimes as speech_runtimes_page
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server


class SpeechRuntimesHttpTest(RecordTestCase):
    def setUp(self):
        super().setUp()
        fake_bin = self.home / "fake-bin"
        fake_bin.mkdir()
        self.systemctl_log = self.home / "systemctl.log"
        systemctl = fake_bin / "systemctl"
        systemctl.write_text('#!/bin/sh\nprintf called >> "$SYSTEMCTL_LOG"\n', encoding="utf-8")
        systemctl.chmod(0o755)
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.config), "SYSTEMCTL_LOG": str(self.systemctl_log),
            "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", "")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")
        records = [
            ("exe-breeze", "synthesis", "breeze-server", "a" * 40,
             {"project": "Breeze-TTS-2.cpp", "recipe": "Breeze synthesis (Vulkan)",
              "backend": "vulkan", "commit": "a" * 40,
              "submodules": {"third_party/ggml": "b" * 40}}, "2026-10-14T00:00:00Z"),
            ("exe-whisper", "recognition", "build/bin/whisper-server", "c" * 40,
             {"project": "whisper.cpp", "recipe": "whisper recognition (CPU)",
              "backend": "cpu", "commit": "c" * 40}, "2026-10-12T00:00:00Z"),
            ("whisper-old", "recognition", "build/bin/whisper-server", "d" * 40,
             {"project": "whisper.cpp", "recipe": "whisper recognition (CPU)",
              "backend": "cpu", "commit": "d" * 40}, "2026-10-11T00:00:00Z"),
        ]
        for identifier, kind, executable, commit, provenance, installed_at in records:
            if identifier in ("exe-breeze", "exe-whisper"):
                self.connection.execute(
                    "UPDATE speech_executables SET path=?, sha256=?, version=?, notes=?, installed_at=?"
                    " WHERE executable_id=?",
                    (executable, "e" * 64, commit, json.dumps(provenance), installed_at, identifier))
            else:
                self.connection.execute(
                    "INSERT INTO speech_executables"
                    " (executable_id, kind, location_id, path, sha256, version, notes, installed_at)"
                    " VALUES (?, ?, 'loc-models', ?, ?, ?, ?, ?)",
                    (identifier, kind, executable, "e" * 64, commit, json.dumps(provenance), installed_at))
        self.connection.commit()
        self.connection.execute(
            "INSERT INTO builds (build_id, label, kind, location, backend, source_revision,"
            " target, recipe, toolchain, state, notes) VALUES"
            " ('llama-build', 'LLama fixture', 'llama-vulkan', '/installed/llama',"
            " 'vulkan', ?, NULL, 'llama.cpp Vulkan', '', 'registered', '')", ("f" * 40,))
        for identifier, outcome in (("running", None), ("failed", "failed"),
                                    ("cancelled", "cancelled"), ("interrupted", "interrupted")):
            self.connection.execute(
                "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, outcome, started_at)"
                " VALUES (?, 'build', ?, ?, ?, '2026-10-14T00:00:00Z')",
                (f"job-{identifier}", f"matuto-{identifier}",
                 json.dumps({"recipe": "Breeze synthesis (Vulkan)"}), outcome))
        self.connection.commit()
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf

    def request(self, path, *, authenticated=True):
        headers = {"Host": ALLOWED_HOST}
        if authenticated:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        request = urllib.request.Request(self.address + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                body = response.read()
                return response.status, body
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_section_and_list_show_all_identity_fields_and_recipe_routes_without_writing(self):
        page = speech_runtimes_page.html({})
        self.assertIn("Profiles › Speech profiles", page)
        self.assertIn("Model files", page)
        self.assertIn('/speech-runtimes.css', page)
        self.assertEqual((speech_runtimes_page.PAGE, speech_runtimes_page.NAME,
                          speech_runtimes_page.TITLE, speech_runtimes_page.ORDER),
                         ("runtime", "speech-runtimes", "Speech runtimes", 3))
        before = list(self.connection.iterdump())
        status, body = self.request("/api/speech-runtimes")
        self.assertEqual(status, 200)
        entries = json.loads(body)["executables"]
        self.assertEqual(len(entries), 3)
        self.assertEqual({item["project"] for item in entries},
                         {"Breeze-TTS-2.cpp", "whisper.cpp"})
        breeze = next(item for item in entries if item["project"] == "Breeze-TTS-2.cpp")
        self.assertEqual((breeze["recipe"], breeze["backend"], breeze["commit"], breeze["ggmlCommit"]),
                         ("Breeze synthesis (Vulkan)", "Vulkan", "a" * 40, "b" * 40))
        self.assertEqual(breeze["installationPath"], str(self.models))
        self.assertEqual(breeze["executablePath"], str(self.models / "breeze-server"))
        self.assertEqual(breeze["sha256"], "e" * 64)
        self.assertEqual(breeze["installHref"],
                         "#/runtime/install?recipe=Breeze%20synthesis%20%28Vulkan%29")
        whispers = [item for item in entries if item["project"] == "whisper.cpp"]
        self.assertEqual({item["commit"] for item in whispers}, {"c" * 40, "d" * 40})
        self.assertEqual({item["backend"] for item in whispers}, {"CPU"})
        self.assertTrue(all(item["installHref"].endswith(
            "recipe=whisper%20recognition%20%28CPU%29") for item in whispers))
        self.assertEqual(before, list(self.connection.iterdump()))
        self.assertFalse(self.systemctl_log.exists())
        status, stylesheet = self.request("/speech-runtimes.css")
        self.assertEqual(status, 200)
        self.assertIn(b"var(--accent)", stylesheet)

    def test_empty_store_and_session_gate(self):
        self.connection.execute("UPDATE model_profiles SET speech_executable_id=NULL")
        self.connection.execute("DELETE FROM speech_executables")
        self.connection.commit()
        status, body = self.request("/api/speech-runtimes")
        self.assertEqual((status, json.loads(body)["executables"]), (200, []))
        status, body = self.request("/api/speech-runtimes", authenticated=False)
        self.assertEqual(status, 401, body)
        self.assertNotIn(b"/installed/speech", body)
        status, body = self.request("/", authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", body)
        self.assertNotIn(b"Speech runtimes", body)
        status, body = self.request("/speech-runtimes.js", authenticated=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b"Installed speech runtimes", body)

    def test_wrong_methods_do_not_modify_records(self):
        before = list(self.connection.iterdump())
        request = urllib.request.Request(
            self.address + "/api/speech-runtimes", method="POST",
            headers={"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}",
                     "Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.csrf},
            data=b"{}")
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 405)
        else:
            self.fail("POST to the inventory API must be rejected")
        self.assertEqual(before, list(self.connection.iterdump()))


if __name__ == "__main__":
    unittest.main()
