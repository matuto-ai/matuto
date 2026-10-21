"""Model storage and artifact locations: one operator-saved root, and no other.

The package ships, infers and falls back to no root. Only the operator saves
model storage, from Settings › Storage & artifacts; until then the download
destination and native discovery are refused naming the missing setting, and a
saved root that is unavailable now is named with its problem rather than
replaced. That one root is also its linked artifact location, so the two can
never name different directories; every artifact location is offered for
adoption, model storage's linked location among them, and only the root is a
download destination or a discovery directory. Changing a saved location
belongs to location relocation.
"""

import contextlib
import datetime
import os
import sqlite3
import stat
import unicodedata
from pathlib import Path

from matuto import admission, store, verification

SETTINGS = "Settings › Storage & artifacts"
# Weights another account could replace with its own bytes are not Matuto's, so
# a root writable by group or others is reported unavailable instead of used.
WRITABLE_BY_ANOTHER = 0o022


def save_model_storage(connection, path):
    """Save the first model storage directly, and report the whole outcome.

    A well-formed path is saved whether or not it is usable now, and the result
    says which, so the caller can report that partial outcome honestly instead
    of pretending nothing was saved. Changing an already saved model storage is
    Location relocation's work, so a second save is refused rather than applied.
    """
    text = _well_formed(path, "model storage")
    saved = model_storage(connection)
    if saved is not None:
        raise store.StoreError(
            f"model storage is already saved at {saved}; changing a saved "
            f"location is not offered here: {SETTINGS}")
    location_id = _save(connection, text, link=True)
    available, reason = availability(text)
    router = store.regenerate_router_files(_store(connection))
    return {"path": text, "location_id": location_id,
            "available": available, "reason": reason, "router": router}


def add_artifact_location(connection, path):
    """Save one additional artifact location, offered for adoption.

    Model storage's linked location is one of these too: adoption takes files in
    any configured location, so the root is never excluded from the list.
    """
    text = _well_formed(path, "artifact location")
    existing = _location_id(connection, text)
    if existing is not None:
        raise store.StoreError(
            f"an artifact location at {text} is already configured: {SETTINGS}")
    location_id = _save(connection, text, link=False)
    available, reason = availability(text)
    return {"path": text, "location_id": location_id,
            "available": available, "reason": reason}


def _well_formed(path, what):
    """The path as the operator typed it, or a refusal naming what is wrong."""
    try:
        text = os.fspath(path)
    except TypeError:
        raise store.StoreError(f"a {what} must be one path") from None
    if not isinstance(text, str) or not text.startswith("/"):
        raise store.StoreError(f"a {what} must be one absolute path: {path!r}")
    # Control and format characters, in every Unicode block: C1 controls and the
    # format characters (a bidi override, a zero-width joiner) are how one path
    # is made to read as another. Private-use and unassigned characters are not
    # controls, and a well-formed path holding one is saved.
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in text):
        raise store.StoreError(
            f"a {what} must be one path holding no control character: {path!r}")
    # Split, not normalised: normalising would resolve the parent away and let a
    # path the operator never typed be saved as the directory it names.
    if os.pardir in text.split("/"):
        raise store.StoreError(
            f"a {what} must be one path holding no parent (..) component: {path!r}")
    return text


