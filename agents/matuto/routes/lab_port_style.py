"""The same-origin styles for Settings › Lab port."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/lab-port.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "lab-port.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The Lab port styles are read-only.")
    return 200, STYLE.read_bytes(), "text/css"
