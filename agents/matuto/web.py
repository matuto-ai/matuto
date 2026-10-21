"""The manager's HTTP surface and its browser protections. README.md holds the route contract.

The address is fixed: loopback only, `http://matuto.localhost:8890/`, no secret in it.
Every request is checked before a route runs, so no discovered route can opt out.
"""

import json
import secrets
from collections import namedtuple
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from matuto import login, sessions
from matuto.discovery import item_names, load_item

MANAGER_HOST = "127.0.0.1"
MANAGER_PORT = 8890
BROWSER_HOST = "matuto.localhost"
ALLOWED_HOST = f"{BROWSER_HOST}:{MANAGER_PORT}"
MANAGER_ORIGIN = f"http://{ALLOWED_HOST}"
MANAGER_ADDRESS = f"{MANAGER_ORIGIN}/"
SESSION_COOKIE = "matuto_session"
ROUTES = "routes"
PUBLIC_ROUTES = frozenset(("/sign-in", "/create-login", "/auth.js"))
MAX_BODY = 1_048_576

# One hand-off of the token to a page: it reads this attribute and sends the value
# back in X-CSRF-Token, which every mutation needs beside an exact Origin match.
Request = namedtuple("Request", "method path query headers body csrf")


class RefusedError(Exception):
    """A route refuses; the answer is a JSON error body."""


def session_cookie(value):
    """The one Set-Cookie for the session. No Domain attribute makes it host-only."""
    return f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/"


def expired_session_cookie():
    """Clear this browser's session without changing any other session."""
    return f"{SESSION_COOKIE}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/"


def session_token(headers):
    """Read only the manager's cookie; malformed cookies count as no session."""
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("Cookie", ""))
    except (CookieError, ValueError):
        return ""
    morsel = cookie.get(SESSION_COOKIE)
    return morsel.value if morsel else ""


