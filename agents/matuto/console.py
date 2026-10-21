"""Serve llama.cpp's console on its own origin, only for signed-in agentic serving."""

import http.client
import re
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from matuto import serving_state, sessions
from matuto.web import MANAGER_ORIGIN, MAX_BODY, SESSION_COOKIE

CONSOLE_HOST = "127.0.0.1"
CONSOLE_PORT = 8889
CONSOLE_ALLOWED_HOST = f"matuto.localhost:{CONSOLE_PORT}"
CONSOLE_ORIGIN = f"http://{CONSOLE_ALLOWED_HOST}"
CONSOLE_ADDRESS = f"{CONSOLE_ORIGIN}/"
ROUTER_HOST = "127.0.0.1"
ROUTER_PORT = 8888
ROUTER_TIMEOUT = 300
REQUEST_HEADERS = {"accept", "content-type", "if-modified-since", "if-none-match", "last-event-id", "range", "x-conversation-id"}
RESPONSE_HEADERS = {"accept-ranges", "cache-control", "content-range", "content-type", "etag", "last-modified", "location", "vary"}
CONSOLE_MUTATIONS = {
    "POST": {"/models/load", "/models/unload", "/v1/chat/completions",
             "/v1/chat/completions/control", "/v1/streams/lookup"},
}


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "Matuto"

    def log_message(self, *_arguments):
        pass

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self.forward
        raise AttributeError(name)

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", f"frame-ancestors {MANAGER_ORIGIN}")

    def reply(self, status, body, content_type="text/plain; charset=utf-8"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.request_version = self.protocol_version
        self.reply(code, "invalid request")

    def session_ok(self):
        raw = self.headers.get("Cookie", "")
        if len(re.findall(r"(?:^|;)\s*" + re.escape(SESSION_COOKIE) + r"=", raw)) != 1:
            return False
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            token = cookie[SESSION_COOKIE].value
        except (CookieError, KeyError, ValueError):
            return False
        try:
            return sessions.session_is_valid(token)
        except Exception:
            return False

    def forward(self):
        self.request_version = self.protocol_version
        if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") != CONSOLE_ALLOWED_HOST:
            self.reply(403, "forbidden host")
            return
        if any(value == "cross-site" for value in self.headers.get_all("Sec-Fetch-Site", [])):
            self.reply(403, "cross-site request refused")
            return
        if not self.session_ok():
            self.reply(403, f"Sign in at {MANAGER_ORIGIN}/ before using the model console.")
            return
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self.reply(400, "invalid request target")
            return
        try:
            state = serving_state.read()
        except Exception:
            state = None
        if _blocked_operation(self.command, unquote(parsed.path)):
            self.reply(403, _operation_refusal(self.command, unquote(parsed.path)))
            return
        if state is None:
            self.reply(503, "The serving state is unknown. Check serving status in Overview.")
            return
        if self.command not in ("GET", "HEAD"):
            origins = self.headers.get_all("Origin", [])
            if len(origins) != 1 or origins[0] != CONSOLE_ORIGIN:
                self.reply(403, "mutation requires the console's exact Origin")
                return
            if parsed.path not in CONSOLE_MUTATIONS.get(self.command, ()):
                self.reply(403, _operation_refusal(self.command, parsed.path))
                return
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            self.reply(400, "invalid request length")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.reply(400, "invalid request length")
            return
        if not 0 <= length <= MAX_BODY:
            self.reply(413, "request body exceeds the 1 MiB limit")
            return

        if state.state != serving_state.AGENTIC or not state.healthy:
            self.reply(503, _serving_refusal(state))
            return
        try:
            key = serving_state.router_key(serving_state.router_key_path())
        except Exception:
            key = None
        if not key:
            self.reply(502, "The router is unavailable; check its configuration and start agentic serving from Overview.")
            return

        body = self.rfile.read(length) if length else None
        headers = {name: value for name, value in self.headers.items()
                   if name.lower() in REQUEST_HEADERS}
        headers["Authorization"] = f"Bearer {key}"
        headers["Accept-Encoding"] = "identity"
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        connection = None
        headers_sent = False
        try:
            connection = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=ROUTER_TIMEOUT)
            connection.request(self.command, path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() not in RESPONSE_HEADERS or name.lower() == "content-encoding":
                    continue
                self.send_header(name, _redact(value, key))
            self.security_headers()
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            headers_sent = True
            if self.command != "HEAD":
                _copy_redacted(response, self.wfile, key)
        except (OSError, TimeoutError, http.client.HTTPException):
            if not headers_sent:
                self.reply(502, "The router did not answer; start or repair agentic serving from Overview.")
        finally:
            if connection is not None:
                connection.close()


class _StreamingRedactor:
    """Redact the router credential without buffering a console response."""

    def __init__(self, key):
        self.secret = key.encode("ascii")
        self.pending = b""

    def feed(self, chunk, final=False):
        data = self.pending + chunk
        cut = len(data) if final else max(0, len(data) - len(self.secret) + 1)
        output, position, search = [], 0, 0
        while True:
            match = data.find(self.secret, search)
            if match < 0 or match >= cut:
                break
            output.extend((data[position:match], b"[redacted]"))
            position = match + len(self.secret)
            search = position
        output.append(data[position:min(cut, len(data))])
        self.pending = data[max(cut, position):]
        return b"".join(output)


def _copy_redacted(response, destination, key):
    redactor = _StreamingRedactor(key)
    while True:
        chunk = response.read1(65_536)
        if not chunk:
            break
        destination.write(redactor.feed(chunk))
        destination.flush()
    if redactor.pending:
        destination.write(redactor.feed(b"", final=True))
        destination.flush()


def _redact(value, key):
    return value.replace(key, "[redacted]")


def _blocked_operation(method, path):
    return (method == "DELETE" or path == "/models" and method == "POST"
            or re.search(r"(?:^|/)(?:download|delete|remove)(?:/|$)", path, re.I) is not None)


def _operation_refusal(method, path):
    if method == "DELETE" or re.search(r"(?:^|/)(?:delete|remove)(?:/|$)", path, re.I):
        return "Model deletion is managed outside Matuto; use external file management."
    if path == "/models" and method == "POST" or re.search(r"(?:^|/)download(?:/|$)", path, re.I):
        return "Get model files through Model files; the native console cannot download models."
    return "This console operation is not allowed. Use Model files to get models and external file management to delete them."


def _serving_refusal(state):
    if state.state == serving_state.STOPPED:
        reason, recovery = "Agentic serving is stopped.", "Start agentic serving from Overview."
    elif state.state == serving_state.VOICE:
        reason, recovery = "Voice serving is active.", "Switch to agentic serving from Overview."
    elif state.state == serving_state.AGENTIC:
        reason, recovery = "Agentic serving is unhealthy.", "Restart serving from Overview."
    else:
        reason, recovery = "The serving state is unknown or in transition.", "Check serving status in Overview."
    if state.reason:
        reason += f" {state.reason}"
    return f"{reason} {recovery} Then reopen the console."


def build_console_server(port=CONSOLE_PORT):
    """Bind only the console's fixed loopback address; tests may request a free port."""
    return ConsoleServer((CONSOLE_HOST, port), ConsoleHandler)
