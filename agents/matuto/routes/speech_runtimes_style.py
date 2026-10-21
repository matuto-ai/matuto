"""Serve the Runtime › Speech runtimes stylesheet."""

from pathlib import Path

PATH = "/speech-runtimes.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Speech runtimes stylesheet is read-only.")
    stylesheet = Path(__file__).resolve().parents[1] / "static" / "speech_runtimes.css"
    return 200, stylesheet.read_bytes(), "text/css"
