"""Serve the same-origin update-section script."""

from pathlib import Path

PATH = "/updates.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "updates.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The update page script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
