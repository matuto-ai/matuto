"""The manager serves only its fixed address, behind the browser protections, to discovered routes."""

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import console, login, sessions, store, web  # noqa: E402
from matuto.discovery import load_item  # noqa: E402
from matuto.web import (ALLOWED_HOST, MANAGER_ADDRESS, MANAGER_HOST, MANAGER_PORT,  # noqa: E402
                        MANAGER_ORIGIN, SESSION_COOKIE, build_server, session_cookie)

FIXTURE_ROUTE = '''"""A route added as a file: it records every mutation it is allowed to serve."""

import json
from pathlib import Path

from matuto.web import RefusedError

STATE = Path(__file__).with_name("state.json")
PATH = "/api/fixture"


def handle(request):
    state = json.loads(STATE.read_text()) if STATE.exists() else {"writes": 0}
    if request.method in ("GET", "HEAD"):
        return 200, state
    if request.method == "POST":
        state["writes"] += 1
        STATE.write_text(json.dumps(state))
        return 200, state
    raise RefusedError("the fixture serves GET and POST")
'''

SECOND_ROUTE = '''PUBLIC = True
PATH = "/api/second"


def handle(request):
    return 200, {"token_reached_the_route": bool(request.csrf)}
'''


def call(address, path, method="GET", host=ALLOWED_HOST, cookie=None, **headers):
    request = urllib.request.Request(f"{address}{path}", method=method,
                                     data=b"{}" if method == "POST" else None,
                                     headers={"Host": host, **({"Cookie": cookie} if cookie else {}),
                                              **{name: value for name, value in headers.items()
                                                 if value is not None}})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def raw(address, request):
    """Send bytes straight at the socket, for the inputs urllib will not build."""
    target = urlsplit(address)
    with socket.create_connection((target.hostname, target.port), timeout=5) as connection:
        connection.sendall(request)
        connection.settimeout(2)
        received = b""
        while True:
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
    return received


class ServingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = Path(tempfile.mkdtemp())
        cls.home = Path(tempfile.mkdtemp())
        cls.config_environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cls.home)})
        cls.config_environment.start()
        store.open_store().close()
        login.create_login("operator", "test-password", "test-password")
        token, _expiry = sessions.start_session("operator", "test-password")
        cls.cookie = f"{SESSION_COOKIE}={token}"
        routes = cls.tree / "routes"
        routes.mkdir()
        (routes / "fixture.py").write_text(FIXTURE_ROUTE)
        cls.state_file = routes / "state.json"
        cls.server = build_server(port=0, routes_root=cls.tree)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.address = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tree, ignore_errors=True)
        shutil.rmtree(cls.home, ignore_errors=True)
        cls.config_environment.stop()

    def setUp(self):
        self.state_file.unlink(missing_ok=True)

    def writes(self):
        return json.loads(self.state_file.read_text())["writes"] if self.state_file.exists() else 0

    def get(self, path="/api/fixture", **kwargs):
        return call(self.address, path, cookie=self.cookie, **kwargs)

    def mutation(self, token="<server token>", origin=MANAGER_ORIGIN, host=ALLOWED_HOST):
        headers = {"X-CSRF-Token": self.token if token == "<server token>" else token}
        if origin is not None:
            headers["Origin"] = origin
        return call(self.address, "/api/fixture", "POST", host=host, cookie=self.cookie, **headers)

    @property
    def token(self):
        return self.server.RequestHandlerClass.csrf

    def assertRefused(self, result, before=0):
        status, body, headers = result
        self.assertEqual(status, 403, body)
        self.assertIn("error", json.loads(body))
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.writes(), before, "a refused request changed the route's state")

    def test_get_from_the_fixed_address_is_served(self):
        status, body, headers = self.get(**{"Sec-Fetch-Site": "none"})

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"writes": 0})
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_get_without_sec_fetch_site_is_served(self):
        status, body, _headers = self.get()

        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"writes": 0})

    def test_get_from_the_manager_to_its_own_console_origin_is_served(self):
        status, _body, _headers = self.get(**{"Sec-Fetch-Site": "same-site"})

        self.assertEqual(status, 200)

    def test_mutation_with_origin_and_token_reaches_its_route(self):
        status, body, _headers = self.mutation()

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"writes": 1})

    def test_read_only_methods_need_no_token(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, _body, _headers = self.get(method=method)

                self.assertEqual(status, 200)

    def test_foreign_host_is_refused_before_the_route(self):
        self.assertRefused(self.get(host="evil.example"))
        before = self.writes()

        self.assertRefused(self.mutation(host="evil.example"), before)

    def test_the_fixed_address_is_bookmarkable(self):
        self.assertEqual(MANAGER_ADDRESS, "http://matuto.localhost:8890/")
        self.assertEqual(ALLOWED_HOST, "matuto.localhost:8890")
        self.assertEqual(MANAGER_PORT, 8890)
        self.assertNotIn("?", MANAGER_ADDRESS)
        self.assertEqual(json.loads(self.get()[1]), {"writes": 0})

    def test_cross_site_request_is_refused(self):
        self.assertRefused(self.get(**{"Sec-Fetch-Site": "cross-site"}))

    def test_mutation_origin_must_match_exactly(self):
        for origin in (None, "null", "http://matuto.localhost:8889", "http://evil.example"):
            with self.subTest(origin=origin):
                before = self.writes()

                self.assertRefused(self.mutation(origin=origin), before)

    def test_mutation_without_the_token_is_refused(self):
        for token in (None, "not-the-token"):
            with self.subTest(token=token):
                before = self.writes()

                self.assertRefused(self.mutation(token=token), before)

    def test_every_other_method_needs_origin_and_token(self):
        for method in ("PATCH", "OPTIONS", "DELETE"):
            with self.subTest(method=method):
                before = self.writes()
                self.assertRefused(call(self.address, "/api/fixture", method), before)
                reached = call(self.address, "/api/fixture", method, cookie=self.cookie,
                               **{"Origin": MANAGER_ORIGIN, "X-CSRF-Token": self.token})
                # The fixture refuses these methods itself, so it ran behind the protections.
                self.assertEqual(reached[0], 409, reached[1])

    def test_a_token_byte_outside_ascii_is_a_refusal_not_a_crash(self):
        answer = raw(self.address, b"POST /api/fixture HTTP/1.1\r\nHost: matuto.localhost:8890\r\n"
                                  b"Origin: http://matuto.localhost:8890\r\nX-CSRF-Token: \xff\r\n"
                                  b"Content-Length: 0\r\n\r\n")

        self.assertIn(b"403 Forbidden", answer)
        self.assertIn(b"frame-ancestors 'none'", answer)
        self.assertIn(b'{"error"', answer)
        self.assertEqual(self.writes(), 0)

    def test_an_unparsable_request_target_is_a_refusal(self):
        answer = raw(self.address, b"GET http://[invalid/api/fixture HTTP/1.1\r\nHost: matuto.localhost:8890\r\n\r\n")

        self.assertIn(b"400 Bad Request", answer)
        self.assertIn(b"frame-ancestors 'none'", answer)
        self.assertIn(b'{"error"', answer)

    def test_a_protocol_refusal_still_sends_the_framing_policy(self):
        for request in (b"GET /api/fixture HTTP/2.0\r\nHost: matuto.localhost:8890\r\n\r\n",
                        b"BOGUS\r\n\r\n"):
            with self.subTest(request=request):
                answer = raw(self.address, request)

                self.assertIn(b"HTTP/1.", answer)
                self.assertIn(b"frame-ancestors 'none'", answer)
                self.assertIn(b'{"error"', answer)

    def test_a_route_refusal_is_a_json_error(self):
        status, body, _headers = call(self.address, "/api/fixture", "DELETE", cookie=self.cookie,
                                      **{"Host": ALLOWED_HOST, "Origin": MANAGER_ORIGIN,
                                         "X-CSRF-Token": self.token})

        self.assertEqual(status, 409, body)
        self.assertEqual(json.loads(body), {"error": "the fixture serves GET and POST"})

    def test_unknown_path_is_a_json_error(self):
        status, body, headers = self.get("/nowhere")

        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "not found"})
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_the_session_cookie_is_httponly_strict_and_host_only(self):
        cookie = session_cookie("session-value")

        self.assertIn("session-value", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn("Domain", cookie)

    def test_each_server_gets_its_own_unguessable_token(self):
        other = build_server(port=0, routes_root=self.tree)
        self.addCleanup(other.server_close)

        self.assertGreaterEqual(len(self.token), 32)
        self.assertNotEqual(self.token, other.RequestHandlerClass.csrf)

    def test_the_listening_socket_is_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertEqual(MANAGER_HOST, "127.0.0.1")

    def test_every_added_route_file_is_served_behind_the_protections(self):
        tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tree, ignore_errors=True)
        (tree / "routes").mkdir()
        (tree / "routes" / "fixture.py").write_text(FIXTURE_ROUTE)
        (tree / "routes" / "second.py").write_text(SECOND_ROUTE)
        server = build_server(port=0, routes_root=tree)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        address = f"http://127.0.0.1:{server.server_address[1]}"

        status, body, _headers = call(address, "/api/second", host=ALLOWED_HOST, cookie=self.cookie)
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body), {"token_reached_the_route": True})
        anonymous_status, _anonymous_body, _headers = call(address, "/api/second")
        self.assertEqual(anonymous_status, 401)
        refused_status, _body, _headers = call(address, "/api/second", host="evil.example")
        self.assertEqual(refused_status, 403)


class TakenPortTest(unittest.TestCase):
    def test_a_taken_port_is_an_error_naming_the_address(self):
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind((MANAGER_HOST, 0))
        held.listen(1)
        self.addCleanup(held.close)

        stream = io.StringIO()
        command = load_item("commands", "serve")
        with mock.patch.object(web, "MANAGER_PORT", held.getsockname()[1]), \
                mock.patch.object(command, "build_server", side_effect=lambda:
                                  build_server(port=web.MANAGER_PORT)), \
                mock.patch.object(command, "build_console_server", side_effect=lambda:
                                  console.build_console_server(port=0)), redirect_stderr(stream):
            status = command.run([])
            # The refused serve moved nowhere: the port still belongs to the holder.
            with self.assertRaises(OSError):
                build_server(port=web.MANAGER_PORT)

        self.assertEqual(status, 1)
        self.assertIn(MANAGER_ADDRESS, stream.getvalue())
        self.assertEqual(MANAGER_PORT, 8890)


if __name__ == "__main__":
    unittest.main()