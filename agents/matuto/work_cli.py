"""CLI presentation of the shared runner's run and job records."""

import codecs
import json
import re
import signal
import sys
import time
from pathlib import Path

from matuto import admission, discovery, runner, serving_state, store


def open_runner():
    """Open the runner with the app's serving read; unknown card use fails closed."""
    latest = {}

    def read_serving():
        latest["state"] = serving_state.read().state
        return latest["state"]

    def card_holder():
        # Matuto serving is the card holder admission can offer to stop. When Matuto
        # is stopped, no current reader proves a foreign llama-server is absent.
        if latest.get("state") in (admission.AGENTIC, admission.VOICE):
            return admission.Card(admission.CARD_FREE)
        return admission.Card(admission.CARD_UNKNOWN)

    try:
        return runner.Runner(serving_state=read_serving, card_holder=card_holder)
    except (OSError, store.StoreError) as error:
        print(f"matuto: {error}", file=sys.stderr)
        return None


def record_target(opened, name):
    """Resolve one language profile or bundle by its stored name or ID."""
    connection = opened.connection()
    rows = connection.execute(
        "SELECT profile_id AS record_id, name, kind FROM model_profiles"
        " WHERE kind = 'language' AND (name = ? OR profile_id = ?)"
        " UNION ALL SELECT bundle_id AS record_id, name, 'bundle' AS kind FROM bundles"
        " WHERE name = ? OR bundle_id = ?",
        (name, name, name, name)).fetchall()
    if not rows:
        raise ValueError(f"no language profile or bundle named {name!r} is stored")
    if len(rows) != 1:
        raise ValueError(f"{name!r} matches more than one stored record; use its record ID")
    row = rows[0]
    record = store.read_record(opened, row["kind"], row["record_id"])
    fingerprint = store.fingerprint(opened, record)
    if fingerprint is None:
        raise ValueError(f"{row['name']!r} has no complete served configuration and cannot run")
    return runner.Target(row["kind"], row["record_id"], row["name"], fingerprint)


def follow(running, work_id):
    """Print new output until the work ends; return success only for completion."""
    offset = 0
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            work = next((item for item in running.works() if item.work_id == work_id), None)
            if work is None:
                print(f"matuto: no run or job has ID {work_id!r}", file=sys.stderr)
                return 1
            output, offset = running.output_chunk(work, offset)
            finished = bool(work.outcome) and len(output) < runner.LOG_LIMIT
            text = decoder.decode(output, final=finished)
            if text:
                print(text, end="", flush=True)
            if finished:
                print(f"Run {work.work_id}: {work.outcome}")
                return 0 if work.outcome == runner.COMPLETED else 1
            if not work.outcome:
                time.sleep(runner.POLL_SECONDS)
    except KeyboardInterrupt:
        stopped = running.stop(work_id)
        if stopped is None:
            print(f"matuto: run {work_id!r} no longer exists", file=sys.stderr)
            return 1
        print(f"Run {work_id}: {stopped.outcome or stopped.state}")
        return 0 if stopped.outcome == runner.COMPLETED else 1


def list_work(running):
    """Print all runs and shared jobs in the runner's common newest-first list."""
    entries = running.works()
    for work in entries:
        address = ""
        if work.kind == "serve":
            progress = running.progress(work)
            match = (re.fullmatch(r"https?://127\.0\.0\.1:\d{1,5}", progress.step)
                     if progress else None)
            if not match and progress:
                match = re.search(r"https?://127\.0\.0\.1:\d{1,5}\b", progress.log)
            address = f"\t{match.group(0)}" if match else ""
        print(f"{work.work_id}\t{work.kind}\t{work.purpose}\t{work.state}\t{work.started_at}{address}")
    if not entries:
        print("No runs or jobs.")
    return 0


def run_procedure(procedure, name):
    """Start through the runner, then follow the recorded work to its outcome."""
    if procedure not in discovery.item_names("procedures"):
        print(f"matuto: unknown procedure {procedure!r}", file=sys.stderr)
        return 2
    procedure_module = discovery.load_item("procedures", procedure)
    running = open_runner()
    if running is None:
        return 1
    try:
        target = record_target(running.store, name)
        command = [sys.executable, str(Path(__file__).with_name("procedure_runner.py")),
                   procedure, target.kind, target.id, target.fingerprint]
        if hasattr(procedure_module, "prepare"):
            command.append(json.dumps(procedure_module.prepare(running.store, target), separators=(",", ":")))
        request = runner.Request.run(
            procedure, f"{procedure} for {target.label}", command, target,
            getattr(procedure_module, "REVISION", "") or
            (procedure_module.revision() if hasattr(procedure_module, "revision") else ""),
            pty=bool(getattr(procedure_module, "PTY", False)),
            dirty_checkout=(procedure_module.dirty_checkout()
                            if hasattr(procedure_module, "dirty_checkout") else False))
        interrupted = False

        def defer_interrupt(_signum, _frame):
            nonlocal interrupted
            interrupted = True

        previous_handler = signal.signal(signal.SIGINT, defer_interrupt)
        try:
            started = running.start(request)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
        if interrupted:
            if started.work is None:
                print("matuto: interrupted before a run was recorded", file=sys.stderr)
                return 1
            stopped = running.stop(started.work.work_id)
            print(f"Run {stopped.work_id}: {stopped.outcome or stopped.state}")
            return 0 if stopped.outcome == runner.COMPLETED else 1
        if started.decision != admission.ADMIT:
            print(f"matuto: {started.reason}", file=sys.stderr)
            return 1
        if started.work is None:
            print("matuto: the runner admitted no work", file=sys.stderr)
            return 1
        try:
            return follow(running, started.work.work_id)
        except KeyboardInterrupt:
            stopped = running.stop(started.work.work_id)
            print(f"Run {stopped.work_id}: {stopped.outcome or stopped.state}")
            return 0 if stopped.outcome == runner.COMPLETED else 1
    except (OSError, store.StoreError, ValueError) as error:
        print(f"matuto: {error}", file=sys.stderr)
        return 1
    finally:
        running.close()
