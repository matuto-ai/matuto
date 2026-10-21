"""Search context rungs as child runs without changing the stored record."""

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import signal
import uuid
from datetime import datetime, timezone

from matuto import context_search as rules, discovery, runner, store
from matuto.procedure_support import evidence, report

REVISION = "context-search-v2"


def dirty_checkout():
    return discovery.load_item("procedures", "wq").dirty_checkout()


def prepare(opened, target, start_rung=None):
    """Pin the requested rung; default to the largest offered rung for CLI runs."""
    record = store.read_record(opened, target.kind, target.id)
    trained_context = _trained_context(opened, record)
    offered = rules.offered_rungs(trained_context)
    if start_rung is None and offered:
        start_rung = offered[0]
    verdict = rules.judge(trained_context, start_rung, {})
    if verdict.outcome == rules.REFUSED:
        raise store.StoreError(verdict.reason)
    return {"start_rung": start_rung, "trained_context": trained_context}


def run(opened, record, pinned=None):
    """Run each planned rung in an in-memory store copy and persist its child run."""
    stop_state = {"stopped": False}
    previous_term = signal.signal(signal.SIGTERM,
                                  lambda *_: stop_state.__setitem__("stopped", True))
    try:
        return _run(opened, record, pinned, stop_state)
    finally:
        signal.signal(signal.SIGTERM, previous_term)


