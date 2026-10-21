"""The Profiles import/export section uses the protected manager and shared store API."""

import contextlib
import io
import json
import os
import socket
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from test_import_export import ImportExportTest

from matuto import cli, login, sessions, store
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server


class ImportExportHttpTest(unittest.TestCase):
    def setUp(self):
        ImportExportTest.setUp(self)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.inventory = {"state": "completed", "devices": [{"pciAddress": "0000:04:00.0",
                                                                   "vramBytes": 100}]}
        detection = mock.patch("matuto.cards.detect", return_value=self.inventory)
        detection.start()
        self.addCleanup(detection.stop)
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        # Faster polling avoids waiting up to half a second at each test server shutdown.
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf

    def import_document(self, document):
        return store.import_records(self.opened, document, help_text={
            "server": "", "smoke": "", "bench": ""})

    def request(self, path, *, method="GET", body=None, token=True, csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if token:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        if method not in ("GET", "HEAD"):
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        data = body
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.address + path, method=method, headers=headers, data=data)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_section_import_refusal_and_exports_share_one_http_fixture(self):
        status, body, _headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Import / export", body)
        self.assertIn(b"/import-export.js", body)
        self.assertIn(b"Download all records as JSON file", body)
        status, script, _headers = self.request("/import-export.js")
        self.assertEqual(status, 200)
        self.assertIn(b"/api/import-export", script)

        broken = json.loads(json.dumps(self.document))
        next(row for row in broken["records"] if row["kind"] == "bundle")["members"]["recognition"] = "missing"
        status, body, _headers = self.request("/api/import-export", method="POST", body=broken)
        self.assertEqual(status, 409)
        error = json.loads(body)["error"]
        self.assertIn("bundle-new", error)
        self.assertIn("members.recognition", error)
        self.assertEqual(self.opened.connection().execute("SELECT count(*) FROM cards").fetchone()[0], 0)

        status, body, _headers = self.request("/api/import-export", method="POST", body=self.document)
        self.assertEqual(status, 200, body)
        results = json.loads(body)["results"]
        self.assertEqual([(row["kind"], row["action"]) for row in results], [
            ("card", "created"), ("language", "created"), ("synthesis", "created"),
            ("recognition", "created"), ("bundle", "created")])
        self.assertFalse(store.read_record(self.opened, "language", "language-new")["published"])
        self.assertFalse(store.read_record(self.opened, "bundle", "bundle-new").get("qualification"))

        status, listing, _headers = self.request("/api/import-export")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(listing)["records"]), 5)
        for path, expected_count in (("/api/import-export?download=1", 5),
                                     ("/api/import-export?download=1&record=language%3Alanguage-new", 1)):
            status, body, headers = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("attachment; filename=\"matuto-records.json\"", headers["Content-Disposition"])
            document = json.loads(body)
            self.assertEqual(len(document["records"]), expected_count)
            self.assertTrue(all("published" not in row and "qualification" not in row for row in document["records"]))
            if expected_count == 5:
                path = self.home / "page-export.json"
                path.write_bytes(body)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["matuto", "import", str(path)]), 0)

    def test_cli_export_import_round_trips_and_http_matches_utf8_contract(self):
        self.import_document(self.document)
        path = self.home / "cli-export.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["matuto", "export", str(path), "language:language-new"]), 0)
        cli_document = json.loads(path.read_text(encoding="utf-8"))
        status, body, _headers = self.request("/api/import-export", method="POST", body=cli_document)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["results"][0]["action"], "unchanged")

        encoded = json.dumps(self.document).encode("utf-16")
        invalid = self.home / "utf16-import.json"
        invalid.write_bytes(encoded)
        changes = self.opened.connection().total_changes
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["matuto", "import", str(invalid)]), 1)
        status, body, _headers = self.request("/api/import-export", method="POST", body=encoded)
        self.assertEqual(status, 409)
        self.assertIn("utf-8", json.loads(body)["error"])
        self.assertEqual(self.opened.connection().total_changes, changes)

    def test_malformed_or_oversized_uploads_and_protected_routes_change_nothing(self):
        before = self.opened.connection().total_changes
        for options in ({"token": False}, {"csrf": False}, {"origin": False}):
            status, _body, _headers = self.request("/api/import-export", method="POST",
                                                     body=self.document, **options)
            self.assertEqual(status, 403 if options.get("token", True) else 401)
        status, body, _headers = self.request("/api/import-export", method="POST", body=b"not json")
        self.assertEqual(status, 409)
        self.assertIn("malformed", json.loads(body)["error"])
        status, body, _headers = self.request("/api/import-export", method="POST",
                                              body=b'{"version":1,"version":1,"records":[]}')
        self.assertEqual(status, 409)
        self.assertIn("duplicate JSON field", json.loads(body)["error"])
        status, body, _headers = self.request("/api/import-export", method="POST",
                                               body=b" " * (1024 * 1024 + 1))
        self.assertEqual(status, 400)
        self.assertIn("1 MiB limit", json.loads(body)["error"])
        request = (f"POST /api/import-export HTTP/1.1\r\nHost: {ALLOWED_HOST}\r\n"
                   f"Cookie: {SESSION_COOKIE}={self.token}\r\nOrigin: {MANAGER_ORIGIN}\r\n"
                   f"X-CSRF-Token: {self.csrf}\r\nTransfer-Encoding: chunked\r\n\r\n"
                   "0\r\n\r\n").encode()
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall(request)
            chunks = []
            while chunk := connection.recv(4096):
                chunks.append(chunk)
            chunked_response = b"".join(chunks)
        self.assertIn(b"400", chunked_response)
        self.assertIn(b"1 MiB limit", chunked_response)
        status, _body, _headers = self.request("/api/import-export?download=1", token=False)
        self.assertEqual(status, 401)
        self.assertEqual(self.opened.connection().execute("SELECT count(*) FROM cards").fetchone()[0], 0)
        self.assertEqual(self.opened.connection().total_changes, before)


if __name__ == "__main__":
    unittest.main()
