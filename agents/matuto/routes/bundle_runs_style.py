"""Serve the same-origin bundle-run section styles."""

from pathlib import Path

PATH = "/bundle-runs.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "bundle_runs.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the bundle-runs styles are read-only")
    return 200, STYLE.read_bytes(), "text/css"
