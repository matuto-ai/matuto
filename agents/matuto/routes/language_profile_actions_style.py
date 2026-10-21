"""Serve the Language profile actions styles."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/language-profile-actions.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "language-profile-actions.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The language profile actions stylesheet is read-only.")
    return 200, STYLE.read_bytes(), "text/css"
