"""The Model files wizard's session-gated HTTP interface."""

import json
import os
import re
import uuid
from pathlib import Path

from matuto import (acquisition, adoption, admission, download, origins, route_gate, router,
                    runner, serving_state, storage, store, verification)
from matuto.web import RefusedError

PATH = "/api/acquisition"
_COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


def handle(request):
    if request.method not in ("GET", "POST"):
        raise RefusedError("Model files accepts GET and POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    opened = store.open_store()
    try:
        connection = opened.connection()
        config_root = opened.path.parents[2]
        current = {}

        def serving():
            current["state"] = serving_state.read(
                config_root=config_root, store_connection=connection).state
            return current["state"]

        running = runner.Runner(config_root, opened_store=opened, serving_state=serving,
                         card_holder=lambda: router.card_holder(config_root, connection))
        if request.method == "GET":
            return 200, _prepare(connection, running)
        payload = _payload(request)
        action = payload.get("action")
        if action == "artifact-files":
            return 200, _artifact_files(connection, payload.get("location_id"))
        if action == "verify-record":
            acquisition_id = _record_id(payload)
            result = verification.verify_record(connection, acquisition_id,
                                                consent=payload.get("consent") is True)
            return 200, {"complete": result.complete, "reason": result.reason,
                         "label": result.label, "verified_at": result.verified_at,
                         "files": [{"name": item.file_name, "state": item.state,
                                    "reason": item.reason} for item in result.files]}
        if action == "adopt":
            location_id = payload.get("location_id")
            file_name = payload.get("file_name")
            if not isinstance(location_id, str) or not isinstance(file_name, str):
                raise RefusedError("Choose a file from a configured artifact location.")
            location = connection.execute(
                "SELECT path FROM artifact_locations WHERE location_id = ?", (location_id,)).fetchone()
            if location is None or file_name not in {item["name"] for item in _artifact_files(connection, location_id)["files"]}:
                raise RefusedError("Choose a listed file from a configured artifact location.")
            result = adoption.adopt(connection, "acq-" + uuid.uuid4().hex,
                                    payload.get("name") or file_name, Path(location["path"]) / file_name,
                                    consent=payload.get("consent") is True)
            return 200, {"acquisition_id": result.acquisition_id, "complete": result.complete,
                         "label": result.label, "verified_at": result.verified_at}
        if action in ("resume", "discard-preview", "discard"):
            job_id = payload.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise RefusedError("A download job ID is required.")
            if action == "resume":
                result = download.resume(running, job_id)
                if result.start.decision != admission.ADMIT or result.start.work is None:
                    raise RefusedError(result.start.reason or "The download was not resumed.")
                return 200, {"job_id": result.start.work.work_id, "warning": result.warning}
            if action == "discard-preview":
                return 200, _discard_data(download.preview_discard(running, job_id))
            preview = _discard_input(payload.get("preview"), job_id)
            try:
                download.confirm_discard(running, preview)
            except download.DiscardChanged as error:
                fresh = _discard_data(error.preview)
                return 409, {"error": str(error), "preview": fresh}
            except download.DownloadError as error:
                try:
                    fresh = _discard_data(download.preview_discard(running, job_id))
                except download.DownloadError as preview_error:
                    fresh = {"job_id": job_id, "files": [], "total_size": 0,
                             "consequence": f"No fresh safe preview is available: {preview_error}"}
                return 409, {"error": str(error), "preview": fresh}
            return 200, {"job_id": job_id, "discarded": True}
        if action == "list":
            destination = storage.download_destination(connection)
            _require_origins(connection)
            resolved = acquisition.list_files(connection, payload.get("repository"),
                                              payload.get("revision"),
                                              api_base=acquisition.API_BASE)
            return 200, {"repository": resolved.repository, "revision": resolved.revision,
                         "files": [_file(item) for item in resolved.files],
                         "total_size": resolved.total_size, "destination": destination}
        if action in ("resolve", "acquire"):
            destination = storage.download_destination(connection)
            expected_destination = None
            if action == "acquire":
                expected_destination = payload.get("destination")
                if not isinstance(expected_destination, str) or expected_destination != destination:
                    raise RefusedError("Model storage changed since Review; resolve the request again.")
            origin = _require_origins(connection, payload.get("repository"))
            revision = payload.get("revision")
            selected = payload.get("files")
            if action == "acquire" and (not isinstance(revision, str) or not _COMMIT.fullmatch(revision)):
                raise RefusedError("Acquire needs the full immutable commit shown in Review.")
            resolved = acquisition.resolve(connection, payload.get("repository"), revision,
                                           selected, api_base=acquisition.API_BASE)
            if action == "resolve":
                return 200, {"repository": resolved.repository, "origin": origin["name"],
                             "revision": resolved.revision,
                             "files": [_file(item) for item in resolved.files],
                             "total_size": resolved.total_size, "destination": destination}
            consent = payload.get("consent") is True
            if any(item.sha256 is None for item in resolved.files) and not consent:
                raise RefusedError(
                    "Consent to locally hashed; no published checksum is required before Acquire.")
            result = download.submit(running, resolved, consent=consent,
                                     api_base=acquisition.API_BASE,
                                     expected_destination=expected_destination)
            if result.start.decision != admission.ADMIT or result.start.work is None:
                reason = result.start.reason or "The download was not started."
                raise RefusedError(reason)
            return 200, {"job_id": result.start.work.work_id,
                         "text": "Download started.", "warning": result.warning}
        raise RefusedError("Choose list, resolve or acquire for the download wizard.")
    except (acquisition.ResolutionError, adoption.AdoptionError, download.DownloadError,
            store.StoreError, verification.VerificationError) as error:
        raise RefusedError(str(error)) from None
    finally:
        opened.close()


def _prepare(connection, running):
    """Name each prerequisite and current shared work without guessing defaults."""
    blockers = []
    path = storage.model_storage(connection)
    if path is None:
        blockers.append({"text": "Model storage is not saved.",
                         "href": "#/settings/storage-artifacts", "label": "Settings › Storage & artifacts"})
    else:
        available, reason = storage.availability(path)
        if not available:
            blockers.append({"text": f"Saved model storage {path} {reason}.",
                             "href": "#/settings/storage-artifacts", "label": "Settings › Storage & artifacts"})
    configured = origins.list_all(connection)
    if not configured:
        blockers.append({"text": "No download origin is configured.",
                         "href": "#/settings/origins", "label": "Settings › Download origins"})
    works = running.works()
    work = next((item for item in works if not item.outcome), None)
    current = None if work is None else {
        "text": f"The shared slot is held by {work.kind} for {work.purpose}; finish or cancel it. Nothing is queued.",
        "job_id": work.work_id, "kind": work.kind, "state": work.state}
    locations = [dict(row) for row in connection.execute(
        "SELECT location_id, path FROM artifact_locations ORDER BY path")]
    return {"ready": not blockers and current is None, "blockers": blockers,
            "origins": [{"kind": item["kind"], "name": item["name"]} for item in configured],
            "work": current, "downloads": _downloads(connection, running),
            "records": _records(connection), "artifact_locations": locations}


def _record_id(payload):
    value = payload.get("acquisition_id")
    if not isinstance(value, str) or not value:
        raise RefusedError("Choose an acquisition record.")
    return value


def _records(connection):
    records = []
    for row in connection.execute(
            "SELECT r.*, l.path AS location, v.reason AS verification_reason FROM acquisition_records r"
            " JOIN artifact_locations l USING (location_id)"
            " LEFT JOIN acquisition_verification_failures v USING (acquisition_id)"
            " ORDER BY r.name, r.acquisition_id"):
        files = []
        for file in connection.execute(
                "SELECT file_name, size_bytes, published_sha256, computed_sha256, verified_at"
                " FROM acquisition_files WHERE acquisition_id = ? ORDER BY file_name",
                (row["acquisition_id"],)):
            file = dict(file)
            if not row["verified_at"]:
                if not row["verification_reason"]:
                    file["reason"] = f"{file['file_name']} has not passed full-set verification."
            elif not file["verified_at"] or len(file["computed_sha256"]) != 64:
                file["reason"] = f"{file['file_name']} has no recorded SHA-256 verification."
            else:
                try:
                    root = Path(row["location"]).resolve(strict=True)
                    path = root / file["file_name"]
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(root) or resolved != path or not path.is_file():
                        file["reason"] = f"{file['file_name']} is missing or is not a regular file."
                    elif resolved.stat().st_size != file["size_bytes"]:
                        file["reason"] = f"{file['file_name']} has changed size."
                except (OSError, RuntimeError, ValueError):
                    file["reason"] = f"{file['file_name']} is missing or unreadable."
            files.append(file)
        reason = row["verification_reason"] or next(
            (file.get("reason") for file in files if file.get("reason")), "")
        verified_at = row["verified_at"]
        if verified_at is None and files:
            previous = {file["verified_at"] for file in files}
            if len(previous) == 1 and None not in previous:
                verified_at = previous.pop()
        complete = bool(files) and row["verified_at"] is not None and not reason
        label = ("locally hashed; publisher unverified" if row["source"] == "adoption" else
                 "locally hashed; no published checksum" if row["checksum_status"] == "locally-hashed" else
                 "published checksums matched")
        # shortcut: infer only from linked profiles and recognizable artifact names;
        # ambiguous formats ask the operator to choose a profile destination.
        profile_kinds = {item["kind"] for item in connection.execute(
            "SELECT DISTINCT p.kind FROM profile_artifacts a JOIN model_profiles p USING (profile_id)"
            " WHERE a.acquisition_id = ?", (row["acquisition_id"],))}
        names = [Path(file["file_name"]).name.casefold() for file in files]
        model_names = [name for name in names if name.endswith(".gguf")
                       and not re.search(r"(?:mmproj|mtp)", name)]
        kind_names = model_names or names
        speech_named = any(re.search(r"(?:^|[-_.])(?:breeze|whisper|speech|voice|tts)(?:[-_.]|$)", name)
                           for name in kind_names)
        language_named = any(re.search(r"(?:^|[-_.])(?:llama|qwen|mistral|gemma|deepseek|phi)(?:[-_.]|$)", name)
                             or re.search(r"-[0-9]+-of-[0-9]+\.gguf$", name)
                             for name in kind_names)
        if profile_kinds:
            if profile_kinds == {"language"}:
                profile_kind = "language"
            elif profile_kinds <= {"synthesis", "recognition"}:
                profile_kind = "speech"
            else:
                profile_kind = "unknown"
        elif speech_named != language_named:
            profile_kind = "speech" if speech_named else "language"
        else:
            profile_kind = "unknown"
        records.append({"acquisition_id": row["acquisition_id"], "name": row["name"],
                        "source": row["repository"] or "unknown",
                        "revision": row["revision"] or "unknown", "label": label,
                        "verified_at": verified_at, "complete": complete,
                        "reason": reason or ("the record contains no files" if not files else ""),
                        "location": row["location"], "profile_kind": profile_kind, "files": files})
    return records


def _artifact_files(connection, location_id):
    if not isinstance(location_id, str) or not location_id:
        raise RefusedError("Choose a configured artifact location.")
    row = connection.execute("SELECT path FROM artifact_locations WHERE location_id = ?",
                             (location_id,)).fetchone()
    if row is None:
        raise RefusedError("Choose a configured artifact location.")
    root = Path(row["path"])
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise OSError
    except OSError:
        raise RefusedError("The configured artifact location is unavailable.") from None
    files = []
    for directory, subdirs, names in os.walk(root, followlinks=False):
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()
                      and name != ".matuto-downloads"]
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            try:
                adoption._refuse_staging(connection, root, Path(relative))
                files.append({"name": relative, "size": path.stat().st_size})
            except adoption.AdoptionError:
                continue
    return {"files": files}