def _save(connection, path, *, link):
    """The location row, and the one model-storage row when it is the root."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        # The one root is the same directory as its linked artifact location, so
        # a path already known is linked rather than stored twice: the path is
        # the store's own key on artifact_locations.
        location_id = _location_id(connection, path)
        if location_id is None:
            location_id = _new_id(connection, "loc")
            connection.execute(
                "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
                " VALUES (?, ?, '', ?)", (location_id, path, _now()))
        if link:
            connection.execute(
                "INSERT INTO model_storage (setting_id, location_id, saved_at)"
                " VALUES (1, ?, ?)", (location_id, _now()))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return location_id


def _store(connection):
    database_path = connection.execute("PRAGMA database_list").fetchone()["file"]
    return store.Store(Path(database_path), connection)


def _published_profile_qualification(connection, profiles):
    opened = _store(connection)
    result = []
    for profile in profiles:
        profile = dict(profile)
        if profile["kind"] == "language" and profile["published"]:
            profile["qualification"] = store.read_record(
                opened, "language", profile["profile_id"])["qualification"]
        result.append(profile)
    return result


def _new_id(connection, prefix):
    """An id no stored location holds, so two saves never collide."""
    existing = {row[0] for row in connection.execute(
        "SELECT location_id FROM artifact_locations")}
    index = 1
    while f"{prefix}-{index}" in existing:
        index += 1
    return f"{prefix}-{index}"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _location_id(connection, path):
    row = connection.execute(
        "SELECT location_id FROM artifact_locations WHERE path = ?", (path,)).fetchone()
    return None if row is None else row["location_id"]


def model_storage(connection):
    """The one saved model-storage path, or None when the operator saved none."""
    row = connection.execute(
        "SELECT l.path AS path FROM model_storage AS s"
        " JOIN artifact_locations AS l ON l.location_id = s.location_id"
        " WHERE s.setting_id = 1").fetchone()
    return None if row is None else row["path"]


def download_destination(connection):
    """The one saved model storage, which is where downloads go.

    Raises store.StoreError naming the missing setting or the unusable path, so
    nothing downstream substitutes another directory.
    """
    return _model_root(connection)


def discovery_directory(connection):
    """The same directory, and the only one native discovery is offered."""
    return _model_root(connection)


def _model_root(connection):
    path = model_storage(connection)
    if path is None:
        raise store.StoreError(
            "no model storage is saved, so there is no directory to use; "
            f"save one in {SETTINGS}")
    available, reason = availability(path)
    if not available:
        raise store.StoreError(
            f"the saved model storage {path} {reason}; fix it in {SETTINGS}")
    return path


def artifact_locations(connection):
    """Every configured artifact location, model storage's linked one among them.

    Adoption takes files in any of them, so the list is never narrowed to
    the additional ones: only the download destination and native discovery are
    the root alone.
    """
    return [row["path"] for row in connection.execute(
        "SELECT path FROM artifact_locations ORDER BY path")]


def preview_location_change(connection, location_id, path):
    """Describe the records and served configurations a path change affects."""
    text = _well_formed(path, "artifact location")
    location = connection.execute(
        "SELECT l.location_id, l.path, s.location_id AS model_storage_id"
        " FROM artifact_locations AS l LEFT JOIN model_storage AS s"
        " ON s.setting_id = 1 WHERE l.location_id = ?", (location_id,)).fetchone()
    if location is None:
        raise store.StoreError(f"no artifact location {location_id!r} is configured")
    records = [dict(row) for row in connection.execute(
        "SELECT r.acquisition_id, r.name, r.source, r.verified_at"
        " FROM acquisition_records AS r"
        " WHERE r.location_id = ? ORDER BY r.name, r.acquisition_id", (location_id,))]
    record_ids = [row["acquisition_id"] for row in records]
    placeholders = ",".join("?" for _ in record_ids)
    profiles = []
    bundles = []
    if record_ids:
        profiles = [dict(row) for row in connection.execute(
            "SELECT DISTINCT p.profile_id, p.kind, p.name, p.published"
            " FROM model_profiles AS p JOIN profile_artifacts AS a"
            " ON a.profile_id = p.profile_id WHERE a.acquisition_id IN (" + placeholders + ")"
            " ORDER BY p.name, p.profile_id", record_ids)]
        profiles = _published_profile_qualification(connection, profiles)
        profile_ids = [row["profile_id"] for row in profiles]
        if profile_ids:
            profile_marks = ",".join("?" for _ in profile_ids)
            bundles = [dict(row) for row in connection.execute(
                "SELECT DISTINCT b.bundle_id, b.name FROM bundles AS b"
                " JOIN bundle_members AS m ON m.bundle_id = b.bundle_id"
                " WHERE m.profile_id IN (" + profile_marks + ") ORDER BY b.name, b.bundle_id",
                profile_ids)]
    is_model_storage = location["model_storage_id"] == location_id
    change = {"from": location["path"], "to": text}
    return {"location_id": location_id, "path": location["path"], "new_path": text,
            "model_storage": is_model_storage,
            "changes": {"model_storage": change if is_model_storage else None,
                        "artifact_location": change},
            "records": records, "profiles": profiles, "bundles": bundles}


def change_location(connection, location_id, path, *, serving_state):
    """Change only the stored path, then reverify every record under one writer lock.

    The lock is shared with Runner.start's admission transaction: an admitted
    run either becomes visible to this check or starts after the new path commits.
    """
    text = _well_formed(path, "artifact location")
    connection.execute("BEGIN IMMEDIATE")
    try:
        preview = preview_location_change(connection, location_id, text)
        reason = location_busy_reason(connection, preview, serving_state)
        if reason:
            raise store.StoreError(reason)
        if text != preview["path"]:
            try:
                connection.execute("UPDATE artifact_locations SET path = ? WHERE location_id = ?",
                                   (text, location_id))
            except sqlite3.IntegrityError:
                raise store.StoreError(f"an artifact location at {text} is already configured") from None
        outcomes = []
        path_available, path_reason = availability(text)
        for record in preview["records"]:
            checked = verification.verify_record(connection, record["acquisition_id"])
            if path_available:
                available, reason = checked.complete, checked.reason
            else:
                connection.execute(
                    "UPDATE acquisition_records SET verified_at = NULL WHERE acquisition_id = ?",
                    (record["acquisition_id"],))
                available = False
                reason = (f"{checked.reason} ({text} {path_reason})"
                          if checked.reason else f"{text} {path_reason}")
            outcomes.append({**record, "verified_at": checked.verified_at if available else None,
                             "available": available, "reason": reason,
                             "files": [{"file_name": item.file_name, "state": item.state,
                                        "reason": item.reason} for item in checked.files]})
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    opened = _store(connection)
    router = store.regenerate_router_files(opened)
    profiles = _published_profile_qualification(connection, preview["profiles"])
    reason = (next((item["reason"] for item in outcomes if not item["available"]), None)
              or (f"{text} {path_reason}" if not path_available else None))
    return {**preview, "profiles": profiles, "path": text, "records": outcomes,
            "available": path_available and all(item["available"] for item in outcomes),
            "reason": reason, "router": router}


def location_busy_reason(connection, preview, serving_state):
    """Name the exact serving or work action that prevents this edit."""
    serving_state = getattr(serving_state, "state", serving_state)
    if serving_state not in (admission.STOPPED, admission.AGENTIC, admission.VOICE):
        return f"the serving state is unknown; cannot safely change {preview['path']}"
    ids = {item["acquisition_id"] for item in preview["records"]}
    profile_ids = {item["profile_id"] for item in preview["profiles"]}
    bundle_ids = {item["bundle_id"] for item in preview["bundles"]}
    if serving_state == admission.AGENTIC:
        root = connection.execute(
            "SELECT location_id FROM model_storage WHERE setting_id = 1").fetchone()
        if root and root["location_id"] == preview["location_id"]:
            return f"Stop serving before changing model storage at {preview['path']}"
        if any(item["published"] for item in preview["profiles"]):
            return f"Stop serving before changing {preview['path']}; a published profile uses it"
    if serving_state == admission.VOICE:
        running = connection.execute("SELECT bundle_id FROM voice_serving WHERE voice_serving_id = 1").fetchone()
        if running and running["bundle_id"] in bundle_ids:
            return (f"Stop serving before changing {preview['path']}; voice bundle "
                    f"{running['bundle_id']} uses it")
    if preview["model_storage"]:
        download = connection.execute(
            "SELECT job_id, pinned_request FROM jobs"
            " WHERE kind = ? AND outcome IS NULL ORDER BY started_at LIMIT 1",
            (admission.DOWNLOAD,)).fetchone()
        if download:
            return (f"Stop or finish the download job {download['job_id']} in Runs & jobs "
                    f"before changing model storage at {preview['path']}")
    refs = ids | profile_ids | bundle_ids
    if refs:
        marks = ",".join("?" for _ in refs)
        work = connection.execute(
            "SELECT run_id, procedure FROM runs WHERE outcome IS NULL AND record_id IN (" + marks + ")"
            " ORDER BY started_at LIMIT 1", sorted(refs)).fetchone()
        if work:
            return (f"Stop or finish the {work['procedure']} run {work['run_id']} in Runs & jobs "
                    f"before changing {preview['path']}")
    return ""


def availability(path):
    """(available, reason) for one configured path, read now and writing nothing.

    A missing, unreadable, non-directory or another-account-writable root is
    named with its problem; nothing here creates or replaces a directory.
    """
    try:
        status = os.stat(path)
    except FileNotFoundError:
        return False, "does not exist"
    except OSError:
        return False, "cannot be read"
    if not stat.S_ISDIR(status.st_mode):
        return False, "is not a directory"
    if status.st_mode & WRITABLE_BY_ANOTHER:
        return False, "is writable by another account"
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return False, "cannot be read, written and entered by this account"
    return True, None