def _run(opened, record, pinned, stop_state):
    pinned = pinned or prepare(opened, _target(record))
    start_rung = pinned.get("start_rung")
    trained_context = _trained_context(opened, record)
    verdict = rules.judge(trained_context, start_rung, {})
    if verdict.outcome == rules.REFUSED:
        raise store.StoreError(verdict.reason)
    if pinned.get("trained_context") != trained_context:
        raise store.StoreError("the stored trained context changed after context-search admission")

    evidence_root = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    results = {}
    rung_records = {}
    children = []
    copy = store.snapshot(opened)
    connection = copy.connection()
    connection.execute("PRAGMA query_only = OFF")
    original_context = _profile_context(connection, record)
    try:
        while verdict.outcome == rules.RUNNING and not stop_state["stopped"]:
            rung = verdict.next_rung
            connection.execute("UPDATE model_profiles SET context_size = ? WHERE profile_id = ?",
                               (rung, _language_id(record)))
            rung_record = store.read_record(copy, record["kind"], _record_id(record))
            rung_fingerprint = store.fingerprint(copy, rung_record)
            if rung_fingerprint is None:
                raise store.StoreError(f"cannot fingerprint the {rules.label(rung)} context copy")
            rung_dir = evidence_root / "rungs" / rules.label(rung)
            rung_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            rung_started = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            procedure_name = "wq" if record["kind"] == "language" else "bundle"
            procedure_revision = ""
            child_dirty = False
            module = None
            load_error = ""
            try:
                module = discovery.load_item("procedures", procedure_name)
                procedure_revision = getattr(module, "REVISION", "")
                if not procedure_revision and hasattr(module, "revision"):
                    procedure_revision = module.revision()
                child_dirty = module.dirty_checkout() if hasattr(module, "dirty_checkout") else False
            except Exception as error:
                load_error = str(error)
            child = _start_child_run(record, rung, rung_fingerprint, rung_started,
                                    procedure_revision, child_dirty)
            (rung_dir / "context-rung.json").write_text(json.dumps({
                "run_id": child, "context_size": rung, "label": rules.label(rung),
                "fingerprint": rung_fingerprint, "outcome": "running", "checks": []},
                indent=2, sort_keys=True) + "\n")
            previous_evidence = os.environ.get("MATUTO_RUN_EVIDENCE_DIR")
            previous_prefix = os.environ.get("MATUTO_PROGRESS_PREFIX")
            os.environ["MATUTO_RUN_EVIDENCE_DIR"] = str(rung_dir)
            os.environ["MATUTO_PROGRESS_PREFIX"] = f"rung {rules.label(rung)}"
            report("context search", f"rung {rules.label(rung)}, starting procedure")
            try:
                if module is None:
                    raise store.StoreError(load_error)
                child_target = type("Target", (), {"kind": record["kind"], "id": _record_id(record),
                                                    "label": record["name"], "fingerprint": rung_fingerprint})()
                pinned_spec = (module.prepare(copy, child_target) if record["kind"] == "language" else None)
                return_code = module.run(copy, rung_record, pinned_spec) if pinned_spec else module.run(copy, rung_record)
            except Exception as error:
                return_code = 1
                (rung_dir / "checks.json").write_text(json.dumps(_error_checks(record["kind"], str(error))))
                (rung_dir / "summary.txt").write_text(f"Rung procedure failed: {error}\n")
            finally:
                if previous_evidence is None:
                    os.environ.pop("MATUTO_RUN_EVIDENCE_DIR", None)
                else:
                    os.environ["MATUTO_RUN_EVIDENCE_DIR"] = previous_evidence
                if previous_prefix is None:
                    os.environ.pop("MATUTO_PROGRESS_PREFIX", None)
                else:
                    os.environ["MATUTO_PROGRESS_PREFIX"] = previous_prefix

            try:
                rung_evidence = _read_rung_evidence(rung_dir, return_code)
            except store.StoreError as error:
                if stop_state["stopped"]:
                    rung_evidence = {"checks": _error_checks(record["kind"], "the context search was stopped",
                                                               stopped=True),
                                    "record": {}, "summary": "Rung cancelled when the context search stopped.",
                                    "outcome": "cancelled"}
                else:
                    rung_evidence = {"checks": _error_checks(record["kind"], str(error)),
                                    "record": {}, "summary": f"Rung evidence was incomplete: {error}",
                                    "outcome": "errored"}
            if stop_state["stopped"]:
                rung_evidence["outcome"] = "cancelled"
            (rung_dir / "context-rung.json").write_text(json.dumps({
                "run_id": child, "context_size": rung, "label": rules.label(rung),
                "fingerprint": rung_fingerprint, "outcome": rung_evidence["outcome"],
                "checks": rung_evidence["checks"]}, indent=2, sort_keys=True) + "\n")
            _record_child_run(child, record, rung, rung_fingerprint, rung_evidence,
                              return_code, evidence_root, procedure_revision, child_dirty)
            children.append(child)
            ceiling = (store.read_card(copy, rung_record["card_id"])["vram_ceiling_bytes"]
                       if record["kind"] == "language" else None)
            results[rung] = _rung_result(record["kind"], rung_evidence, ceiling)
            rung_records[rung] = {"fingerprint": rung_fingerprint,
                                  "outcome": rung_evidence["outcome"],
                                  "checks": rung_evidence["checks"]}
            verdict = (rules.Verdict(rules.NO_WINNER, reason="The context search was stopped; it names no winner.")
                       if stop_state["stopped"] else rules.judge(trained_context, start_rung, results))
            report("context search", f"rung {rules.label(rung)} recorded; {verdict.reason or 'continuing'}")
    finally:
        connection.execute("UPDATE model_profiles SET context_size = ? WHERE profile_id = ?",
                           (original_context, _language_id(record)))
        copy.close()

    if stop_state["stopped"]:
        verdict = rules.Verdict(rules.NO_WINNER,
                                reason="The context search was stopped; it names no winner.")
    checks = ([{"name": "context search", "status": "PASS" if verdict.outcome == rules.WINNER else "FAIL",
                "detail": verdict.reason}] if results else
              [{"name": "context search", "status": "SKIP", "detail": verdict.reason}])
    summary = {"outcome": verdict.outcome, "winner": verdict.rung,
               "checked": verdict.checked, "reason": verdict.reason,
               "runs": children, "rungs": [
                   {"context": rung, "label": rules.label(rung), "run_id": children[index],
                    **rung_records[rung]}
                   for index, rung in enumerate(results)]}
    evidence("context_search", dict(record, fingerprint=store.fingerprint(opened, record)),
             checks, verdict.reason or "Context search stopped without a winner.",
             **{"context-search.json": json.dumps(summary, indent=2, sort_keys=True) + "\n"})
    if stop_state["stopped"]:
        os.kill(os.getpid(), signal.SIGTERM)
    return 0


def _target(record):
    return type("Target", (), {"kind": record["kind"], "id": _record_id(record),
                                "label": record["name"],
                                "fingerprint": record.get("fingerprint", "")})()


def _record_id(record):
    return record.get("profile_id") or record.get("bundle_id")


def _language_id(record):
    return record["members"]["language"] if record["kind"] == "bundle" else record["profile_id"]


def _trained_context(opened, record):
    return store.read_trained_context(opened, _language_id(record))


def _profile_context(connection, record):
    row = connection.execute("SELECT context_size FROM model_profiles WHERE profile_id = ?",
                             (_language_id(record),)).fetchone()
    if row is None:
        raise store.StoreError("the context search's language profile no longer exists")
    return row["context_size"]


