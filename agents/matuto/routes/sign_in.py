"""Sign-in form and submission; this is one of the two public auth routes."""

import json
import sqlite3

from email.utils import format_datetime

from matuto import login, sessions, store
from matuto.page import sign_in as page
from matuto.web import (RefusedError, expired_session_cookie, safe_return_to,
                        session_cookie, session_token)

PATH = "/sign-in"
def handle(request):
    query = request.query
    next_path = _one(query, "next", "/")
    token_in_cookie = session_token(request.headers)
    ended = _one(query, "ended") == "1" or bool(
        token_in_cookie and not sessions.session_is_valid(token_in_cookie))
    if _one(query, "password_changed") == "1":
        message = "Password changed. Every session ended; sign in again."
    elif _one(query, "signed_out") == "1":
        message = "You signed out of this browser. Serving and jobs were not stopped."
    else:
        message = "Your session ended. Sign in again to continue. Unsaved form values are not restored." if ended else ""
    headers = {"Set-Cookie": expired_session_cookie()} if ended else None
    if request.method in ("GET", "HEAD"):
        try:
            login.login_generation()
        except login.LoginError as error:
            if str(error) == login.NO_LOGIN:
                return 303, b"", "text/plain", {"Location": _create_url(next_path)}
            return 503, page.render(request.csrf, next_path, message="The login is unavailable. Try again later."), "text/html"
        return 200, page.render(request.csrf, next_path, message=message), "text/html", headers or {}
    if request.method != "POST":
        raise RefusedError("sign-in serves GET and POST")
    username = ""
    try:
        data = _json_body(request.body)
        username = _text(data, "username")
        password = _text(data, "password")
        next_path = safe_return_to(data.get("next", next_path))
        token, expiry = sessions.start_session(username, password)
    except sessions.SessionError:
        return 401, page.render(request.csrf, next_path,
                                message="The username or password did not match.",
                                username=username), "text/html"
    except login.LoginError as error:
        if str(error) == login.NO_LOGIN:
            return 409, page.render(request.csrf, next_path,
                                    message="No login is set. Create a login first."), "text/html"
        return 503, page.render(request.csrf, next_path,
                                message="The login is unavailable. Try again later."), "text/html"
    except (store.StoreError, OSError, sqlite3.Error):
        return 503, page.render(request.csrf, next_path, username=username,
                                message="A session could not be started. Try signing in again."), "text/html"
    return 303, b"", "text/plain", {
        "Location": next_path,
        "Set-Cookie": f"{session_cookie(token)}; Expires={format_datetime(expiry, usegmt=True)}"
    }


def _json_body(body):
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        raise RefusedError("invalid sign-in form") from None
    if not isinstance(value, dict):
        raise RefusedError("invalid sign-in form")
    return value


def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str):
        raise RefusedError("invalid sign-in form")
    return value


def _one(query, key, default=None):
    values = query.get(key)
    return values[0] if values else default


def _create_url(next_path):
    from urllib.parse import quote
    return "/create-login?next=" + quote(safe_return_to(next_path), safe="/")
