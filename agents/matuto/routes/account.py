"""Return the authenticated account name and this page request's CSRF token."""

from matuto import login
from matuto.web import RefusedError

PATH = "/api/account"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("Account details are read-only")
    return 200, {"username": login.current_username(), "csrf": request.csrf}
