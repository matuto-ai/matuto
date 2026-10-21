"""Serve the context-search page script."""

from pathlib import Path

PATH = "/context-search.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "context_search.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The context-search page script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
