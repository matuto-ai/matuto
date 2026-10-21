"""The signed-in Storage & artifacts page script."""

from pathlib import Path

from matuto.web import RefusedError

PATH = "/storage.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "storage.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("the storage page script is read-only")
    return 200, SCRIPT.read_bytes(), "text/javascript"
