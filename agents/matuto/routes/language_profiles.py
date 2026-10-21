"""List and create language profiles through the protected manager API."""

import contextlib
import json
import os
import sqlite3
from collections.abc import Mapping

from matuto import cards, registry, router, store
from matuto.web import RefusedError

PATH = "/api/language-profiles"


def _device_context(opened):
    """Use current GPU detection for honest availability labels and store validation."""
    detected = cards.detect(opened)
    return detected


def _runtime_validation(connection, requested_tools=()):
    """Read server options and requested optional-tool help for launch-argument save validation."""
    selected = connection.execute(
        "SELECT b.build_id, b.location, b.backend, c.gpu_identity"
        " FROM runtime_selection AS s JOIN builds AS b ON b.build_id = s.build_id"
        " JOIN cards AS c ON c.card_id = s.card_id WHERE s.selection_id = 1").fetchone()
    if selected is None:
        return None, None, {}
    available, reason = registry.availability(connection, selected["build_id"])
    if not available:
        raise store.StoreError(reason or "The active runtime build is unavailable.")
    from matuto import matuto_runtime

    environment = matuto_runtime.execution_environment()
    help_text = {}
    missing_tools = {}
    selector = None
    for tool, executable in (("server", "llama-server"), ("smoke", "llama-cli"),
                             ("bench", "llama-bench")):
        if tool != "server" and tool not in requested_tools:
            continue
        try:
            directory, descriptor = matuto_runtime._tool_descriptor(selected["location"], executable)
        except FileNotFoundError:
            if tool == "server":
                raise
            missing_tools[tool] = executable
            continue
        try:
            help_text[tool] = matuto_runtime.probe(descriptor, directory, "--help", environment)
            if tool == "server":
                if not all(flag in help_text[tool] for flag in ("--device", "--list-devices")):
                    raise store.StoreError("The active build does not advertise device selection.")
                selection = dict(selected, runtime_backend=router.BACKENDS.get(selected["backend"]))
                if selection["runtime_backend"] is None:
                    raise store.StoreError("The active build has an unknown backend.")
                selector = router._selector(descriptor, directory, environment, selection)
        finally:
            os.close(descriptor)
            os.close(directory)
    return help_text, selector, missing_tools


def _complete_picks(connection):
    """Offer only records whose stored verification and every file identity are complete."""
    picks = {"model": [], "mmproj": [], "mtp": []}
    records = connection.execute(
        "SELECT r.acquisition_id, r.name FROM acquisition_records AS r"
        " WHERE r.verified_at IS NOT NULL AND EXISTS (SELECT 1 FROM acquisition_files f"
        " WHERE f.acquisition_id = r.acquisition_id)"
        " AND NOT EXISTS (SELECT 1 FROM acquisition_files f WHERE f.acquisition_id = r.acquisition_id"
        " AND (f.verified_at IS NULL OR length(f.computed_sha256) <> 64))"
        " ORDER BY r.name, r.acquisition_id").fetchall()
    for record in records:
        files = connection.execute(
            "SELECT file_name FROM acquisition_files WHERE acquisition_id = ? ORDER BY file_name",
            (record["acquisition_id"],)).fetchall()
        for file in files:
            for role in picks:
                picks[role].append({"name": record["name"], "file_name": file["file_name"],
                                    "pick": {"acquisition_id": record["acquisition_id"],
                                             "file_name": file["file_name"]}})
    return picks


def _list(opened):
    connection = opened.connection()
    detected = _device_context(opened)
    records = []
    for row in connection.execute(
            "SELECT profile_id FROM model_profiles WHERE kind = 'language' ORDER BY name, profile_id"):
        profile = store.read_record(opened, "language", row["profile_id"])
        qualification = store.qualification_state(opened, profile)
        available, reason = store.record_availability(
            profile, connection, detected_gpus=detected)
        if qualification["state"] == "unavailable":
            available = False
            reason = reason or qualification["reason"]
        records.append({"profile_id": profile["profile_id"], "name": profile["name"],
                        "alias": profile["alias"], "published": profile["published"],
                        "qualification": qualification, "available": available, "reason": reason})
    cards_list = [dict(row) for row in connection.execute(
        "SELECT card_id, name FROM cards ORDER BY name, card_id")]
    prerequisites = []
    if connection.execute(
            "SELECT 1 FROM runtime_selection WHERE selection_id = 1").fetchone() is None:
        runtime_href, runtime_destination = (
            ("#/runtime/builds", "Runtime › llama.cpp builds")
            if registry.listing(connection) else
            ("#/runtime/install", "Runtime › Install a version"))
        prerequisites.append({"name": "a selected runtime", "href": runtime_href,
                              "destination": runtime_destination})
    if not cards_list:
        prerequisites.append({"name": "a card", "href": "#/settings/cards",
                              "destination": "Settings › Cards"})
    catalogue = {tool: [] for tool in ("server", "smoke", "bench")}
    for row in connection.execute(
            "SELECT flag, takes_value, tools FROM argument_catalogue ORDER BY flag"):
        for tool in catalogue:
            if tool in (row["tools"] or "").split(","):
                catalogue[tool].append({"flag": row["flag"], "takes_value": bool(row["takes_value"])})
    return {"profiles": records, "cards": cards_list, "picks": _complete_picks(connection),
            "catalogue": catalogue, "prerequisites": prerequisites}


def handle(request):
    if request.method not in ("GET", "POST"):
        raise RefusedError("Language profiles supports GET and POST.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method == "GET":
                return 200, _list(opened)
            try:
                record = json.loads(request.body or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RefusedError("The language profile form is not valid JSON.") from None
            if not isinstance(record, Mapping):
                raise RefusedError("language profile field 'record' must be an object.")
            if record.get("kind") != "language":
                raise RefusedError("language profile field 'kind' must be 'language'.")
            rows = record.get("arguments")
            requested_tools = ({row["tool"] for row in rows
                                if isinstance(row, Mapping) and isinstance(row.get("tool"), str)}
                               if isinstance(rows, list) else set())
            help_text, selector, missing_tools = _runtime_validation(
                opened.connection(), requested_tools)
            saved, warnings = store.save_record(
                opened, record, help_text=help_text, selector=selector)
            for tool, executable in missing_tools.items():
                if any(row["tool"] == tool for row in saved["arguments"]):
                    warnings.append(
                        f"{tool} arguments are saved but unavailable on this build because {executable} is missing.")
            return 201, {"profile_id": saved["profile_id"], "name": saved["name"],
                         "alias": saved["alias"], "published": saved["published"],
                         "warnings": warnings}
    except RefusedError:
        raise
    except (store.StoreError, store.RecordRefusal, sqlite3.Error, OSError, ValueError) as error:
        raise RefusedError(str(error)) from None