def _read_rung_evidence(directory, return_code):
    try:
        checks = json.loads((directory / "checks.json").read_text())
        summary = (directory / "summary.txt").read_text().strip()
    except (OSError, ValueError) as error:
        raise store.StoreError(f"rung procedure did not record complete evidence: {error}") from None
    outcome = "completed" if return_code == 0 else "errored"
    record = {}
    report_path = directory / "wq" / "report.json"
    if report_path.is_file():
        details = json.loads(report_path.read_text())
        outcome = "completed" if not details.get("interrupted") else "interrupted"
        rows = details.get("results", ())
        server_loaded = any(row.get("id") == "SETUP" and row.get("status") == "PASS" for row in rows)
        peaks = [row.get("observations", {}).get("sampled_peak_vram_bytes") for row in rows]
        peaks = [value for value in peaks if type(value) is int]
        record.update(server_loaded=server_loaded, peak_vram=max(peaks) if peaks else None)
    return {"checks": checks, "record": record, "summary": summary, "outcome": outcome}


def _error_checks(kind, detail, *, stopped=False):
    status = "SKIP" if stopped else "ERROR"
    untouched = "SKIP"
    names = ("WQ-06", "WQ-10") if kind == "language" else ("fit", "real-time", "recognition")
    return ([{"name": "procedure", "status": status, "detail": detail}]
            + [{"name": name, "status": untouched, "detail": "not reached"} for name in names])


def _rung_result(kind, item, vram_ceiling=None):
    checks = tuple((row["name"], row["status"]) for row in item["checks"])
    if kind == "language":
        facts = item["record"]
        return rules.RungResult(kind, checks, facts.get("server_loaded", False),
                                facts.get("peak_vram"), vram_ceiling)
    return rules.RungResult(kind, checks)


def _start_child_run(record, rung, fingerprint, started, procedure_revision, dirty_checkout):
    """Persist a rung before execution so runner recovery can settle interruption."""
    live = store.open_store(os.environ.get("MATUTO_STORE_CONFIG_ROOT"))
    try:
        connection = live.connection()
        connection.execute("BEGIN IMMEDIATE")
        parent = connection.execute(
            "SELECT unit_name FROM runs WHERE procedure='context_search' AND outcome IS NULL"
            " AND record_kind=? AND record_id=? ORDER BY started_at DESC LIMIT 1",
            (record["kind"], _record_id(record))).fetchone()
        if parent is None:
            raise store.StoreError("the context-search parent run is no longer active")
        build = connection.execute("SELECT build_id FROM runtime_selection WHERE selection_id=1").fetchone()
        run_id = "run-" + uuid.uuid4().hex
        connection.execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, fingerprint, build_id,"
            " procedure, procedure_revision, dirty_checkout, unit_name, started_at, summary)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, record["kind"], _record_id(record), record["name"], fingerprint,
             build["build_id"] if build else None,
             "wq" if record["kind"] == "language" else "bundle", procedure_revision,
             int(bool(dirty_checkout)), parent["unit_name"], started,
             f"Context search rung {rules.label(rung)} is running."))
        connection.commit()
        return run_id
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        live.close()


def _record_child_run(run_id, record, rung, fingerprint, rung_evidence, return_code,
                      evidence_root, procedure_revision, dirty_checkout):
    """Commit a rung's checks and artifacts into its already-persisted run."""
    live = store.open_store(os.environ.get("MATUTO_STORE_CONFIG_ROOT"))
    try:
        connection = live.connection()
        connection.execute("BEGIN IMMEDIATE")
        outcome = rung_evidence["outcome"]
        if outcome == "completed" and return_code != 0:
            outcome = "errored"
        connection.execute(
            "UPDATE runs SET procedure_revision=?, dirty_checkout=? WHERE run_id=? AND outcome IS NULL",
            (procedure_revision, int(bool(dirty_checkout)), run_id))
        rung_dir = evidence_root / "rungs" / rules.label(rung)
        runner._purge_wq_private(rung_dir)
        artifacts = []
        for path in sorted(rung_dir.rglob("*")):
            if path.is_file():
                body = path.read_bytes()
                truncated = len(body) > runner.EVIDENCE_LIMIT
                body = body[:runner.EVIDENCE_LIMIT]
                artifacts.append((path.relative_to(rung_dir).as_posix(),
                                  mimetypes.guess_type(path.name)[0] or "",
                                  len(body), hashlib.sha256(body).hexdigest(), int(truncated), body))
        checks = [(sequence, row["name"], row["status"], row.get("detail", ""))
                  for sequence, row in enumerate(rung_evidence["checks"])]
        store.write_run_evidence(
            connection, run_id, outcome=outcome,
            ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            summary=rung_evidence["summary"], log="", log_truncated=False,
            checks=checks, artifacts=artifacts)
        connection.commit()
        return run_id
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        live.close()
