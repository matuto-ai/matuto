"""Serve the same-origin behavior for Profiles › Import / export."""

from pathlib import Path

PATH = "/import-export.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "import_export.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The import/export script is read-only.")
    return 200, SCRIPT.read_bytes(), "text/javascript"
