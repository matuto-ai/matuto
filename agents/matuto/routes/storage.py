"""Storage & artifacts data and mutations for Settings."""

import json

from matuto import route_gate, serving_actions, storage, store, verification
from matuto.web import RefusedError

PATH = "/api/storage"


def handle(request):
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method == "GET":
        return 200, _read()
    if request.method != "POST":
        raise RefusedError("Storage changes require POST.")
    try:
        payload = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON storage action.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON storage action.")
    action = payload.get("action")
    opened = store.open_store()
    try:
        connection = opened.connection()
        if action == "save-model-storage":
            result = storage.save_model_storage(connection, payload.get("path"))
        elif action == "add-artifact-location":
            result = storage.add_artifact_location(connection, payload.get("path"))
        elif action == "preview-change":
            result = storage.preview_location_change(
                connection, _location_id(payload), payload.get("path"))
            reason = storage.location_busy_reason(
                connection, result, serving_actions.read_state().state)
            if reason:
                raise store.StoreError(reason)
        elif action == "confirm-change":
            state = serving_actions.read_state().state
            result = storage.change_location(
                connection, _location_id(payload), payload.get("path"),
                serving_state=state)
        else:
            raise RefusedError("Choose a storage action.")
        return 200, result
    except (store.StoreError, verification.VerificationError) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()


def _location_id(payload):
    value = payload.get("location_id")
    if not isinstance(value, str) or not value:
        raise RefusedError("Choose a configured artifact location.")
    return value


def _read():
    opened = store.open_store()
    try:
        connection = opened.connection()
        root = storage.model_storage(connection)
        model = None
        if root is not None:
            available, reason = storage.availability(root)
            row = connection.execute(
                "SELECT location_id FROM artifact_locations WHERE path = ?", (root,)).fetchone()
            model = {"path": root, "location_id": row["location_id"],
                     "available": available, "reason": reason}
        locations = []
        for row in connection.execute(
                "SELECT location_id, path FROM artifact_locations ORDER BY path"):
            available, reason = storage.availability(row["path"])
            impact = storage.preview_location_change(
                connection, row["location_id"], row["path"])
            locations.append({**impact, "available": available, "reason": reason})
        return {"model_storage": model, "locations": locations}
    finally:
        opened.close()
