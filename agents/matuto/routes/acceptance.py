"""Qualification acceptance and withdrawal on the Runs & jobs page."""

import json
import sqlite3

from matuto import route_gate, sessions, store
from matuto.web import RefusedError

PATH = "/api/acceptance"


def handle(request):
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method not in ("GET", "POST"):
        raise RefusedError("Acceptance supports GET and POST.")
    try:
        if request.method == "GET":
            kind = _query(request, "kind")
            record_id = _query(request, "id")
            if kind not in store.QUALIFICATION_KINDS or not record_id:
                raise RefusedError("Choose a language profile or bundle.")
            return 200, _details(kind, record_id)
        payload = _payload(request.body)
        action = payload.get("action")
        if action == "accept":
            kind = payload.get("kind")
            record_id = payload.get("id")
            if kind not in store.QUALIFICATION_KINDS or not isinstance(record_id, str):
                raise RefusedError("Choose a language profile or bundle.")
            token = route_gate.session_token(request)
            username = sessions.session_username(token)
            if not username:
                raise RefusedError("Sign in to Matuto to accept qualification.")
            opened = store.open_store()
            try:
                acceptance = store.accept(
                    opened, kind, record_id, payload.get("run_ids"), payload.get("scope"),
                    payload.get("exceptions"), payload.get("residual_risks"), username)
                return 200, {"acceptance": acceptance}
            finally:
                opened.close()
        if action == "withdraw":
            kind, record_id = payload.get("kind"), payload.get("id")
            acceptance_id = payload.get("acceptance_id")
            if kind not in store.QUALIFICATION_KINDS or not isinstance(record_id, str):
                raise RefusedError("Choose a language profile or bundle.")
            opened = store.open_store()
            try:
                row = opened.connection().execute(
                    "SELECT a.acceptance_id, a.withdrawn_at, q.fingerprint"
                    " FROM acceptances a JOIN qualification_records q USING (qualification_id)"
                    " WHERE a.acceptance_id = ? AND q.record_kind = ? AND q.record_id = ?",
                    (acceptance_id, kind, record_id)).fetchone()
                record = store.read_record(opened, kind, record_id)
                fingerprint = store.fingerprint(opened, record)
                if (row is None or row["withdrawn_at"] is not None
                        or fingerprint is None or row["fingerprint"] != fingerprint):
                    raise RefusedError("Only the current active acceptance can be withdrawn.")
                withdrawn = store.withdraw(opened, acceptance_id)
                return 200, {"acceptance": withdrawn}
            finally:
                opened.close()
        raise RefusedError("Choose Accept or Withdraw.")
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise RefusedError(str(error)) from None


def _details(kind, record_id):
    opened = store.open_store()
    try:
        record = store.read_record(opened, kind, record_id)
        state = store.qualification_state(opened, record)
        fingerprint = state.get("fingerprint")
        connection = opened.connection()
        active_build = connection.execute(
            "SELECT build_id FROM runtime_selection WHERE selection_id = 1").fetchone()
        active_build = active_build[0] if active_build else None
        runs = []
        for row in connection.execute(
                "SELECT run_id, record_label, fingerprint, build_id, procedure, outcome,"
                " dirty_checkout, started_at FROM runs WHERE record_kind = ? AND record_id = ?"
                " ORDER BY started_at DESC, run_id", (kind, record_id)):
            run = dict(row)
            run["checks"] = [dict(check) for check in connection.execute(
                "SELECT name, status, detail FROM checks WHERE run_id = ? ORDER BY sequence",
                (run["run_id"],))]
            run["reason"] = _citation_reason(run, fingerprint)
            run["citable"] = not run["reason"]
            if run["build_id"] and run["build_id"] != active_build:
                build = connection.execute(
                    "SELECT label FROM builds WHERE build_id = ?", (run["build_id"],)).fetchone()
                run["build_warning"] = "This run used a different llama.cpp build: " + (
                    build[0] if build else run["build_id"])
            runs.append(run)
        acceptances = []
        for row in connection.execute(
                "SELECT a.acceptance_id, a.scope, a.residual_risks, a.accepted_by,"
                " a.accepted_at, a.withdrawn_at, q.fingerprint"
                " FROM acceptances a JOIN qualification_records q USING (qualification_id)"
                " WHERE q.record_kind = ? AND q.record_id = ?"
                " ORDER BY a.accepted_at, a.acceptance_id", (kind, record_id)):
            acceptance = dict(row)
            acceptance["runs"] = [dict(citation) for citation in connection.execute(
                "SELECT ar.run_id, r.procedure, r.record_label, ar.run_fingerprint,"
                " ar.build_id FROM acceptance_runs ar JOIN runs r USING (run_id)"
                " WHERE ar.acceptance_id = ? ORDER BY r.started_at, ar.run_id",
                (acceptance["acceptance_id"],))]
            acceptance["exceptions"] = [dict(exception) for exception in connection.execute(
                "SELECT check_name, reason FROM acceptance_exceptions"
                " WHERE acceptance_id = ? ORDER BY check_name", (acceptance["acceptance_id"],))]
            acceptance["withdrawable"] = (not acceptance["withdrawn_at"] and fingerprint is not None
                                           and acceptance["fingerprint"] == fingerprint)
            acceptances.append(acceptance)
        return {"record": {"id": record_id, "kind": kind, "name": record["name"],
                            "state": state}, "runs": runs, "acceptances": acceptances}
    finally:
        opened.close()


def _citation_reason(run, fingerprint):
    if run["outcome"] != "completed":
        return f"Outcome is {run['outcome'] or 'still running'}, not completed."
    if run["dirty_checkout"]:
        return "This run came from a dirty checkout."
    if not fingerprint:
        return "The record's current fingerprint is unavailable."
    if not run["fingerprint"] or run["fingerprint"] != fingerprint:
        return "This run's fingerprint does not match the record's current fingerprint."
    if not run["build_id"]:
        return "The llama.cpp build was not recorded."
    if run["procedure"] in ("smoke", "bundle") and any(
            check["status"] != "PASS" for check in run["checks"]):
        return f"The required {run['procedure']} checks did not all pass."
    return ""


def _query(request, name):
    values = request.query.get(name, [])
    return values[0] if values else ""


def _payload(body):
    try:
        value = json.loads(body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("Expected an acceptance request as JSON.") from None
    if not isinstance(value, dict):
        raise RefusedError("Expected an acceptance request as JSON.")
    return value
