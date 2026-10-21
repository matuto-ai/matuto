"""Protected HTTP actions for Language profiles."""

import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission, live_records, login, sessions, serving_state, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class LanguageProfileActionsHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.models = self.home / "models"
        self.models.mkdir()
        (self.models / "model.gguf").write_bytes(b"model bytes")
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        connection = self.opened.connection()
        connection.executescript(f"""
            INSERT INTO cards VALUES ('card-a', 'Test GPU', '0000:03:00.0', 1000,
                -1, 16, 64, 16, 1, '', '{now}');
            INSERT INTO artifact_locations VALUES ('loc-a', '{self.models}', '', '{now}');
            INSERT INTO acquisition_records VALUES ('acq-a', 'Model set', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc-a', '{now}', '', '{now}');
            INSERT INTO acquisition_files VALUES ('acq-a', 'model.gguf', 11, NULL,
                '{'a' * 64}', '{now}');
        """)
        login.create_login("operator", "secret", "secret")
        self.cookie, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener()

    def request(self, path, *, method="GET", body=None, authenticated=True, protect=True):
        headers = {"Host": ALLOWED_HOST}
        if authenticated:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.cookie}"
        if method not in ("GET", "HEAD") and protect:
            headers.update({"Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.csrf})
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def post(self, body):
        status, payload, _headers = self.request(
            "/api/language-profile-actions", method="POST", body=body)
        return status, json.loads(payload)

    def language(self, profile_id, alias=None):
        return {"kind": "language", "profile_id": profile_id, "name": f"Profile {profile_id}",
                "alias": alias or f"alias-{profile_id}", "context_size": 4096,
                "thinking": "off", "card_id": "card-a", "notes": "original notes",
                "model": {"acquisition_id": "acq-a", "file_name": "model.gguf"},
                "mmproj": None, "mtp": None, "arguments": []}

    def save(self, profile, *, published=False):
        store.save_record(self.opened, profile)
        if published:
            self.opened.connection().execute(
                "UPDATE model_profiles SET published = 1 WHERE profile_id = ?",
                (profile["profile_id"],))

    def test_action_routes_require_session_origin_and_csrf(self):
        status, _body, _headers = self.request("/api/language-profile-actions", authenticated=False)
        self.assertEqual(status, 401)
        status, _body, _headers = self.request("/api/language-profile-edits?profile_id=missing", authenticated=False)
        self.assertEqual(status, 401)
        status, _body, _headers = self.request("/api/language-profile-edits", method="POST",
            body={"action":"save","record":{}}, protect=False)
        self.assertEqual(status, 403)
        for headers in ({"Cookie": f"{SESSION_COOKIE}={self.cookie}"},
                        {"Cookie": f"{SESSION_COOKIE}={self.cookie}", "Origin": MANAGER_ORIGIN}):
            request = urllib.request.Request(self.address + "/api/language-profile-actions",
                data=b'{"action":"remove","profile_id":"missing"}', method="POST",
                headers={"Host": ALLOWED_HOST, **headers})
            try:
                self.opener.open(request, timeout=5)
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 403)
            else:
                self.fail("unprotected mutation was accepted")

    def test_duplicate_preserves_original_and_never_copies_evidence(self):
        original = self.language("source")
        self.save(original, published=True)
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
            " procedure_revision, started_at) VALUES ('run-source', 'language', 'source',"
            " 'Source', 'smoke', 'test', '2026-10-13T00:00:00+00:00')")
        connection.execute(
            "INSERT INTO qualification_records (qualification_id, record_kind, record_id,"
            " record_label, fingerprint, created_at) VALUES ('qual-source', 'language', 'source',"
            " 'Source', 'fp', '2026-10-13T00:00:00+00:00')")
        connection.execute(
            "INSERT INTO acceptances (acceptance_id, qualification_id, scope, accepted_by, accepted_at)"
            " VALUES ('accept-source', 'qual-source', 'test', 'operator', '2026-10-13T00:00:00+00:00')")
        before = store.read_record(self.opened, "language", "source")
        status, result = self.post({"action": "duplicate", "profile_id": "source",
            "new_id": "copy", "name": "Copy profile", "alias": "copy-alias"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["profile_id"], "copy")
        copy = store.read_record(self.opened, "language", "copy")
        self.assertEqual((copy["name"], copy["alias"], copy["published"]),
                         ("Copy profile", "copy-alias", False))
        self.assertEqual(store.read_record(self.opened, "language", "source"), before)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM runs WHERE record_id='copy'").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM qualification_records WHERE record_id='copy'").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM acceptances WHERE qualification_id='qual-source'").fetchone()[0], 1)

    def test_unpublish_refuses_if_profile_becomes_live_after_page_read(self):
        self.save(self.language("published"), published=True)
        live = mock.patch.object(live_records, "read_state",
                                 return_value=serving_state.State(admission.AGENTIC))
        with live:
            status, result = self.post({"action": "unpublish", "profile_id": "published"})
        self.assertEqual(status, 409)
        self.assertIn("Stop serving", result["error"])
        self.assertTrue(store.read_record(self.opened, "language", "published")["published"])

    def test_publish_requires_candidate_confirmation_and_refuses_alias_collisions(self):
        self.save(self.language("candidate"))
        status, result = self.post({"action": "publish", "profile_id": "candidate"})
        self.assertEqual(status, 409)
        self.assertIn("not qualified", result["error"])
        self.assertFalse(store.read_record(self.opened, "language", "candidate")["published"])
        status, result = self.post({"action": "publish", "profile_id": "candidate",
            "confirmation": "Publish Profile candidate as not qualified"})
        self.assertEqual(status, 200, result)
        self.assertTrue(store.read_record(self.opened, "language", "candidate")["published"])
        self.save(self.language("collision", alias="alias-candidate"))
        status, result = self.post({"action": "publish", "profile_id": "collision",
            "confirmation": "Publish Profile collision as not qualified"})
        self.assertEqual(status, 409)
        self.assertIn("candidate", result["error"])
        self.assertFalse(store.read_record(self.opened, "language", "collision")["published"])

        qualified = self.language("qualified")
        self.save(qualified)
        fingerprint = store.fingerprint(self.opened, store.read_record(self.opened, "language", "qualified"))
        connection = self.opened.connection()
        connection.execute("INSERT INTO qualification_records (qualification_id, record_kind, record_id,"
                           " record_label, fingerprint, created_at) VALUES ('qual-qualified', 'language',"
                           " 'qualified', 'Qualified', ?, '2026-10-13T00:00:00+00:00')", (fingerprint,))
        connection.execute("INSERT INTO acceptances (acceptance_id, qualification_id, scope, accepted_by,"
                           " accepted_at) VALUES ('accept-qualified', 'qual-qualified', 'test', 'operator',"
                           " '2026-10-13T00:00:00+00:00')")
        status, result = self.post({"action": "publish", "profile_id": "qualified"})
        self.assertEqual(status, 200, result)
        self.assertTrue(store.read_record(self.opened, "language", "qualified")["published"])

    def test_remove_refusal_and_allowed_removal_preserve_history(self):
        self.save(self.language("published"), published=True)
        stopped = mock.patch.object(live_records, "read_state",
                                     return_value=serving_state.State(admission.STOPPED))
        with stopped:
            status, result = self.post({"action": "remove", "profile_id": "published"})
        self.assertEqual(status, 409)
        self.assertIn("published", result["error"])
        self.assertIsNotNone(self.opened.connection().execute(
            "SELECT 1 FROM model_profiles WHERE profile_id='published'").fetchone())
        self.save(self.language("removable"))
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO qualification_records (qualification_id, record_kind, record_id,"
            " record_label, fingerprint, created_at) VALUES ('qual-removable', 'language',"
            " 'removable', 'Removable', 'fp', '2026-10-13T00:00:00+00:00')")
        status, result = self.post({"action": "remove", "profile_id": "removable"})
        self.assertEqual(status, 200, result)
        self.assertIsNone(connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id='removable'").fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM qualification_records WHERE qualification_id='qual-removable'").fetchone())

    def test_get_shows_shared_live_and_reference_reasons_and_scripts_load(self):
        self.save(self.language("published"), published=True)
        self.save(self.language("referenced"))
        connection = self.opened.connection()
        connection.execute("INSERT INTO bundles VALUES ('bundle-ref', 'Voice bundle', '', 128,"
                           " 'test prompt', '2026-10-13T00:00:00+00:00')")
        connection.execute("INSERT INTO bundle_members VALUES ('bundle-ref', 'language', 'referenced')")
        status, body, _headers = self.request("/api/language-profile-actions")
        self.assertEqual(status, 200)
        by_id = {row["profile_id"]: row for row in json.loads(body)["profiles"]}
        self.assertIn("unpublish before removing", by_id["published"]["remove_reason"])
        self.assertIn("Voice bundle (bundle-ref)", by_id["referenced"]["remove_reason"])
        stopped = mock.patch.object(live_records, "read_state",
                                     return_value=serving_state.State(admission.STOPPED))
        with stopped:
            status, refusal = self.post({"action": "remove", "profile_id": "referenced"})
        self.assertEqual(status, 409)
        self.assertIn("bundle", refusal["error"])
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id='referenced'").fetchone())
        status, script, headers = self.request("/language-profile-actions.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript")
        self.assertIn(b"matutoPopup.open", script)
        status, style, headers = self.request("/language-profile-actions.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css")
        self.assertIn(b"language-profile-duplicate-form", style)
        status, page, _headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"/language-profile-actions.js", page)
        self.assertIn(b"/language-profile-actions.css", page)


if __name__ == "__main__":
    unittest.main()
