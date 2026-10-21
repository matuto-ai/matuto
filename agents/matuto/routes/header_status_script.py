"""Serve the header's same-origin status poller."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/header-status.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "header_status.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("the header status script is read-only")
    return 200, SCRIPT.read_bytes(), "text/javascript"
