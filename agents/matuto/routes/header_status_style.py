"""Serve the header status label wrapping rule."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/header-status.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "header_status.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("the header status stylesheet is read-only")
    return 200, STYLE.read_bytes(), "text/css"
