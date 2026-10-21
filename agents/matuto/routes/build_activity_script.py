"""The same-origin Build activity page script."""

from pathlib import Path

PATH = "/build-activity.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The Build activity script is read-only.")
    script = Path(__file__).resolve().parents[1] / "static" / "build_activity.js"
    return 200, script.read_bytes(), "text/javascript"
