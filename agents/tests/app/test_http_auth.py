"""Authentication is enforced at the manager's HTTP boundary for every route."""

import datetime
import json
import os
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
import shutil
import selectors
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store, updater  # noqa: E402
from matuto.web import (ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server)  # noqa: E402

FIXTURE_ROUTE = '''PATH = "/private-page"
def handle(request):
    return 200, {"protected": "private-value"}, "application/json"
'''
MUTATION_ROUTE = '''import json
from pathlib import Path
PATH = "/api/mutate"
STATE = Path(__file__).with_name("writes.json")
def handle(request):
    state = int(STATE.read_text()) if STATE.exists() else 0
    if request.method == "POST":
        state += 1
        STATE.write_text(str(state))
    return 200, {"writes": state, "protected": "private-value"}
'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class HttpAuthTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.now = datetime.datetime.now(datetime.timezone.utc)
        moment = sessions._moment
        self.clock = mock.patch("matuto.sessions._moment",
                                side_effect=lambda value=None: moment(self.now if value is None else value))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        store.open_store().close()
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        routes = self.tree / "routes"
        routes.mkdir()
        for source in (AGENTS / "matuto" / "routes").glob("*.py"):
            shutil.copy(source, routes / source.name)
        static = self.tree / "static"
        static.mkdir()
        shutil.copy(AGENTS / "matuto" / "static" / "auth.js", static / "auth.js")
        (routes / "fixture.py").write_text(FIXTURE_ROUTE)
        (routes / "mutate.py").write_text(MUTATION_ROUTE)
        self.server = build_server(port=0, routes_root=self.tree)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, *, method="GET", body=None, cookie=None,
                csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
        if method not in ("GET", "HEAD"):
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.address + path, method=method,
                                         data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def create_login(self):
        return self.request("/create-login", method="POST", body={
            "username": "operator", "password": "secret", "confirm": "secret", "next": "/private-page"
        })

    @staticmethod
    def session_from(headers):
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        return cookie[SESSION_COOKIE].value

    def test_every_signed_out_page_shows_create_login_but_api_refuses_without_data(self):
        for path in ("/", "/private-page"):
            with self.subTest(path=path):
                status, body, _headers = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(b"Create login", body)
        status, body, _headers = self.request("/api/mutate")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "sign-in is needed"})

    def test_retired_capability_route_is_an_unknown_authenticated_route(self):
        login.create_login("operator", "secret", "secret")
        signed_out, sign_in, _headers = self.request("/auth?cap=retired-value")
        self.assertEqual(signed_out, 200)
        self.assertIn(b"Sign in", sign_in)
        token, _expiry = sessions.start_session("operator", "secret")
        status, body, headers = self.request("/auth?cap=retired-value", cookie=token)
        unknown_status, unknown_body, _unknown_headers = self.request("/unknown", cookie=token)
        self.assertEqual((status, body), (unknown_status, unknown_body))
        self.assertNotIn("Set-Cookie", headers)
        self.assertFalse((login.login_directory() / "browser-capability").exists())

    def test_public_forms_load_the_same_origin_csrf_script(self):
        status, body, _headers = self.request("/create-login")
        self.assertEqual(status, 200)
        self.assertIn(b'name="confirm"', body)
        self.assertIn(b'<script src="/auth.js" defer></script>', body)
        self.assertIn(f'name="csrf-token" content="{self.csrf}"'.encode(), body)
        status, script, headers = self.request("/auth.js")
        self.assertEqual(status, 200)
        self.assertIn(b"X-CSRF-Token", script)
        self.assertEqual(headers["Content-Type"], "text/javascript")

    def test_create_login_sets_a_fresh_seven_day_session_and_returns_to_requested_page(self):
        status, _body, headers = self.create_login()
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/private-page")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("Domain", headers["Set-Cookie"])
        self.assertIn("Expires=", headers["Set-Cookie"])
        token = self.session_from(headers)
        saved = store.open_store()
        try:
            recorded = datetime.datetime.fromisoformat(saved.connection().execute(
                "SELECT expires_at FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()[0])
        finally:
            saved.close()
        cookie = SimpleCookie()
        cookie.load(headers["Set-Cookie"])
        cookie_expiry = parsedate_to_datetime(cookie[SESSION_COOKIE]["expires"])
        self.assertEqual(cookie_expiry, recorded.replace(microsecond=0))
        self.assertEqual(recorded, self.now + datetime.timedelta(days=7))
        status, body, _headers = self.request("/private-page", cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"protected": "private-value"})

    def test_created_login_recovers_from_a_store_failure_by_signing_in(self):
        with mock.patch("matuto.sessions.store.open_store", side_effect=store.StoreError("unreadable")):
            status, body, headers = self.create_login()
        self.assertEqual(status, 503)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn(b'<h1>Sign in</h1>', body)
        self.assertIn(b'The login was created', body)
        self.assertTrue(login.check_login("operator", "secret"))
        status, _body, headers = self.request("/sign-in", method="POST", body={
            "username": "operator", "password": "secret", "next": "/private-page"
        })
        self.assertEqual(status, 303)
        self.assertTrue(sessions.session_is_valid(self.session_from(headers)))

    def test_update_health_recognizes_the_real_sign_in_form_not_recovery_help(self):
        with self.assertRaises(updater.UpdateError):
            updater._health(2, self.address, ALLOWED_HOST, "fixture-version")
        login.create_login("operator", "secret", "secret")
        updater._health(2, self.address, ALLOWED_HOST, "fixture-version")

    def test_authenticated_root_shell_receives_a_matching_csp_nonce(self):
        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        status, body, headers = self.request("/", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn(f'nonce="{self.csrf}"'.encode(), body)
        self.assertIn(f"'nonce-{self.csrf}'".encode(), headers["Content-Security-Policy"].encode())

    def test_every_discovered_route_is_gated_and_a_lost_mutation_is_not_replayed(self):
        status, body, _headers = self.request("/private-page")
        self.assertEqual(status, 200)
        self.assertIn(b"Create login", body)
        status, body, _headers = self.request("/api/mutate", method="POST", body={})
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "sign-in is needed"})
        self.assertFalse((self.tree / "routes" / "writes.json").exists())
        _status, _body, headers = self.create_login()
        token = self.session_from(headers)
        status, body, _headers = self.request("/api/mutate", cookie=token)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["writes"], 0)

    def test_create_login_rejects_empty_or_mismatched_fields_without_storing_a_login(self):
        for fields in ({"username": "", "password": "secret", "confirm": "secret"},
                       {"username": "operator", "password": "", "confirm": ""},
                       {"username": "operator", "password": "secret", "confirm": ""},
                       {"username": "operator", "password": "secret", "confirm": "other"}):
            with self.subTest(fields=fields):
                status, _body, _headers = self.request("/create-login", method="POST", body=fields)
                self.assertEqual(status, 400)
                self.assertFalse(login.login_file().exists())

    def test_login_mutations_require_the_exact_origin_and_csrf_token(self):
        for csrf, origin in ((False, True), (True, False)):
            status, _body, _headers = self.request("/create-login", method="POST", body={
                "username": "operator", "password": "secret", "confirm": "secret"
            }, csrf=csrf, origin=origin)
            self.assertEqual(status, 403)
            self.assertFalse(login.login_file().exists())
        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        for path, body in (("/sign-in", {"username": "operator", "password": "secret"}),
                           ("/sign-out", {})):
            for csrf, origin in ((False, True), (True, False)):
                with self.subTest(path=path, csrf=csrf, origin=origin):
                    status, _body, _headers = self.request(path, method="POST", body=body,
                        cookie=token, csrf=csrf, origin=origin)
                    self.assertEqual(status, 403)
                    self.assertTrue(sessions.session_is_valid(token))
                    opened = store.open_store()
                    try:
                        self.assertEqual(opened.connection().execute(
                            "SELECT count(*) FROM sessions").fetchone()[0], 1)
                    finally:
                        opened.close()

    def test_create_login_is_not_offered_or_allowed_after_a_login_exists(self):
        login.create_login("operator", "secret", "secret")
        original = login.login_file().read_bytes()
        status, _body, headers = self.request("/create-login")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/sign-in?next=/")
        status, body, _headers = self.request("/create-login", method="POST", body={
            "username": "attacker", "password": "other", "confirm": "other"
        })
        self.assertEqual(status, 409)
        self.assertIn(b"Sign in instead", body)
        self.assertIn(b"<h1>Sign in</h1>", body)
        self.assertNotIn(b"<h1>Create login</h1>", body)
        self.assertEqual(login.login_file().read_bytes(), original)

    def test_failed_sign_in_keeps_username_and_starts_no_session(self):
        login.create_login("operator", "secret", "secret")
        for username, password in (("operator", "wrong"), ("other", "secret")):
            with self.subTest(username=username):
                status, body, _headers = self.request("/sign-in", method="POST", body={
                    "username": username, "password": password, "next": "/private-page"
                })
                self.assertEqual(status, 401)
                self.assertIn(f'value="{username}"'.encode(), body)
                self.assertIn(b"The username or password did not match", body)
                self.assertIn(b"matuto-manager reset-login", body)
        opened = store.open_store()
        try:
            self.assertEqual(opened.connection().execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        finally:
            opened.close()

    def test_successful_sign_in_uses_a_new_token_and_rejects_foreign_return_targets(self):
        login.create_login("operator", "secret", "secret")
        old, _expiry = sessions.start_session("operator", "secret")
        status, _body, headers = self.request("/sign-in", method="POST", cookie=old, body={
            "username": "operator", "password": "secret", "next": "https://evil.example/path"
        })
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        fresh = self.session_from(headers)
        self.assertNotEqual(fresh, old)
        self.assertTrue(sessions.session_is_valid(old))
        self.assertTrue(sessions.session_is_valid(fresh))

    def test_scheme_relative_and_backslash_return_targets_go_to_overview(self):
        login.create_login("operator", "secret", "secret")
        for target in ("//evil.example/path", "/\\evil.example/path"):
            with self.subTest(target=target):
                status, _body, headers = self.request("/sign-in", method="POST", body={
                    "username": "operator", "password": "secret", "next": target
                })
                self.assertEqual(status, 303)
                self.assertEqual(headers["Location"], "/")

    def test_sign_out_ends_only_this_session_and_clears_its_cookie(self):
        login.create_login("operator", "secret", "secret")
        first, _expiry = sessions.start_session("operator", "secret")
        second, _expiry = sessions.start_session("operator", "secret")
        status, body, headers = self.request("/sign-out", method="POST", cookie=first)
        self.assertEqual(status, 200)
        self.assertIn(b"Serving and jobs were not stopped", body)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertFalse(sessions.session_is_valid(first))
        self.assertTrue(sessions.session_is_valid(second))
        status, body, _headers = self.request("/api/mutate", cookie=second)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["writes"], 0)

    def test_expired_or_ended_cookie_gets_expiry_message_and_is_cleared(self):
        login.create_login("operator", "secret", "secret")
        status, _body, headers = self.request("/sign-in", method="POST", body={
            "username": "operator", "password": "secret", "next": "/private-page"
        })
        self.assertEqual(status, 303)
        expired = self.session_from(headers)
        self.now += sessions.SESSION_LIFETIME + datetime.timedelta(seconds=1)
        status, body, headers = self.request("/private-page", cookie=expired)
        self.assertEqual(status, 200)
        self.assertIn(b"Your session ended", body)
        self.assertIn(b"Unsaved form values are not restored", body)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.now -= sessions.SESSION_LIFETIME + datetime.timedelta(seconds=1)
        sessions.sign_out(expired)
        status, body, _headers = self.request("/private-page", cookie=expired)
        self.assertEqual(status, 200)
        self.assertIn(b"Your session ended", body)

    def test_signed_out_deep_page_returns_after_sign_in(self):
        login.create_login("operator", "secret", "secret")
        status, body, _headers = self.request("/private-page?view=details")
        self.assertEqual(status, 200)
        self.assertIn(b'<h1>Sign in</h1>', body)
        self.assertIn(b'value="/private-page?view=details"', body)
        status, _body, headers = self.request("/sign-in", method="POST", body={
            "username": "operator", "password": "secret", "next": "/private-page?view=details"
        })
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/private-page?view=details")
        status, body, _headers = self.request(headers["Location"], cookie=self.session_from(headers))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"protected": "private-value"})

    def test_session_cookie_survives_a_manager_restart_in_another_process(self):
        status, _body, headers = self.create_login()
        self.assertEqual(status, 303)
        token = self.session_from(headers)
        source = (
            "import sys\n"
            f"sys.path.insert(0, {str(AGENTS)!r})\n"
            "from pathlib import Path\n"
            "from matuto.web import build_server\n"
            f"server = build_server(port=0, routes_root=Path({str(self.tree)!r}))\n"
            "print(server.server_address[1], flush=True)\n"
            "server.serve_forever()\n"
        )
        environment = {**os.environ, "XDG_CONFIG_HOME": str(self.home)}
        process = subprocess.Popen([sys.executable, "-B", "-c", source], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "restarted manager did not report readiness")
            port = process.stdout.readline().strip()
            self.assertTrue(port.isdigit(), process.stderr.read() if process.poll() is not None else "invalid port")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/private-page",
                headers={"Host": ALLOWED_HOST, "Cookie": f"{SESSION_COOKIE}={token}"})
            with self.opener.open(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"protected": "private-value"})
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_store_unavailability_never_serves_data_or_expires_a_session(self):
        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        path = store.store_path()
        os.chmod(path, 0o644)
        try:
            status, body, _headers = self.request("/private-page", cookie=token)
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(status, 503)
        self.assertNotIn(b"private-value", body)
        self.assertNotIn("Set-Cookie", _headers)
        with mock.patch("matuto.sessions.store.read_session_at", side_effect=store.StoreError("unreadable")):
            status, body, headers = self.request("/private-page", cookie=token)
            self.assertEqual(status, 503)
            self.assertNotIn(b"private-value", body)
            self.assertNotIn("Set-Cookie", headers)
            status, body, headers = self.request("/api/mutate", cookie=token)
            self.assertEqual(status, 503)
            self.assertNotIn(b"private-value", body)
            self.assertNotIn("Set-Cookie", headers)

    def test_password_change_reset_and_unsafe_login_never_serve_protected_data(self):
        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        login.change_password("secret", "next-secret", "next-secret")
        status, body, _headers = self.request("/private-page", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", body)
        self.assertNotIn(b"private-value", body)
        login.reset_login()
        status, body, _headers = self.request("/private-page", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn(b"Create login", body)
        self.assertNotIn(b"private-value", body)

        login.create_login("operator", "secret", "secret")
        token, _expiry = sessions.start_session("operator", "secret")
        os.chmod(login.login_file(), 0o644)
        status, body, _headers = self.request("/private-page", cookie=token)
        self.assertEqual(status, 503)
        self.assertNotIn(b"private-value", body)


if __name__ == "__main__":
    unittest.main()
