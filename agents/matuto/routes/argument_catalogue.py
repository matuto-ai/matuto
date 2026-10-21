"""The protected Settings › Argument catalogue API."""

import contextlib
import json
import sqlite3

from matuto import store
from matuto.web import RefusedError

PATH = "/api/argument-catalogue"


def handle(request):
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method == "GET":
                return 200, {"entries": store.list_argument_catalogue(opened)}
            if request.method != "POST":
                raise RefusedError("Argument catalogue accepts GET and POST.")
            try:
                payload = json.loads(request.body or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RefusedError("Argument catalogue request is not valid JSON.") from None
            if not isinstance(payload, dict):
                raise RefusedError("Argument catalogue request must be an object.")
            action = payload.get("action")
            if action in ("add", "edit"):
                identifier = payload.get("argument_id") if action == "edit" else None
                if action == "edit" and (not isinstance(identifier, str) or not identifier):
                    raise RefusedError("Choose an argument catalogue entry to edit.")
                identifier = store.save_argument_catalogue(opened, payload.get("entry"),
                                                          argument_id=identifier)
                return 200, {"argument_id": identifier}
            if action == "remove":
                identifier = payload.get("argument_id")
                if not isinstance(identifier, str) or not identifier:
                    raise RefusedError("Choose an argument catalogue entry to remove.")
                return 200, store.remove_argument_catalogue(opened, identifier)
            raise RefusedError("Choose an argument catalogue action.")
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None
