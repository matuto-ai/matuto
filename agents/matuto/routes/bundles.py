"""The protected bundle record API for Profiles › Bundles."""

import hashlib
import json

from matuto import cards, route_gate, store
from matuto.web import RefusedError

PATH = "/api/bundles"


def handle(request):
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method == "GET":
        return 200, _listing()
    if request.method != "POST":
        raise RefusedError("Bundles accepts GET and POST.")
    payload = _payload(request.body)
    action = payload.get("action")
    opened = store.open_store()
    try:
        if action == "preview":
            before = store.read_record(opened, "bundle", _identifier(payload))
            after = _record(payload)
            store.validate_record(after, opened.connection())
            preview = store.preview_edit(opened, before, after)
            availability = _availability(opened, after)
            return 200, {"preview": preview, "availability": availability,
                          "review_token": _review_token(before, after, preview, availability)}
        if action == "save":
            connection = opened.connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                record = _record(payload)
                store.validate_record(record, connection)
                record_id = record["bundle_id"]
                exists = connection.execute(
                    "SELECT 1 FROM bundles WHERE bundle_id = ?", (record_id,)).fetchone() is not None
                mode = payload.get("mode")
                if mode not in ("create", "edit") or exists != (mode == "edit"):
                    raise RefusedError("The bundle changed while this form was open; reload it and try again.")
                before = store.read_record(opened, "bundle", record_id) if exists else None
                if before is not None:
                    preview = store.preview_edit(opened, before, record)
                    if preview["kind"] in ("served-change", "unavailable"):
                        if payload.get("confirm") is not True:
                            raise RefusedError("Review the changed served values before saving this bundle.")
                        availability = _availability(opened, record)
                        if payload.get("review_token") != _review_token(before, record, preview, availability):
                            raise RefusedError("The review changed; review the current bundle values again.")
                saved, warnings = store.save_record(opened, record,
                                                    record_id=record_id if exists else None)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return 200, {"bundle": _bundle_view(opened, saved), "warnings": warnings}
        if action == "duplicate":
            copied, warnings = store.duplicate_record(
                opened, "bundle", _identifier(payload), payload.get("new_id"), payload.get("name"))
            return 200, {"bundle": _bundle_view(opened, copied), "warnings": warnings}
        if action == "remove":
            removed = store.remove_record(opened, "bundle", _identifier(payload))
            return 200, {"removed": removed["bundle_id"]}
        raise RefusedError("Choose a bundle action.")
    except (store.StoreError, store.RecordRefusal) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()


def _listing():
    opened = store.open_store()
    try:
        connection = opened.connection()
        inventory = cards.detect(opened)
        detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
        bundles = [store.read_record(opened, "bundle", row[0])
                   for row in connection.execute("SELECT bundle_id FROM bundles ORDER BY name, bundle_id")]
        profiles = {kind: [] for kind in ("language", "synthesis", "recognition")}
        for row in connection.execute(
                "SELECT profile_id, kind FROM model_profiles ORDER BY name, profile_id"):
            record = store.read_record(opened, row["kind"], row["profile_id"])
            available, reason = store.record_availability(record, connection, detected_gpus=detected)
            profiles[row["kind"]].append({"id": row["profile_id"], "name": record["name"],
                                           "available": available, "reason": reason})
        return {"bundles": [_bundle_view(opened, item, detected) for item in bundles],
                "profiles": profiles}
    finally:
        opened.close()


def _bundle_view(opened, record, detected=None):
    if detected is None:
        inventory = cards.detect(opened)
        detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    connection = opened.connection()
    members = {}
    for role, identifier in record["members"].items():
        kind = role
        member = store.read_record(opened, kind, identifier)
        available, reason = store.record_availability(member, connection, detected_gpus=detected)
        members[role] = {"id": identifier, "name": member["name"],
                         "available": available, "reason": reason}
    available, reason = store.record_availability(record, connection, detected_gpus=detected)
    return {"id": record["bundle_id"], "name": record["name"], "members": members,
            "load_tokens": record.get("load_tokens"), "load_prompt": record.get("load_prompt"),
            "notes": record.get("notes", ""),
            "qualification": store.qualification_state(opened, record),
            "available": available, "reason": reason}


def _review_token(before, after, preview, availability):
    content = json.dumps([before, after, preview, availability], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _availability(opened, record):
    inventory = cards.detect(opened)
    detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    available, reason = store.record_availability(record, opened.connection(), detected_gpus=detected)
    return {"available": available, "reason": reason}


def _record(payload):
    members = payload.get("members")
    if not isinstance(members, dict):
        members = {role: payload.get(role) for role in ("language", "synthesis", "recognition")}
    return {"kind": "bundle", "bundle_id": payload.get("bundle_id"),
            "name": payload.get("name"), "members": members,
            "load_tokens": payload.get("load_tokens"), "load_prompt": payload.get("load_prompt"),
            "notes": payload.get("notes", "")}


def _identifier(payload):
    identifier = payload.get("bundle_id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise RefusedError("Bundle field 'bundle_id' is required.")
    return identifier


def _payload(body):
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON bundle request.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON bundle request.")
    return payload
