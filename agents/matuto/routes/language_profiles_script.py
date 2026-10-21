"""The same-origin client script for the Language profiles section."""

from matuto.page.language_profiles import SCRIPT
from matuto.web import RefusedError

PATH = "/language-profiles.js"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("The language profile script is read-only.")
    return 200, SCRIPT, "text/javascript"
