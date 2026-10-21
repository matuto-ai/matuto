"""The session a mutation route carries itself, until the login guard gates the whole surface.

`matuto.web` already refuses every request without the fixed Host and without the
exact Origin and CSRF token a mutation needs, before any route runs. The session
behind the cookie is checked here as well, so a route added as a file is refused
without a live session rather than relying on a gate that does not exist yet. A
store or a login that cannot be read leaves no session valid, so this fails closed.
"""

from http.cookies import CookieError, SimpleCookie

from matuto import sessions
from matuto.web import SESSION_COOKIE

NO_SESSION = "Sign in to Matuto to reach this page."
REFUSED = (403, {"error": NO_SESSION})


def session_token(request):
    """The token this request's cookie carries, or "" when it carries none."""
    try:
        cookie = SimpleCookie()
        cookie.load(request.headers.get("Cookie", ""))
        value = cookie[SESSION_COOKIE].value
    except (CookieError, KeyError, ValueError):
        return ""
    return value if isinstance(value, str) and value else ""


def require_session(request, *, now=None, config_root=None):
    """Refuse this request unless its session is live for the current login.

    Returns the refusal answer a route returns unchanged, so no route answers a
    signed-out request with anything it might have read.
    """
    token = session_token(request)
    if token and sessions.session_is_valid(token, now=now, config_root=config_root):
        return None
    return REFUSED