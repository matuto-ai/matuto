"""The protected API for Profiles › Speech profiles."""

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Mapping

from matuto import cards, live_records, store
from matuto.web import RefusedError

PATH = "/api/speech-profiles"


def handle(request):
    if request.method not in ("GET", "POST"):
        raise RefusedError("Speech profiles supports GET and POST.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method == "GET":
                return 200, _listing(opened)
            payload = _payload(request.body)
            action = payload.get("action")
            connection = opened.connection()
            if action == "preview":
                record = _record(payload)
                store.validate_record(record, connection)
                before = store.read_record(opened, record["kind"], record["profile_id"])
                _edit_mode(connection, record, "edit")
                preview = _preview_edit(opened, before, record)
                if preview["kind"] in ("served-change", "unavailable"):
                    live_records.refuse_live_change(opened, record["kind"], record["profile_id"])
                _name_affected_bundles(opened, preview)
                availability = _availability(opened, record)
                return 200, {"preview": preview, "availability": availability,
                             "review_token": _review_token(before, record, preview, availability)}
            if action == "save":
                record = _record(payload)
                mode = payload.get("mode")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    store.validate_record(record, connection)
                    exists = connection.execute(
                        "SELECT kind FROM model_profiles WHERE profile_id = ?",
                        (record["profile_id"],)).fetchone()
                    _edit_mode(connection, record, mode, exists)
                    if mode == "edit":
                        before = store.read_record(opened, record["kind"], record["profile_id"])
                        preview = _preview_edit(opened, before, record)
                        if preview["kind"] in ("served-change", "unavailable"):
                            if payload.get("confirm") is not True:
                                raise RefusedError("Review the changed served values before saving this speech profile.")
                            availability = _availability(opened, record)
                            if payload.get("review_token") != _review_token(
                                    before, record, preview, availability):
                                raise RefusedError("The review changed; review the current speech profile values again.")
                    saved, _warnings = store.save_record(
                        opened, record, record_id=record["profile_id"] if mode == "edit" else None)
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
                return 200 if mode == "edit" else 201, {"profile": _profile_view(opened, saved)}
            if action == "duplicate":
                kind = _speech_kind(payload.get("kind"))
                source_id = _identifier(payload.get("profile_id"))
                source = store.read_record(opened, kind, source_id)
                copied, _warnings = store.duplicate_record(
                    opened, source["kind"], source_id, payload.get("new_id"), payload.get("name"))
                return 201, {"profile": _profile_view(opened, copied)}
            if action == "remove":
                kind = _speech_kind(payload.get("kind"))
                removed = store.remove_record(opened, kind, _identifier(payload.get("profile_id")))
                return 200, {"removed": removed["profile_id"]}
            raise RefusedError("Choose a speech profile action.")
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, store.ActiveRunRefusal,
            sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None


def _listing(opened):
    connection = opened.connection()
    inventory = cards.detect(opened)
    detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    executables = []
    for entry in store.list_speech_executables(connection):
        provenance = entry.get("provenance") or {}
        executables.append({"id": entry["executable_id"], "kind": entry["kind"],
                            "project": provenance.get("project", "Unknown project"),
                            "backend": provenance.get("backend", "Unknown backend"),
                            "commit": provenance.get("commit") or entry.get("version") or "Unknown commit"})
    weights = []
    for row in connection.execute(
            "SELECT r.acquisition_id, r.name, f.file_name FROM acquisition_records r"
            " JOIN acquisition_files f USING (acquisition_id)"
            " WHERE r.verified_at IS NOT NULL AND f.verified_at IS NOT NULL"
            " AND length(f.computed_sha256) = 64"
            " AND NOT EXISTS (SELECT 1 FROM acquisition_files incomplete"
            " WHERE incomplete.acquisition_id = r.acquisition_id"
            " AND (incomplete.verified_at IS NULL OR length(incomplete.computed_sha256) <> 64))"
            " ORDER BY r.name, f.file_name"):
        weights.append({"name": row["name"], "file_name": row["file_name"],
                        "pick": {"acquisition_id": row["acquisition_id"], "file_name": row["file_name"]}})
    cards_list = [dict(row) for row in connection.execute(
        "SELECT card_id, name FROM cards ORDER BY name, card_id")]
    profiles = []
    for row in connection.execute(
            "SELECT profile_id, kind FROM model_profiles WHERE kind IN ('synthesis','recognition')"
            " ORDER BY name, profile_id"):
        profile = store.read_record(opened, row["kind"], row["profile_id"])
        profiles.append(_profile_view(opened, profile, detected))
    return {"profiles": profiles, "executables": executables, "weights": weights, "cards": cards_list}


def _profile_view(opened, record, detected=None):
    connection = opened.connection()
    if detected is None:
        inventory = cards.detect(opened)
        detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    available, reason = store.record_availability(record, connection, detected_gpus=detected)
    executable = connection.execute(
        "SELECT executable_id, kind FROM speech_executables WHERE executable_id = ?",
        (record.get("speech_executable_id"),)).fetchone()
    weights = record.get("weights")
    weight_name = None
    if weights:
        weight_name = weights["file_name"]
    card = connection.execute("SELECT name FROM cards WHERE card_id = ?", (record.get("card_id"),)).fetchone()
    bundles = [dict(row) for row in connection.execute(
        "SELECT b.bundle_id AS id, b.name FROM bundle_members m JOIN bundles b USING(bundle_id)"
        " WHERE m.profile_id = ? ORDER BY b.name, b.bundle_id", (record["profile_id"],))]
    provenance = next((item for item in store.list_speech_executables(connection)
                       if item["executable_id"] == record.get("speech_executable_id")), {})
    identity = provenance.get("provenance") or {}
    result = {"id": record["profile_id"], "kind": record["kind"], "name": record["name"],
              "speech_executable_id": record.get("speech_executable_id"),
              "executable": (f"{identity.get('project', 'Unknown project')} — "
                             f"{identity.get('backend', 'Unknown backend')} — "
                             f"{identity.get('commit') or provenance.get('version') or 'Unknown commit'}"
                             if executable else "Unavailable"),
              "weights": weights, "weights_name": weight_name,
              "arguments": record.get("arguments", []), "notes": record.get("notes", ""),
              "bundles": bundles, "available": available, "reason": reason}
    if record["kind"] == "synthesis":
        result.update(card_id=record.get("card_id"), card=card["name"] if card else None)
    return result


def _record(payload):
    record = payload.get("record")
    if not isinstance(record, Mapping):
        raise RefusedError("Speech profile field 'record' must be an object.")
    if record.get("kind") not in ("synthesis", "recognition"):
        raise RefusedError("Speech profile field 'kind' must be synthesis or recognition.")
    return dict(record)


def _edit_mode(connection, record, mode, existing=None):
    if mode not in ("create", "edit"):
        raise RefusedError("Speech profile mode must be create or edit.")
    if existing is None:
        existing = connection.execute("SELECT kind FROM model_profiles WHERE profile_id = ?",
                                      (record["profile_id"],)).fetchone()
    if (mode == "edit") != (existing is not None) or (existing and existing["kind"] != record["kind"]):
        raise RefusedError("The speech profile changed while this form was open; reload it and try again.")


def _availability(opened, record):
    inventory = cards.detect(opened)
    detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    available, reason = store.record_availability(record, opened.connection(), detected_gpus=detected)
    return {"available": available, "reason": reason}


def _preview_edit(opened, before, after):
    preview = store.preview_edit(opened, before, after)
    if before.get("speech_executable_id") != after.get("speech_executable_id"):
        if not any(change["field"] == "speech_executable_id"
                   for change in preview["changed_values"]):
            preview["changed_values"].append({"field": "speech_executable_id",
                "label": "Speech executable", "old": before.get("speech_executable_id"),
                "new": after.get("speech_executable_id")})
        if preview["kind"] == "cosmetic-only":
            preview["kind"] = "served-change"
            affected = {item["id"] for item in preview["affected"]}
            connection = opened.connection()
            for row in connection.execute(
                    "SELECT DISTINCT bundle_id FROM bundle_members WHERE profile_id = ? ORDER BY bundle_id",
                    (before["profile_id"],)):
                if row["bundle_id"] not in affected:
                    bundle = store.read_record(opened, "bundle", row["bundle_id"])
                    preview["affected"].append({"kind": "bundle", "id": row["bundle_id"],
                        "state": store.qualification_state(opened, bundle)})
    _name_affected_bundles(opened, preview)
    return preview


def _name_affected_bundles(opened, preview):
    for item in preview["affected"]:
        if item["kind"] == "bundle":
            item["name"] = store.read_record(opened, "bundle", item["id"])["name"]


def _review_token(before, after, preview, availability):
    value = json.dumps([before, after, preview, availability], sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _speech_kind(value):
    if value not in ("synthesis", "recognition"):
        raise RefusedError("Choose a synthesis or recognition speech profile.")
    return value


def _identifier(value):
    if not isinstance(value, str) or not value.strip():
        raise RefusedError("Speech profile field 'profile_id' is required.")
    return value


def _payload(body):
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON speech profile request.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON speech profile request.")
    return payload
