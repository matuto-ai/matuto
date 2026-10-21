"""Storage settings are session-gated and use the manager's HTTP interface."""

import hashlib
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
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, login, sessions, storage, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class StorageHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tree = Path(tempfile.mkdtemp(prefix="storage-routes-"))
        cls.addClassCleanup(shutil.rmtree, cls.tree, ignore_errors=True)
        routes = cls.tree / "routes"
        routes.mkdir()
        for source in (AGENTS / "matuto" / "routes").glob("*.py"):
            shutil.copy(source, routes / source.name)
        cls.server = build_server(port=0, routes_root=cls.tree)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join, 5)
        cls.addClassCleanup(cls.server.shutdown)
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.csrf = cls.server.RequestHandlerClass.csrf
        cls.opener = urllib.request.build_opener(NoRedirect)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="storage-http-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.opened = store.open_store()
        self.connection = self.opened.connection()
        self.addCleanup(self.opened.close)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")

    def request(self, method="GET", body=None, *, token=None, csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if token:
            headers["Cookie"] = f"{SESSION_COOKIE}={token}"
        if method not in ("GET", "HEAD"):
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.address + "/api/storage", method=method,
                                         data=data, headers=headers)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def usable(self, name):
        path = self.home / name
        path.mkdir(mode=0o700)
        return path

    def test_get_refuses_without_session_and_shows_explicit_empty_storage(self):
        self.assertEqual(self.request(token=None), (401, {"error": "sign-in is needed"}))
        status, body = self.request(token=self.token)
        self.assertEqual(status, 200)
        self.assertIsNone(body["model_storage"])
        self.assertEqual(body["locations"], [])

    def test_first_root_and_additional_location_save_directly(self):
        root, extra = self.usable("models"), self.usable("extra")
        status, saved = self.request("POST", {"action": "save-model-storage", "path": str(root)},
                                     token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(saved["available"])
        status, added = self.request("POST", {"action": "add-artifact-location", "path": str(extra)},
                                     token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(added["available"])
        status, body = self.request(token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["model_storage"]["path"], str(root))
        self.assertEqual([item["path"] for item in body["locations"]], [str(extra), str(root)])
        self.assertTrue(next(item for item in body["locations"] if item["path"] == str(root))["model_storage"])

    def test_location_listing_and_relocation_include_records_without_files(self):
        old, new = self.usable("old"), self.usable("new")
        saved = storage.save_model_storage(self.connection, old)
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, verified_at, created_at) VALUES ('empty', 'Incomplete', 'adoption',"
            " 'locally-hashed', ?, NULL, 'created')", (saved["location_id"],))
        status, listing = self.request(token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual([record["acquisition_id"] for record in listing["locations"][0]["records"]],
                         ["empty"])
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, result = self.request("POST", {"action": "confirm-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(result["records"][0]["available"])
        self.assertEqual(result["records"][0]["files"], [])
        self.assertIn("names no required file", result["records"][0]["reason"])

    def test_malformed_path_is_refused_and_first_save_keeps_no_change(self):
        status, body = self.request("POST", {"action": "save-model-storage", "path": "relative"},
                                    token=self.token)
        self.assertEqual(status, 409)
        self.assertIn("absolute path", body["error"])
        self.assertIsNone(storage.model_storage(self.connection))

    def test_preview_does_not_save_and_confirmation_commits_without_moving_files(self):
        old, new = self.usable("old"), self.usable("new")
        source = old / "model.gguf"
        source.write_bytes(b"fixture model")
        relocated = new / source.name
        relocated.write_bytes(source.read_bytes())
        saved = storage.save_model_storage(self.connection, old)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, verified_at, created_at) VALUES ('acq', 'Model', 'adoption',"
            " 'locally-hashed', ?, 'before', 'created')", (saved["location_id"],))
        self.connection.execute(
            "INSERT INTO acquisition_files VALUES ('acq', 'model.gguf', ?, NULL, ?, 'before')",
            (source.stat().st_size, digest))
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, preview = self.request("POST", {"action": "preview-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual((preview["path"], preview["new_path"]), (str(old), str(new)))
        self.assertTrue(preview["model_storage"])
        self.assertEqual(preview["changes"]["model_storage"],
                         {"from": str(old), "to": str(new)})
        self.assertEqual(storage.model_storage(self.connection), str(old))
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, result = self.request("POST", {"action": "confirm-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(storage.model_storage(self.connection), str(new))
        self.assertTrue(result["available"])
        self.assertTrue(result["records"][0]["available"])
        self.assertEqual(result["records"][0]["files"][0]["state"], "verified")
        self.assertTrue(source.exists())
        self.assertTrue(relocated.exists())
        self.assertEqual(source.read_bytes(), relocated.read_bytes())

    def test_additional_location_preview_changes_only_that_location(self):
        old, new = self.usable("old"), self.usable("new")
        saved = storage.add_artifact_location(self.connection, old)
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, preview = self.request("POST", {"action": "preview-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual((preview["path"], preview["new_path"]), (str(old), str(new)))
        self.assertFalse(preview["model_storage"])
        self.assertIsNone(preview["changes"]["model_storage"])
        self.assertEqual(preview["changes"]["artifact_location"],
                         {"from": str(old), "to": str(new)})
        self.assertEqual(storage.artifact_locations(self.connection), [str(old)])

    def test_unavailable_relocation_returns_each_failed_file(self):
        old, new = self.usable("old"), self.usable("new")
        file = old / "model.gguf"
        file.write_bytes(b"fixture model")
        saved = storage.save_model_storage(self.connection, old)
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, verified_at, created_at) VALUES ('acq', 'Model', 'adoption',"
            " 'locally-hashed', ?, 'before', 'created')", (saved["location_id"],))
        self.connection.execute(
            "INSERT INTO acquisition_files VALUES ('acq', 'model.gguf', ?, NULL, ?, 'before')",
            (file.stat().st_size, digest))
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, result = self.request("POST", {"action": "confirm-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(result["available"])
        self.assertIn("model.gguf", result["reason"])
        self.assertEqual(len(result["records"][0]["files"]), 1)
        file_result = result["records"][0]["files"][0]
        self.assertEqual((file_result["file_name"], file_result["state"]),
                         ("model.gguf", "missing"))
        self.assertIn("model.gguf is missing", file_result["reason"])
        self.assertEqual(self.connection.execute(
            "SELECT computed_sha256 FROM acquisition_files WHERE acquisition_id = 'acq'"
        ).fetchone()[0], digest)
        self.assertTrue(file.exists())
        self.assertEqual(list(new.iterdir()), [])

    def test_confirmation_rechecks_busy_download_and_csrf_is_required(self):
        old, new = self.usable("old"), self.usable("new")
        saved = storage.save_model_storage(self.connection, old)
        self.connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES ('download-1', 'download', 'unit', 'destination', 'now')")
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.STOPPED)):
            status, body = self.request("POST", {"action": "confirm-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 409)
        self.assertIn("download job", body["error"])
        self.assertIn("Runs & jobs", body["error"])
        self.assertEqual(storage.model_storage(self.connection), str(old))
        status, _body = self.request("POST", {"action": "add-artifact-location",
            "path": str(new)}, token=self.token, csrf=False)
        self.assertEqual(status, 403)
        status, _body = self.request("POST", {"action": "add-artifact-location",
            "path": str(new)}, token=self.token, origin=False)
        self.assertEqual(status, 403)
        self.assertNotIn(str(new), storage.artifact_locations(self.connection))

    def test_confirmation_refuses_agentic_serving_by_name(self):
        old, new = self.usable("old"), self.usable("new")
        saved = storage.save_model_storage(self.connection, old)
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.AGENTIC)):
            status, body = self.request("POST", {"action": "preview-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 409)
        self.assertIn("Stop serving", body["error"])
        with mock.patch("matuto.serving_actions.read_state", return_value=SimpleNamespace(state=admission.AGENTIC)):
            status, body = self.request("POST", {"action": "confirm-change",
                "location_id": saved["location_id"], "path": str(new)}, token=self.token)
        self.assertEqual(status, 409)
        self.assertIn("Stop serving", body["error"])
        self.assertEqual(storage.model_storage(self.connection), str(old))

    def test_failed_method_and_unknown_action_do_not_mutate(self):
        self.assertEqual(self.request("DELETE", token=self.token)[0], 409)
        self.assertEqual(self.request("POST", {"action": "unknown"}, token=self.token)[0], 409)
        self.assertIsNone(storage.model_storage(self.connection))


if __name__ == "__main__":
    unittest.main()
