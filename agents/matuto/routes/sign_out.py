"""End only the current browser's session."""

from matuto import sessions
from matuto.page import sign_in
from matuto.web import RefusedError, session_token, expired_session_cookie

PATH = "/sign-out"


def handle(request):
    if request.method != "POST":
        raise RefusedError("Sign out requires POST")
    token = session_token(request.headers)
    if token:
        sessions.sign_out(token)
    return 200, sign_in.render(request.csrf, "/", message=(
        "You signed out of this browser. Serving and jobs were not stopped.")), "text/html", {
            "Set-Cookie": expired_session_cookie()
        }
