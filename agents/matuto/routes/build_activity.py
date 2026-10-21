"""The authenticated Build activity API for recorded runtime build jobs."""

import json
import sqlite3

from matuto import admission, login, runner, runtime_build, serving_state, store, versions

PATH = "/api/build-activity"


def _runner():
    latest = {}

    def read_serving():
        latest["state"] = serving_state.read().state
        return latest["state"]

    def card_holder():
        state = latest.get("state")
        if state in (admission.AGENTIC, admission.VOICE):
            return admission.Card(admission.CARD_FREE)
        return admission.Card(admission.CARD_UNKNOWN)

    return runner.Runner(serving_state=read_serving, card_holder=card_holder)


def _pin(history):
    try:
        pin = json.loads(history["pinnedRequest"])
    except (TypeError, ValueError):
        pin = {}
    return pin if isinstance(pin, dict) else {}


def _serialize(opened, running):
    entries = []
    connection = opened.connection()
    for work in running.works():
        if work.kind != admission.BUILD:
            continue
        history = runtime_build.history(connection, work.work_id)
        if history is None:
            continue
        pin = _pin(history)
        provenance = history["provenance"] or {
            key: pin[key] for key in ("recipe", "ref", "commit", "target") if pin.get(key)
        }
        progress = running.progress(work)
        outcome = {"failed": "errored"}.get(work.outcome, work.outcome)
        state = outcome or work.state
        entries.append({
            "id": work.work_id,
            "recipe": pin.get("recipe", "Unknown recipe"),
            "ref": pin.get("ref", ""),
            "commit": pin.get("commit", ""),
            "target": pin.get("target") or "",
            "state": state,
            "outcome": outcome,
            "startedAt": work.started_at,
            "startedBy": login.current_username(),
            "phase": progress.phase if progress else "",
            "step": progress.step if progress else "",
            "elapsed": progress.elapsed if progress else 0,
            "log": progress.log if progress else "",
            "logTruncated": bool(progress and progress.log_truncated),
            "historyLogEmpty": not history["log"],
            "unknown": progress.unknown if progress else "",
            "failureReason": (history["reason"] or {
                "errored": "The build job ended unsuccessfully.",
                "cancelled": "The build was stopped before completion.",
                "interrupted": "The build ended without recording completion.",
            }.get(outcome, "")),
            "provenance": provenance,
            "outputDigests": history["outputDigests"],
            "canStop": not bool(work.outcome) and work.state != runner.UNKNOWN,
            "canRetry": outcome in ("errored", "cancelled", "interrupted"),
            "canDeleteHistory": bool(work.outcome),
        })
    state = serving_state.read().state
    contention = state == admission.AGENTIC and any(
        entry["state"] in (runner.RUNNING, runner.LAUNCHING) for entry in entries)
    return {"builds": entries, "contention": contention}


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON request") from error
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    return value


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        return 405, {"error": "Build activity accepts GET and POST requests only."}
    try:
        running = _runner()
    except (OSError, store.StoreError) as error:
        return 503, {"error": str(error)}
    try:
        if request.method in ("GET", "HEAD"):
            data = _serialize(running.store, running)
            data["csrf"] = request.csrf
            return 200, data

        body = _json_body(request)
        action, work_id = body.get("action"), body.get("id")
        if not isinstance(work_id, str) or not work_id:
            raise ValueError("a build job ID is required")
        work = running.work(work_id)
        if work is None or work.kind != admission.BUILD:
            return 404, {"error": f"No build job has ID {work_id!r}."}
        if action == "stop":
            stopped = running.stop(work_id)
            if stopped is None:
                return 404, {"error": f"No build job has ID {work_id!r}."}
            if stopped.state == runner.UNKNOWN and not stopped.outcome:
                return 409, {"error": f"The state of build {work_id} is unknown; it was not reported stopped."}
            return 200, {"id": stopped.work_id,
                         "outcome": {"failed": "errored"}.get(stopped.outcome, stopped.outcome),
                         "state": stopped.state}
        if action == "retry":
            if work.outcome not in ("failed", runner.CANCELLED, runner.INTERRUPTED):
                raise ValueError(f"Build {work_id} cannot be retried unless it errored, was cancelled, or was interrupted.")
            history = runtime_build.history(running.store.connection(), work_id)
            pin = _pin(history)
            try:
                pinned = versions.pin(pin["recipe"], pin["ref"], pin["commit"], pin.get("target"))
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"Build {work_id} has no valid pinned request to retry: {error}") from None
            result = running.start(runtime_build.runner_request(pinned))
            if result.decision != admission.ADMIT:
                reason = result.reason or "The build retry was refused; nothing was queued."
                if reason.startswith("Voice serving is up. Stop serving before"):
                    reason = reason.replace("Voice serving is up. Stop serving before",
                                            "Voice serving is up. Stop serving on Overview before", 1)
                return 409, {"error": reason}
            return 202, {"work": result.work.work_id if result.work else None}
        if action == "delete-history":
            if not work.outcome:
                raise ValueError("A running build's history cannot be deleted.")
            connection = running.store.connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                runtime_build.forget_history(connection, work_id)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return 200, {"deletedHistory": work_id}
        raise ValueError("unknown Build activity action")
    except (OSError, sqlite3.Error, login.LoginError, store.StoreError, ValueError) as error:
        return 409, {"error": str(error)}
    finally:
        running.close()
