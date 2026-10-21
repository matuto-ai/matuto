"""Card records and their Settings › Cards consequence reviews."""

import hashlib
import json
import sqlite3

from matuto import cards, store
from matuto.web import RefusedError

PATH = "/api/cards"


def handle(request):
    if request.method == "GET":
        return 200, _listing()
    if request.method != "POST":
        raise RefusedError("Cards accepts GET and POST.")
    payload = _payload(request.body)
    opened = store.open_store()
    try:
        connection = opened.connection()
        action = payload.get("action")
        if action == "add":
            record = _record(payload)
            saved = cards.add(opened, record, payload.get("gpu"))
            return 201, {"card": saved}
        if action in ("preview", "save"):
            identifier = _identifier(payload)
            if action == "preview":
                before = store.read_card(opened, identifier)
                record = dict(_record(payload), created_at=before["created_at"])
                _validate_edit(opened, before, record)
                preview = _edit_preview(opened, before, record)
                return 200, {"preview": preview,
                             "review_token": _review_token(before, record, preview)}
            connection.execute("BEGIN IMMEDIATE")
            try:
                before = store.read_card(opened, identifier)
                record = dict(_record(payload), created_at=before["created_at"])
                _validate_edit(opened, before, record)
                preview = _edit_preview(opened, before, record)
                if preview["kind"] == "unchanged":
                    raise RefusedError("This card has no changes to save.")
                if preview["kind"] in ("served-change", "unavailable"):
                    if payload.get("confirm") is not True:
                        raise RefusedError("Review the changed card values before saving.")
                    if payload.get("review_token") != _review_token(before, record, preview):
                        raise RefusedError("The card review changed; review the current values again.")
                saved, _warnings = store.save_record(opened, record, record_id=identifier)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            return 200, {"card": saved}
        if action == "remove":
            removed = store.remove_record(opened, "card", _identifier(payload))
            return 200, {"removed": removed["card_id"]}
        raise RefusedError("Choose a card action.")
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()


def _listing():
    opened = store.open_store()
    try:
        inventory = cards.detect(opened)
        connection = opened.connection()
        for item in inventory["cards"]:
            device = next((gpu for gpu in inventory["devices"]
                           if gpu["pciAddress"] == item["gpu_identity"]), None)
            item["gpu_name"] = device.get("displayName", device["pciAddress"]) if device else item["gpu_identity"]
        holders = {}
        for row in connection.execute(
                "SELECT card_id, profile_id, name FROM model_profiles WHERE card_id IS NOT NULL"
                " ORDER BY name, profile_id"):
            holders.setdefault(row["card_id"], []).append(
                {"id": row["profile_id"], "name": row["name"], "kind": "profile"})
        for row in connection.execute(
                "SELECT card_id FROM runtime_selection WHERE selection_id = 1"):
            holders.setdefault(row["card_id"], []).append(
                {"id": "runtime-selection", "name": "Runtime selection", "kind": "runtime"})
        return {**inventory, "holders": holders}
    finally:
        opened.close()


def _record(payload):
    return {"kind": "card", "card_id": payload.get("card_id"),
            "name": payload.get("name"), "gpu_identity": payload.get("gpu_identity"),
            "vram_ceiling_bytes": payload.get("vram_ceiling_bytes"),
            "default_gpu_layers": payload.get("default_gpu_layers"),
            "smoke_output_tokens": payload.get("smoke_output_tokens"),
            "bench_prompt_tokens": payload.get("bench_prompt_tokens"),
            "bench_generation_tokens": payload.get("bench_generation_tokens"),
            "bench_repetitions": payload.get("bench_repetitions"),
            "notes": payload.get("notes", "")}


def _identifier(payload):
    identifier = payload.get("card_id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise RefusedError("Card field 'card_id' is required.")
    return identifier


def _payload(body):
    try:
        payload = json.loads(body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON card request.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON card request.")
    return payload


def _validate_edit(opened, before, record):
    detected = cards.detect(opened)
    same_gpu = record["gpu_identity"] == before["gpu_identity"]
    if same_gpu and (detected["state"] != "completed" or
                     not any(device["pciAddress"] == record["gpu_identity"]
                             for device in detected["devices"])):
        detected = {"devices": [before["gpu_identity"]]}
    store.validate_record(record, opened.connection(), detected_gpus=detected)


def _edit_preview(opened, before, after):
    preview = store.preview_edit(opened, before, after)
    for item in preview["affected"]:
        table = "model_profiles" if item["kind"] in ("language", "synthesis", "recognition") else "bundles"
        column = "profile_id" if table == "model_profiles" else "bundle_id"
        row = opened.connection().execute(
            f"SELECT name FROM {table} WHERE {column} = ?", (item["id"],)).fetchone()
        item["name"] = row["name"] if row else item["id"]
    return preview


def _review_token(before, after, preview):
    content = json.dumps([before, after, preview], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
