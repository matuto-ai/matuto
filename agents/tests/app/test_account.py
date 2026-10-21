"""Settings › Account changes passwords and signs out through the manager HTTP boundary."""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store  # noqa: E402
from matuto.web import ALLOWED_HOST, MANAGER_ORIGIN, SESSION_COOKIE, build_server  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class AccountHttpTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        store.open_store().close()
        login.create_login("operator", "current-secret", "current-secret")
        self.first, _ = sessions.start_session("operator", "current-secret")
        self.second, _ = sessions.start_session("operator", "current-secret")
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        shutil.copytree(AGENTS / "matuto" / "routes", self.tree / "routes")
        shutil.copytree(AGENTS / "matuto" / "static", self.tree / "static")
        self.server = build_server(port=0, routes_root=self.tree)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.csrf = self.server.RequestHandlerClass.csrf
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, *, method="GET", body=None, cookie=None, csrf=True, origin=True):
        headers = {"Host": ALLOWED_HOST}
        if cookie:
            headers["Cookie"] = f"{SESSION_COOKIE}={cookie}"
        if method not in ("GET", "HEAD"):
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
            if origin:
                headers["Origin"] = MANAGER_ORIGIN
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.address + path, method=method,
                                         data=data, headers=headers)
        try:
            response = self.opener.open(request, timeout=5)
            return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    @staticmethod
    def record_bytes():
        return login.login_file().read_bytes()

    def change(self, token, current="current-secret", new="next-secret", confirm="next-secret", **extra):
        return self.request("/api/account/change-password", method="POST", cookie=token,
                            body={"current_password": current, "new_password": new,
                                  "confirmation": confirm, **extra})

    def test_account_section_shows_read_only_username_and_protected_route(self):
        status, body, _ = self.request("/", cookie=self.first)
        self.assertEqual(status, 200)
        self.assertIn(b"Account", body)
        self.assertIn(b'class="account-username"', body)
        self.assertNotIn(b'name="username"', body)
        self.assertIn(b"Signing out ends this browser's session only", body)
        status, body, _ = self.request("/api/account", cookie=self.first)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"username": "operator", "csrf": self.csrf})
        status, body, _ = self.request("/api/account")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"error": "sign-in is needed"})
        self.assertNotIn(b'"username": "operator"', body)

    def test_success_changes_password_keeps_username_and_ends_every_session(self):
        status, body, headers = self.change(self.first)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/sign-in?password_changed=1")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(login.current_username(), "operator")
        self.assertFalse(sessions.session_is_valid(self.first))
        self.assertFalse(sessions.session_is_valid(self.second))
        self.assertTrue(login.check_login("operator", "next-secret"))
        self.assertFalse(login.check_login("operator", "current-secret"))
        status, body, _ = self.request(headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn(b"Password changed. Every session ended; sign in again.", body)

    def test_invalid_changes_and_supplied_username_leave_login_and_sessions_unchanged(self):
        before = self.record_bytes()
        invalid = (("wrong", "next-secret", "next-secret", "current password is wrong"),
                   ("current-secret", "", "", "a new password is required"),
                   ("current-secret", "next-secret", "different", "passwords do not match"))
        for current, new, confirm, message in invalid:
            with self.subTest(message=message):
                status, body, _ = self.change(self.first, current, new, confirm)
                self.assertEqual(status, 400)
                self.assertIn(message, json.loads(body)["error"])
                self.assertEqual(self.record_bytes(), before)
                self.assertTrue(sessions.session_is_valid(self.first))
                self.assertTrue(sessions.session_is_valid(self.second))
        status, body, _ = self.change(self.first, username="attacker")
        self.assertEqual(status, 409)
        self.assertEqual(self.record_bytes(), before)
        self.assertEqual(login.current_username(), "operator")
        self.assertTrue(sessions.session_is_valid(self.first))
        self.assertTrue(sessions.session_is_valid(self.second))

    def test_change_password_requires_origin_csrf_and_a_valid_session(self):
        payload = {"current_password": "current-secret", "new_password": "next-secret",
                   "confirmation": "next-secret"}
        for options in ({"csrf": False}, {"origin": False}):
            with self.subTest(options=options):
                status, body, _ = self.request("/api/account/change-password", method="POST",
                                               cookie=self.first, body=payload, **options)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "forbidden"})
                self.assertTrue(sessions.session_is_valid(self.first))
        status, body, _ = self.request("/api/account/change-password", method="POST", body=payload)
        self.assertEqual(status, 401)
        self.assertTrue(login.check_login("operator", "current-secret"))

    def test_sign_out_only_ends_its_browser_session_and_checks_csrf_and_origin(self):
        status, body, _ = self.request("/sign-out", method="POST", cookie=self.first, body={})
        self.assertEqual(status, 200)
        self.assertIn(b"Serving and jobs were not stopped", body)
        self.assertFalse(sessions.session_is_valid(self.first))
        self.assertTrue(sessions.session_is_valid(self.second))
        for options in ({"csrf": False}, {"origin": False}):
            with self.subTest(options=options):
                status, body, _ = self.request("/sign-out", method="POST", cookie=self.second,
                                               body={}, **options)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "forbidden"})
                self.assertTrue(sessions.session_is_valid(self.second))


if __name__ == "__main__":
    unittest.main()