def _downloads(connection, running):
    """Expose download progress and only offer recovery for safe retained partials."""
    works = {work.work_id: work for work in running.works() if work.kind == admission.DOWNLOAD}
    entries = []
    for work_id, work in works.items():
        progress = running.progress(work)
        row = connection.execute(
            "SELECT phase FROM jobs WHERE job_id = ? AND kind = ?", (work_id, admission.DOWNLOAD)
        ).fetchone()
        try:
            phase = json.loads(row["phase"] or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            phase = {}
        known = bool(progress and not progress.unknown and not work.outcome)
        phase_name = progress.phase if known else ""
        if not isinstance(phase, dict) or phase_name != phase.get("phase"):
            phase = {}
        partials = None
        if work.outcome in ("failed", "cancelled", "interrupted"):
            try:
                preview = download.preview_discard(running, work_id)
                partials = _discard_data(preview) if preview.files else None
            except download.DownloadError:
                partials = None
        try:
            pinned = json.loads(work.purpose)
            purpose = f"{pinned['repository']}@{pinned['revision']}"
        except (KeyError, TypeError, json.JSONDecodeError):
            purpose = "model files"
        entries.append({
            "id": work_id, "state": work.outcome or work.state, "outcome": work.outcome,
            "started_at": work.started_at, "purpose": purpose,
            "progress": progress.__dict__ if progress else None,
            "phase": phase_name, "file": phase.get("file", ""),
            "transferred": phase.get("transferred"), "total": phase.get("total"),
            "reason": progress.log.splitlines()[-1] if progress and work.outcome and progress.log else "",
            "partials": partials,
        })
    return entries


def _discard_data(preview):
    return {"job_id": preview.job_id,
            "files": [{"name": name, "size": size} for name, size in preview.files],
            "total_size": preview.total_size, "consequence": preview.consequence}


def _discard_input(value, job_id):
    if not isinstance(value, dict) or value.get("job_id") != job_id:
        raise RefusedError("Discard confirmation must include its preview.")
    files = value.get("files")
    total = value.get("total_size")
    if (not isinstance(files, list) or type(total) is not int or total < 0
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   or type(item.get("size")) is not int or item["size"] < 0 for item in files)):
        raise RefusedError("Discard confirmation must include its preview.")
    return download.DiscardPreview(job_id, tuple((item["name"], item["size"]) for item in files), total)


def _require_origins(connection, repository=None):
    configured = origins.list_all(connection)
    if not configured:
        raise RefusedError("No download origin is configured; add one in Settings › Download origins.")
    if repository is None or not isinstance(repository, str):
        if repository is None:
            return configured
        raise RefusedError("Choose a valid Hugging Face repository.")
    for origin in configured:
        admitted = (origin["kind"] == "repository" and
                    origin["name"].casefold() == repository.casefold()) or (
                    origin["kind"] == "organisation" and
                    repository.partition("/")[0].casefold() == origin["name"].casefold())
        if admitted:
            return origin
    raise RefusedError(f"No configured download origin admits {repository!r}.")


def _file(item):
    return {"name": item.name, "size": item.size, "sha256": item.sha256,
            "checksum_status": item.checksum_status}


def _payload(request):
    try:
        value = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise RefusedError("Expected a JSON request for the download wizard.") from None
    if not isinstance(value, dict):
        raise RefusedError("Expected a JSON request for the download wizard.")
    return value
