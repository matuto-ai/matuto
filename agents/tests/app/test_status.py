"""The signed-in status endpoint discovers providers and only reports live work."""

import json
import os
from pathlib import Path
import shutil
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

from matuto import discovery, login, sessions, store  # noqa: E402
from matuto.routes import status  # noqa: E402
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server  # noqa: E402


class StatusEndpointTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        fake_bin = self.home / "bin"
        fake_bin.mkdir()
        systemctl = fake_bin / "systemctl"
        systemctl.write_text(
            '#!/bin/sh\n'
            'if [ "$2" = "list-units" ]; then echo "0 unit files listed."; exit 0; fi\n'
            'if [ "$2" = "show" ]; then\n'
            '  echo "LoadState=${MATUTO_TEST_LOAD_STATE:-loaded}"\n'
            '  echo "ActiveState=${MATUTO_TEST_ACTIVE_STATE:-active}"\n'
            '  echo "SubState=${MATUTO_TEST_SUB_STATE:-running}"\n'
            '  exit 0\n'
            'fi\n'
            'if [ "$2" = "is-active" ]; then\n'
            '  if [ "$3" = "active-unit" ]; then echo active; else echo inactive; fi\n'
            '  exit 0\n'
            'fi\n'
            'exit 1\n')
        systemctl.chmod(0o755)
        self.path = mock.patch.dict(os.environ, {"PATH": f"{fake_bin}:{os.environ['PATH']}"})
        self.path.start()
        self.addCleanup(self.path.stop)
        store.open_store().close()
        login.create_login("operator", "secret", "secret")
        self.cookie, _ = sessions.start_session("operator", "secret")
        self.server = build_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"

    def request(self, path, cookie=True):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={self.cookie}"
        request = urllib.request.Request(self.address + path, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=4)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def add_job(self, *, job_id="job-1", kind="build", unit="active-unit"):
        opened = store.open_store()
        try:
            opened.connection().execute(
                "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
                " VALUES (?, ?, ?, ?, '2026-10-12T00:00:00+00:00')",
                (job_id, kind, unit, "fixture request"))
        finally:
            opened.close()

    def test_api_is_session_gated_and_returns_stable_ordered_provider_items(self):
        status_code, body, _ = self.request("/api/status", cookie=False)
        self.assertEqual(status_code, 401)
        self.assertEqual(json.loads(body), {"error": "sign-in is needed"})

        database = store.store_path()
        before = database.stat().st_mtime_ns
        status_code, body, _ = self.request("/api/status")
        self.assertEqual(status_code, 200)
        answer = json.loads(body)
        self.assertEqual([item["type"] for item in answer["items"]], ["serving", "current-work"])
        self.assertEqual([item["label"] for item in answer["items"]], ["Not serving", "Nothing running"])
        self.assertEqual(answer["items"][0]["href"], "#/overview/serving")
        self.assertEqual(answer["items"][1]["href"], "#/runs")
        self.assertFalse(answer["console"]["usable"])
        console_status, console_body, _ = self.request("/api/console")
        self.assertEqual(console_status, 200)
        self.assertEqual(answer["console"], json.loads(console_body)["console"])
        self.assertNotIn("secret", body.decode().lower())
        self.assertEqual(database.stat().st_mtime_ns, before, "status reads must not write the store")

        status_code, body, _ = self.request("/api/status")
        self.assertEqual(json.loads(body), answer, "repair selection and item order stay stable")

    def test_finished_and_failed_units_are_not_reported_as_running(self):
        self.add_job()
        for active_state, sub_state in (("active", "exited"), ("failed", "failed"),
                                        ("inactive", "dead")):
            with self.subTest(active_state=active_state, sub_state=sub_state), mock.patch.dict(
                    os.environ, {"MATUTO_TEST_ACTIVE_STATE": active_state,
                                 "MATUTO_TEST_SUB_STATE": sub_state}):
                status_code, body, _ = self.request("/api/status")
            self.assertEqual(status_code, 200)
            current_work = next(item for item in json.loads(body)["items"]
                                if item["type"] == "current-work")
            self.assertEqual(current_work["label"], "Nothing running")

    def test_active_work_and_card_holding_run_are_named_separately(self):
        self.add_job()
        status_code, body, _ = self.request("/api/status")
        self.assertEqual(status_code, 200)
        items = json.loads(body)["items"]
        self.assertEqual(items[0]["label"], "Not serving")
        self.assertEqual(items[1]["label"], "Building runtime")
        self.assertEqual(items[1]["href"], "#/runs?work=job-1")
        self.assertFalse(items[1]["card_holding"])

        opened = store.open_store()
        try:
            connection = opened.connection()
            connection.execute("DELETE FROM jobs")
            connection.execute(
                "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
                " procedure_revision, unit_name, started_at) VALUES (?, 'language', 'fixture',"
                " 'Demo profile', 'smoke', 'test', 'active-unit', '2026-10-12T00:00:01+00:00')",
                ("run-1",))
        finally:
            opened.close()
        status_code, body, _ = self.request("/api/status")
        self.assertEqual(status_code, 200)
        items = json.loads(body)["items"]
        self.assertEqual(items[0]["label"], "Card held by a run")
        self.assertEqual(items[1]["label"], "Smoke: Demo profile")
        self.assertTrue(items[1]["card_holding"])
        self.assertEqual(items[1]["href"], "#/runs?work=run-1")

    def test_new_provider_files_are_collected_and_one_stable_repair_is_shown(self):
        provider_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, provider_root, ignore_errors=True)
        provider_dir = provider_root / "status"
        provider_dir.mkdir()
        for source in (Path(__file__).resolve().parents[2] / "matuto" / "status").glob("*.py"):
            if source.name != "__init__.py":
                shutil.copy(source, provider_dir / source.name)
        providers = {
            "additional": {"name": "additional", "type": "repair", "label": "Extra repair",
                           "detail": "fixture", "href": "#/settings"},
            "another": {"name": "another", "type": "repair", "label": "Another repair",
                        "detail": "fixture", "href": "#/settings"},
        }
        for name, item in providers.items():
            (provider_dir / f"{name}.py").write_text(
                f"def report(connection):\n    return {item!r}\n")
        item_names = discovery.item_names
        load_item = discovery.load_item
        with mock.patch.object(discovery, "item_names", side_effect=lambda location: item_names(
                location, root=provider_root)), mock.patch.object(discovery, "load_item", side_effect=lambda location, name:
                load_item(location, name, root=provider_root)):
            result_status, result = status.handle(SimpleNamespace(method="GET"))
            again_status, again = status.handle(SimpleNamespace(method="GET"))
        self.assertEqual((result_status, again_status), (200, 200))
        repairs = [item for item in result["items"] if item["type"] == "repair"]
        self.assertEqual(repairs, [providers["additional"]])
        self.assertEqual([item for item in again["items"] if item["type"] == "repair"], repairs)

    def test_root_loads_the_poller_and_the_poller_is_session_gated(self):
        status_code, body, headers = self.request("/")
        self.assertEqual(status_code, 200)
        self.assertIn("frame-src http://matuto.localhost:8889;", headers["Content-Security-Policy"])
        self.assertIn(b'<link rel="stylesheet" href="/header-status.css">', body)
        self.assertIn(b'<script src="/header-status.js" defer></script>', body)
        status_code, body, headers = self.request("/header-status.css")
        self.assertEqual(status_code, 200)
        self.assertIn(b"overflow-wrap:anywhere", body)
        self.assertEqual(headers["Content-Type"], "text/css")
        status_code, body, headers = self.request("/header-status.js")
        self.assertEqual(status_code, 200)
        self.assertIn(b"/api/status", body)
        self.assertEqual(headers["Content-Type"], "text/javascript")
        status_code, body, _ = self.request("/header-status.js", cookie=False)
        self.assertEqual(status_code, 200)
        self.assertIn(b"Create login", body)
        status_code, body, _ = self.request("/sign-in?next=%2F%23%2Fsettings&ended=1", cookie=False)
        self.assertEqual(status_code, 200)
        self.assertIn(b"Your session ended", body)
        self.assertIn(b"Unsaved form values are not restored", body)


if __name__ == "__main__":
    unittest.main()
