"""The signed-in Model console route and its per-document framing policy."""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, serving_state, sessions, store  # noqa: E402
from matuto.web import ALLOWED_HOST, SESSION_COOKIE, build_server  # noqa: E402

FAKE_SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, sys
state = json.loads(open(os.environ["FAKE_UNITS"]).read())
if "is-active" in sys.argv:
    print(state.get(sys.argv[-1], "inactive"))
elif "list-units" in sys.argv:
    pass
'''


class RouterHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b'{"status":"ok"}' if self.path == "/health" else
                b'{"data":[{"id":"fixture-model","status":{"value":"loaded"}}]}')
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def request(address, path, cookie):
    req = urllib.request.Request(address + path, headers={"Host": ALLOWED_HOST,
                                "Cookie": cookie})
    try:
        return urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        return error


class ConsolePageHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        cls.bin = cls.home / "bin"
        cls.bin.mkdir()
        systemctl = cls.bin / "systemctl"
        systemctl.write_text(FAKE_SYSTEMCTL, encoding="utf-8")
        systemctl.chmod(0o755)
        cls.unit_state = cls.home / "units.json"
        cls.unit_state.write_text(json.dumps({serving_state.ROUTER_UNIT: "active"}), encoding="utf-8")
        cls.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cls.home),
                                               "FAKE_UNITS": str(cls.unit_state),
                                               "PATH": f"{cls.bin}{os.pathsep}{os.environ['PATH']}"})
        cls.env.start()
        cls.router = ThreadingHTTPServer(("127.0.0.1", 0), RouterHandler)
        cls.router_thread = threading.Thread(target=cls.router.serve_forever, daemon=True)
        cls.router_thread.start()
        cls.original_read = serving_state.read
        cls.reader_patch = mock.patch.object(
            serving_state, "read", side_effect=lambda: cls.original_read(router_port=cls.router.server_address[1]))
        cls.reader_patch.start()
        opened = store.open_store()
        opened.close()
        key = cls.home / store.CONFIG_ROOT_NAME / "pi-local" / "api-key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("t" * 40 + "\n", encoding="utf-8")
        key.chmod(0o600)
        login.create_login("operator", "test-password", "test-password")
        token, _expiry = sessions.start_session("operator", "test-password")
        cls.cookie = f"{SESSION_COOKIE}={token}"
        cls.server = build_server(port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.reader_patch.stop()
        cls.router.shutdown()
        cls.router.server_close()
        cls.router_thread.join(timeout=5)
        cls.env.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        self.unit_state.write_text(json.dumps({serving_state.ROUTER_UNIT: "active"}), encoding="utf-8")

    def get(self, path):
        with request(self.address, path, self.cookie) as response:
            return response.status, response.read(), response.headers

    def test_shell_frames_only_the_console_origin_and_exposes_its_status(self):
        status, body, headers = self.get("/")
        self.assertEqual(status, 200)
        csp = headers["Content-Security-Policy"]
        self.assertEqual(re.search(r"frame-src ([^;]+);", csp).group(1), "http://matuto.localhost:8889")
        self.assertIn("frame-ancestors 'none'", csp)
        html = body.decode()
        self.assertIn('id="heading-console"', html)
        self.assertIn('id="model-console-frame"', html)
        frame = re.search(r'<iframe\b[^>]*>', html)
        self.assertIsNotNone(frame)
        self.assertNotRegex(frame.group(0), r'\bsrc=')
        self.assertIn('<small class="hint" hidden="">needs agentic serving</small>', html)

        status, body, _headers = self.get("/api/console")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["state"], "agentic")
        self.assertTrue(data["console"]["usable"])

    def test_other_manager_responses_keep_the_default_no_frame_policy(self):
        _status, _body, headers = self.get("/api/overview")
        csp = headers["Content-Security-Policy"]
        self.assertNotIn("frame-src", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_provider_reports_unusable_from_the_same_unknown_read(self):
        self.unit_state.write_text(json.dumps({serving_state.ROUTER_UNIT: "banana"}), encoding="utf-8")
        _status, body, _headers = self.get("/api/console")
        data = json.loads(body)
        self.assertEqual(data["state"], "unknown")
        self.assertFalse(data["console"]["usable"])
        self.assertFalse(data["console"]["transition"])
        self.assertTrue(data["console"]["reason"])

    def test_provider_marks_a_transition_from_the_serving_units(self):
        self.unit_state.write_text(json.dumps({serving_state.ROUTER_UNIT: "activating"}), encoding="utf-8")
        _status, body, _headers = self.get("/api/console")
        data = json.loads(body)
        self.assertEqual(data["state"], "unknown")
        self.assertTrue(data["console"]["transition"])

    def test_provider_marks_a_voice_router_transition(self):
        self.unit_state.write_text(json.dumps({serving_state.ROUTER_UNIT: "inactive",
                                               serving_state.VOICE_ROUTER_UNIT: "activating"}), encoding="utf-8")
        _status, body, _headers = self.get("/api/console")
        data = json.loads(body)
        self.assertEqual(data["state"], "voice")
        self.assertTrue(data["console"]["transition"])


if __name__ == "__main__":
    unittest.main()
