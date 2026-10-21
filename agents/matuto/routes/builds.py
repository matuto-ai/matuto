"""The protected API for Runtime › llama.cpp builds."""

import contextlib
import json
import sqlite3

from matuto import (admission, cards, installation, registry, runner, runtime_builds,
                    runtime_selection, serving_state, store)
from matuto.web import RefusedError

PATH = "/api/builds"


def _body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RefusedError("The runtime build form is not valid JSON.") from None
    if not isinstance(value, dict):
        raise RefusedError("The runtime build action must be an object.")
    return value


def _inspection(result):
    return {"state": result["state"], "reason": result.get("reason", ""),
            "findings": [{"file": item.file, "entry": item.entry, "reason": item.reason}
                         for item in result.get("findings", ())],
            "needs_consent": result.get("needs_consent", False),
            "consent_digest": result.get("consent_digest"),
            "observations": result.get("observations", {})}


def _selected(connection):
    row = connection.execute(
        "SELECT s.build_id, b.label, b.backend, s.card_id, c.name AS card"
        " FROM runtime_selection AS s LEFT JOIN builds AS b ON b.build_id = s.build_id"
        " LEFT JOIN cards AS c ON c.card_id = s.card_id WHERE s.selection_id = 1").fetchone()
    return dict(row) if row else None


def _list(opened):
    connection = opened.connection()
    inventory = cards.detect(opened)
    selected = _selected(connection)
    entries = registry.listing(connection)
    for entry in entries:
        if entry["build_id"]:
            entry["available"], entry["reason"] = registry.availability(connection, entry["build_id"])
            if entry["source_revision"] is None and not entry["available"]:
                try:
                    findings = installation.loader_findings(entry["location"], entry["recipe"])
                except (OSError, ValueError, installation.InspectionError) as error:
                    findings = (installation.Finding("", "", str(error)),)
                entry["loader_findings"] = [
                    {"file": item.file, "entry": item.entry, "reason": item.reason}
                    for item in findings]
        else:
            entry["available"] = False
    profiles = [dict(row) for row in connection.execute(
        "SELECT profile_id, name, card_id FROM model_profiles WHERE card_id IS NOT NULL"
        " ORDER BY name, profile_id")]
    return {"entries": entries, "selection": selected,
            "cards": [{"card_id": card["card_id"], "name": card["name"],
                       "gpu_identity": card["gpu_identity"], "detected": card["detected"]}
                      for card in inventory["cards"]],
            "profiles": profiles, "csrf": None}


def _action(opened, request, body):
    connection = opened.connection()
    action = body.get("action")
    build_id, location = body.get("build_id"), body.get("location")
    if action == "inspect":
        entries = registry.listing(connection)
        entry = (next((item for item in entries if item["build_id"] == build_id), None)
                 if build_id else next((item for item in entries
                                        if item["location"] == location), None))
        if entry is None:
            raise store.StoreError("That build tree was not found.")
        if entry.get("source_revision") is None:
            result = registry.inspect_external(connection, build_id) if build_id else {
                "state": "unchecked", "reason": "Register this external tree before inspecting it.",
                "findings": (), "needs_consent": False}
            return {"inspection": _inspection(result)}
        result = registry.inspect(connection, entry["location"])
        return {"inspection": {"state": result.state, "reason": str(result.reason or ""),
                                "findings": [{"file": item.file, "entry": item.entry,
                                              "reason": item.reason}
                                             for item in result.loader_findings],
                                "needs_consent": False, "observations": result.observations}}
    if action == "register":
        if body.get("external"):
            result = registry.register_external(connection, build_id, location,
                                                body.get("recipe"), body.get("label"))
        else:
            result = registry.register(connection, build_id, location, body.get("label"))
        return {"entry": result}
    if action == "rename":
        return {"entry": registry.register(connection, build_id, location, body.get("label"))}
    if action == "forget":
        registry.forget(connection, build_id)
        return {"forgotten": build_id}
    if action == "trust":
        result = registry.inspect_external(connection, build_id, trust_digest=body.get("trust_digest"))
        return {"inspection": _inspection(result)}
    if action in ("select", "use"):
        inventory = cards.detect(opened)
        detected = {item["pciAddress"] for item in inventory["devices"]}
        _target_state, targets, _reason = runtime_builds._host_targets()
        hip_targets = runtime_builds._targets_for_cards(inventory["devices"], targets)
        serving = serving_state.read().state
        work = runner._current_work(connection)
        runtime_run = work is not None and (
            work.kind in admission.RUN_KINDS and work.kind not in admission.NO_CARD_RUNS)
        if action == "select":
            selected = runtime_selection.select(
                connection, build_id, body.get("backend"), body.get("card_id"),
                serving=serving, runtime_run=runtime_run, detected_cards=detected,
                hip_targets=hip_targets)
        else:
            selected = runtime_selection.use(
                connection, build_id, serving=serving, runtime_run=runtime_run,
                detected_cards=detected, hip_targets=hip_targets)
        return {"selection": selected}
    raise store.StoreError("Choose a runtime build action.")


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        raise RefusedError("Runtime builds accepts GET and POST requests only.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method in ("GET", "HEAD"):
                result = _list(opened)
                result["csrf"] = request.csrf
                return 200, result
            result = _action(opened, request, _body(request))
            result.update(_list(opened))
            result["csrf"] = request.csrf
            return 200, result
    except RefusedError:
        raise
    except (OSError, sqlite3.Error, store.StoreError, TypeError, ValueError) as error:
        raise RefusedError(str(error)) from None
