"""Build activity reads and changes build jobs only through the shared runner."""

import json
import os
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

from matuto import admission, login, runner, sessions, store
from matuto.page import build_activity as build_activity_page
from matuto.routes import build_activity
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server


class BuildActivityTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.home.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home.cleanup)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        self.works = []
        self.fake = FakeRunner(self.opened, self.works)

    def add_build(self, identifier, *, started, outcome=None, recipe="llama.cpp HIP",
                  target="gfx1201", reason="", log="[redacted]", phase="[]"):
        pin = {"recipe": recipe, "ref": "main", "commit": "a" * 40, "target": target}
        provenance = {"recipe": recipe, "commit": "a" * 40, "target": target,
                      "origin": "https://example.test/source"}
        self.opened.connection().execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, phase, outcome,"
            " provenance, output_digests, log, started_at, ended_at)"
            " VALUES (?, 'build', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier, f"matuto-{identifier}.service", json.dumps(pin), phase, outcome,
             json.dumps({**provenance, "reason": reason}), json.dumps({"server": {"sha256": "b" * 64}}),
             log, started, "later" if outcome else None))
        item = runner.Work(identifier, admission.BUILD, json.dumps(pin), f"matuto-{identifier}.service",
                           started, outcome or "", outcome or runner.RUNNING, ended_at="later" if outcome else "")
        self.works.append(item)
        self.fake.progresses[identifier] = SimpleNamespace(
            phase="building", step="2 of 3", elapsed=17, log="safe output", log_truncated=False,
            unknown="")
        return item

    def route(self, method="GET", body=b""):
        with mock.patch.object(build_activity, "_runner", return_value=self.fake), \
             mock.patch.object(login, "current_username", return_value="operator"):
            return build_activity.handle(SimpleNamespace(method=method, body=body,
                                                         csrf="session-csrf"))

    def test_build_page_is_discovered_as_runtime_activity_section(self):
        self.assertEqual((build_activity_page.PAGE, build_activity_page.NAME,
                          build_activity_page.TITLE, build_activity_page.ORDER),
                         ("runtime", "activity", "Build activity", 4))
        markup = build_activity_page.html({})
        self.assertIn("Loading build activity…", markup)
        self.assertIn('<script src="/build-activity.js" defer></script>', markup)

    def test_listing_shows_newest_builds_full_pin_outcomes_progress_and_contention(self):
        self.add_build("build-new", started="2026-10-13T10:00:00Z", recipe="llama.cpp HIP")
        self.add_build("build-old", started="2026-10-12T10:00:00Z", outcome="failed",
                       reason="cmake exited 1", recipe="Breeze synthesis (Vulkan)", target=None)
        with mock.patch.object(build_activity, "_runner", return_value=self.fake), \
             mock.patch.object(build_activity.serving_state, "read",
                               return_value=SimpleNamespace(state=admission.AGENTIC)):
            status, result = self.route()
        self.assertEqual(status, 200)
        self.assertEqual(result["csrf"], "session-csrf")
        self.assertTrue(result["contention"])
        self.assertEqual([item["id"] for item in result["builds"]], ["build-new", "build-old"])
        active, ended = result["builds"]
        self.assertEqual((active["recipe"], active["ref"], active["commit"], active["target"]),
                         ("llama.cpp HIP", "main", "a" * 40, "gfx1201"))
        self.assertEqual((active["phase"], active["step"], active["elapsed"], active["log"]),
                         ("building", "2 of 3", 17, "safe output"))
        self.assertEqual(active["startedBy"], "operator")
        self.assertTrue(active["canStop"])
        self.assertEqual(ended["outcome"], "errored")
        self.assertEqual(ended["failureReason"], "cmake exited 1")
        self.assertTrue(ended["canRetry"])
        self.assertTrue(ended["canDeleteHistory"])
        self.assertEqual(ended["provenance"]["origin"], "https://example.test/source")
        self.assertFalse(active["historyLogEmpty"])
        self.assertFalse(ended["historyLogEmpty"])

    def test_stop_preserves_a_completion_that_wins_and_rejects_nonbuild_work(self):
        work = self.add_build("build-stop", started="2026-10-13T10:00:00Z")
        self.fake.work = lambda _identifier: work
        self.fake.stop = lambda _identifier: runner.Work(
            work.work_id, work.kind, work.purpose, work.unit, work.started_at,
            runner.COMPLETED, runner.COMPLETED, ended_at="later")
        status, result = self.route("POST", b'{"action":"stop","id":"build-stop"}')
        self.assertEqual((status, result["outcome"]), (200, "completed"))

        completed = runner.Work(work.work_id, work.kind, work.purpose, work.unit,
                                work.started_at, runner.COMPLETED, runner.COMPLETED, ended_at="later")
        stop_calls = []
        self.fake.work = lambda _identifier: completed
        self.fake.stop = lambda identifier: (stop_calls.append(identifier), completed)[1]
        status, result = self.route("POST", b'{"action":"stop","id":"build-stop"}')
        self.assertEqual((status, result["outcome"], stop_calls), (200, "completed", ["build-stop"]))

        self.fake.work = lambda _identifier: runner.Work(
            "smoke-1", "smoke", "smoke", "unit", "now")
        status, result = self.route("POST", b'{"action":"stop","id":"smoke-1"}')
        self.assertEqual(status, 404)
        self.assertIn("No build job", result["error"])

    def test_retry_reuses_the_pinned_commit_and_returns_runner_refusal(self):
        work = self.add_build("build-retry", started="2026-10-12T10:00:00Z", outcome="failed")
        self.fake.work = lambda _identifier: work
        pin = SimpleNamespace(recipe="llama.cpp HIP", ref="main", commit="a" * 40,
                              target="gfx1201")
        self.fake.start = lambda request: SimpleNamespace(
            decision=admission.REFUSE, reason="The admission slot is held by build-current.")
        with mock.patch.object(build_activity.versions, "pin", return_value=pin) as make_pin, \
             mock.patch.object(build_activity.runtime_build, "runner_request", return_value="same-pin"):
            status, result = self.route("POST", b'{"action":"retry","id":"build-retry"}')
            self.assertEqual(status, 409)
            self.assertIn("build-current", result["error"])

            self.fake.start = lambda _request: SimpleNamespace(
                decision=admission.REFUSE,
                reason="Voice serving is up. Stop serving before building llama.cpp HIP aaaa for gfx1201.")
            status, result = self.route("POST", b'{"action":"retry","id":"build-retry"}')
        self.assertEqual(status, 409)
        self.assertIn("Stop serving on Overview", result["error"])
        make_pin.assert_called_with("llama.cpp HIP", "main", "a" * 40, "gfx1201")

    def test_delete_history_keeps_pin_provenance_and_digests_and_refuses_running_build(self):
        ended = self.add_build("build-delete", started="2026-10-12T10:00:00Z", outcome="failed",
                               log="sensitive log")
        self.fake.work = lambda _identifier: ended
        status, result = self.route("POST", b'{"action":"delete-history","id":"build-delete"}')
        self.assertEqual((status, result), (200, {"deletedHistory": "build-delete"}))
        history = self.opened.connection().execute(
            "SELECT pinned_request, provenance, output_digests, log FROM jobs WHERE job_id = ?",
            ("build-delete",)).fetchone()
        self.assertEqual(history["log"], "")
        self.assertEqual(json.loads(history["pinned_request"])["commit"], "a" * 40)
        self.assertIn("origin", json.loads(history["provenance"]))
        self.assertIn("server", json.loads(history["output_digests"]))

        running = self.add_build("build-live", started="2026-10-13T10:00:00Z")
        self.fake.work = lambda _identifier: running
        status, result = self.route("POST", b'{"action":"delete-history","id":"build-live"}')
        self.assertEqual(status, 409)
        self.assertIn("cannot be deleted", result["error"])

    def test_real_http_route_is_session_gated_and_mutations_require_csrf(self):
        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        work = self.add_build("build-http", started="2026-10-13T10:00:00Z")

        class HttpRunner:
            def __init__(self, **_kwargs):
                self.store = store.open_store()
            def close(self):
                self.store.close()
            def works(self):
                return [work]
            def progress(self, _work):
                return SimpleNamespace(phase="building", step="2 of 3", elapsed=17,
                                       log="redacted", log_truncated=False, unknown="")
            def work(self, work_id):
                return work if work_id == work.work_id else None
            def stop(self, _work_id):
                return runner.Work(work.work_id, work.kind, work.purpose, work.unit,
                                   work.started_at, runner.COMPLETED, runner.COMPLETED,
                                   ended_at="later")

        server = build_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_address[1]}"

        def request(path, *, token="", method="GET", csrf="", body=None):
            headers = {"Host": ALLOWED_HOST}
            if token:
                headers["Cookie"] = f"{SESSION_COOKIE}={token}"
            data = None
            if method != "GET":
                headers["Origin"] = MANAGER_ORIGIN
                if csrf:
                    headers["X-CSRF-Token"] = csrf
                data = json.dumps(body or {}).encode()
            operation = urllib.request.Request(address + path, method=method,
                                               headers=headers, data=data)
            try:
                with urllib.request.urlopen(operation, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as refusal:
                return refusal.code, refusal.read()

        with mock.patch.object(runner, "Runner", HttpRunner), \
             mock.patch.object(build_activity.serving_state, "read",
                               return_value=SimpleNamespace(state=admission.AGENTIC)):
            status, body = request("/api/build-activity")
            self.assertEqual(status, 401)
            status, body = request("/api/build-activity", token=token)
            self.assertEqual(status, 200)
            payload = json.loads(body)
            csrf = payload["csrf"]
            self.assertEqual([item["id"] for item in payload["builds"]], ["build-http"])
            self.assertTrue(payload["contention"])
            self.assertEqual(payload["builds"][0]["commit"], "a" * 40)
            status, _body = request("/api/build-activity", token=token, method="POST",
                                    body={"action": "stop", "id": "build-http"})
            self.assertEqual(status, 403)
            status, body = request("/api/build-activity", token=token, method="POST", csrf=csrf,
                                   body={"action": "stop", "id": "build-http"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["outcome"], "completed")
            status, _body = request("/build-activity.js", token=token)
            self.assertEqual(status, 200)


class FakeRunner:
    def __init__(self, opened, works):
        self.store = opened
        self.works = lambda: works
        self.progresses = {}
        self.work = lambda _identifier: None
        self.stop = lambda _identifier: None
        self.start = lambda _request: None

    def close(self):
        pass

    def progress(self, work):
        return self.progresses[work.work_id]


if __name__ == "__main__":
    unittest.main()
