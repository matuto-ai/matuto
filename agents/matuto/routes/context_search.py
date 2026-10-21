"""Read context-search summaries and the rung runs they name."""

import json
import sqlite3

from matuto import context_search as rules, route_gate, store
from matuto.routes import acceptance
from matuto.web import RefusedError

PATH = "/api/context-search"


def handle(request):
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method not in ("GET", "HEAD"):
        raise RefusedError("Context-search results are read-only.")
    opened = None
    try:
        opened = store.open_store()
        connection = opened.connection()
        records = []
        for row in connection.execute(
                "SELECT profile_id AS id, name, 'language' AS kind FROM model_profiles"
                " WHERE kind='language' UNION ALL SELECT bundle_id AS id, name, 'bundle' AS kind"
                " FROM bundles ORDER BY name"):
            record = dict(row)
            language_id = record["id"]
            if record["kind"] == "bundle":
                member = connection.execute(
                    "SELECT profile_id FROM bundle_members WHERE bundle_id=? AND role='language'",
                    (record["id"],)).fetchone()
                language_id = member[0] if member else ""
            trained = store.read_trained_context(opened, language_id) if language_id else None
            record["language_id"] = language_id
            record["rungs"] = list(rules.offered_rungs(trained))
            record["trained_context"] = trained
            records.append(record)

        searches = []
        for row in connection.execute(
                "SELECT run_id, record_kind, record_id, record_label, outcome, started_at, ended_at"
                " FROM runs WHERE procedure='context_search' ORDER BY started_at DESC, run_id"):
            search = dict(row)
            evidence = store.read_run_evidence(opened, search["run_id"])
            artifact = next((item["body"] for item in evidence["artifacts"]
                             if item["name"] == "context-search.json"), None) if evidence else None
            summary = json.loads(artifact) if artifact else None
            search["summary"] = summary
            search["winner"] = summary.get("winner") if summary else None
            search["checked"] = summary.get("checked") if summary else None
            search["children"] = []
            current_fingerprint = None
            try:
                current_record = store.read_record(opened, search["record_kind"], search["record_id"])
                current_fingerprint = store.fingerprint(opened, current_record)
            except store.StoreError:
                pass
            if summary:
                for rung in summary.get("rungs", []):
                    child = store.read_run_evidence(opened, rung["run_id"])
                    child_record = child["record"] if child else {}
                    checks = child["checks"] if child else rung.get("checks", [])
                    citation = {
                        "outcome": child_record.get("outcome", rung.get("outcome", "")),
                        "dirty_checkout": child_record.get("dirty_checkout", False),
                        "fingerprint": child_record.get("fingerprint", rung.get("fingerprint", "")),
                        "build_id": child_record.get("build_id"),
                        "procedure": child_record.get("procedure", ""),
                        "checks": checks,
                    }
                    reason = acceptance._citation_reason(citation, current_fingerprint)
                    search["children"].append({
                        "id": rung["run_id"], "context": rung["context"], "label": rung["label"],
                        "fingerprint": rung.get("fingerprint", ""), "outcome": rung.get("outcome", ""),
                        "checks": checks,
                        "winner": (summary.get("outcome") == rules.WINNER
                                   and summary.get("winner") == rung["context"]),
                        "citable": not reason, "citation_reason": reason,
                        "record_id": search["record_id"], "record_kind": search["record_kind"],
                    })
            searches.append(search)
        return 200, {"records": records, "searches": searches}
    except (OSError, sqlite3.Error, store.StoreError, ValueError, TypeError) as error:
        raise RefusedError(str(error)) from None
    finally:
        if opened is not None:
            opened.close()
