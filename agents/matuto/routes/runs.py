"""The authenticated Runs & jobs API: list, start, stop and delete work."""

import json
import re
import sqlite3
import sys
from pathlib import Path

from matuto import admission, context_search, router, runner, serving_state, store
from matuto.discovery import item_names, load_item
from matuto.work_cli import record_target

PATH = "/api/runs"


def _runner():
    return runner.Runner(serving_state=lambda: serving_state.read().state,
                         card_holder_with_connection=lambda connection:
                             router.card_holder(connection=connection))


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON request") from error
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def _serialize(opened, running):
    entries = []
    connection = opened.connection()
    for work in running.works():
        progress = running.progress(work)
        address = ""
        if progress and work.kind == "serve":
            match = re.fullmatch(r"https?://127\.0\.0\.1:\d{1,5}", progress.step)
            if not match:
                match = re.search(r"https?://(?:127\.0\.0\.1|localhost):\d{1,5}\b", progress.log)
            address = match.group(0) if match else ""
        item = {
            "id": work.work_id, "kind": work.kind, "purpose": work.purpose,
            "state": work.outcome or work.state, "outcome": work.outcome,
            "started_at": work.started_at, "ended_at": work.ended_at,
            "record": work.record_label, "procedure": work.kind if work.record_id else "",
            "progress": progress.__dict__ if progress else None,
            "address": address,
            "checks": [],
        }
        if work.record_id:
            item["checks"] = [dict(row) for row in connection.execute(
                "SELECT name, status, detail FROM checks WHERE run_id = ? ORDER BY sequence",
                (work.work_id,))]
        entries.append(item)
    context_searches = {}
    for search in connection.execute(
            "SELECT run_id, outcome FROM runs WHERE procedure='context_search'"):
        artifact = connection.execute(
            "SELECT body FROM run_evidence WHERE run_id=? AND name='context-search.json'",
            (search["run_id"],)).fetchone()
        summary = json.loads(artifact[0]) if artifact else None
        winning = summary.get("outcome") == context_search.WINNER if summary else False
        result = (context_search.label(summary["winner"]) if winning and summary.get("winner")
                  else context_search.NO_WINNER if search["outcome"] else context_search.RUNNING)
        context_searches[search["run_id"]] = {
            "role": "search", "result": result, "winner": winning,
        }
        for rung in (summary or {}).get("rungs", []):
            context_searches[rung["run_id"]] = {
                "role": "rung", "rung": rung["label"], "result": result,
                "winner": winning and rung["context"] == summary.get("winner"),
            }
    for entry in entries:
        if entry["id"] in context_searches:
            entry["context_search"] = context_searches[entry["id"]]
    eligible = connection.execute(
        "SELECT profile_id AS id, name, 'language' AS kind FROM model_profiles"
        " WHERE kind = 'language' UNION ALL SELECT bundle_id AS id, name, 'bundle' AS kind"
        " FROM bundles ORDER BY name").fetchall()
    records = [dict(row) for row in eligible]
    wq = load_item("procedures", "wq")
    for record in records:
        if record["kind"] == "language":
            record["wq_limits"] = wq.form_defaults(opened, record["id"])
    return {"entries": entries,
            "procedures": [{"id": name, "label": "WQ" if name == "wq" else name.replace('-', ' ').title()}
                           for name in item_names("procedures")],
            "records": records}


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        return 405, {"error": "Runs & jobs accepts GET and POST requests only."}
    try:
        opened = _runner()
    except (OSError, store.StoreError) as error:
        return 503, {"error": str(error)}
    try:
        if request.method in ("GET", "HEAD"):
            data = _serialize(opened.store, opened)
            data["csrf"] = request.csrf
            return 200, data
        body = _json_body(request)
        action = body.get("action")
        work_id = body.get("id")
        if not isinstance(work_id, str) or not work_id:
            if action in ("stop", "delete"):
                raise ValueError("a work ID is required")
        if action == "start":
            procedure = body.get("procedure")
            record_id = body.get("record")
            if not isinstance(procedure, str) or procedure not in item_names("procedures"):
                raise ValueError("choose an available procedure")
            module = load_item("procedures", procedure)
            target = record_target(opened.store, record_id)
            command = [sys.executable, str(Path(__file__).resolve().parents[1] / "procedure_runner.py"),
                       procedure, target.kind, target.id, target.fingerprint]
            if "limits" in body and procedure != "wq":
                raise ValueError("only WQ accepts start-request limits")
            if "start_rung" in body and procedure != "context_search":
                raise ValueError("only context search accepts a start rung")
            if hasattr(module, "prepare"):
                if procedure == "wq":
                    pinned = module.prepare(opened.store, target, body.get("limits"))
                elif procedure == "context_search":
                    pinned = module.prepare(opened.store, target, body.get("start_rung"))
                else:
                    pinned = module.prepare(opened.store, target)
                command.append(json.dumps(pinned, separators=(",", ":")))
            request = runner.Request.run(
                procedure, f"{procedure} for {target.label}", command, target, getattr(module, "REVISION", "") or
                (module.revision() if hasattr(module, "revision") else ""),
                pty=bool(getattr(module, "PTY", False)),
                dirty_checkout=module.dirty_checkout() if hasattr(module, "dirty_checkout") else False)
            result = opened.start(request)
            if result.decision != admission.ADMIT:
                return 409, {
                    "error": result.reason,
                    "offer": result.decision == admission.STOP_SERVING_AND_RUN,
                }
            return 202, {"work": result.work.work_id if result.work else None}
        if action == "stop":
            work = opened.stop(work_id)
            if work is None:
                return 404, {"error": f"No run or job has ID {work_id!r}."}
            if work.state == runner.UNKNOWN and not work.outcome:
                return 409, {
                    "error": f"The state of {work_id} is unknown; it was not reported stopped."
                }
            return 200, {"id": work.work_id, "outcome": work.outcome, "state": work.state}
        if action == "delete":
            store.refuse_accepted_run_deletion(opened.store, work_id)
            store.delete_run(opened.store, work_id)
            return 200, {"deleted": work_id}
        raise ValueError("unknown Runs & jobs action")
    except (OSError, sqlite3.Error, store.StoreError, ValueError) as error:
        response = {"error": str(error)}
        if isinstance(getattr(error, "field", None), str):
            response["field"] = error.field
        return 409, response
    finally:
        opened.close()
