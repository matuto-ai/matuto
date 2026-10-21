"""Serve the Speech profiles page stylesheet."""

from pathlib import Path

PATH = "/speech-profiles.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "speech_profiles.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The speech profiles stylesheet is read-only.")
    return 200, STYLE.read_bytes(), "text/css"
