"""Serve the Speech profiles page script."""

from pathlib import Path

PATH = "/speech-profiles.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "speech_profiles.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The speech profiles script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
