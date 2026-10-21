"""Serve the Download origins section's same-origin styles."""

from pathlib import Path

PATH = "/origins.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "origins.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Download origins style is read-only.")
    return 200, STYLE.read_bytes(), "text/css"
