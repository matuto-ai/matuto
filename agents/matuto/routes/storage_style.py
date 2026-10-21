"""The signed-in Storage & artifacts page styles."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/storage.css"
STYLES = Path(__file__).resolve().parents[1] / "static" / "storage.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("the storage page stylesheet is read-only")
    return 200, STYLES.read_bytes(), "text/css"
