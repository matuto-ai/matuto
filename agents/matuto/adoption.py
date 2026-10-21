"""Adopt complete model files from configured artifact locations in place."""

import contextlib
import datetime
import json
from dataclasses import replace
import os
import re
import sqlite3
import stat
from pathlib import Path

from matuto import storage, verification

_SHARD = re.compile(r"^(?P<stem>.+)-(?P<index>[0-9]+)-of-(?P<count>[0-9]+)\.gguf$")
LABEL = "locally hashed; publisher unverified"


class AdoptionError(Exception):
    """A refused local adoption, with a cause the operator can fix."""


def adopt(connection, acquisition_id, name, path, *, consent=False, now=None):
    """Record and hash a file from a configured artifact location without moving it.

    The selected path is the operator's explicit local input. Split GGUF models
    expand to every shard before the record is created. Consent is required
    before any store mutation because local files have no publisher checksum.
    """
    if consent is not True:
        raise AdoptionError('explicit consent to "locally hashed; publisher unverified" is required')
    if not isinstance(acquisition_id, str) or not acquisition_id.strip():
        raise AdoptionError("an acquisition record ID is required")
    if not isinstance(name, str) or not name.strip():
        raise AdoptionError("an acquisition record name is required")
    try:
        selected = Path(os.fspath(path))
    except (TypeError, ValueError):
        raise AdoptionError("choose a file inside a configured artifact location") from None
    if not selected.is_absolute() or ".." in selected.parts:
        raise AdoptionError("choose an absolute file path without parent traversal")

    location_id, configured, root, relative = _configured_file(connection, selected)
    files = _required_files(root, relative)
    if connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = ?", (acquisition_id,)
    ).fetchone():
        raise AdoptionError(f"acquisition record {acquisition_id!r} already exists")

    descriptors = {}
    try:
        for file_name in files:
            descriptors[file_name] = _safe_file(connection, root, file_name)
        moment = _stamp(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
                " origin_id, repository, revision, location_id, verified_at, notes, created_at)"
                " VALUES (?, ?, 'adoption', 'locally-hashed', NULL, NULL, NULL, ?, NULL, '', ?)",
                (acquisition_id, name.strip(), location_id, moment))
            connection.executemany(
                "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes,"
                " published_sha256, computed_sha256, verified_at)"
                " VALUES (?, ?, ?, NULL, '', NULL)",
                [(acquisition_id, file_name, os.fstat(descriptors[file_name]).st_size)
                 for file_name in files])
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

        try:
            result = verification.verify_record(
                connection, acquisition_id, consent=True, now=now, expected_files=descriptors)
            if not result.complete:
                raise AdoptionError(result.reason)
            _check_configured_root(connection, location_id, configured, root)
            for file_name, original_fd in descriptors.items():
                current_fd = _safe_file(connection, root, file_name)
                try:
                    original = os.fstat(original_fd)
                    current = os.fstat(current_fd)
                    if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                        raise AdoptionError(f"{file_name} changed while it was being adopted")
                finally:
                    os.close(current_fd)
            _check_configured_root(connection, location_id, configured, root)
            return replace(result, label=LABEL)
        except BaseException:
            # Do not leave a failed adoption as a selectable record or partial identity.
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM acquisition_files WHERE acquisition_id = ?",
                                   (acquisition_id,))
                connection.execute("DELETE FROM acquisition_records WHERE acquisition_id = ?",
                                   (acquisition_id,))
                connection.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
            raise
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def _configured_file(connection, selected):
    """Match the selected file and its resolved target to one configured root."""
    try:
        actual = selected.resolve(strict=True)
        status = actual.stat()
    except OSError:
        raise AdoptionError(f"the selected file {selected} is missing or unreadable") from None
    if not stat.S_ISREG(status.st_mode):
        raise AdoptionError(f"the selected path {selected} is not a regular file")
    for row in connection.execute("SELECT location_id, path FROM artifact_locations"):
        configured = Path(row["path"])
        try:
            relative = selected.relative_to(configured)
            root = configured.resolve(strict=True)
            actual.relative_to(root)
        except (OSError, ValueError):
            continue
        if relative.parts:
            return row["location_id"], configured, root, relative.as_posix()
    raise AdoptionError(f"the selected file {selected} is outside every configured artifact location")


