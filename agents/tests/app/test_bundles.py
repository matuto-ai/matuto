"""The protected Bundles page reads and changes records through the manager HTTP API."""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(Path(__file__).parent))

from matuto import login, sessions, store  # noqa: E402
from matuto.web import (ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server)  # noqa: E402
from test_record_validation import RecordTestCase  # noqa: E402


class BundlesHttpTest(unittest.TestCase):
    _fill = RecordTestCase._fill
    file = RecordTestCase.file
    executable = RecordTestCase.executable
    build_server = RecordTestCase.build_server
    insert_language_profile = RecordTestCase.insert_language_profile
    bundle = RecordTestCase.bundle

    def setUp(self):
        RecordTestCase.setUp(self)
        self.insert_language_profile()
        self.bundle_record = dict(self.bundle(), load_tokens=128, load_prompt="hello")
        store.save_record(self.store, self.bundle_record)
        self._accept_bundle()
        self.login_environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        self.login_environment.start()
        self.addCleanup(self.login_environment.stop)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret", config_root=self.config)
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener()

    def _accept_bundle(self):
        fingerprint = store.fingerprint(self.store, self.bundle_record)
        connection = self.store.connection()
        connection.execute("INSERT INTO qualification_records (qualification_id, record_kind,"
                           " record_id, record_label, fingerprint, created_at)"
                           " VALUES ('qual-original', 'bundle', 'bundle-voice', 'Voice 192K', ?,"
                           " '2026-10-12T00:00:00+00:00')", (fingerprint,))
        connection.execute("INSERT INTO acceptances (acceptance_id, qualification_id, scope,"
                           " accepted_by, accepted_at) VALUES ('accept-original', 'qual-original',"
                           " 'fixture', 'operator', '2026-10-12T00:00:00+00:00')")

    def _request(self, path, *, method="GET", body=None):
        headers = {"Host": ALLOWED_HOST}
        if getattr(self, "token", None):
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        if method not in ("GET", "HEAD"):
            headers["Origin"] = MANAGER_ORIGIN
            headers["X-CSRF-Token"] = self.csrf
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def _post(self, body):
        status, payload, _headers = self._request("/api/bundles", method="POST", body=body)
        return status, json.loads(payload)

    def test_list_includes_bundle_own_state_member_kinds_and_availability(self):
        status, payload, _headers = self._request("/api/bundles")
        self.assertEqual(status, 200)
        result = json.loads(payload)
        bundle = result["bundles"][0]
        self.assertEqual(bundle["qualification"]["state"], "qualified")
        self.assertEqual(set(bundle["members"]), {"language", "synthesis", "recognition"})
        self.assertEqual(bundle["members"]["language"]["name"], "Qwen3.6 Coder 192K")
        self.assertEqual(set(result["profiles"]), {"language", "synthesis", "recognition"})
        self.assertNotIn("qualification", result["profiles"]["synthesis"][0])

    def test_confirmation_rejects_a_review_whose_qualification_state_changed(self):
        edited = dict(self.bundle_record, load_tokens=256, load_prompt="A changed load.")
        status, preview = self._post({"action": "preview", **edited})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["state"]["state"], "candidate")
        fingerprint = store.fingerprint(self.store, edited)
        connection = self.store.connection()
        connection.execute("INSERT INTO qualification_records (qualification_id, record_kind,"
                           " record_id, record_label, fingerprint, created_at)"
                           " VALUES ('qual-after-review', 'bundle', 'bundle-voice', 'Voice 192K', ?,"
                           " '2026-10-12T00:00:00+00:00')", (fingerprint,))
        connection.execute("INSERT INTO acceptances (acceptance_id, qualification_id, scope,"
                           " accepted_by, accepted_at) VALUES ('accept-after-review',"
                           " 'qual-after-review', 'fixture', 'operator',"
                           " '2026-10-12T00:00:00+00:00')")
        status, refusal = self._post({"action": "save", "mode": "edit", **edited,
            "confirm": True, "review_token": preview["review_token"]})
        self.assertEqual(status, 409)
        self.assertIn("review changed", refusal["error"])
        self.assertEqual(store.read_record(self.store, "bundle", "bundle-voice")["load_tokens"], 128)

    def test_missing_member_artifact_is_reported_as_bundle_unavailability(self):
        self.file("Qwen3.6-35B-A3B-Q4_K_M.gguf").unlink()
        status, payload, _headers = self._request("/api/bundles")
        self.assertEqual(status, 200)
        bundle = json.loads(payload)["bundles"][0]
        self.assertFalse(bundle["available"])
        self.assertIn("Qwen3.6-35B-A3B-Q4_K_M.gguf", bundle["reason"])
        self.assertFalse(bundle["members"]["language"]["available"])
        self.assertIn("Qwen3.6-35B-A3B-Q4_K_M.gguf", bundle["members"]["language"]["reason"])

    def test_served_edit_requires_review_confirmation_and_cosmetic_edit_saves_directly(self):
        edited = dict(self.bundle_record, load_tokens=256, load_prompt="A changed load.")
        status, preview = self._post({"action": "preview", **edited})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["kind"], "served-change")
        self.assertEqual(preview["preview"]["state"]["state"], "candidate")
        status, refusal = self._post({"action": "save", "mode": "edit", **edited})
        self.assertEqual(status, 409)
        self.assertIn("Review the changed served values", refusal["error"])
        self.assertEqual(store.read_record(self.store, "bundle", "bundle-voice")["load_tokens"], 128)
        status, saved = self._post({"action": "save", "mode": "edit", **edited,
                                    "confirm": True, "review_token": preview["review_token"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["bundle"]["load_tokens"], 256)
        cosmetic = dict(edited, name="Renamed voice", notes="new note")
        status, saved = self._post({"action": "save", "mode": "edit", **cosmetic})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["bundle"]["name"], "Renamed voice")
        restored = dict(cosmetic, load_tokens=128, load_prompt="hello")
        status, preview = self._post({"action": "preview", **restored})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["state"]["state"], "qualified")
        status, saved = self._post({"action": "save", "mode": "edit", **restored,
                                    "confirm": True, "review_token": preview["review_token"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["bundle"]["qualification"]["state"], "qualified")

    def test_member_change_with_the_same_fingerprint_still_gets_reviewed(self):
        store.duplicate_record(self.store, "language", "prof-coder", "prof-equivalent",
                               "Equivalent language", alias="qwen3-coder-copy")
        edited = dict(self.bundle_record, load_tokens=256,
                      members=dict(self.bundle_record["members"], language="prof-equivalent"))
        status, preview = self._post({"action": "preview", **edited})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["preview"]["kind"], "served-change")
        self.assertEqual(preview["preview"]["state"]["state"], "candidate")
        self.assertEqual({row["field"] for row in preview["preview"]["changed_values"]},
                         {"members.language", "stated_load"})
        member_change = next(row for row in preview["preview"]["changed_values"]
                             if row["field"] == "members.language")
        self.assertEqual((member_change["old"], member_change["new"]),
                         ("prof-coder", "prof-equivalent"))
        status, refusal = self._post({"action": "save", "mode": "edit", **edited})
        self.assertEqual(status, 409)
        self.assertIn("Review the changed served values", refusal["error"])

    def test_create_refuses_wrong_member_kind_without_storing_anything(self):
        record = dict(self.bundle_record, bundle_id="bundle-new", name="New voice")
        status, created = self._post({"action": "save", "mode": "create", **record})
        self.assertEqual(status, 200, created)
        self.assertEqual(created["bundle"]["qualification"]["state"], "candidate")
        broken = dict(record, bundle_id="bundle-wrong")
        broken["members"] = dict(record["members"], synthesis="prof-coder")
        status, refusal = self._post({"action": "save", "mode": "create", **broken})
        self.assertEqual(status, 409)
        self.assertIn("members.synthesis", refusal["error"])
        self.assertIsNone(self.store.connection().execute(
            "SELECT 1 FROM bundles WHERE bundle_id='bundle-wrong'").fetchone())

    def test_malformed_member_is_refused_with_its_field_before_preview_or_save(self):
        edited = dict(self.bundle_record,
                      members=dict(self.bundle_record["members"], language=["prof-coder"]))
        for action in ("preview", "save"):
            request = {"action": action, **edited}
            if action == "save":
                request["mode"] = "edit"
            status, refusal = self._post(request)
            self.assertEqual(status, 409)
            self.assertIn("members.language", refusal["error"])
        self.assertEqual(store.read_record(self.store, "bundle", "bundle-voice")["members"],
                         self.bundle_record["members"])

    def test_duplicate_is_a_candidate_and_removal_keeps_history_browsable(self):
        status, copied = self._post({"action": "duplicate", "bundle_id": "bundle-voice",
                                     "new_id": "bundle-copy", "name": "Voice copy"})
        self.assertEqual(status, 200, copied)
        self.assertEqual(copied["bundle"]["qualification"]["state"], "candidate")
        self.assertEqual({role: member["id"] for role, member in copied["bundle"]["members"].items()},
                         self.bundle_record["members"])
        self.assertIsNone(self.store.connection().execute(
            "SELECT 1 FROM qualification_records WHERE record_kind='bundle'"
            " AND record_id='bundle-copy'").fetchone())
        self.assertEqual(store.read_record(self.store, "bundle", "bundle-voice")["name"], "Voice 192K")
        self.assertEqual(store.qualification_state(self.store,
            store.read_record(self.store, "bundle", "bundle-voice"))["state"], "qualified")
        copied_record = store.read_record(self.store, "bundle", "bundle-copy")
        fingerprint = store.fingerprint(self.store, copied_record)
        connection = self.store.connection()
        connection.execute("INSERT INTO runs (run_id, record_kind, record_id, record_label,"
                           " procedure, procedure_revision, started_at)"
                           " VALUES ('run-copy', 'bundle', 'bundle-copy', 'Voice copy',"
                           " 'bundle', 'fixture', '2026-10-12T00:00:00+00:00')")
        connection.execute("INSERT INTO qualification_records (qualification_id, record_kind,"
                           " record_id, record_label, fingerprint, created_at)"
                           " VALUES ('qual-copy', 'bundle', 'bundle-copy', 'Voice copy', ?,"
                           " '2026-10-12T00:00:00+00:00')", (fingerprint,))
        status, invalid = self._post({"action": "qualify", "bundle_id": "bundle-voice"})
        self.assertEqual(status, 409)
        self.assertIn("Choose a bundle action", invalid["error"])
        status, removed = self._post({"action": "remove", "bundle_id": "bundle-copy"})
        self.assertEqual(status, 200, removed)
        self.assertIsNone(self.store.connection().execute(
            "SELECT 1 FROM bundles WHERE bundle_id='bundle-copy'").fetchone())
        self.assertIsNotNone(self.store.connection().execute(
            "SELECT 1 FROM runs WHERE run_id='run-copy'").fetchone())
        self.assertIsNotNone(self.store.connection().execute(
            "SELECT 1 FROM qualification_records WHERE qualification_id='qual-copy'").fetchone())

    def test_shell_and_page_script_are_session_gated_and_expose_no_qualification_editor(self):
        status, page, _headers = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Bundles", page)
        self.assertIn(b"csrf-token", page)
        self.assertIn(b"/bundles.js", page)
        status, style, headers = self._request("/bundles.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css")
        self.assertIn(b"min-height:44px", style)
        status, script, headers = self._request("/bundles.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript")
        self.assertIn(b"matutoPopup.open", script)
        self.assertNotIn(b"type=\"qualification\"", script)
        self.token = ""
        status, _body, _headers = self._request("/api/bundles")
        self.assertEqual(status, 401)
