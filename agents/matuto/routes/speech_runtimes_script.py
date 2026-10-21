"""Serve the same-origin Runtime › Speech runtimes script."""

from pathlib import Path

PATH = "/speech-runtimes.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Speech runtimes script is read-only.")
    script = Path(__file__).resolve().parents[1] / "static" / "speech_runtimes.js"
    return 200, script.read_bytes(), "text/javascript"
