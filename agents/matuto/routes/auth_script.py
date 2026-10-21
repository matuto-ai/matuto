"""The public same-origin script used by the sign-in and first-use forms."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/auth.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "auth.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("the authentication script is read-only")
    return 200, SCRIPT.read_bytes(), "text/javascript"
