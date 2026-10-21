"""Argument catalogue CRUD and profile-use conversion through the store and HTTP seams."""

import json
import os
import sqlite3
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(Path(__file__).parent))

from matuto import login, router, sessions, store, voice  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402
from test_record_validation import RecordTestCase  # noqa: E402


class ArgumentCatalogueStoreTest(RecordTestCase):
    def add_uses(self):
        self.insert_language_profile()
        self.connection.execute(
            "INSERT INTO argument_catalogue VALUES ('arg-context', '--ctx-size', 1,"
            " 'Context size', 'server,smoke,bench', '')")
        self.connection.executemany(
            "INSERT INTO profile_arguments VALUES ('prof-coder', ?, ?, 'arg-context', '--ctx-size', ?)",
            (("server", 0, "65536"), ("smoke", 0, "65536"), ("bench", 0, "65536")))

    def test_removal_converts_all_rows_in_place_without_changing_argv_or_fingerprint(self):
        self.add_uses()
        before = store.read_record(self.store, "language", "prof-coder")
        fingerprint = store.fingerprint(self.store, before)

        removed = store.remove_argument_catalogue(self.store, "arg-context")

        after = store.read_record(self.store, "language", "prof-coder")
        self.assertEqual([row["option"] for row in after["arguments"]], ["--ctx-size"] * 3)
        self.assertEqual([row["value"] for row in after["arguments"]], ["65536"] * 3)
        self.assertEqual([row["tool"] for row in after["arguments"]], ["bench", "server", "smoke"])
        self.assertEqual(store.fingerprint(self.store, after), fingerprint)
        self.assertEqual(len(removed["converted"]), 3)
        self.assertEqual({row["name"] for row in removed["converted"]}, {"Qwen3.6 Coder 192K"})
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM argument_catalogue WHERE argument_id = 'arg-context'").fetchone()[0], 0)

    def test_failed_removal_rolls_back_both_entry_and_row_conversion(self):
        self.add_uses()
        self.connection.execute("CREATE TRIGGER fail_catalogue_delete BEFORE DELETE ON argument_catalogue"
                                " BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")

        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture failure"):
            store.remove_argument_catalogue(self.store, "arg-context")

        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM argument_catalogue WHERE argument_id = 'arg-context'").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM profile_arguments WHERE argument_id = 'arg-context'").fetchone()[0], 3)

    def test_edit_keeps_profile_rows_while_duplicate_and_invalid_entries_are_refused(self):
        self.add_uses()
        self.connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        profile = {"profile_id": "prof-coder", "name": "Qwen3.6 Coder 192K"}
        router._launch_check(self.store, "GPU0", "--ctx-size N")
        voice._launch_check_profile(self.store, profile, "GPU0", "--ctx-size N")
        rows_before = [tuple(row) for row in self.connection.execute(
            "SELECT tool, position, argument_id, flag, value FROM profile_arguments"
            " WHERE argument_id = 'arg-context' ORDER BY tool")]

        store.save_argument_catalogue(self.store, {
            "flag": "--context-size", "takes_value": True, "description": "Updated", "tools": ["server"]
        }, argument_id="arg-context")

        rows_after = [tuple(row) for row in self.connection.execute(
            "SELECT tool, position, argument_id, flag, value FROM profile_arguments"
            " WHERE argument_id = 'arg-context' ORDER BY tool")]
        self.assertEqual(rows_after, rows_before)
        saved = store.read_record(self.store, "language", "prof-coder")
        self.assertEqual({row["option"] for row in saved["arguments"]}, {"--ctx-size"})
        store.validate_record(saved, self.connection, detected_gpus=self.detected,
                              help_text={tool: "--ctx-size N" for tool in ("server", "smoke", "bench")})
        router._launch_check(self.store, "GPU0", "--ctx-size N")
        voice._launch_check_profile(self.store, profile, "GPU0", "--ctx-size N")
        store.save_argument_catalogue(self.store, {
            "flag": "--context-size", "takes_value": False, "description": "Updated", "tools": ["server"]
        }, argument_id="arg-context")
        router._launch_check(self.store, "GPU0", "--ctx-size N")
        voice._launch_check_profile(self.store, profile, "GPU0", "--ctx-size N")
        with self.assertRaisesRegex(store.RecordRefusal, "already in use"):
            store.save_argument_catalogue(self.store, {
                "flag": "--context-size", "takes_value": False, "description": "Another", "tools": ["bench"]
            })
        for invalid in (
            {"flag": "--bad=value", "takes_value": True, "description": "x", "tools": ["server"]},
            {"flag": "--other", "takes_value": True, "description": " ", "tools": ["server"]},
            {"flag": "--other", "takes_value": True, "description": "x", "tools": []},
        ):
            with self.subTest(entry=invalid), self.assertRaises(store.RecordRefusal):
                store.save_argument_catalogue(self.store, invalid)


