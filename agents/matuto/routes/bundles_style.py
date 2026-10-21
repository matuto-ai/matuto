"""Serve the same-origin styles for Profiles › Bundles."""

from pathlib import Path

PATH = "/bundles.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "bundles.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The bundle page styles are read-only.")
    return 200, STYLE.read_bytes(), "text/css"
