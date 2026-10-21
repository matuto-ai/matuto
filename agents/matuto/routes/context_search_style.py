"""Serve the context-search page stylesheet."""

from pathlib import Path

PATH = "/context-search.css"
STYLE = Path(__file__).resolve().parents[1] / "static" / "context_search.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The context-search styles are read-only.")
    return 200, STYLE.read_bytes(), "text/css"
