"""Serve the Install a version page's same-origin script."""

from pathlib import Path

PATH = "/install.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the Install a version script is read-only")
    script = Path(__file__).resolve().parents[1] / "static" / "install.js"
    return 200, script.read_bytes(), "text/javascript"
