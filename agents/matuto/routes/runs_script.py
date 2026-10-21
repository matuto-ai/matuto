"""The Runs & jobs page's same-origin browser script."""

from pathlib import Path

PATH = "/runs.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("the Runs & jobs script is read-only")
    script = Path(__file__).resolve().parents[1] / "static" / "runs.js"
    return 200, script.read_bytes(), "text/javascript"
