"""Show Maintenance state and start the independent Repair action."""

import json
import re
import secrets
import subprocess
import sys
import threading

from matuto import removal, route_gate, store, updater, updates
from matuto.web import RefusedError

PATH = "/api/maintenance"
_REMOVAL_LOCK = threading.Lock()
_REMOVAL_STATES = {}
_REMOVAL_DELIVERED = {}


def _set_removal_state(removal_id, stage, *, kept=None, error=""):
    state = {"stage": stage}
    if kept is not None:
        state["kept"] = kept
    if error:
        state["error"] = str(error)[:500]
    with _REMOVAL_LOCK:
        _REMOVAL_STATES[removal_id] = state
        if stage == "starting":
            _REMOVAL_DELIVERED[removal_id] = threading.Event()
        while len(_REMOVAL_STATES) > 8:
            oldest = next(iter(_REMOVAL_STATES))
            _REMOVAL_STATES.pop(oldest)
            _REMOVAL_DELIVERED.pop(oldest, None)


def _removal_state(removal_id):
    with _REMOVAL_LOCK:
        return dict(_REMOVAL_STATES.get(removal_id, {"stage": "pending"}))


def _ack_removal_handoff(removal_id):
    with _REMOVAL_LOCK:
        delivered = _REMOVAL_DELIVERED.get(removal_id)
    if delivered is not None:
        delivered.set()


def _wait_for_removal_handoff(removal_id, *, timeout=10):
    with _REMOVAL_LOCK:
        delivered = _REMOVAL_DELIVERED.get(removal_id)
    if delivered is None or not delivered.wait(timeout=timeout):
        raise removal.RemovalError("The actual kept-data handoff was not acknowledged; finalization was stopped.")


def _update_state():
    raw = store.read_update_state(store.store_path())
    details = json.loads(raw["phase"]) if raw and raw.get("phase") else {}
    return raw, details if isinstance(details, dict) else {}


def _repair_result():
    try:
        _raw, details = _update_state()
    except (OSError, store.StoreError, TypeError, ValueError):
        return None
    result = details.get("repair_result")
    if result not in ("complete", "failed", "in_progress"):
        return None
    return {"stage": result, "error": details.get("repair_error", "")}


def _record_repair(result, error=""):
    raw, details = _update_state()
    version = updater.installed_version()
    stage = details.get("stage", "complete")
    list_number = details.get("list_number")
    details_to_write = {"repair_result": result, "repair_error": error[:500]}
    if result == "in_progress":
        prior_result = details.get("repair_result")
        recovered_update = (details.get("repair_recovered_update") is True
                            if prior_result == "failed" else False)
        details_to_write.update(repair_prior_result=prior_result,
                                repair_recovered_update=recovered_update)
    store.write_update_state(store.store_path(), version, stage, list_number=list_number,
                             details=details_to_write)


def _status(csrf):
    plan = removal.preview()
    update = updates.display()
    return {"removal": plan, "installation": update.get("installation"),
            "repair": _repair_result(), "csrf": csrf}


def handle(request):
    refused = route_gate.require_session(request)
    if refused:
        return refused
    if request.method in ("GET", "HEAD"):
        removal_id = request.query.get("removal_id", [None])[0]
        if removal_id is not None:
            state = _removal_state(removal_id)
            acknowledge = (_ack_removal_handoff if request.method == "GET"
                           and state.get("stage") == "finalizing" else None)
            after_reply = (lambda: acknowledge(removal_id)) if acknowledge else None
            return 200, state, "application/json", {}, after_reply
        try:
            return 200, _status(request.csrf)
        except (OSError, TypeError, ValueError, store.StoreError, removal.RemovalError) as error:
            raise RefusedError(f"Maintenance status is unavailable: {error}") from None
    if request.method != "POST":
        raise RefusedError("Maintenance accepts GET and POST requests only.")
    try:
        body = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("The Maintenance request is invalid JSON.") from None
    if (not isinstance(body, dict)
            or set(body) not in ({"action"}, {"action", "removalId"})
            or (set(body) == {"action", "removalId"}
                and (body.get("action") != "remove"
                     or not isinstance(body.get("removalId"), str)
                     or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                         body["removalId"], re.IGNORECASE)))):
        raise RefusedError("Choose Repair Matuto, preview removal, or Remove Matuto.")
    action = body["action"]
    removal_id = body.get("removalId")
    if action == "remove" and removal_id is None:
        raise RefusedError("Refresh Maintenance before confirming removal.")
    try:
        _raw, update = _update_state()
        if update.get("stage") == "maintenance" and action != "repair":
            raise RefusedError("Matuto update ended in maintenance; Repair Matuto must finish before removal.")
        if action == "repair":
            script = updater.independent_updater_script(updater.data_directory())
            unit = f"matuto-repair-{secrets.token_hex(12)}.service"
            _record_repair("in_progress")
            command = ["systemd-run", "--user", "--collect", f"--unit={unit}", "--",
                       sys.executable, "-B", str(script), "repair"]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, timeout=15, check=False)
            except subprocess.TimeoutExpired:
                raise RefusedError("Repair start is taking longer than expected; check its recorded outcome before retrying.") from None
            except OSError as error:
                _record_repair("failed", str(error))
                raise RefusedError(f"The independent Repair process could not start: {error}") from None
            if result.returncode:
                reason = result.stdout.strip() or f"systemd-run exited {result.returncode}"
                _record_repair("failed", reason)
                raise RefusedError(f"The independent Repair process could not start: {reason}")
            return 202, {"started": True, "unit": unit}
        if action in ("remove-preview", "remove"):
            if action == "remove":
                _set_removal_state(removal_id, "starting")
            try:
                plan = removal.preview()
                if plan["blockers"]:
                    refused = RefusedError("Removal is blocked: " + "; ".join(plan["blockers"]))
                    refused.blockers = plan["blockers"]
                    if removal_id:
                        _set_removal_state(removal_id, "refused", error=refused)
                    raise refused
                if action == "remove-preview":
                    return 200, plan
                kept = []
                try:
                    def handoff(actual):
                        kept.extend(actual)
                        _set_removal_state(removal_id, "finalizing", kept=actual)
                        _wait_for_removal_handoff(removal_id)
                    result = removal.remove(confirm=True, on_kept=handoff)
                    actual = result["kept"]
                    _set_removal_state(removal_id, "complete", kept=actual)
                    return 200, {"removed": True, "kept": actual}
                except removal.RemovalError as error:
                    if kept:
                        _set_removal_state(removal_id, "failed", kept=kept, error=error)
                        return 200, {"removed": False, "finalStepFailed": True,
                                     "kept": kept, "error": str(error)}
                    _set_removal_state(removal_id, "refused", error=error)
                    raise
            except RefusedError:
                raise
            except (OSError, TypeError, ValueError, store.StoreError,
                    removal.RemovalError) as error:
                if removal_id:
                    _set_removal_state(removal_id, "refused", error=error)
                raise
    except RefusedError:
        raise
    except (OSError, TypeError, ValueError, subprocess.SubprocessError, store.StoreError,
            updater.UpdateError, removal.RemovalError) as error:
        raise RefusedError(str(error)) from None
    raise RefusedError("Choose Repair Matuto, preview removal, or Remove Matuto.")
