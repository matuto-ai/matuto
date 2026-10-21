"""Serve the Settings › Cards page script."""

from pathlib import Path

PATH = "/cards.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "cards.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Cards page script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
