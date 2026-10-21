"""Overview serving data is read through the protected manager HTTP interface."""

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

from matuto import login, sessions, store  # noqa: E402
from matuto.discovery import load_item  # noqa: E402
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server  # noqa: E402


class OverviewServingHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.route = load_item("routes", "overview_serving")
        self.server.RequestHandlerClass.routes["/api/overview/serving"] = self.route.handle
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}/api/overview/serving"

    def get(self, session=True):
        headers = {"Host": ALLOWED_HOST}
        if session:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        request = urllib.request.Request(self.address, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        with response:
            return response.status, json.loads(response.read())

    def state_response(self, state, profile_data=None, *, runtime=None, storage=None):
        voice_options = [{"id": "bundle-one", "name": "Studio voice", "qualification": "qualified",
                          "available": True, "offered": True, "reasons": []}]
        parts = (SimpleNamespace(profile="speech-one", role="synthesis", state="active", stray=False,
                                 address="http://127.0.0.1:18081"),) if state in ("voice", "voice_partial") else ()
        reported_state = {"agentic_unhealthy": "agentic", "voice_partial": "voice",
                          "transitioning": "unknown"}.get(state, state)
        healthy = state not in ("agentic_unhealthy", "voice_partial", "unknown", "transitioning")
        reason = "the router is transitioning" if state == "transitioning" else (
            "the router health check failed" if state == "agentic_unhealthy" else
            "unit is unknown" if state == "unknown" else "")
        units = ((self.route.serving_state.VOICE_ROUTER_UNIT, "active"),) if state in ("voice", "voice_partial") else (
            ((self.route.serving_state.ROUTER_UNIT, "active"),) if reported_state == "agentic" else ())
        report = SimpleNamespace(state=reported_state, healthy=healthy, reason=reason,
                                 model="model-one" if reported_state == "agentic" else "",
                                 bundle="bundle-one" if reported_state == "voice" else "", parts=parts, units=units,
                                 address="http://127.0.0.1:8888" if reported_state == "agentic" else
                                 "http://127.0.0.1:8887" if reported_state == "voice" else "")
        work = {"name": "current_work", "type": "current-work", "label": "Nothing running", "href": "#/runs"}
        patchers = [mock.patch.object(self.route.serving_state, "read", return_value=report),
                   mock.patch.object(self.route.runner, "_current_work", return_value=None),
                   mock.patch.object(self.route.current_work_status, "report", return_value=work),
                   mock.patch.object(self.route.voice, "bundle_options", return_value=voice_options),
                   mock.patch.object(self.route.voice, "_runtime", return_value=runtime or self.route.serving.Runtime(True)),
                   mock.patch.object(self.route.voice, "_storage", return_value=storage or self.route.serving.Storage(True)),
                   mock.patch.object(self.route, "_published_profiles", return_value=profile_data or [])]
        with patch_all(patchers):
            return self.get()

    def test_overview_sections_and_serving_script_are_discovered(self):
        from matuto.shell import render

        page = render({}, "test-token")
        for name, title in (("setup", "Setup"), ("serving", "Serving"), ("current-work", "Current work")):
            self.assertIn(f'<h3 id="section-overview-{name}">{title}</h3>', page)
        self.assertNotIn('href="#/overview/serving"', page)
        request = urllib.request.Request(
            self.address.replace("/api/overview/serving", "/overview-serving.js"),
            headers={"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertIn(b"/api/overview/serving", response.read())
        stylesheet = urllib.request.Request(
            self.address.replace("/api/overview/serving", "/overview-serving.css"),
            headers={"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(stylesheet, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
            stylesheet = response.read()
            self.assertIn(b"min-height:44px", stylesheet)
            self.assertIn(b".overview-qualify{color:var(--accent)", stylesheet)

    def test_session_gate_and_state_specific_controls(self):
        connection = self.opened.connection()
        stamp = "2026-10-14T00:00:00+00:00"
        connection.execute(
            "INSERT INTO cards (card_id,name,gpu_identity,vram_ceiling_bytes,default_gpu_layers,"
            "smoke_output_tokens,bench_prompt_tokens,bench_generation_tokens,bench_repetitions,created_at)"
            " VALUES ('card-one','Test card','0000:01:00.0',1000,-1,8,8,8,1,?)", (stamp,))
        connection.execute(
            "INSERT INTO builds (build_id,label,kind,location,backend,recipe,state)"
            " VALUES ('build-one','Test runtime','llama-hip','/builds/test','hip','llama-hip','registered')")
        connection.execute(
            "INSERT INTO runtime_selection (selection_id,build_id,card_id,backend,saved_at)"
            " VALUES (1,'build-one','card-one','hip',?)", (stamp,))
        self.assertEqual(self.get(False)[0], 401)
        cases = {
            "stopped": {"start_agentic", "serve_voice"},
            "agentic": {"stop_serving", "restart", "serve_voice"},
            "agentic_unhealthy": {"stop_serving", "restart", "serve_voice"},
            "voice": {"stop_serving", "switch_to_agentic"},
            "voice_partial": {"stop_serving", "switch_to_agentic"},
            "transitioning": set(self.route.serving.CONTROLS),
        }
        for state, names in cases.items():
            with self.subTest(state=state):
                profile = {"profile_id": "profile-candidate", "name": "Candidate profile", "published": True,
                           "qualification": {"state": "candidate"}}
                status, body = self.state_response(state, [profile])
                self.assertEqual(status, 200)
                controls = {item["name"]: item for item in body["controls"]}
                self.assertEqual(set(controls), names)
                self.assertEqual(body["runtime"], {"build": "Test runtime", "backend": "hip", "card": "Test card"})
                if state == "stopped":
                    self.assertFalse(body["publishLink"])
                self.assertEqual(body["profiles"], [profile])
                if state in ("agentic_unhealthy",):
                    self.assertFalse(body["healthy"])
                    self.assertEqual(body["headline"], "Agentic serving is unhealthy.")
                if state in ("voice", "voice_partial"):
                    self.assertEqual(controls["stop_serving"]["href"], "/api/serving/voice/stop")
                    self.assertEqual(body["parts"][0]["state"], "active")
                    if state == "voice":
                        self.assertEqual(controls["switch_to_agentic"]["reason"], "")
                    self.assertEqual(body["parts"][0]["address"], "http://127.0.0.1:18081")
                if state == "voice_partial":
                    self.assertIn("speech part is still running", controls["switch_to_agentic"]["reason"])
                if state == "transitioning":
                    self.assertIn("transitioning", body["headline"])
                    self.assertTrue(all("transitioning" in item["reason"] for item in body["controls"]))
                self.assertIsNone(body["currentWork"])

    def test_stopped_state_links_to_profiles_when_none_are_published(self):
        status, body = self.state_response("stopped")
        self.assertEqual(status, 200)
        self.assertTrue(body["publishLink"])
        start = next(item for item in body["controls"] if item["name"] == "start_agentic")
        self.assertIn("Nothing is published", start["reason"])

    def test_start_names_runtime_storage_and_publication_blockers(self):
        status, body = self.state_response(
            "stopped", runtime=self.route.serving.Runtime(False, "No runtime build and card are selected"),
            storage=self.route.serving.Storage(False, "The configured storage path is unavailable"))
        self.assertEqual(status, 200)
        start = next(item for item in body["controls"] if item["name"] == "start_agentic")
        for blocker in ("No runtime build and card are selected", "configured storage path is unavailable",
                        "Nothing is published"):
            self.assertIn(blocker, start["reason"])
        self.assertTrue(body["publishLink"])

    def test_unknown_state_refuses_every_control_and_active_work_is_linked(self):
        status, body = self.state_response("unknown")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["controls"]), 5)
        self.assertTrue(all(item["reason"] for item in body["controls"]))
        with mock.patch.object(self.route.serving_state, "read", return_value=SimpleNamespace(
                state="stopped", healthy=False, reason="", model="", bundle="", parts=(), address="")), \
             mock.patch.object(self.route.runner, "_current_work", return_value=SimpleNamespace(
                 kind=self.route.admission.BUILD, purpose="llama.cpp HIP 9f1c2d4")), \
             mock.patch.object(self.route.current_work_status, "report", return_value={
                 "name": "current_work", "type": "current-work", "label": "Building runtime",
                 "href": "#/runs?work=build-one"}), \
             mock.patch.object(self.route.voice, "bundle_options", return_value=[]), \
             mock.patch.object(self.route.voice, "_runtime", return_value=self.route.serving.Runtime(True)), \
             mock.patch.object(self.route.voice, "_storage", return_value=self.route.serving.Storage(True)), \
             mock.patch.object(self.route, "_published_profiles", return_value=[]):
            status, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body["currentWork"]["label"], "Building runtime: llama.cpp HIP 9f1c2d4")
        self.assertEqual(body["currentWork"]["href"], "#/runs?work=build-one")


def patch_all(items):
    """Enter a small ordered group of unittest patches."""
    from contextlib import ExitStack
    stack = ExitStack()
    for item in items:
        stack.enter_context(item)
    return stack


if __name__ == "__main__":
    unittest.main()
