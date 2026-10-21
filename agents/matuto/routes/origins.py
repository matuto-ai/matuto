"""List, add and remove the configured public Hugging Face download origins."""

import json
import sqlite3

from matuto import origins, store
from matuto.web import RefusedError

PATH = "/api/origins"


def _body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("Download origins request is not valid JSON.") from None
    if not isinstance(value, dict):
        raise RefusedError("Download origins request must be an object.")
    return value


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        raise RefusedError("Download origins accepts GET and POST requests only.")
    try:
        opened = store.open_store()
    except (OSError, store.StoreError) as error:
        raise RefusedError(str(error)) from None
    try:
        connection = opened.connection()
        if request.method in ("GET", "HEAD"):
            return 200, {"origins": origins.list_all(connection), "csrf": request.csrf}

        body = _body(request)
        action = body.get("action")
        if action == "add":
            item = origins.add(connection, body.get("kind"), body.get("name"))
            return 201, {"origin": item}
        if action == "remove":
            origin_id = body.get("origin_id")
            if not isinstance(origin_id, str) or not origin_id:
                raise RefusedError("Choose a configured origin to remove.")
            origins.remove(connection, origin_id)
            return 200, {"removed": origin_id}
        raise RefusedError("Choose Add origin or Remove origin.")
    except RefusedError:
        raise
    except (OSError, sqlite3.Error, store.StoreError, ValueError) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()
