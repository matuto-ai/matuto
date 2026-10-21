"""The lab port setting is exercised through the authenticated manager HTTP API."""

import json
import os
import shutil
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
sys.path.insert(0, str(AGENTS))

from matuto import admission, login, runner, sessions, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import os, pathlib, sys
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
args = sys.argv[1:]
if (state / "silent").exists():
    sys.exit(1)
names = [arg for arg in args if not arg.startswith("-")]
if names[0] == "list-units":
    sys.exit(0)
if names[0] == "show":
    unit = names[1]
    wanted = [arg.split("=", 1)[1] for arg in args if arg.startswith("--property=")]
    present = (state / "units" / unit).exists()
    values = {"LoadState": "loaded" if present else "not-found",
              "ActiveState": "active" if present else "inactive",
              "SubState": "running" if present else "dead", "Result": "success",
              "ExecMainStatus": "0", "StateChangeTimestampMonotonic": ""}
    print("\\n".join(f"{key}={values[key]}" for key in wanted))
    sys.exit(0)
sys.exit(2)
'''

FAKE_SYSTEMD_RUN = '''#!/usr/bin/env python3
import os, pathlib, sqlite3, sys
state = pathlib.Path(os.environ["RUNNER_SYSTEMD"])
unit = next(arg.split("=", 1)[1] for arg in sys.argv[1:] if arg.startswith("--unit="))
connection = sqlite3.connect(os.environ["RUNNER_STORE"])
port = connection.execute("SELECT port FROM lab_port WHERE setting_id = 1").fetchone()
with (state / "ports.log").open("a") as log:
    log.write(str(port[0] if port else "unset") + "\\n")
