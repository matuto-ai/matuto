"""Serve the Settings › Cards touch-target stylesheet."""

from pathlib import Path

PATH = "/cards.css"
STYLESHEET = Path(__file__).resolve().parents[1] / "static" / "cards.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Cards stylesheet is read-only.")
    return 200, STYLESHEET.read_bytes(), "text/css"
