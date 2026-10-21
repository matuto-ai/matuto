"""Serve the same-origin bundle-run page script."""

from pathlib import Path

PATH = "/bundle-runs.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "bundle_runs.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the bundle-runs page script is read-only")
    return 200, SCRIPT.read_bytes(), "text/javascript"