units = state / "units"
units.mkdir(exist_ok=True)
(units / unit).write_text("active")
'''


class LabPortHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.state = self.home / "systemd"
        (self.state / "units").mkdir(parents=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, source in (("systemctl", FAKE_SYSTEMCTL), ("systemd-run", FAKE_SYSTEMD_RUN)):
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o700)
        self.environment = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.home / "config"),
            "XDG_DATA_HOME": str(self.home / "data"),
            "RUNNER_SYSTEMD": str(self.state),
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.opened = store.open_store()
        os.environ["RUNNER_STORE"] = str(self.opened.path)
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

    def request(self, path="/api/lab-port", *, method="GET", body=None,
                cookie=True, csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method not in ("GET", "HEAD"):
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        request = urllib.request.Request(self.address + path, method=method,
                                         data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def save(self, port):
        return self.request(method="POST", body={"port": port})

    def test_settings_section_keeps_loopback_fixed_and_loads_its_script(self):
        request = urllib.request.Request(self.address + "/", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            page = response.read()
        self.assertIn(b"The temporary lab server always binds to loopback", page)
        self.assertIn(b"id=\"labPortNumber\"", page)
        self.assertIn(b"/lab-port.js", page)
        self.assertIn(b"/lab-port.css", page)
        style_request = urllib.request.Request(self.address + "/lab-port.css", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(style_request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/css")
            self.assertIn(b"min-height:44px", response.read())
        script_request = urllib.request.Request(self.address + "/lab-port.js", headers={
            "Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={self.token}"})
        with urllib.request.urlopen(script_request, timeout=5) as response:
            self.assertEqual(response.headers.get_content_type(), "text/javascript")
            self.assertIn(b"/api/lab-port", response.read())

    def add_run(self, run_id="serve-test", *, unit="matuto-serve-test.service"):
        self.opened.connection().execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
            " procedure_revision, unit_name, started_at)"
            " VALUES (?, 'language', 'profile-test', 'Test profile', 'serve', '', ?,"
            " '2026-10-13T00:00:00+00:00')", (run_id, unit))
        (self.state / "units" / unit).write_text("active")

    def test_port_is_shown_saved_and_rejects_invalid_or_host_values_unchanged(self):
        self.assertEqual(self.request()[1], {"port": None, "csrf": self.csrf})
        self.assertEqual(self.save(8091), (200, {"port": 8091}))
        for invalid in (0, 65536, "8092", 8092.5, True):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.save(invalid)[0], 409)
                self.assertEqual(self.request()[1]["port"], 8091)
        self.assertEqual(self.request(method="POST", body={"port": 8092, "host": "0.0.0.0"})[0], 409)
        self.assertEqual(self.opened.connection().execute(
            "SELECT count(*) FROM pragma_table_info('lab_port') WHERE name = 'host'").fetchone()[0], 0)

    def test_port_change_that_wins_writer_lock_commits_before_admission(self):
        import datetime

        port_saved = threading.Event()
        release_save = threading.Event()
        start_done = threading.Event()
        result = {}

        class Timestamp:
            def isoformat(self, **_kwargs):
                port_saved.set()
                if not release_save.wait(5):
                    raise TimeoutError("test did not release the port save")
                return "2026-10-13T00:00:00+00:00"

        class ControlledDateTime:
            @staticmethod
            def now(_timezone):
                return Timestamp()

        def start_run():
            running = runner.Runner(serving_state=lambda: admission.STOPPED,
                                    card_holder=lambda: admission.Card(admission.CARD_FREE))
            try:
                result["start"] = running.start(runner.Request.run(
                    "serve", "serve for Test profile", ("true",),
                    runner.Target("language", "profile-test", "Test profile", "fingerprint"), ""))
            finally:
                running.close()
                start_done.set()

        with mock.patch("matuto.lab_port.datetime",
                        SimpleNamespace(datetime=ControlledDateTime, timezone=datetime.timezone)):
            route = threading.Thread(target=lambda: result.setdefault("save", self.save(8092)))
            route.start()
            self.assertTrue(port_saved.wait(5))
            admission_thread = threading.Thread(target=start_run)
            admission_thread.start()
            self.assertFalse(start_done.wait(0.1), "admission must wait for the port transaction")
            release_save.set()
            route.join(10)
            admission_thread.join(10)

        self.assertFalse(route.is_alive())
        self.assertFalse(admission_thread.is_alive())
        self.assertEqual(result["save"], (200, {"port": 8092}))
        self.assertEqual(result["start"].decision, admission.ADMIT)
        self.assertEqual(self.state.joinpath("ports.log").read_text().strip(), "8092")
        self.assertEqual(self.request()[1]["port"], 8092)

    def test_mutation_stays_behind_session_origin_and_csrf_gates(self):
        self.assertEqual(self.save(8091)[0], 200)
        self.assertEqual(self.request(method="POST", body={"port": 8092}, cookie=False)[0], 401)
        self.assertEqual(self.request(method="POST", body={"port": 8092}, csrf=False)[0], 403)
        self.assertEqual(self.request(method="POST", body={"port": 8092}, origin=False)[0], 403)
        self.assertEqual(self.request()[1]["port"], 8091)

    def test_active_run_and_unknown_systemd_state_refuse_without_changing_saved_port(self):
        self.save(8091)
        self.add_run()
        status, body = self.save(8092)
        self.assertEqual(status, 409)
        self.assertIn("serve run for Test profile (serve-test)", body["error"])
        self.assertIn("Runs & jobs", body["error"])
        self.assertEqual(self.request()[1]["port"], 8091)
        self.state.joinpath("silent").touch()
        status, body = self.save(8092)
        self.assertEqual(status, 409)
        self.assertIn("state of the serve run", body["error"])
        self.assertIn("serve-test", body["error"])
        self.assertEqual(self.request()[1]["port"], 8091)

    def test_admission_that_wins_the_writer_lock_refuses_the_port_change(self):
        self.save(8091)
        checked = threading.Event()
        release = threading.Event()
        original = runner.Runner.works

        def pause_after_observation(instance):
            result = original(instance)
            checked.set()
            self.assertTrue(release.wait(5))
            return result

        result = {}
        with mock.patch.object(runner.Runner, "works", pause_after_observation):
            thread = threading.Thread(target=lambda: result.setdefault("response", self.save(8092)))
            thread.start()
            self.assertTrue(checked.wait(5))
            running = runner.Runner(serving_state=lambda: "stopped",
                                    card_holder=lambda: admission.Card(admission.CARD_FREE))
            try:
                started = running.start(runner.Request.run(
                    "serve", "serve for Test profile", ("true",),
                    runner.Target("language", "profile-test", "Test profile", "fingerprint"), ""))
            finally:
                running.close()
            self.assertEqual(started.decision, "admit")
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["response"][0], 409)
        self.assertIn("serve run for Test profile", result["response"][1]["error"])
        self.assertEqual(self.request()[1]["port"], 8091)


if __name__ == "__main__":
    unittest.main()
