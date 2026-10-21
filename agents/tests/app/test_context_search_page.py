"""The context-search page exposes the rules' rungs and stored search evidence."""

import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store
from matuto.page import context_search as page
from matuto.routes import context_search as route, runs as runs_route
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server


def gguf_fixture(context):
    def text(value):
        encoded = value.encode()
        return struct.pack("<Q", len(encoded)) + encoded
    metadata = (text("general.architecture") + struct.pack("<I", 8) + text("qwen3")
                + text("qwen3.context_length") + struct.pack("<II", 4, context))
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + metadata


class ContextSearchPageTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home.cleanup)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        connection = self.opened.connection()
        self.model_root = Path(self.home.name) / "models"
        self.model_root.mkdir()
        self.model_path = self.model_root / "studio.gguf"
        self.set_trained_context(196608)
        connection.execute("INSERT INTO artifact_locations VALUES ('model-root',?,'','now')",
                           (str(self.model_root),))
        connection.execute("INSERT INTO model_profiles (profile_id,kind,name,alias,context_size,created_at)"
                           " VALUES ('language-1','language','Studio','studio',65536,'now')")
        connection.execute("INSERT INTO acquisition_records (acquisition_id,name,source,location_id,created_at)"
                           " VALUES ('model-acq','Studio','adoption','model-root','now')")
        digest = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        connection.execute("INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,computed_sha256)"
                           " VALUES ('model-acq','studio.gguf',?,?)",
                           (self.model_path.stat().st_size, digest))
        connection.execute("INSERT INTO profile_artifacts VALUES ('language-1','model','model-acq','studio.gguf')")
        connection.execute("INSERT INTO bundles (bundle_id,name,created_at) VALUES ('bundle-1','Voice','now')")
        connection.execute("INSERT INTO bundle_members (bundle_id,role,profile_id)"
                           " VALUES ('bundle-1','language','language-1')")
        login.create_login("operator", "secret", "secret")
        self.token, _expiry = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"

    def set_trained_context(self, context):
        self.model_path.write_bytes(gguf_fixture(context))
        if hasattr(self, "opened"):
            connection = self.opened.connection()
            connection.execute("UPDATE acquisition_files SET size_bytes=?,computed_sha256=?"
                               " WHERE acquisition_id='model-acq' AND file_name='studio.gguf'",
                               (self.model_path.stat().st_size,
                                hashlib.sha256(self.model_path.read_bytes()).hexdigest()))
            connection.commit()

    def http_get(self, path, authenticated=True):
        headers = {"Host": ALLOWED_HOST}
        if authenticated:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        request = urllib.request.Request(self.address + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_page_is_a_runs_section_with_an_addressable_form_and_result_lists(self):
        self.assertEqual((page.PAGE, page.NAME, page.TITLE), ("runs", "context-search", "Context search"))
        markup = page.html({})
        for expected in ("Start a context search", "Start rung", "Current context searches",
                         "Context-search history", "/context-search.js", "/context-search.css"):
            self.assertIn(expected, markup)

    def test_api_lists_only_offered_rungs_for_profiles_and_bundle_language_members(self):
        status, body = self.http_get("/api/context-search", authenticated=False)
        self.assertEqual(status, 401)
        status, body = self.http_get("/api/context-search")
        self.assertEqual(status, 200)
        data = json.loads(body)
        records = {record["id"]: record for record in data["records"]}
        expected = [196608, 131072, 98304]
        self.assertEqual(records["language-1"]["rungs"], expected)
        self.assertEqual(records["bundle-1"]["rungs"], expected)
        self.assertEqual(records["language-1"]["trained_context"], 196608)
        self.assertEqual(records["bundle-1"]["trained_context"], 196608)
        self.assertEqual(records["bundle-1"]["language_id"], "language-1")

    def test_api_uses_trained_metadata_for_floor_not_editable_profile_context(self):
        self.opened.connection().execute(
            "UPDATE model_profiles SET context_size=196608 WHERE profile_id='language-1'")
        self.opened.connection().commit()
        self.set_trained_context(65536)
        with mock.patch.object(route.route_gate, "require_session", return_value=None):
            status, data = route.handle(type("Request", (), {"method": "GET"})())
        record = next(item for item in data["records"] if item["id"] == "language-1")
        self.assertEqual(status, 200)
        self.assertEqual(record["trained_context"], 65536)
        self.assertEqual(record["rungs"], [])

    def test_api_disables_search_when_trained_context_metadata_is_unreadable(self):
        self.model_path.write_bytes(b"not GGUF metadata")
        with mock.patch.object(route.route_gate, "require_session", return_value=None):
            status, data = route.handle(type("Request", (), {"method": "GET"})())
        record = next(item for item in data["records"] if item["id"] == "language-1")
        self.assertEqual(status, 200)
        self.assertIsNone(record["trained_context"])
        self.assertEqual(record["rungs"], [])

    def test_api_renders_recorded_winner_and_rung_checks_without_redeciding(self):
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO runs (run_id,record_kind,record_id,record_label,procedure,procedure_revision,"
            "outcome,started_at,ended_at) VALUES ('search-1','language','language-1','Studio',"
            "'context_search','v1','completed','now','later')")
        connection.execute(
            "INSERT INTO runs (run_id,record_kind,record_id,record_label,procedure,procedure_revision,"
            "outcome,started_at,ended_at) VALUES ('rung-1','language','language-1','Studio','wq','v1',"
            "'completed','now','later')")
        summary = {"outcome": "winner", "winner": 196608, "checked": True,
                   "rungs": [{"run_id": "rung-1", "context": 196608, "label": "192K",
                              "fingerprint": "fp", "outcome": "completed",
                              "checks": [{"name": "WQ-06", "status": "PASS"}]}]}
        body = json.dumps(summary).encode()
        connection.execute("INSERT INTO run_evidence (run_id,name,media_type,size_bytes,sha256,body)"
                           " VALUES ('search-1','context-search.json','application/json',?,?,?)",
                           (len(body), hashlib.sha256(body).hexdigest(), body))
        connection.execute("INSERT INTO checks (run_id,sequence,name,status,detail)"
                           " VALUES ('rung-1',0,'WQ-06','PASS','passed')")
        with mock.patch.object(route.route_gate, "require_session", return_value=None):
            status, data = route.handle(type("Request", (), {"method": "GET"})())
        self.assertEqual(status, 200)
        search = data["searches"][0]
        self.assertEqual((search["winner"], search["checked"]), (196608, True))
        self.assertEqual(search["children"][0]["checks"], [
            {"sequence": 0, "name": "WQ-06", "status": "PASS", "detail": "passed"}])
        self.assertTrue(search["children"][0]["winner"])

        connection.execute(
            "INSERT INTO runs (run_id,record_kind,record_id,record_label,procedure,procedure_revision,"
            "outcome,started_at,ended_at) VALUES ('search-2','language','language-1','Studio',"
            "'context_search','v1','completed','now','later')")
        connection.execute(
            "INSERT INTO runs (run_id,record_kind,record_id,record_label,procedure,procedure_revision,"
            "outcome,started_at,ended_at) VALUES ('rung-2','language','language-1','Studio','wq','v1',"
            "'completed','now','later')")
        no_winner = {"outcome": "no winner", "winner": None,
                     "rungs": [{"run_id": "rung-2", "context": 98304, "label": "96K",
                                "outcome": "completed", "checks": []}]}
        body = json.dumps(no_winner).encode()
        connection.execute("INSERT INTO run_evidence (run_id,name,media_type,size_bytes,sha256,body)"
                           " VALUES ('search-2','context-search.json','application/json',?,?,?)",
                           (len(body), hashlib.sha256(body).hexdigest(), body))

        def work(run_id, kind, outcome):
            return SimpleNamespace(work_id=run_id, kind=kind, purpose=f"{kind} for Studio",
                record_id="language-1", record_label="Studio", outcome=outcome, state=outcome,
                started_at="now", ended_at="later")
        with mock.patch.object(runs_route, "load_item",
                                return_value=SimpleNamespace(form_defaults=lambda *_args: {})):
            entries = runs_route._serialize(self.opened, SimpleNamespace(
                works=lambda: [work("search-1", "context_search", "completed"),
                               work("rung-1", "wq", "completed"),
                               work("search-2", "context_search", "completed"),
                               work("rung-2", "wq", "completed")],
                progress=lambda _work: None))["entries"]
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(by_id["search-1"]["context_search"],
                         {"role": "search", "result": "192K", "winner": True})
        self.assertEqual(by_id["rung-1"]["context_search"],
                         {"role": "rung", "rung": "192K", "result": "192K", "winner": True})
        self.assertEqual(by_id["search-2"]["context_search"],
                         {"role": "search", "result": "no winner", "winner": False})
        self.assertEqual(by_id["rung-2"]["context_search"],
                         {"role": "rung", "rung": "96K", "result": "no winner", "winner": False})


if __name__ == "__main__":
    unittest.main()
