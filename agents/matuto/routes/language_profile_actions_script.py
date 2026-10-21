"""Serve the Language profile actions behavior script."""

from pathlib import Path

PATH = "/language-profile-actions.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "language-profile-actions.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The language profile actions script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
