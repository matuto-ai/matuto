"""Protected edit and consequence-review API for language profiles."""

import contextlib
import hashlib
import json
import sqlite3
from collections.abc import Mapping

from matuto import arguments, cards, live_records, store
from matuto.routes import language_profiles
from matuto.web import RefusedError

PATH = "/api/language-profile-edits"


def handle(request):
    if request.method not in ("GET", "POST"):
        raise RefusedError("Language profile edits supports GET and POST.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method == "GET":
                profile_id = request.query.get("profile_id", [""])[0]
                profile = store.read_record(opened, "language", profile_id)
                return 200, {"profile": profile, **language_profiles._list(opened)}
            payload = _payload(request.body)
            action = payload.get("action")
            record = payload.get("record")
            if not isinstance(record, Mapping) or record.get("kind") != "language":
                raise RefusedError("Language profile field 'record' must be a language profile object.")
            record = dict(record)
            connection = opened.connection()
            if action == "preview":
                before, record, preview, availability = _preview(opened, record)
                return 200, {"preview": preview, "availability": availability,
                             "review_token": _review_token(before, record, preview, availability)}
            if action != "save":
                raise RefusedError("Choose a language profile edit action.")
            connection.execute("BEGIN IMMEDIATE")
            try:
                record, help_text, selector, missing_tools = _runtime_record(connection, record)
                before = store.read_record(opened, "language", record["profile_id"])
                preview = _named_preview(opened, before, record)
                if preview["kind"] != "unchanged":
                    live_records.refuse_live_change(opened, "language", record["profile_id"])
                if preview["kind"] in ("served-change", "unavailable"):
                    if payload.get("confirm") is not True:
                        raise RefusedError("Review the changed served values before saving this language profile.")
                    availability = _availability(opened, record)
                    if payload.get("review_token") != _review_token(before, record, preview, availability):
                        raise RefusedError("The review changed; review the current language profile values again.")
                saved, warnings = store.save_record(opened, record, record_id=record["profile_id"],
                                                    help_text=help_text, selector=selector)
                for tool, executable in missing_tools.items():
                    if any(row["tool"] == tool for row in saved["arguments"]):
                        warnings.append(f"{tool} arguments are saved but unavailable on this build because {executable} is missing.")
                connection.execute("COMMIT")
                if saved.get("published") or before.get("published"):
                    warnings.extend(store._router_omission_warnings(
                        store.regenerate_router_files(opened)))
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return 200, {"profile_id": saved["profile_id"], "warnings": warnings}
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError, KeyError) as error:
        raise RefusedError(str(error)) from None


def _preview(opened, record):
    connection = opened.connection()
    record, _help_text, _selector, _missing_tools = _runtime_record(connection, record)
    before = store.read_record(opened, "language", record["profile_id"])
    preview = _named_preview(opened, before, record)
    if preview["kind"] != "unchanged":
        live_records.refuse_live_change(opened, "language", record["profile_id"])
    return before, record, preview, _availability(opened, record)


def _runtime_record(connection, record):
    store.validate_record(record, connection)
    rows = record.get("arguments")
    requested_tools = ({row["tool"] for row in rows
                        if isinstance(row, Mapping) and isinstance(row.get("tool"), str)}
                       if isinstance(rows, list) else set())
    help_text, selector, missing_tools = language_profiles._runtime_validation(
        connection, requested_tools)
    normalized = dict(record)
    normalized["arguments"] = arguments.canonicalize_server_aliases(rows or [], help_text)
    store.validate_record(normalized, connection, help_text=help_text, selector=selector)
    return normalized, help_text, selector, missing_tools


def _named_preview(opened, before, record):
    preview = store.preview_edit(opened, before, record)
    for item in preview["affected"]:
        if item["kind"] == "bundle":
            item["name"] = store.read_record(opened, "bundle", item["id"])["name"]
    return preview


def _availability(opened, record):
    inventory = cards.detect(opened)
    detected = inventory.get("devices", ()) if inventory.get("state") == "completed" else ()
    available, reason = store.record_availability(record, opened.connection(), detected_gpus=detected)
    return {"available": available, "reason": reason}


def _review_token(before, after, preview, availability):
    value = json.dumps([before, after, preview, availability], sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload(body):
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON language profile edit request.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON language profile edit request.")
    return payload
