"""The console proxy's HTTP boundary, including the router and serving gate."""

import datetime
import http.server
import os
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import console, login, serving_state, sessions  # noqa: E402
from matuto.discovery import load_item  # noqa: E402

KEY = "0123456789abcdef0123456789abcdef01234567"


class RouterHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []

    def log_message(self, *_arguments):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def do_DELETE(self):
        self.respond()

    def respond(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.requests.append((self.command, self.path, dict(self.headers.items()), self.rfile.read(length)))
        if self.path == "/health":
            self.send_json(b'{"status":"ok"}')
            return
        if self.path == "/models":
            self.send_json(b'{"data":[{"id":"test-model","status":{"value":"loaded"}}]}')
            return
        chunks = (b"upstream echoed " + KEY.encode()[:12], KEY.encode()[12:] + b" tail")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-Secret", KEY)
        self.send_header("Location", "/" + KEY)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def send_json(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ConsoleProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        cls.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cls.home)})
        cls.environment.start()
        login.create_login("operator", "password", "password")
        key_path = serving_state.router_key_path()
        key_path.parent.mkdir(parents=True, mode=0o700)
        key_path.parent.chmod(0o700)
        key_path.write_text(KEY + "\n", encoding="ascii")
        key_path.chmod(0o600)
        cls.token, _expiry = sessions.start_session("operator", "password")
        cls.expired_token, _expiry = sessions.start_session(
            "operator", "password", now=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))
        RouterHandler.requests = []
        cls.router = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RouterHandler)
        cls.router_thread = threading.Thread(target=cls.router.serve_forever, daemon=True)
        cls.router_thread.start()
        cls.console_server = console.build_console_server(port=0)
        cls.console_thread = threading.Thread(target=cls.console_server.serve_forever, daemon=True)
        cls.console_thread.start()
        cls.address = f"http://127.0.0.1:{cls.console_server.server_address[1]}"
        cls.router_port_patch = mock.patch.object(console, "ROUTER_PORT", cls.router.server_address[1])
        cls.router_port_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.router_port_patch.stop()
        cls.console_server.shutdown()
        cls.console_server.server_close()
        cls.console_thread.join(timeout=5)
        cls.router.shutdown()
        cls.router.server_close()
        cls.router_thread.join(timeout=5)
        cls.environment.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        RouterHandler.requests.clear()
        self.serving_patch = mock.patch.object(serving_state, "read", return_value=serving_state.State(
            serving_state.AGENTIC, healthy=True, model="test-model"))
        self.serving_patch.start()
        self.addCleanup(self.serving_patch.stop)

    def request(self, path="/", method="GET", *, token=None, host=None, origin=None,
                sec_fetch_site=None, body=None, extra=None, timeout=5):
        headers = {"Host": host or console.CONSOLE_ALLOWED_HOST}
        if token is not None:
            headers["Cookie"] = f"{console.SESSION_COOKIE}={token}"
        if origin is not None:
            headers["Origin"] = origin
        if sec_fetch_site is not None:
            headers["Sec-Fetch-Site"] = sec_fetch_site
        headers.update(extra or {})
        request = urllib.request.Request(self.address + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def signed_in(self, **kwargs):
        return self.request(token=self.token, **kwargs)

    def test_console_serves_under_manager_framing_policy_and_injects_only_server_key(self):
        status, body, headers = self.signed_in()

        self.assertEqual(status, 200)
        self.assertNotIn(KEY.encode(), body)
        self.assertIn(b"[redacted]", body)
        self.assertEqual(headers["Content-Security-Policy"], f"frame-ancestors {console.MANAGER_ORIGIN}")
        self.assertNotIn("X-Frame-Options", headers)
        self.assertNotIn("X-Secret", headers)
        self.assertEqual(headers["Location"], "/[redacted]")
        method, path, upstream_headers, _body = RouterHandler.requests[0]
        self.assertEqual((method, path), ("GET", "/"))
        self.assertEqual(upstream_headers["Authorization"], f"Bearer {KEY}")
        self.assertNotIn("Cookie", upstream_headers)

    def test_an_allowlisted_mutation_reaches_router_only_with_console_origin(self):
        status, _body, _headers = self.signed_in(
            path="/models/load", method="POST", origin=console.CONSOLE_ORIGIN,
            body=b"{}", extra={"Authorization": "Bearer browser-secret", "X-Not-Allowed": "no"})

        self.assertEqual(status, 200)
        forwarded = RouterHandler.requests[0][2]
        self.assertEqual(forwarded["Authorization"], f"Bearer {KEY}")
        self.assertNotIn("Cookie", forwarded)
        self.assertNotIn("X-Not-Allowed", forwarded)

    def test_host_cross_site_missing_session_and_bad_origin_never_reach_router(self):
        cases = (
            dict(token=self.token, host="evil.example"),
            dict(token=self.token, sec_fetch_site="cross-site"),
            dict(token=None),
            dict(token="altered-session"),
            dict(token=self.expired_token),
            dict(token=self.token, method="POST", path="/models/load", origin=None, body=b"{}"),
            dict(token=self.token, method="POST", path="/models/load", origin=console.MANAGER_ORIGIN, body=b"{}"),
            dict(token=self.token, method="POST", path="/models/load", origin="http://evil.example", body=b"{}"),
        )
        for case in cases:
            with self.subTest(case=case):
                status, _body, headers = self.request(**case)
                self.assertEqual(status, 403)
                self.assertEqual(headers["Content-Security-Policy"], f"frame-ancestors {console.MANAGER_ORIGIN}")
                self.assertNotIn("X-Frame-Options", headers)
        self.assertEqual(RouterHandler.requests, [])

    def test_download_and_delete_are_refused_with_their_management_handoffs(self):
        cases = (
            ("POST", "/models", "Model files"),
            ("GET", "/models/download", "Model files"),
            ("GET", "/models/down%6coad", "Model files"),
            ("POST", "/models/download", "Model files"),
            ("DELETE", "/models/test-model", "external file management"),
        )
        states = (serving_state.STOPPED, serving_state.AGENTIC, serving_state.VOICE,
                  serving_state.UNKNOWN_STATE)
        answers = [serving_state.State(state, healthy=state == serving_state.AGENTIC)
                   for _case in cases for state in states]
        with mock.patch.object(serving_state, "read", side_effect=answers):
            for method, path, pointer in cases:
                for state in states:
                    with self.subTest(method=method, path=path, state=state):
                        status, body, _headers = self.signed_in(
                            method=method, path=path, origin=console.CONSOLE_ORIGIN, body=b"{}")
                        self.assertEqual(status, 403)
                        self.assertIn(pointer.encode(), body)
        self.assertEqual(RouterHandler.requests, [])

    def test_body_limit_is_enforced_before_reading_or_forwarding(self):
        raw = socket.create_connection(("127.0.0.1", self.console_server.server_address[1]), timeout=5)
        raw.sendall((f"POST /models/load HTTP/1.1\r\nHost: {console.CONSOLE_ALLOWED_HOST}\r\n"
                     f"Origin: {console.CONSOLE_ORIGIN}\r\nCookie: {console.SESSION_COOKIE}={self.token}\r\n"
                     f"Content-Length: {console.MAX_BODY + 1}\r\n\r\n").encode())
        response = raw.recv(4096)
        raw.close()

        self.assertIn(b"413 Request Entity Too Large", response)
        self.assertEqual(RouterHandler.requests, [])

    def test_each_request_reads_serving_state_and_blocks_with_specific_recovery(self):
        states = (
            (serving_state.State(serving_state.STOPPED), b"stopped", b"Start agentic serving"),
            (serving_state.State(serving_state.AGENTIC, healthy=False, reason="router health failed"),
             b"unhealthy", b"Restart serving"),
            (serving_state.State(serving_state.VOICE, healthy=True), b"Voice serving", b"Switch to agentic serving"),
            (serving_state.State(serving_state.UNKNOWN_STATE, healthy=False, reason="router did not answer"),
             b"router did not answer", b"Check serving status"),
        )
        with mock.patch.object(serving_state, "read", side_effect=[state for state, _reason, _action in states]) as read_state:
            for _state, reason, recovery in states:
                status, body, _headers = self.signed_in()
                self.assertEqual(status, 503)
                self.assertIn(reason, body)
                self.assertIn(recovery, body)

        self.assertEqual(read_state.call_count, len(states))
        self.assertEqual(RouterHandler.requests, [])

    def test_switch_to_voice_between_requests_blocks_the_next_request(self):
        with mock.patch.object(serving_state, "read", side_effect=(
                serving_state.State(serving_state.AGENTIC, healthy=True),
                serving_state.State(serving_state.VOICE, healthy=True))):
            self.assertEqual(self.signed_in()[0], 200)
            self.assertEqual(self.signed_in()[0], 503)

        self.assertEqual(len(RouterHandler.requests), 1)

    def test_router_that_does_not_answer_gives_a_bounded_safe_error(self):
        class SilentRouter(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                time.sleep(0.3)

        silent = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SilentRouter)
        silent.daemon_threads = True
        thread = threading.Thread(target=silent.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(silent.shutdown)
        self.addCleanup(silent.server_close)
        with mock.patch.object(console, "ROUTER_PORT", silent.server_address[1]), \
                mock.patch.object(console, "ROUTER_TIMEOUT", 0.05):
            status, body, headers = self.signed_in()

        self.assertEqual(status, 502)
        self.assertLessEqual(len(body), 256)
        self.assertNotIn(KEY.encode(), body)
        self.assertEqual(headers["Content-Security-Policy"], f"frame-ancestors {console.MANAGER_ORIGIN}")

    def test_real_serving_reader_gates_http_proxy_with_fake_systemctl_and_router(self):
        self.serving_patch.stop()
        state_file = self.home / "systemd-state"
        systemctl_dir = self.home / "fake-bin"
        systemctl_dir.mkdir()
        systemctl = systemctl_dir / "systemctl"
        systemctl.write_text(
            "#!/bin/sh\n"
            "state=$(cat \"$MATUTO_TEST_SYSTEMD_STATE\")\n"
            "if [ \"$state\" = silent ]; then sleep 1; exit 0; fi\n"
            "case \"$2\" in\n"
            "  is-active)\n"
            "    case \"$3\" in\n"
            "      matuto-app-router.service) [ \"$state\" = agentic ] && echo active || echo inactive ;;\n"
            "      matuto-app-voice-router.service) [ \"$state\" = voice ] && echo active || echo inactive ;;\n"
            "      *) echo inactive ;;\n"
            "    esac ;;\n"
            "  list-units) echo '0 unit files listed.' ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n",
            encoding="utf-8")
        systemctl.chmod(0o700)
        original_read = serving_state.read
        environment = {"PATH": f"{systemctl_dir}:{os.environ['PATH']}",
                      "MATUTO_TEST_SYSTEMD_STATE": str(state_file)}
        try:
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(serving_state, "SECONDS", 0.05), \
                    mock.patch.object(serving_state, "read", side_effect=lambda: original_read(
                        router_port=self.router.server_address[1])):
                state_file.write_text("agentic", encoding="utf-8")
                status, _body, _headers = self.signed_in()
                self.assertEqual(status, 200)
                self.assertEqual([request[1] for request in RouterHandler.requests],
                                 ["/models", "/health", "/"])
                self.assertEqual(RouterHandler.requests[-1][2]["Authorization"], f"Bearer {KEY}")

                state_file.write_text("voice", encoding="utf-8")
                status, body, _headers = self.signed_in()
                self.assertEqual(status, 503)
                self.assertIn(b"Voice serving is active", body)
                self.assertEqual(len(RouterHandler.requests), 3)

                state_file.write_text("silent", encoding="utf-8")
                status, body, _headers = self.signed_in()
                self.assertEqual(status, 503)
                self.assertIn(b"unknown or in transition", body)
                self.assertEqual(len(RouterHandler.requests), 3)
        finally:
            self.serving_patch.start()

    def test_slow_streaming_chat_response_is_forwarded_completely(self):
        event = b'data: {"choices":[{"delta":{"content":"' + b"x" * 120 + b'"}}]}\n\n'
        done = b"data: [DONE]\n\n"

        class DelayedRouter(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_arguments):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(f"{len(event):X}\r\n".encode() + event + b"\r\n")
                self.wfile.flush()
                time.sleep(5.2)
                self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n0\r\n\r\n")

        slow_router = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DelayedRouter)
        slow_router_thread = threading.Thread(target=slow_router.serve_forever, daemon=True)
        slow_router_thread.start()
        self.addCleanup(slow_router_thread.join, 5)
        self.addCleanup(slow_router.shutdown)
        self.addCleanup(slow_router.server_close)
        with mock.patch.object(console, "ROUTER_PORT", slow_router.server_address[1]):
            status, body, _headers = self.signed_in(
                path="/v1/chat/completions", method="POST", origin=console.CONSOLE_ORIGIN,
                body=b"{}", timeout=12)

        self.assertEqual(status, 200)
        self.assertEqual(body, event + done)

    def test_console_protocol_refusal_includes_security_headers_for_http_0_9(self):
        raw = socket.create_connection(("127.0.0.1", self.console_server.server_address[1]), timeout=5)
        raw.sendall(b"GET /\r\n\r\n")
        response = bytearray()
        while chunk := raw.recv(4096):
            response.extend(chunk)
        raw.close()

        self.assertTrue(response.startswith(b"HTTP/1.0 403 Forbidden\r\n"))
        self.assertIn(b"Content-Security-Policy: frame-ancestors " + console.MANAGER_ORIGIN.encode(), response)
        self.assertEqual(RouterHandler.requests, [])

    def test_same_site_and_none_fetch_metadata_are_allowed(self):
        for value in ("same-site", "none"):
            with self.subTest(value=value):
                status, _body, _headers = self.signed_in(sec_fetch_site=value)
                self.assertEqual(status, 200)

        self.assertEqual(len(RouterHandler.requests), 2)

    def test_every_other_mutation_method_is_off_the_allowlist(self):
        for method in ("PUT", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, _body, _headers = self.signed_in(
                    path="/models/load", method=method, origin=console.CONSOLE_ORIGIN)
                self.assertEqual(status, 403)

        self.assertEqual(RouterHandler.requests, [])

    def test_console_uses_only_its_fixed_loopback_address(self):
        self.assertEqual(console.CONSOLE_ADDRESS, "http://matuto.localhost:8889/")
        self.assertEqual(self.console_server.server_address[0], "127.0.0.1")
        self.assertEqual(console.CONSOLE_PORT, 8889)


class ConsoleTakenPortTest(unittest.TestCase):
    def test_taken_fixed_console_port_refuses_without_fallback(self):
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind((console.CONSOLE_HOST, 0))
        held.listen(1)
        self.addCleanup(held.close)
        command = load_item("commands", "serve")
        stream = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(stream.close)
        with mock.patch.object(console, "CONSOLE_PORT", held.getsockname()[1]), \
                mock.patch.object(command, "build_console_server", side_effect=lambda:
                                  console.build_console_server(port=console.CONSOLE_PORT)), \
                mock.patch.object(command, "build_server", side_effect=AssertionError("manager must not bind")), \
                mock.patch("sys.stderr", stream):
            result = command.run([])
        stream.seek(0)

        self.assertEqual(result, 1)
        self.assertIn(console.CONSOLE_ADDRESS, stream.read())


if __name__ == "__main__":
    unittest.main()
