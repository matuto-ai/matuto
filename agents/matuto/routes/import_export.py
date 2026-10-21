"""Import and export portable records through the same store API as the CLI."""

import json
import sqlite3

from matuto import store
from matuto.web import RefusedError

PATH = "/api/import-export"
KINDS = {"card", "language", "synthesis", "recognition", "bundle"}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def handle(request):
    if request.method == "GET":
        return _get(request)
    if request.method != "POST":
        raise RefusedError("Import / export supports GET and POST.")
    try:
        document = json.loads(request.body.decode("utf-8"), object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        raise RefusedError("The document is malformed JSON.") from None
    except (UnicodeDecodeError, ValueError) as error:
        raise RefusedError(str(error)) from None
    opened = None
    try:
        opened = store.open_store()
        return 200, {"results": store.import_records(opened, document)}
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None
    finally:
        if opened is not None:
            opened.close()


def _get(request):
    opened = None
    try:
        opened = store.open_store()
        connection = opened.connection()
        selected = request.query.get("record", [])
        if request.query.get("download") == ["1"]:
            records = None
            if selected:
                records = []
                for value in selected:
                    kind, separator, identifier = value.partition(":")
                    if not separator or kind not in KINDS or not identifier:
                        raise RefusedError("Choose valid records to export.")
                    records.append((kind, identifier))
            document = store.export_records(opened, records)
            return 200, json.dumps(document, ensure_ascii=False), "application/json", {
                "Content-Disposition": 'attachment; filename="matuto-records.json"'}
        records = [dict(kind="card", id=row[0], name=row[1]) for row in connection.execute(
            "SELECT card_id, name FROM cards ORDER BY name, card_id")]
        records += [dict(kind=row["kind"], id=row["profile_id"], name=row["name"])
                    for row in connection.execute(
                        "SELECT profile_id, kind, name FROM model_profiles ORDER BY name, profile_id")]
        records += [dict(kind="bundle", id=row[0], name=row[1]) for row in connection.execute(
            "SELECT bundle_id, name FROM bundles ORDER BY name, bundle_id")]
        return 200, {"records": records}
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None
    finally:
        if opened is not None:
            opened.close()
