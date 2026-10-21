"""Serve the same-origin page behavior script for Profiles › Bundles."""

from pathlib import Path

PATH = "/bundles.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "bundles.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The bundle page script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