def _check_configured_root(connection, location_id, configured, root):
    """Refuse a configured path or symlink target changed during adoption."""
    row = connection.execute(
        "SELECT path FROM artifact_locations WHERE location_id = ?", (location_id,)).fetchone()
    if row is None or Path(row["path"]) != configured:
        raise AdoptionError("the configured artifact location changed while adopting the files")
    try:
        current = configured.resolve(strict=True)
    except OSError:
        raise AdoptionError("the configured artifact location is no longer available") from None
    if current != root:
        raise AdoptionError("the configured artifact location changed while adopting the files")


def _required_files(root, selected_name):
    """Expand a selected GGUF shard to its entire contiguous shard set."""
    match = _SHARD.fullmatch(Path(selected_name).name)
    if match is None:
        return [selected_name]
    try:
        count = int(match.group("count"))
        index = int(match.group("index"))
    except ValueError:
        raise AdoptionError(f"the selected shard {selected_name!r} has an invalid shard number") from None
    if count < 1 or not 1 <= index <= count:
        raise AdoptionError(f"the selected shard {selected_name!r} has an invalid shard number")
    parent = Path(selected_name).parent
    found = {}
    try:
        entries = (root / parent).iterdir()
        for entry in entries:
            sibling = _SHARD.fullmatch(entry.name)
            if sibling and (sibling.group("stem"), sibling.group("count")) == (
                    match.group("stem"), match.group("count")):
                try:
                    shard_index = int(sibling.group("index"))
                except ValueError:
                    raise AdoptionError(
                        f"the shard set for {selected_name!r} has an invalid shard number") from None
                if shard_index in found:
                    raise AdoptionError(f"the shard set for {selected_name!r} has duplicate shard numbers")
                found[shard_index] = (parent / entry.name).as_posix()
    except OSError:
        raise AdoptionError(f"the shard set for {selected_name!r} cannot be listed") from None
    if len(found) != count or min(found) != 1 or max(found) != count:
        raise AdoptionError(f"the shard set for {selected_name!r} is incomplete")
    return [found[number] for number in sorted(found)]


def _safe_file(connection, root, file_name):
    """Open one regular file beneath the configured root without following links."""
    relative = Path(file_name)
    if relative.is_absolute() or not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise AdoptionError(f"{file_name} is not a safe path inside its artifact location")
    if (not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0)
            or os.open not in os.supports_dir_fd):
        raise AdoptionError("safe file descriptor traversal is unavailable on this platform")
    _refuse_staging(connection, root, relative)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    file_fd = None
    try:
        directory_fd = _open_directory(root, directory_flags)
        try:
            for part in relative.parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
            status = os.fstat(file_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        if file_fd is not None:
            os.close(file_fd)
        raise AdoptionError(f"{file_name} is missing, linked, or cannot be opened safely") from None
    if not stat.S_ISREG(status.st_mode):
        os.close(file_fd)
        raise AdoptionError(f"{file_name} is not a regular file")
    return file_fd


def _open_directory(path, flags):
    """Walk an absolute directory path without following a replaced component."""
    if not path.is_absolute():
        raise OSError("artifact root is not absolute")
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _refuse_staging(connection, root, relative):
    """Never adopt from the active store's private work/download staging area."""
    database_path = next((row[2] for row in connection.execute("PRAGMA database_list")
                          if row[1] == "main"), "")
    if not database_path:
        raise AdoptionError("cannot identify Matuto's private download or work staging directory")
    private_work = (Path(database_path).parent / "work").resolve(strict=False)
    candidate = root / relative
    try:
        candidate.relative_to(private_work)
    except ValueError:
        pass
    else:
        raise AdoptionError("Matuto's private download or work staging files cannot be adopted")
    selected = candidate.resolve(strict=False)
    roots = {Path(row[0]).resolve(strict=False) / ".matuto-downloads"
             for row in connection.execute("SELECT path FROM artifact_locations")}
    model_root = storage.model_storage(connection)
    if model_root is not None:
        roots.add(Path(model_root).resolve(strict=False) / ".matuto-downloads")
    for row in connection.execute("SELECT pinned_request FROM jobs WHERE kind = 'download'"):
        try:
            destination = json.loads(row[0])["destination"]
            if isinstance(destination, str) and Path(destination).is_absolute():
                roots.add(Path(destination).resolve(strict=False) / ".matuto-downloads")
        except (ValueError, TypeError, KeyError):
            continue
    if any(selected == root or root in selected.parents for root in roots):
        raise AdoptionError("Matuto's private download or work staging files cannot be adopted")


def _stamp(now=None):
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
