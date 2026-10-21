"""Overview's same-origin serving and current-work page script."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/overview-serving.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "overview_serving.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The Overview serving script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
