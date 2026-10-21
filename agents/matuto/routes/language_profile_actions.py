"""Protected actions for language profiles in Profiles › Language profile actions."""

import json

from matuto import live_records, route_gate, store
from matuto.routes import language_profiles
from matuto.web import RefusedError

PATH = "/api/language-profile-actions"


def _live_reason(opened, profile_id):
    try:
        live_records.refuse_live_change(opened, "language", profile_id)
    except store.RecordRefusal as error:
        return str(error)
    return ""


def _remove_reason(connection, profile):
    if profile["published"]:
        return "Published profile; unpublish before removing."
    bundles = connection.execute(
        "SELECT b.name, b.bundle_id FROM bundle_members AS m JOIN bundles AS b USING (bundle_id)"
        " WHERE m.profile_id = ? ORDER BY b.name, b.bundle_id", (profile["profile_id"],)).fetchall()
    if bundles:
        holders = ", ".join(f"{row['name']} ({row['bundle_id']})" for row in bundles)
        return f"Referenced by bundle {holders}."
    return ""


def handle(request):
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method == "GET":
        opened = store.open_store()
        try:
            data = language_profiles._list(opened)
            connection = opened.connection()
            for profile in data["profiles"]:
                profile["unpublish_reason"] = _live_reason(opened, profile["profile_id"]) \
                    if profile["published"] else ""
                profile["remove_reason"] = _remove_reason(connection, profile)
                if not profile["remove_reason"]:
                    profile["remove_reason"] = _live_reason(opened, profile["profile_id"])
            return 200, data
        finally:
            opened.close()
    if request.method != "POST":
        raise RefusedError("Language profile actions supports GET and POST.")
    try:
        payload = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("Expected a JSON language profile action.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON language profile action.")
    profile_id = payload.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise RefusedError("Language profile field 'profile_id' is required.")
    opened = store.open_store()
    try:
        action = payload.get("action")
        if action == "duplicate":
            copied, warnings = store.duplicate_record(
                opened, "language", profile_id, payload.get("new_id"), payload.get("name"),
                alias=payload.get("alias"))
            return 200, {"profile_id": copied["profile_id"], "warnings": warnings}
        if action == "publish":
            profile, warnings = store.publish_profile(
                opened, profile_id, confirmation=payload.get("confirmation"))
            return 200, {"profile_id": profile["profile_id"], "warnings": warnings}
        if action == "unpublish":
            profile, warnings = store.unpublish_profile(opened, profile_id)
            return 200, {"profile_id": profile["profile_id"], "warnings": warnings}
        if action == "remove":
            removed = store.remove_record(opened, "language", profile_id)
            return 200, {"removed": removed["profile_id"]}
        raise RefusedError("Choose a language profile action.")
    except (store.StoreError, store.RecordRefusal) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()
