"""Change the signed-in operator's password and return them to sign-in."""

import json

from matuto import login
from matuto.web import RefusedError, expired_session_cookie

PATH = "/api/account/change-password"
FIELDS = {"current_password", "new_password", "confirmation"}


def handle(request):
    if request.method != "POST":
        raise RefusedError("Change password requires POST")
    try:
        data = json.loads(request.body)
    except (UnicodeDecodeError, ValueError):
        raise RefusedError("invalid Change password form") from None
    if not isinstance(data, dict) or set(data) != FIELDS or any(
            not isinstance(data[field], str) for field in FIELDS):
        raise RefusedError("invalid Change password form")
    try:
        login.change_password(data["current_password"], data["new_password"],
                             data["confirmation"])
    except login.LoginError as error:
        return 400, {"error": str(error)}
    return 303, b"", "text/plain", {
        "Location": "/sign-in?password_changed=1",
        "Set-Cookie": expired_session_cookie(),
    }
