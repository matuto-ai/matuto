"""The same-origin script that renders Overview's record-derived setup state."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/overview.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "overview.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The Overview script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
