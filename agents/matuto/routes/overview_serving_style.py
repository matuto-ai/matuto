"""Overview serving controls' same-origin stylesheet."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/overview-serving.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "overview_serving.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The Overview serving stylesheet is read-only.")
    return 200, STYLE.read_bytes(), "text/css"
