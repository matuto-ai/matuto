"""Download origins page and protected HTTP operations."""

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlsplit

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import acquisition, download, login, runner, sessions, storage, store  # noqa: E402
from matuto.page import origins as origins_page  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402

COMMIT = "0123456789abcdef0123456789abcdef01234567"


class HuggingFaceFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path.endswith(f"/resolve/{COMMIT}/model.gguf"):
            if started := getattr(self.server, "transfer_started", None):
                started.set()
                self.server.release_transfer.wait(5)
            raw = b"data"
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        document = {"id": "fixture/model", "private": False, "gated": False,
                    "sha": COMMIT, "siblings": [{"rfilename": "model.gguf", "size": 4,
                    "lfs": {"sha256": hashlib.sha256(b"data").hexdigest()}}]}
        status = 200 if path.endswith(("/revision/main", f"/revision/{COMMIT}")) else 404
        body = document if status == 200 else {"error": "not found"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


class DownloadOriginsHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home.cleanup)
        store.open_store().close()
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf

    def request(self, path, *, method="GET", cookie=None, csrf=True,
                origin=MANAGER_ORIGIN, body=None):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
        data = None
        if method not in ("GET", "HEAD"):
            headers["Origin"] = origin
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            data = json.dumps(body or {}).encode()
        request = urllib.request.Request(self.address + path, method=method,
                                         headers=headers, data=data)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_empty_section_add_list_refusals_remove_and_session_protection(self):
        status, body, _ = self.request("/api/origins")
        self.assertEqual(status, 401)
        self.assertNotIn(b"origins", body)
        status, _body, _ = self.request("/api/origins", cookie=self.token,
                                        method="POST", csrf=False,
                                        body={"action": "add", "kind": "organisation", "name": "owner"})
        self.assertEqual(status, 403)
        status, _body, _ = self.request("/api/origins", cookie=self.token,
                                        method="POST", origin="http://evil.example",
                                        body={"action": "add", "kind": "organisation", "name": "owner"})
        self.assertEqual(status, 403)

        status, body, _ = self.request("/", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"New downloads are disabled until an origin is added.", body)
        self.assertIn(b"This is not model endorsement", body)
        self.assertIn(b"Private or gated repositories and other hosts are not supported.", body)
        for path in ("/origins.js", "/origins.css"):
            status, protected, _ = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(b"Create login", protected)
        status, script, _ = self.request("/origins.js", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"Remove origin", script)
        status, style, _ = self.request("/origins.css", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"grid-template-columns", style)

        before = self.request("/api/origins", cookie=self.token)[1]
        for kind, name, reason in (("organisation", "owner/repo", "one organisation name"),
                                   ("repository", "owner", "owner/repository"),
                                   ("organisation", "https://other.test/model", "one organisation name")):
            status, body, _ = self.request("/api/origins", cookie=self.token, method="POST",
                body={"action": "add", "kind": kind, "name": name})
            self.assertEqual(status, 409)
            self.assertIn(reason.encode(), body)
            self.assertEqual(self.request("/api/origins", cookie=self.token)[1], before)

        organisation = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "add", "kind": "organisation", "name": "owner"})
        repository = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "add", "kind": "repository", "name": "fixture/model"})
        self.assertEqual(organisation[0], 201)
        self.assertEqual(repository[0], 201)
        ids = [json.loads(organisation[1])["origin"]["origin_id"],
               json.loads(repository[1])["origin"]["origin_id"]]
        listed = self.request("/api/origins", cookie=self.token)
        self.assertEqual(listed[0], 200)
        self.assertEqual({item["name"] for item in json.loads(listed[1])["origins"]},
                         {"owner", "fixture/model"})
        duplicate = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "add", "kind": "repository", "name": "FIXTURE/Model"})
        self.assertEqual(duplicate[0], 409)
        self.assertIn(b"already configured", duplicate[1])

        fixture = ThreadingHTTPServer(("127.0.0.1", 0), HuggingFaceFixture)
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(fixture.server_close)
        self.addCleanup(fixture.shutdown)
        opened = store.open_store()
        try:
            connection = opened.connection()
            resolved = acquisition.resolve(connection, "fixture/model", "main", ["model.gguf"],
                api_base=f"http://127.0.0.1:{fixture.server_port}")
            self.assertEqual(resolved.repository, "fixture/model")
            class FakeRunner:
                def stop(_self, _work_id):
                    fake_download.state = "cancelled"

            fake_download = type("Download", (), {"state": "running"})()
            with mock.patch.object(runner, "Runner", return_value=FakeRunner()) as make_runner:
                status, body, _ = self.request("/api/origins", cookie=self.token, method="POST",
                    body={"action": "remove", "origin_id": ids[1]})
            self.assertEqual(status, 200, body)
            make_runner.assert_not_called()
            self.assertEqual(fake_download.state, "running")
            self.assertEqual(json.loads(self.request("/api/origins", cookie=self.token)[1])["origins"],
                             [json.loads(organisation[1])["origin"]])
            with self.assertRaisesRegex(acquisition.ResolutionError, "not admitted"):
                acquisition.resolve(connection, "fixture/model", "main", ["model.gguf"],
                    api_base=f"http://127.0.0.1:{fixture.server_port}")
        finally:
            opened.close()

        for action in ("add", "remove"):
            status, _body, _ = self.request("/api/origins", method="POST", cookie=self.token,
                csrf=False, body={"action": action})
            self.assertEqual(status, 403)

    def test_remove_during_download_preserves_completion_and_completed_provenance(self):
        added = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "add", "kind": "repository", "name": "fixture/model"})
        self.assertEqual(added[0], 201)
        origin_id = json.loads(added[1])["origin"]["origin_id"]
        fixture = ThreadingHTTPServer(("127.0.0.1", 0), HuggingFaceFixture)
        fixture.transfer_started = threading.Event()
        fixture.release_transfer = threading.Event()
        thread = threading.Thread(target=fixture.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(fixture.server_close)
        self.addCleanup(fixture.shutdown)
        opened = store.open_store()
        self.addCleanup(opened.close)
        connection = opened.connection()
        destination = Path(self.home.name) / "models"
        destination.mkdir(mode=0o700)
        storage.save_model_storage(connection, destination)
        api_base = f"http://127.0.0.1:{fixture.server_port}"
        resolved = acquisition.resolve(connection, "fixture/model", "main", ["model.gguf"],
                                       api_base=api_base)
        pinned = download._pin(resolved, str(destination), False, "acq-running-origin",
                               origin_id=origin_id, api_base=api_base)
        job_id = "download-0123456789abcdef"
        connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES (?, 'download', 'matuto-download-0123456789abcdef.service', ?, 'now')",
            (job_id, json.dumps(pinned, sort_keys=True, separators=(",", ":"))))
        outcome = []
        errors = []

        def run_download():
            worker_store = store.open_store()
            try:
                outcome.append(download.execute(worker_store.connection(), job_id,
                    report=lambda *_: None, echo=lambda *_: None))
            except BaseException as error:
                errors.append(error)
            finally:
                worker_store.close()

        worker = threading.Thread(target=run_download)
        worker.start()
        try:
            self.assertTrue(fixture.transfer_started.wait(5), "download did not reach the fixture")
            removed = self.request("/api/origins", cookie=self.token, method="POST",
                body={"action": "remove", "origin_id": origin_id})
            self.assertEqual(removed[0], 200, removed[1])
        finally:
            fixture.release_transfer.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcome, [0])
        acquisition_row = connection.execute(
            "SELECT origin_id FROM acquisition_records WHERE acquisition_id = 'acq-running-origin'")
        self.assertEqual(acquisition_row.fetchone()["origin_id"], origin_id)
        self.assertEqual(connection.execute(
            "SELECT enabled FROM download_origins WHERE origin_id = ?", (origin_id,)
        ).fetchone()["enabled"], 0)

        restored = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "add", "kind": "repository", "name": "fixture/model"})
        self.assertEqual(restored[0], 201)
        self.assertEqual(json.loads(restored[1])["origin"]["origin_id"], origin_id)
        removed = self.request("/api/origins", cookie=self.token, method="POST",
            body={"action": "remove", "origin_id": origin_id})
        self.assertEqual(removed[0], 200, removed[1])
        self.assertEqual(json.loads(self.request("/api/origins", cookie=self.token)[1])["origins"], [])
        self.assertIsNone(download._origin_id(connection, "fixture/model"))
        with self.assertRaisesRegex(acquisition.ResolutionError, "not admitted"):
            acquisition.resolve(connection, "fixture/model", "main", ["model.gguf"],
                                api_base=api_base)


class DownloadOriginsPageTest(unittest.TestCase):
    def test_section_names_empty_state_and_direct_add_control(self):
        self.assertEqual((origins_page.PAGE, origins_page.NAME, origins_page.TITLE,
                          origins_page.ORDER), ("settings", "origins", "Download origins", 2))
        markup = origins_page.html({})
        self.assertIn("New downloads are disabled until an origin is added.", markup)
        self.assertIn("Add origin", markup)
        self.assertIn('<link rel="stylesheet" href="/origins.css">', markup)
        self.assertIn('<script src="/origins.js" defer></script>', markup)
        self.assertNotIn("Review", markup)