class ArgumentCatalogueHttpTest(RecordTestCase):
    def setUp(self):
        super().setUp()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
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

    def request(self, path, *, method="GET", body=None, origin=MANAGER_ORIGIN, csrf=None, token=None):
        headers = {"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={token or self.token}"}
        if method != "GET":
            if origin is not None:
                headers["Origin"] = origin
            if csrf is not False:
                headers["X-CSRF-Token"] = self.csrf if csrf is None else csrf
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.address + path, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_http_lists_adds_edits_and_removes_with_named_converted_rows(self):
        status, listed = self.request("/api/argument-catalogue")
        self.assertEqual(status, 200)
        self.assertEqual({row["flag"] for row in listed["entries"]}, {"--parallel", "--single-turn"})
        entry = {"flag": "--ctx-size", "takes_value": True, "description": "Context size",
                 "tools": ["server", "smoke"]}
        status, created = self.request("/api/argument-catalogue", method="POST",
                                       body={"action": "add", "entry": entry})
        self.assertEqual(status, 200)
        self.assertTrue(created["argument_id"].startswith("arg-"))
        self.insert_language_profile()
        connection = store.open_store(self.config)
        try:
            connection.connection().execute(
                "INSERT INTO profile_arguments VALUES ('prof-coder', 'server', 0, ?, '--ctx-size', '65536')",
                (created["argument_id"],))
        finally:
            connection.close()
        status, updated = self.request("/api/argument-catalogue", method="POST", body={
            "action": "edit", "argument_id": created["argument_id"],
            "entry": {**entry, "description": "Updated parallel slots"}})
        self.assertEqual(status, 200, updated)
        status, removed = self.request("/api/argument-catalogue", method="POST", body={
            "action": "remove", "argument_id": created["argument_id"]})
        self.assertEqual(status, 200, removed)
        self.assertEqual([(row["name"], row["tool"], row["flag"], row["value"])
                          for row in removed["converted"]],
                         [("Qwen3.6 Coder 192K", "server", "--ctx-size", "65536")])

    def test_malformed_and_duplicate_http_adds_leave_the_catalogue_unchanged(self):
        before, listed = self.request("/api/argument-catalogue")
        self.assertEqual(before, 200)
        entry = {"flag": "--parallel", "takes_value": True, "description": "Duplicate",
                 "tools": ["server"]}
        for candidate in ({**entry, "flag": "--bad=value"}, entry):
            with self.subTest(entry=candidate):
                status, refusal = self.request("/api/argument-catalogue", method="POST",
                                               body={"action": "add", "entry": candidate})
                self.assertEqual(status, 409)
                self.assertTrue(refusal["error"])
                status, after = self.request("/api/argument-catalogue")
                self.assertEqual(status, 200)
                self.assertEqual(after, listed)

    def test_mutations_refuse_missing_origin_or_csrf(self):
        body = {"action": "add", "entry": {"flag": "--ctx-size", "takes_value": True,
                "description": "Context size", "tools": ["server"]}}
        for origin, csrf in ((None, self.csrf), (MANAGER_ORIGIN, False)):
            with self.subTest(origin=origin, csrf=csrf):
                status, _ = self.request("/api/argument-catalogue", method="POST", body=body,
                                         origin=origin, csrf=csrf)
                self.assertEqual(status, 403)
        status, _ = self.request("/api/argument-catalogue", method="POST", body=body,
                                 token="not-a-session")
        self.assertEqual(status, 401)
