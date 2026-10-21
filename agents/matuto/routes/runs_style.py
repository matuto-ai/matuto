"""The Runs & jobs page's same-origin form styles."""

from pathlib import Path

PATH = "/runs.css"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the Runs & jobs stylesheet is read-only")
    stylesheet = Path(__file__).resolve().parents[1] / "static" / "runs.css"
    return 200, stylesheet.read_bytes(), "text/css"
