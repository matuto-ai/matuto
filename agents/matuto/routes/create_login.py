"""First-use login form and submission; the other public auth route."""

import json
import sqlite3
from email.utils import format_datetime

from matuto import login, sessions, store
from matuto.page import create_login as page, sign_in as sign_in_page
from matuto.web import RefusedError, safe_return_to, session_cookie

PATH = "/create-login"
def handle(request):
    next_path = _one(request.query, "next", "/")
    if request.method in ("GET", "HEAD"):
        try:
            login.login_generation()
        except login.LoginError as error:
            if str(error) == login.NO_LOGIN:
                return 200, page.render(request.csrf, next_path), "text/html"
            return 503, page.render(request.csrf, next_path,
                                    message="The login is unavailable. Try again later."), "text/html"
        return 303, b"", "text/plain", {"Location": _sign_in_url(next_path)}
    if request.method != "POST":
        raise RefusedError("Create login serves GET and POST")
    try:
        data = _json_body(request.body)
        username = _text(data, "username")
        password = _text(data, "password")
        confirmation = _text(data, "confirm")
        next_path = safe_return_to(data.get("next", next_path))
        login.create_login(username, password, confirmation)
        token, expiry = sessions.start_session(username, password)
    except login.LoginError as error:
        if str(error) == "a login already exists":
            return 409, sign_in_page.render(request.csrf, next_path,
                                            message="A login already exists. Sign in instead."), "text/html"
        elif str(error) == login.NO_LOGIN:
            return 503, page.render(request.csrf, next_path,
                                    message="The login changed while the session was starting. Sign in again."), "text/html"
        else:
            return 400, page.render(request.csrf, next_path, message=str(error)), "text/html"
    except (sessions.SessionError, store.StoreError, OSError, sqlite3.Error):
        return 503, sign_in_page.render(request.csrf, next_path, username=username,
            message="The login was created, but a session could not be started. Try signing in again."), "text/html"
    return 303, b"", "text/plain", {
        "Location": next_path,
        "Set-Cookie": f"{session_cookie(token)}; Expires={format_datetime(expiry, usegmt=True)}"
    }


def _json_body(body):
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise RefusedError("invalid Create login form") from None
    if not isinstance(value, dict):
        raise RefusedError("invalid Create login form")
    return value


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str):
        raise RefusedError("invalid Create login form")
    return value


def _one(query, key, default=None):
    values = query.get(key)
    return values[0] if values else default


def _sign_in_url(next_path):
    from urllib.parse import quote
    return "/sign-in?next=" + quote(safe_return_to(next_path), safe="/")
