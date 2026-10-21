"""Settings › Maintenance is exercised through its authenticated manager routes."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store  # noqa: E402
from matuto.discovery import load_item  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class MaintenanceHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="matuto-maintenance-http-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.systemd_log = self.home / "systemd-run.log"
        runner = self.bin / "systemd-run"
        runner.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > \"$SYSTEMD_RUN_LOG\"\n")
        runner.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"), "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "SYSTEMD_RUN_LOG": str(self.systemd_log),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.opened = store.open_store()
        self.addCleanup(self.opened.close)
        login.create_login("operator", "secret", "secret")
        self.token, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.route_globals = self.server.RequestHandlerClass.routes[
            "/api/maintenance"].__globals__
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.route = load_item("routes", "maintenance")

    def request(self, method="GET", body=None, *, cookie=True, csrf=True, origin=True,
                path="/api/maintenance", real_handoff=False):
        if isinstance(body, dict) and body.get("action") == "remove":
            body = {"removalId": "01234567-89ab-cdef-0123-456789abcdef", **body}
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        if method != "GET":
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        request = urllib.request.Request(self.address + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        handoff = (nullcontext() if real_handoff else
                   mock.patch.dict(self.route_globals,
                                   {"_wait_for_removal_handoff": lambda _removal_id: None}))
        try:
            with handoff:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def plan(self, blockers=()):
        return {"remove": [{"item": "Release files", "path": "/release", "size_bytes": 42}],
                "keep": [{"item": "Store", "path": "/store", "size_bytes": 128}],
                "consumer_configuration": "Never touched.",
                "later_bootstrap": "Reuses the kept data.", "blockers": list(blockers)}

    def test_settings_renders_distinct_update_repair_and_removal_controls(self):
        with urllib.request.urlopen(urllib.request.Request(self.address + "/", headers={
                "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})) as response:
            html = response.read()
        self.assertIn(b"Check for updates", html)
        self.assertIn(b"Review Repair Matuto\xe2\x80\xa6", html)
        self.assertIn(b"Review removal\xe2\x80\xa6", html)
        self.assertIn(b"/maintenance.js", html)

    def test_status_and_removal_preview_are_read_only_and_session_gated(self):
        with (mock.patch.object(self.route.removal, "preview", return_value=self.plan()),
              mock.patch.object(self.route.updates, "display", return_value={"installation": None})):
            status, result = self.request()
            self.assertEqual(status, 200)
            self.assertEqual(result["removal"]["keep"][0]["size_bytes"], 128)
            self.assertIsNone(result["repair"])
            status, preview = self.request("POST", {"action": "remove-preview"})
            self.assertEqual(status, 200)
            self.assertEqual(preview["remove"][0]["size_bytes"], 42)
            self.assertFalse(self.systemd_log.exists())
            self.assertEqual(self.request("POST", {"action": "remove-preview"}, cookie=False)[0], 401)
            self.assertEqual(self.request("POST", {"action": "remove-preview"}, csrf=False)[0], 403)
            self.assertEqual(self.request("POST", {"action": "remove-preview"}, origin=False)[0], 403)

    def test_each_live_or_unknown_blocker_refuses_removal_without_calling_operation(self):
        blockers = ["Stop serving on Overview", "Stop run in Runs & jobs",
                    "Cancel download in Runs & jobs", "an update is installing; it must end",
                    "serving state is unknown", "job state is unknown"]
        for blocker in blockers:
            with self.subTest(blocker=blocker):
                with (mock.patch.object(self.route.removal, "preview", return_value=self.plan([blocker])),
                      mock.patch.object(self.route.removal, "remove") as remove):
                    status, result = self.request("POST", {"action": "remove"})
                self.assertEqual(status, 409)
                self.assertIn(blocker, result["error"])
                self.assertEqual(result["blockers"], [blocker])
                remove.assert_not_called()

    def test_confirmation_rechecks_and_refuses_a_blocker_that_appeared(self):
        changing_plan = self.plan()
        def confirm(*, confirm, on_kept):
            raise self.route.removal.RemovalError("Removal refused: Cancel download in Runs & jobs")
        with (mock.patch.object(self.route.removal, "preview", return_value=changing_plan),
              mock.patch.object(self.route.removal, "remove", side_effect=confirm)):
            status, result = self.request("POST", {"action": "remove"})
        self.assertEqual(status, 409)
        self.assertIn("Cancel download in Runs & jobs", result["error"])

    def test_confirmed_removal_returns_actual_kept_list_and_final_failure_view_data(self):
        kept = [{"item": "Store", "path": "/after/first-step/store", "size_bytes": 127}]
        with (mock.patch.object(self.route.removal, "preview", return_value=self.plan()),
              mock.patch.object(self.route.removal, "remove", return_value={"kept": kept}) as remove):
            status, result = self.request("POST", {"action": "remove"})
            self.assertEqual(status, 200)
            self.assertTrue(result["removed"])
            self.assertEqual(result["kept"], kept)
            self.assertTrue(callable(remove.call_args.kwargs["on_kept"]))

        def failed(*, confirm, on_kept):
            on_kept(kept)
            raise self.route.removal.RemovalError("Final removal failed; complete it from the terminal")

        with (mock.patch.object(self.route.removal, "preview", return_value=self.plan()),
              mock.patch.object(self.route.removal, "remove", side_effect=failed)):
            status, result = self.request("POST", {"action": "remove"})
        self.assertEqual(status, 200)
        self.assertFalse(result["removed"])
        self.assertTrue(result["finalStepFailed"])
        self.assertEqual(result["kept"], kept)

    def test_actual_kept_list_is_delivered_before_finalizer_returns(self):
        kept = [{"item": "Store", "path": "/after/first-step/store", "size_bytes": 127}]
        entered = threading.Event()
        finish = threading.Event()
        answer = {}

        def remove(*, confirm, on_kept):
            on_kept(kept)
            entered.set()
            finish.wait(3)
            return {"kept": kept}

        with (mock.patch.object(self.route.removal, "preview", return_value=self.plan()),
              mock.patch.object(self.route.removal, "remove", side_effect=remove)):
            request_thread = threading.Thread(
                target=lambda: answer.__setitem__("result", self.request(
                    "POST", {"action": "remove"}, real_handoff=True)))
            request_thread.start()
            status = None
            result = None
            for _ in range(100):
                status, result = self.request(
                    path="/api/maintenance?removal_id=01234567-89ab-cdef-0123-456789abcdef",
                    real_handoff=True)
                if result.get("stage") == "finalizing":
                    break
                threading.Event().wait(0.01)
            self.assertEqual(status, 200)
            self.assertEqual(result, {"stage": "finalizing", "kept": kept})
            self.assertTrue(entered.wait(2))
            self.assertTrue(request_thread.is_alive(), "the removal request must still be awaiting its final step")
            finish.set()
            request_thread.join(3)
        self.assertFalse(request_thread.is_alive())
        self.assertEqual(answer["result"][1]["kept"], kept)

    def test_removal_does_not_start_finalizer_without_handoff_acknowledgment(self):
        kept = [{"item": "Store", "path": "/after/first-step/store", "size_bytes": 127}]
        finalizer_started = threading.Event()
        wait_for_handoff = self.route_globals["_wait_for_removal_handoff"]

        def remove(*, confirm, on_kept):
            on_kept(kept)
            finalizer_started.set()
            return {"kept": kept}

        with (mock.patch.object(self.route.removal, "preview", return_value=self.plan()),
              mock.patch.object(self.route.removal, "remove", side_effect=remove),
              mock.patch.dict(self.route_globals, {
                  "_wait_for_removal_handoff": lambda removal_id:
                      wait_for_handoff(removal_id, timeout=0)})):
            status, result = self.request("POST", {"action": "remove"}, real_handoff=True)

        self.assertEqual(status, 200)
        self.assertTrue(result["finalStepFailed"])
        self.assertEqual(result["kept"], kept)
        self.assertIn("handoff was not acknowledged", result["error"])
        self.assertFalse(finalizer_started.is_set())

    def test_repair_starts_the_independent_updater_as_a_user_service(self):
        script = self.home / "updater.py"
        script.touch()
        with (mock.patch.object(self.route.updater, "independent_updater_script", return_value=script),
              mock.patch.object(self.route.updater, "installed_version", return_value="2026.10.0")):
            status, result = self.request("POST", {"action": "repair"})
        self.assertEqual(status, 202)
        self.assertTrue(result["started"])
        self.assertTrue(result["unit"].startswith("matuto-repair-"))
        argv = self.systemd_log.read_text().split()
        self.assertIn("--user", argv)
        self.assertIn(f"--unit={result['unit']}", argv)
        self.assertEqual(argv[-2:], [str(script), "repair"])
        phase = json.loads(store.read_update_state(store.store_path())["phase"])
        self.assertEqual(phase["repair_result"], "in_progress")

    def test_repair_is_allowed_to_recover_maintenance_but_removal_stays_blocked(self):
        store.write_update_state(store.store_path(), "2026.10.0", "maintenance",
                                 details={"reason": "fixture update failure",
                                          "repair_result": "failed",
                                          "repair_recovered_update": True})
        script = self.home / "updater.py"
        script.touch()
        with (mock.patch.object(self.route.updater, "independent_updater_script", return_value=script),
              mock.patch.object(self.route.updater, "installed_version", return_value="2026.10.0")):
            status, result = self.request("POST", {"action": "repair"})
            self.assertEqual(status, 202, result)
            phase = json.loads(store.read_update_state(store.store_path())["phase"])
            self.assertEqual(phase["repair_prior_result"], "failed")
            self.assertTrue(phase["repair_recovered_update"])
            status, refused = self.request("POST", {"action": "remove-preview"})
            other_status, other_refused = self.request(
                "POST", {"port": 8091}, path="/api/lab-port")
        self.assertEqual(status, 409)
        self.assertIn("Repair Matuto must finish", refused["error"])
        self.assertEqual(other_status, 409)
        self.assertIn("run Repair Matuto", other_refused["error"])


if __name__ == "__main__":
    unittest.main()
