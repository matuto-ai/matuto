"""Serve the Download origins section's same-origin browser script."""

from pathlib import Path

PATH = "/origins.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "origins.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Download origins script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
