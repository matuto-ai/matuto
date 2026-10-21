"""The Runs & jobs section and API expose the shared runner without bypassing it."""

import json
import os
import re
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import admission, login, router, runner, sessions, serving_state, store
from matuto.page import runs as runs_page
from matuto.procedures import wq as wq_procedure
from matuto.routes import run_evidence, run_stop_serving, runs
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, Request, build_server


class RunsHttpTest(unittest.TestCase):
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

    def test_authenticated_http_list_and_stop_use_the_runner_work_list(self):
        work = type("Work", (), {"work_id": "smoke-terminal", "kind": "smoke",
            "purpose": "smoke for Studio, started in terminal", "outcome": "", "state": "running",
            "started_at": "then", "ended_at": "", "record_label": "Studio",
            "record_id": "profile-1"})()

        class FakeRunner:
            def __init__(_self, **_kwargs):
                _self.store = store.open_store()

            def close(_self):
                _self.store.close()

            def works(_self):
                return [work]

            def progress(_self, _work):
                return SimpleNamespace(phase="Loading", step="", log="", elapsed=1, unknown="")

            def stop(_self, work_id):
                work.outcome = "cancelled"
                work.state = "cancelled"
                return work

        with mock.patch.object(runs.runner, "Runner", side_effect=FakeRunner):
            status, body, _ = self.request("/api/runs", cookie=self.token)
            self.assertEqual(status, 200, body)
            listing = json.loads(body)
            self.assertEqual(listing["csrf"], self.csrf)
            entries = listing["entries"]
            self.assertEqual([(entry["id"], entry["kind"], entry["purpose"], entry["state"])
                              for entry in entries],
                             [("smoke-terminal", "smoke", "smoke for Studio, started in terminal", "running")])
            status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                body={"action": "stop", "id": "smoke-terminal"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["outcome"], "cancelled")

    def test_confirmed_run_checks_real_card_holder_before_stop_and_admits_afterward(self):
        target = SimpleNamespace(kind="language", id="profile-1", label="Studio", fingerprint="fp")
        procedure = SimpleNamespace(PTY=False, REVISION="revision")
        work = SimpleNamespace(work_id="smoke-1")
        active, stopped = SimpleNamespace(state=admission.AGENTIC), SimpleNamespace(state=admission.STOPPED)
        with mock.patch.object(runs, "item_names", return_value=["smoke"]), \
             mock.patch.object(runs, "load_item", return_value=procedure), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(router, "router_pid", return_value=11), \
             mock.patch.object(router, "_unit_pid", return_value=22), \
             mock.patch.object(router, "foreign_holders", side_effect=([], [])) as holder, \
             mock.patch.object(serving_state, "read", side_effect=(active, active, stopped, stopped)), \
             mock.patch.object(runs.runner.Runner, "_answers", return_value=True), \
             mock.patch.object(runs.runner.Runner, "_unit_state", return_value=None), \
             mock.patch.object(runs.runner.Runner, "_prepare"), \
             mock.patch.object(runs.runner.Runner, "_start_unit", return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(runs.runner.Runner, "_launched", return_value=work), \
             mock.patch.object(run_stop_serving.serving_actions, "activate",
                               return_value={"stopped": True}) as stop:
            status, body, _ = self.request("/api/runs/stop-serving", method="POST", cookie=self.token,
                body={"confirm": True, "procedure": "smoke", "record": "profile-1"})
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body), {"work": "smoke-1"})
        self.assertEqual(holder.call_count, 2)
        stop.assert_called_once_with(run_stop_serving.serving_actions.STOP_SERVING, confirmed=True)

    def test_start_settles_ended_run_and_admits_next_without_list_request(self):
        target = SimpleNamespace(kind="language", id="profile-1", label="Studio", fingerprint="fp")
        procedure = SimpleNamespace(PTY=False, REVISION="revision")
        not_found = {runner.LOAD_STATE: runner.NOT_FOUND}
        with mock.patch.object(runs, "item_names", return_value=["smoke"]), \
             mock.patch.object(runs, "load_item", return_value=procedure), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(router, "router_pid", return_value=11), \
             mock.patch.object(router, "_unit_pid", return_value=22), \
             mock.patch.object(router, "foreign_holders", return_value=[]), \
             mock.patch.object(serving_state, "read",
                               return_value=SimpleNamespace(state=admission.STOPPED)), \
             mock.patch.object(runs.runner.Runner, "_answers", return_value=True), \
             mock.patch.object(runs.runner.Runner, "_unit_state", return_value=not_found), \
             mock.patch.object(runs.runner.Runner, "_prepare"), \
             mock.patch.object(runs.runner.Runner, "_start_unit",
                               return_value=SimpleNamespace(returncode=0)), \
             mock.patch.dict(self.server.RequestHandlerClass.routes, {runs.PATH: runs.handle}):
            for _ in range(2):
                status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                    body={"action": "start", "procedure": "smoke", "record": "profile-1"})
                self.assertEqual(status, 202, body)

    def test_wq_http_start_freezes_the_limits_sent_by_the_form(self):
        target = SimpleNamespace(kind="language", id="profile-1", label="Studio", fingerprint="fp-current")
        limits = {"first_byte_seconds": 30, "short_total_seconds": 80,
                  "generation_tokens_per_second": 18, "vram_mb": 12000,
                  "rss_kib": 24000000, "cancel_recovery_seconds": 4,
                  "restart_seconds": 240, "long_execution_seconds": 1500,
                  "retrieval_samples": 4, "retrieval_generation_tokens": 4096}
        pinned = {"record": {"id": "profile-1", "fingerprint": "fp-current"},
                  "wq_limits": limits}
        procedure = SimpleNamespace(prepare=mock.Mock(return_value=pinned), PTY=True,
                                    REVISION="wq-release", dirty_checkout=lambda: False)
        accepted = SimpleNamespace(decision=admission.ADMIT, work=SimpleNamespace(work_id="wq-1"))

        class FakeRunner:
            def __init__(_self):
                _self.store = store.open_store()

            def close(_self):
                _self.store.close()

            def start(_self, request):
                _self.request = request
                return accepted

        runners = []
        def make_runner():
            runners.append(FakeRunner())
            return runners[-1]

        with mock.patch.object(runs, "_runner", side_effect=make_runner), \
             mock.patch.object(runs, "item_names", return_value=["wq"]), \
             mock.patch.object(runs, "load_item", return_value=procedure), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(runs.runner.Request, "run", return_value="runner-request") as make_request, \
             mock.patch.dict(self.server.RequestHandlerClass.routes, {runs.PATH: runs.handle}):
            status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                body={"action": "start", "procedure": "wq", "record": "profile-1", "limits": limits})
        self.assertEqual((status, json.loads(body)), (202, {"work": "wq-1"}))
        procedure.prepare.assert_called_once()
        self.assertIs(procedure.prepare.call_args.args[0], runners[0].store)
        self.assertEqual(procedure.prepare.call_args.args[1:], (target, limits))
        self.assertEqual(json.loads(make_request.call_args.args[2][-1]), pinned)
        self.assertEqual(runners[0].request, "runner-request")

    def test_wq_http_limit_refusal_returns_the_procedure_field_without_starting(self):
        target = SimpleNamespace(kind="language", id="profile-1", label="Studio", fingerprint="fp-current")
        procedure = SimpleNamespace(prepare=mock.Mock(side_effect=wq_procedure.LimitError(
            "retrieval_samples", "retrieval-samples must be a whole number")),
            PTY=True, REVISION="wq-release", dirty_checkout=lambda: False)
        class FakeRunner:
            def __init__(_self):
                _self.store = store.open_store()
                _self.start = mock.Mock()

            def close(_self):
                _self.store.close()

        runners = []
        def make_runner():
            runners.append(FakeRunner())
            return runners[-1]

        with mock.patch.object(runs, "_runner", side_effect=make_runner), \
             mock.patch.object(runs, "item_names", return_value=["wq"]), \
             mock.patch.object(runs, "load_item", return_value=procedure), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.dict(self.server.RequestHandlerClass.routes, {runs.PATH: runs.handle}):
            status, body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                body={"action": "start", "procedure": "wq", "record": "profile-1",
                      "limits": {"retrieval_samples": 1.5}})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "retrieval-samples must be a whole number",
                                            "field": "retrieval_samples"})
        procedure.prepare.assert_called_once()
        self.assertIs(procedure.prepare.call_args.args[0], runners[0].store)
        self.assertEqual(procedure.prepare.call_args.args[1:],
                         (target, {"retrieval_samples": 1.5}))
        runners[0].start.assert_not_called()

    def test_foreign_card_holder_is_refused_before_stopping_serving(self):
        target = SimpleNamespace(kind="language", id="profile-1", label="Studio", fingerprint="fp")
        procedure = SimpleNamespace(PTY=False, REVISION="revision")
        with mock.patch.object(runs, "item_names", return_value=["smoke"]), \
             mock.patch.object(runs, "load_item", return_value=procedure), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(router, "router_pid", return_value=11), \
             mock.patch.object(router, "_unit_pid", return_value=22), \
             mock.patch.object(router, "foreign_holders",
                               return_value=["other llama-server"]) as holder, \
             mock.patch.object(serving_state, "read", return_value=SimpleNamespace(state=admission.AGENTIC)), \
             mock.patch.object(runs.runner.Runner, "_answers", return_value=True), \
             mock.patch.object(run_stop_serving.serving_actions, "activate") as stop:
            status, body, _ = self.request("/api/runs/stop-serving", method="POST", cookie=self.token,
                body={"confirm": True, "procedure": "smoke", "record": "profile-1"})
        self.assertEqual(status, 409, body)
        self.assertFalse(json.loads(body)["offer"])
        self.assertIn("other llama-server", json.loads(body)["error"])
        holder.assert_called_once()
        stop.assert_not_called()

    def test_real_page_and_api_are_session_gated_and_mutations_need_csrf(self):
        status, body, _ = self.request("/api/runs")
        self.assertEqual(status, 401)
        self.assertNotIn(b'"csrf"', body)
        status, body, _ = self.request("/runs.js")
        self.assertEqual(status, 200)
        self.assertIn(b"Create login", body)
        status, body, _ = self.request("/api/runs", cookie=self.token)
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual({item["id"] for item in listing["procedures"]},
                         {"bench", "bundle", "context_search", "serve", "smoke", "verify", "wq"})
        self.assertEqual({key: value for key, value in listing.items() if key != "procedures"},
                         {"entries": [], "records": [], "csrf": self.csrf})
        status, script, _ = self.request("/runs.js", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"Cancel download", script)
        status, stylesheet, headers = self.request("/runs.css", cookie=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"#runs-wq-limits", stylesheet)
        self.assertEqual(headers.get_content_type(), "text/css")
        status, _body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                                        csrf=False, body={"action": "unknown"})
        self.assertEqual(status, 403)
        status, _body, _ = self.request("/api/runs", method="POST", cookie=self.token,
                                        body={"action": "unknown"})
        self.assertEqual(status, 409)
        status, _body, _ = self.request("/api/runs/stop-serving", method="POST",
                                        body={"confirm": True, "procedure": "smoke", "record": "profile-1"})
        self.assertEqual(status, 401)
        status, _body, _ = self.request("/api/runs/stop-serving", method="POST", cookie=self.token,
                                        csrf=False, body={"confirm": True, "procedure": "smoke",
                                                          "record": "profile-1"})
        self.assertEqual(status, 403)


class RunsPageTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home.cleanup)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)

    def test_page_declares_runs_section_and_loads_its_same_origin_script(self):
        section = runs_page
        self.assertEqual((section.PAGE, section.NAME, section.TITLE),
                         ("runs", "runs", "Runs & jobs"))
        markup = section.html({})
        self.assertIn("Current work", markup)
        self.assertIn("Start a run", markup)
        self.assertIn("History", markup)
        self.assertIn("Qualification acceptance", markup)
        self.assertIn('<link rel="stylesheet" href="/runs.css">', markup)
        self.assertIn('<script src="/runs.js" defer></script>', markup)
        self.assertNotRegex(markup, r'<input id="wq-[^"]+"[^>]*(?:\brequired\b|\bmin=)')

    def test_list_exposes_shared_run_and_download_work_with_progress_and_address(self):
        entries = [
            type("Work", (), {"work_id": "serve-1", "kind": "serve", "purpose": "temporary server",
                "outcome": "", "state": "running", "started_at": "then", "ended_at": "",
                "record_label": "Studio", "record_id": "profile-1"})(),
            type("Work", (), {"work_id": "download-1", "kind": "download", "purpose": "model weights",
                "outcome": "", "state": "running", "started_at": "earlier", "ended_at": "",
                "record_label": "", "record_id": ""})(),
        ]

        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def works(_self):
                return entries

            def progress(_self, work):
                log = "\n".join(f"server output {line}" for line in range(61))
                step = "http://127.0.0.1:4321" if work.kind == "serve" else ""
                return type("Progress", (), {"log": log, "phase": "Serving", "step": step, "elapsed": 1})()

        with mock.patch.object(runs, "_runner", return_value=FakeRunner()):
            status, result = runs.handle(type("Request", (), {"method": "GET", "csrf": "fake-current"})())
        self.assertEqual(status, 200)
        self.assertEqual(result["csrf"], "fake-current")
        self.assertEqual([entry["kind"] for entry in result["entries"]], ["serve", "download"])
        self.assertEqual(result["entries"][0]["address"], "http://127.0.0.1:4321")
        self.assertEqual(result["entries"][0]["started_at"], "then")
        self.assertEqual(result["entries"][1]["purpose"], "model weights")
        self.assertEqual({procedure["id"] for procedure in result["procedures"]},
                         {"bench", "bundle", "context_search", "serve", "smoke", "verify", "wq"})
        self.assertEqual(result["records"], [])

    def test_start_dispatches_the_selected_procedure_and_record_to_the_runner(self):
        target = type("Target", (), {"kind": "language", "id": "profile-1",
                                     "label": "Studio", "fingerprint": "fingerprint"})()
        module = type("Procedure", (), {"PTY": True, "REVISION": "release-1"})()
        accepted = type("Start", (), {"decision": "admit", "work": type("Work", (), {
            "work_id": "smoke-1"})()})()

        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def start(_self, request):
                _self.request = request
                return accepted

        fake = FakeRunner()
        with mock.patch.object(runs, "_runner", return_value=fake), \
             mock.patch.object(runs, "item_names", return_value=["smoke"]), \
             mock.patch.object(runs, "load_item", return_value=module), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(runs.runner.Request, "run", return_value="runner-request") as make_request:
            status, result = runs.handle(type("Request", (), {"method": "POST", "body":
                b'{"action":"start","procedure":"smoke","record":"profile-1"}'})())
        self.assertEqual((status, result), (202, {"work": "smoke-1"}))
        make_request.assert_called_once()
        self.assertEqual(make_request.call_args.args[:2], ("smoke", "smoke for Studio"))
        self.assertEqual(fake.request, "runner-request")

    def test_start_passes_the_context_search_start_rung_into_its_pinned_spec(self):
        target = type("Target", (), {"kind": "language", "id": "profile-1",
                                     "label": "Studio", "fingerprint": "fingerprint"})()
        prepared = {"start_rung": 196608}
        module = SimpleNamespace(prepare=mock.Mock(return_value=prepared), dirty_checkout=lambda: False,
                                 revision=lambda: "release-1")
        accepted = type("Start", (), {"decision": "admit", "work": type("Work", (), {
            "work_id": "context-1"})()})()

        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def start(_self, request):
                _self.request = request
                return accepted

        fake = FakeRunner()
        with mock.patch.object(runs, "_runner", return_value=fake), \
             mock.patch.object(runs, "item_names", return_value=["context_search"]), \
             mock.patch.object(runs, "load_item", return_value=module), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(runs.runner.Request, "run", return_value="runner-request") as make_request:
            status, result = runs.handle(type("Request", (), {"method": "POST", "body": json.dumps({
                "action": "start", "procedure": "context_search", "record": "profile-1",
                "start_rung": 196608
            }).encode()})())
        self.assertEqual((status, result), (202, {"work": "context-1"}))
        module.prepare.assert_called_once_with(self.opened, target, 196608)
        self.assertEqual(json.loads(make_request.call_args.args[2][-1]), prepared)
        self.assertEqual(fake.request, "runner-request")

    def test_start_passes_wq_limit_overrides_into_the_frozen_launch_spec(self):
        target = type("Target", (), {"kind": "language", "id": "profile-1",
                                     "label": "Studio", "fingerprint": "fingerprint"})()
        limits = {"vram_mb": 12000, "retrieval_samples": 4}
        prepared = {"record": {"id": "profile-1"}, "wq_limits": limits}
        module = SimpleNamespace(prepare=mock.Mock(return_value=prepared), dirty_checkout=lambda: False,
                                 revision=lambda: "release-1")
        accepted = type("Start", (), {"decision": "admit", "work": type("Work", (), {
            "work_id": "wq-1"})()})()

        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def start(_self, request):
                _self.request = request
                return accepted

        fake = FakeRunner()
        with mock.patch.object(runs, "_runner", return_value=fake), \
             mock.patch.object(runs, "item_names", return_value=["wq"]), \
             mock.patch.object(runs, "load_item", return_value=module), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(runs.runner.Request, "run", return_value="runner-request") as make_request:
            status, result = runs.handle(type("Request", (), {"method": "POST", "body": json.dumps({
                "action": "start", "procedure": "wq", "record": "profile-1", "limits": limits
            }).encode()})())
        self.assertEqual((status, result), (202, {"work": "wq-1"}))
        module.prepare.assert_called_once_with(self.opened, target, limits)
        make_request.assert_called_once()
        self.assertEqual(json.loads(make_request.call_args.args[2][-1]), prepared)
        self.assertEqual(fake.request, "runner-request")

    def test_admission_offer_is_relayed_for_the_page_preview(self):
        decision = type("Start", (), {"decision": "stop serving and run",
                                      "reason": "Stop serving and run: consumers lose the model."})()

        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def start(_self, request):
                return decision

        target = type("Target", (), {"kind": "language", "id": "profile-1",
                                     "label": "Studio", "fingerprint": "fingerprint"})()
        with mock.patch.object(runs, "_runner", return_value=FakeRunner()), \
             mock.patch.object(runs, "item_names", return_value=["smoke"]), \
             mock.patch.object(runs, "load_item", return_value=object()), \
             mock.patch.object(runs, "record_target", return_value=target), \
             mock.patch.object(runs.runner.Request, "run", return_value=object()):
            status, result = runs.handle(type("Request", (), {"method": "POST", "body":
                b'{"action":"start","procedure":"smoke","record":"profile-1"}'})())
        self.assertEqual(status, 409)
        self.assertTrue(result["offer"])
        self.assertIn("Stop serving and run", result["error"])

    def test_confirmed_stop_serving_rechecks_admission_then_starts_the_same_run(self):
        request = Request("POST", run_stop_serving.PATH, {}, {},
                          b'{"confirm":true,"procedure":"smoke","record":"profile-1","parameters":{"limit":7}}', "csrf")
        offer = (409, {"error": "Stop serving and run: consumers lose the model.", "offer": True})
        started = (202, {"work": "smoke-1"})
        with mock.patch.object(run_stop_serving.route_gate, "require_session", return_value=None), \
             mock.patch.object(run_stop_serving.runs, "handle", side_effect=(offer, started)) as start, \
             mock.patch.object(run_stop_serving.serving_state, "read", side_effect=(
                 SimpleNamespace(state=admission.AGENTIC), SimpleNamespace(state=admission.STOPPED))), \
             mock.patch.object(run_stop_serving.serving_actions, "activate",
                               return_value={"stopped": True}) as stop:
            self.assertEqual(run_stop_serving.handle(request), started)
        self.assertEqual(start.call_count, 2)
        forwarded = json.loads(start.call_args_list[0].args[0].body)
        self.assertEqual(forwarded, {"action": "start", "procedure": "smoke", "record": "profile-1",
                                     "parameters": {"limit": 7}})
        self.assertEqual(start.call_args_list[1].args[0].body, start.call_args_list[0].args[0].body)
        stop.assert_called_once_with(run_stop_serving.serving_actions.STOP_SERVING, confirmed=True)

    def test_confirmed_stop_serving_routes_voice_and_refuses_stopping_when_admission_is_taken(self):
        request = Request("POST", run_stop_serving.PATH, {}, {},
                          b'{"confirm":true,"procedure":"bench","record":"profile-2"}', "csrf")
        offer = (409, {"error": "Stop serving and run: voice serving is up.", "offer": True})
        started = (202, {"work": "bench-2"})
        with mock.patch.object(run_stop_serving.route_gate, "require_session", return_value=None), \
             mock.patch.object(run_stop_serving.runs, "handle", side_effect=(offer, started)), \
             mock.patch.object(run_stop_serving.serving_state, "read", side_effect=(
                 SimpleNamespace(state=admission.VOICE), SimpleNamespace(state=admission.STOPPED))), \
             mock.patch.object(run_stop_serving.voice, "activate", return_value={"stopped": True}) as stop:
            self.assertEqual(run_stop_serving.handle(request), started)
        stop.assert_called_once_with(run_stop_serving.serving.STOP_SERVING, confirmed=True)

        refused = (409, {"error": "The admission slot is held by another run.", "offer": False})
        with mock.patch.object(run_stop_serving.route_gate, "require_session", return_value=None), \
             mock.patch.object(run_stop_serving.runs, "handle", return_value=refused), \
             mock.patch.object(run_stop_serving.serving_actions, "activate") as stop_agentic, \
             mock.patch.object(run_stop_serving.voice, "activate") as stop_voice:
            self.assertEqual(run_stop_serving.handle(request), refused)
        stop_agentic.assert_not_called()
        stop_voice.assert_not_called()

        with mock.patch.object(run_stop_serving.route_gate, "require_session", return_value=None), \
             mock.patch.object(run_stop_serving.runs, "handle", side_effect=(offer, started)) as start, \
             mock.patch.object(run_stop_serving.serving_state, "read",
                               return_value=SimpleNamespace(state=admission.AGENTIC)), \
             mock.patch.object(run_stop_serving.serving_actions, "activate",
                               return_value={"stopped": False, "reason": "router would not stop"}):
            status, result = run_stop_serving.handle(request)
        self.assertEqual((status, result),
                         (409, {"error": "router would not stop"}))
        start.assert_called_once()

        slot_taken = (409, {"error": "The admission slot is held by another run.", "offer": False})
        with mock.patch.object(run_stop_serving.route_gate, "require_session", return_value=None), \
             mock.patch.object(run_stop_serving.runs, "handle", side_effect=(offer, slot_taken)) as start, \
             mock.patch.object(run_stop_serving.serving_state, "read", side_effect=(
                 SimpleNamespace(state=admission.AGENTIC), SimpleNamespace(state=admission.STOPPED))), \
             mock.patch.object(run_stop_serving.serving_actions, "activate",
                               return_value={"stopped": True}) as stop:
            self.assertEqual(run_stop_serving.handle(request), slot_taken)
        self.assertEqual(start.call_count, 2)
        stop.assert_called_once_with(run_stop_serving.serving_actions.STOP_SERVING, confirmed=True)

    def test_stop_and_delete_actions_use_runner_and_store_apis(self):
        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

            def stop(_self, work_id):
                return type("Work", (), {"work_id": work_id, "outcome": "cancelled",
                                         "state": "cancelled"})()

        with mock.patch.object(runs, "_runner", return_value=FakeRunner()), \
             mock.patch.object(store, "delete_run") as delete_run:
            stop_status, stop_result = runs.handle(type("Request", (), {
                "method": "POST", "body": b'{"action":"stop","id":"run-1"}'})())
            delete_status, delete_result = runs.handle(type("Request", (), {
                "method": "POST", "body": b'{"action":"delete","id":"run-1"}'})())
        self.assertEqual((stop_status, stop_result),
                         (200, {"id": "run-1", "outcome": "cancelled", "state": "cancelled"}))
        self.assertEqual((delete_status, delete_result), (200, {"deleted": "run-1"}))
        delete_run.assert_called_once_with(self.opened, "run-1")

    def test_evidence_route_returns_the_stores_archive_with_a_run_named_download(self):
        with mock.patch.object(run_evidence.store, "open_store", return_value=self.opened), \
             mock.patch.object(run_evidence.store, "run_archive", return_value=b"zip-bytes") as archive:
            result = run_evidence.handle(type("Request", (), {
                "method": "GET", "query": {"id": ["run-1"]}})())
        self.assertEqual(result[:3], (200, b"zip-bytes", "application/zip"))
        self.assertEqual(result[3]["Content-Disposition"],
                         'attachment; filename="matuto-run-1-evidence.zip"')
        archive.assert_called_once_with(self.opened, "run-1")

    def test_delete_refusal_names_the_acceptance_and_does_not_delete(self):
        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

        with mock.patch.object(runs, "_runner", return_value=FakeRunner()), \
             mock.patch.object(store, "refuse_accepted_run_deletion",
                               side_effect=store.StoreError("acceptance-7 cites run-1")), \
             mock.patch.object(store, "delete_run") as delete_run:
            status, result = runs.handle(type("Request", (), {"method": "POST", "body":
                b'{"action":"delete","id":"run-1"}'})())
        self.assertEqual(status, 409)
        self.assertIn("acceptance-7", result["error"])
        delete_run.assert_not_called()

    def test_other_methods_are_not_treated_as_mutations(self):
        with mock.patch.object(runs, "_runner") as make_runner:
            status, result = runs.handle(type("Request", (), {"method": "DELETE"})())
        self.assertEqual(status, 405)
        self.assertIn("GET and POST", result["error"])
        make_runner.assert_not_called()

    def test_unknown_actions_and_malformed_json_refuse_without_mutation(self):
        class FakeRunner:
            store = self.opened

            def close(_self):
                pass

        for body in (b"not json", b'{"action":"unknown"}'):
            with self.subTest(body=body), mock.patch.object(runs, "_runner", return_value=FakeRunner()):
                status, result = runs.handle(type("Request", (), {"method": "POST", "body": body})())
                self.assertEqual(status, 409)
                self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
