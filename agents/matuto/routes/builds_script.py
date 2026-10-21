"""Serve the llama.cpp builds section's same-origin script."""

from matuto.page.builds import SCRIPT
from matuto.web import RefusedError

PATH = "/builds.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The runtime builds script is read-only.")
    return 200, SCRIPT, "text/javascript"