def safe_return_to(value):
    """Accept only a local path that cannot redirect back into authentication."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/"
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "/"
    if (parsed.scheme or parsed.netloc or parsed.path in ("/sign-in", "/create-login", "/auth.js", "/sign-out")
            or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return "/"
    return value


class ManagerHandler(BaseHTTPRequestHandler):
    server_version = "Matuto"
    routes = {}

    def log_message(self, *_arguments):
        pass

    def __getattr__(self, name):
        """Send every method, named or not, through the same protections."""
        if name.startswith("do_"):
            return self.dispatch
        raise AttributeError(name)

    def security_headers(self, csp=None):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        nonce = self.csrf
        self.send_header("Content-Security-Policy", csp or
                         f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
                         f"style-src 'self' 'nonce-{nonce}'; base-uri 'none'; "
                         "form-action 'none'; frame-ancestors 'none'")

    def reply(self, status, body=b"", content_type="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        body = body.encode() if isinstance(body, str) else body
        # A refusal can leave the request body unread, so this answer ends the connection.
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        csp = headers.pop("Content-Security-Policy", None)
        for name, value in headers.items():
            self.send_header(name, value)
        self.security_headers(csp)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()

    def send_error(self, code, message=None, explain=None):
        """A protocol-level refusal keeps the protections and the JSON body.

        The parser reaches this before it accepts a request version, and the inherited
        HTTP/0.9 state drops every header, so the answer goes out on our own version.
        """
        self.request_version = self.protocol_version
        reason = message or self.responses.get(code, ("refused",))[0]
        self.reply(code, {"error": reason})

    def refuse(self, reason):
        self.reply(403, {"error": reason})

    def dispatch(self):
        if self.headers.get("Host") != ALLOWED_HOST:
            self.refuse("forbidden host")
            return
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.refuse("cross-site request")
            return
        if self.command not in ("GET", "HEAD"):
            # Bytes, not text: a header byte outside ASCII is a wrong token, not an error.
            supplied = self.headers.get("X-CSRF-Token", "").encode()
            if (self.headers.get("Origin") != MANAGER_ORIGIN
                    or not secrets.compare_digest(supplied, self.csrf.encode())):
                self.refuse("forbidden")
                return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self.reply(400, {"error": "invalid request target"})
            return
        route = self.routes.get(parsed.path)
        if route is None and parsed.path in ("/sign-in", "/create-login"):
            self.reply(404, {"error": "authentication page unavailable"})
            return
        if parsed.path not in PUBLIC_ROUTES:
            token = session_token(self.headers)
            try:
                valid_session = bool(token) and sessions.session_is_valid(token)
            except sessions.SessionUnavailable:
                self.reply(503, {"error": "session validity is temporarily unavailable"})
                return
            if not valid_session:
                refusal_headers = {"Set-Cookie": expired_session_cookie()} if token else {}
                if self.command not in ("GET", "HEAD"):
                    self.reply(401, {"error": "sign-in is needed"}, headers=refusal_headers)
                    return
                if parsed.path == "/api" or parsed.path.startswith("/api/"):
                    self.reply(401, {"error": "sign-in is needed"}, headers=refusal_headers)
                    return
                try:
                    login.login_generation()
                    auth_path = "/sign-in"
                    extra = {"ended": ["1"]} if token else {}
                except login.LoginError as error:
                    if str(error) != login.NO_LOGIN:
                        self.reply(503, {"error": "login unavailable"})
                        return
                    auth_path = "/create-login"
                    extra = {}
                route = self.routes.get(auth_path)
                if route is None:
                    self.reply(503, {"error": "authentication page unavailable"})
                    return
                target = parsed.path + ("?" + parsed.query if parsed.query else "")
                if parsed.path in ("/sign-in", "/create-login", "/auth.js"):
                    target = "/"
                query = {"next": [target], **extra}
                request = Request(self.command, auth_path, query, self.headers, b"", self.csrf)
                self._run_route(route, request, clear_cookie=bool(token))
                return
        if route is None:
            self.reply(404, {"error": "not found"})
            return
        if (self.command not in ("GET", "HEAD") and parsed.path not in PUBLIC_ROUTES):
            refusal = _update_mutation_refusal(parsed.path)
            if refusal:
                from matuto import store, updates
                try:
                    blockers = updates.install_status().get("blockers", [])
                except (OSError, store.StoreError):
                    blockers = [{"text": "Matuto update status is unavailable",
                                 "href": "#/settings/maintenance", "label": "Settings › Maintenance"}]
                self.reply(409, {"error": refusal, "blockers": blockers})
                return
        if (parsed.path == "/api/import-export" and self.command == "POST"
                and (self.headers.get("Transfer-Encoding")
                     or len(self.headers.get_all("Content-Length", [])) != 1)):
            self.reply(400, {"error": "The document is malformed or exceeds the 1 MiB limit."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if parsed.path == "/api/import-export" and length > MAX_BODY:
                self.reply(400, {"error": "The document is malformed or exceeds the 1 MiB limit."})
                return
            if not 0 <= length <= MAX_BODY:
                raise ValueError
            body = self.rfile.read(length) if length else b""
        except ValueError:
            self.reply(400, {"error": "invalid request length"})
            return
        request = Request(self.command, parsed.path, parse_qs(parsed.query), self.headers, body, self.csrf)
        self._run_route(route, request)

    def _run_route(self, route, request, clear_cookie=False):
        try:
            status, payload, *extras = route(request)
        except RefusedError as refusal:
            answer = {"error": str(refusal)}
            if getattr(refusal, "blockers", None):
                answer["blockers"] = refusal.blockers
            self.reply(409, answer)
            return
        except sessions.SessionUnavailable:
            self.reply(503, {"error": "session validity is temporarily unavailable"})
            return
        except Exception:
            self.reply(500, {"error": "route failed"})
            return
        content_type = extras[0] if extras and isinstance(extras[0], str) else "application/json"
        headers = extras[1] if len(extras) > 1 and isinstance(extras[1], dict) else {}
        if clear_cookie and "Set-Cookie" not in headers:
            headers = {**headers, "Set-Cookie": expired_session_cookie()}
        self.reply(status, payload, content_type, headers)
        if len(extras) > 2 and callable(extras[2]):
            extras[2]()


def _update_mutation_refusal(route_path=None):
    """Keep every manager mutation out while the independent updater owns the store."""
    from pathlib import Path
    from matuto import store, updater

    path = store.store_path()
    if not Path(path).exists():
        return ""
    try:
        raw = store.read_update_state(path)
    except (OSError, store.StoreError):
        return "The Matuto update state is unavailable; the mutation was refused."
    try:
        state = json.loads(raw["phase"]) if raw and raw.get("phase") else {}
    except (TypeError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    stage, version = state.get("stage"), state.get("version", "unknown")
    if stage in updater.UPDATE_STAGES:
        return f"Matuto update to {version} is in progress ({stage}); wait for it to finish."
    if stage == "maintenance":
        if route_path == "/api/maintenance":
            return ""
        return f"Matuto update to {version} ended in maintenance; run Repair Matuto before changing records."
    pending = state.get("app_install_pending")
    if pending:
        pending_version = pending.get("version", version) if isinstance(pending, dict) else version
        if stage == "failed" and not (isinstance(pending, dict) and pending.get("relaunching")):
            return ""
        if (stage == "complete" and isinstance(pending, dict)
                and pending.get("version") == version
                and pending.get("list_number") == state.get("list_number")):
            return ""
        handoff = updater.app_install_unit_status(pending)
        if handoff != "active":
            return f"Matuto update to {pending_version} was interrupted; run Repair Matuto to continue."
        return f"Matuto update to {pending_version} is starting; wait for it to finish."
    return ""


def build_server(port=MANAGER_PORT, routes_root=None):
    """Bind the loopback manager socket with every discovered route behind the protections.

    A taken port raises OSError; the caller reports the address rather than falling back.
    `port` and `routes_root` exist so tests bind a free loopback port and their own routes.
    """
    routes = {}
    for name in item_names(ROUTES, root=routes_root):
        module = load_item(ROUTES, name, root=routes_root)
        routes[module.PATH] = module.handle
    handler = type("ProtectedManagerHandler", (ManagerHandler,),
                   {"routes": routes, "csrf": secrets.token_urlsafe(32)})
    return ThreadingHTTPServer((MANAGER_HOST, port), handler)