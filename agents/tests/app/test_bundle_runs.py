"""The bundle-run section calls the shared authenticated runner routes."""

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import urllib.error
import urllib.request

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, login, recipes, router, runner, sessions, serving_state, store
from matuto.page import bundle_runs
from matuto.routes import run_stop_serving, runs
from matuto.procedures import bundle as bundle_procedure
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server


class BundleRunsHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home.cleanup)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
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
        self.unit_starts = []
        self.serving_states = [admission.STOPPED]
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(
            runner.Runner, "_answers", return_value=SimpleNamespace(returncode=0)))
        self.patches.enter_context(mock.patch.object(
            runner.Runner, "_start_unit", side_effect=self.start_unit))
        self.patches.enter_context(mock.patch.object(
            runner.Runner, "_unit_state", side_effect=self.unit_state))
        self.patches.enter_context(mock.patch.object(
            serving_state, "read", side_effect=self.read_serving))
        self.patches.enter_context(mock.patch.object(router, "router_pid", return_value=0))
        self.patches.enter_context(mock.patch.object(router, "_unit_pid", return_value=0))
        self.patches.enter_context(mock.patch.object(router, "foreign_holders", return_value=[]))

    def request(self, path, *, method="GET", cookie=None, csrf=True, body=None):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
        data = None
        if method not in ("GET", "HEAD"):
            headers["Origin"] = MANAGER_ORIGIN
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

    def seed_bundle(self):
        connection = self.opened.connection()
        connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,"
            "notes,created_at) VALUES ('card','GPU','0000:01:00.0',32000,1,1,1,1,1,'','now')")
        connection.execute("INSERT INTO artifact_locations VALUES ('files','/fixture','', 'now')")
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id,name,source,checksum_status,"
            "location_id,verified_at,created_at) VALUES ('acq','fixture','adoption',"
            "'locally-hashed','files','now','now')")
        for filename, body in (("model.gguf", b"model"), ("speech.bin", b"speech")):
            connection.execute(
                "INSERT INTO acquisition_files (acquisition_id,file_name,size_bytes,"
                "computed_sha256,verified_at) VALUES ('acq',?,?,?,'now')",
                (filename, len(body), hashlib.sha256(body).hexdigest()))
        for role in ("synthesis", "recognition"):
            backend, recipe = (("vulkan", recipes.BREEZE) if role == "synthesis"
                               else ("cpu", recipes.WHISPER))
            connection.execute(
                "INSERT INTO speech_executables VALUES (?,?,?,?,?,?,?,?)",
                (f"exe-{role}", role, "files", f"/fixture/{role}", "a" * 64,
                 "fixture", json.dumps({"backend": backend, "recipe": recipe}), "now"))
        for identifier, kind, executable, card in (
                ("lang", "language", None, "card"),
                ("synth", "synthesis", "exe-synthesis", "card"),
                ("recognition", "recognition", "exe-recognition", None)):
            connection.execute(
                "INSERT INTO model_profiles (profile_id,kind,name,alias,context_size,card_id,"
                "speech_executable_id,created_at) VALUES (?,?,?, ?,4096,?,?, 'now')",
                (identifier, kind, identifier, "fixture" if kind == "language" else "",
                 card, executable))
            filename, artifact_role = (("model.gguf", "model") if kind == "language"
                                       else ("speech.bin", "weights"))
            connection.execute("INSERT INTO profile_artifacts VALUES (?,?, 'acq',?)",
                               (identifier, artifact_role, filename))
        connection.execute(
            "INSERT INTO bundles (bundle_id, name, load_tokens, load_prompt, created_at)"
            " VALUES ('bundle-1', 'Voice', 8, 'Hello', 'now')")
        for role, profile in (("language", "lang"), ("synthesis", "synth"),
                              ("recognition", "recognition")):
            connection.execute("INSERT INTO bundle_members VALUES ('bundle-1',?,?)", (role, profile))

    def start_unit(self, request, work_id, unit):
        self.unit_starts.append((request, work_id, unit))
        return SimpleNamespace(returncode=0)

    def unit_state(self, _unit):
        return {runner.LOAD_STATE: "loaded", runner.ACTIVE_STATE: "active",
                runner.SUB_STATE: "running", runner.RESULT: "", runner.EXIT_STATUS: "0",
                runner.END_MOMENT: "1"}

    def read_serving(self):
        if len(self.serving_states) > 1:
            state = self.serving_states.pop(0)
        else:
            state = self.serving_states[0]
        return serving_state.State(state)

    def test_http_bundle_page_admission_and_busy_refusal_use_actual_store_and_runner(self):
        self.seed_bundle()
        self.assertEqual((bundle_runs.PAGE, bundle_runs.NAME, bundle_runs.TITLE),
                         ("runs", "bundle-runs", "Bundle runs"))
        self.assertIn('<script src="/bundle-runs.js" defer></script>', bundle_runs.html({}))
        self.assertIn('href="/bundle-runs.css"', bundle_runs.html({}))
        status, body, _ = self.request("/", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"Bundle runs", body)
        status, body, _ = self.request("/bundle-runs.js", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"procedure: 'bundle'", body)
        status, body, _ = self.request("/bundle-runs.css", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"max-width:36rem", body)
        status, _body, _ = self.request("/api/runs")
        self.assertEqual(status, 401)
        status, _body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                                        csrf=False, body={"action": "start"})
        self.assertEqual(status, 403)
        for procedure, record in (("bundle", "synth"), ("speech qualify", "bundle-1")):
            with self.subTest(procedure=procedure, record=record):
                status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                    body={"action": "start", "procedure": procedure, "record": record})
                self.assertEqual(status, 409, body)
                self.assertEqual(self.unit_starts, [])
                self.assertEqual(self.opened.connection().execute(
                    "SELECT count(*) FROM runs").fetchone()[0], 0)

        target = runs.record_target(self.opened, "bundle-1")
        expected_fingerprint = store.fingerprint(
            self.opened, store.read_record(self.opened, "bundle", "bundle-1"))
        self.assertEqual(target.fingerprint, expected_fingerprint)

        status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
            body={"action": "start", "procedure": "bundle", "record": "bundle-1"})
        self.assertEqual(status, 202, body)
        work_id = json.loads(body)["work"]
        self.assertEqual(len(self.unit_starts), 1)
        request = self.unit_starts[0][0]
        self.assertEqual((request.kind, request.record_kind, request.record_id, request.fingerprint),
                         ("bundle", "bundle", "bundle-1", expected_fingerprint))
        row = self.opened.connection().execute(
            "SELECT procedure, record_kind, record_id, fingerprint FROM runs WHERE run_id = ?",
            (work_id,)).fetchone()
        self.assertEqual(tuple(row), ("bundle", "bundle", "bundle-1", expected_fingerprint))

        status, body, _ = self.request("/api/runs", cookie=self.token)
        self.assertEqual(status, 200, body)
        listing = json.loads(body)
        active = next(entry for entry in listing["entries"] if entry["id"] == work_id)
        self.assertEqual((active["procedure"], active["record"], active["state"]),
                         ("bundle", "Voice", "running"))
        self.assertEqual({(item["id"], item["kind"]) for item in listing["records"]},
                         {("bundle-1", "bundle"), ("lang", "language")})

        status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
            body={"action": "start", "procedure": "bundle", "record": "bundle-1"})
        self.assertEqual(status, 409, body)
        refusal = json.loads(body)["error"]
        self.assertIn("bundle for Voice", refusal)
        self.assertEqual(len(self.unit_starts), 1)

    def test_stop_serving_offer_and_confirmation_use_real_http_handlers_and_same_stored_bundle(self):
        self.seed_bundle()
        self.serving_states = [admission.VOICE]
        status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
            body={"action": "start", "procedure": "bundle", "record": "bundle-1"})
        self.assertEqual(status, 409, body)
        self.assertTrue(json.loads(body)["offer"])
        self.assertEqual(self.unit_starts, [])
        self.assertEqual(self.opened.connection().execute("SELECT count(*) FROM runs").fetchone()[0], 0)

        self.serving_states = [admission.VOICE, admission.VOICE, admission.STOPPED,
                               admission.STOPPED, admission.STOPPED]
        with mock.patch.object(run_stop_serving.voice, "activate",
                               return_value={"stopped": True}) as stop:
            status, body, _ = self.request("/api/runs/stop-serving", method="POST", cookie=self.token,
                body={"confirm": True, "procedure": "bundle", "record": "bundle-1"})
        self.assertEqual(status, 202, body)
        work_id = json.loads(body)["work"]
        stop.assert_called_once_with(run_stop_serving.serving.STOP_SERVING, confirmed=True)
        self.assertEqual(len(self.unit_starts), 1)
        request = self.unit_starts[0][0]
        row = self.opened.connection().execute(
            "SELECT procedure, record_kind, record_id, fingerprint FROM runs WHERE run_id = ?",
            (work_id,)).fetchone()
        self.assertEqual(tuple(row), ("bundle", "bundle", "bundle-1", request.fingerprint))
        self.assertEqual((request.record_id, request.fingerprint),
                         ("bundle-1", runs.record_target(self.opened, "bundle-1").fingerprint))



class BundleCheckDetailsTest(unittest.TestCase):
    def test_bundle_recognition_check_reports_text_presence_separately_from_word_matches(self):
        summary = {"performed": ["model_launch", "synthesis_launch", "recognition_launch"],
            "parts": [{"role": role, "profile": role} for role in
                      ("language", "synthesis", "recognition")],
            "combined_occupancy_bytes": 10, "card": {"capacity_bytes": 20},
            "synthesis": {"duration_nanoseconds": 900, "wall_nanoseconds": 400},
            "generation": {"spanned_synthesis": True},
            "decision": {"recognition": {"status": "measured", "text": "unrelated", "word_count": 0,
                                           "wall_nanoseconds": 400, "audio_duration_nanoseconds": 900,
                                           "passed": True}}}
        checks, all_started = bundle_procedure._checks(summary)
        self.assertTrue(all_started)
        self.assertEqual(checks[2]["status"], "PASS")
        self.assertEqual(checks[2]["detail"],
            "text non-empty; matching words 0; recognition time 400 ns; clip duration 900 ns")


if __name__ == "__main__":
    unittest.main()
