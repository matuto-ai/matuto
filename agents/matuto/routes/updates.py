"""Read update status or check the signed release list on explicit operator request."""

import json

from matuto import route_gate, updates
from matuto.web import RefusedError

PATH = "/api/updates"


def handle(request):
    refused = route_gate.require_session(request)
    if refused:
        return refused
    if request.method in ("GET", "HEAD"):
        result = updates.install_status()
        result["csrf"] = request.csrf
        return 200, result
    if request.method != "POST":
        raise RefusedError("Update status supports GET and POST.")
    try:
        body = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("The update check request is invalid.") from None
    if not isinstance(body, dict):
        raise RefusedError("The update request is invalid.")
    if body.get("action") == "check":
        checked = updates.check()
        return 200, {**updates.install_status(), **checked}
    try:
        if body.get("action") == "preview":
            return 200, updates.preview(body.get("version"), body.get("listNumber"))
        if body.get("action") == "install":
            return 200, updates.install(body.get("version"), body.get("listNumber"))
    except (OSError, TypeError, updates.store.StoreError, updates.updater.UpdateError) as error:
        refusal = RefusedError(str(error))
        refusal.blockers = updates.install_status().get("blockers", [])
        raise refusal from None
    raise RefusedError("Choose Check for updates, Review…, or Install.")
