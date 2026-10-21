"""The same-origin script for Settings › Lab port."""

from matuto.page.lab import SCRIPT
from matuto.web import RefusedError

PATH = "/lab-port.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The Lab port script is read-only.")
    return 200, SCRIPT, "text/javascript"
