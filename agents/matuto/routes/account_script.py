"""Serve the account section's same-origin browser script."""

from pathlib import Path

PATH = "/account.js"
SCRIPT = Path(__file__).resolve().parents[1] / "static" / "account.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("The account script is read-only")
    return 200, SCRIPT.read_bytes(), "text/javascript"
