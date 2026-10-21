"""The one pre-update snapshot of the Matuto store.

Before an update changes anything, the store is copied with SQLite's own backup
operation, which reads a consistent view even while a write lands, never by
copying the database file. The copy counts only after it is opened on its own,
passes SQLite's integrity check and yields its schema version; a copy that
cannot be saved or verified is removed and named, so the update that asked for
it refuses while the store and the previous snapshot stay as they were.

Exactly one snapshot is kept, beside the store in the directory the store keeps
private. It is written under a fixed name, so putting the verified copy in place
is a single atomic rename: an interruption at any point leaves the previous
snapshot or the new verified one, never an unverified one, and the previous one
is never lost before its replacement verified. The copy carries the label it was
taken under inside its own store_meta, and the same label — release version,
taken at, schema version, size — is recorded in the store's update-state block.
Asking again for the same release version returns the snapshot already held,
so a retried unfinished update keeps its original; a snapshot whose own label
does not match the recorded one, which an interruption between the rename and
the record can leave, is retaken rather than handed out. A hard interruption can
leave a partial copy under a temporary name, and the next take sweeps it.

There is no restore: this module reads the store and writes the copy, and offers
no way to put a snapshot back over the store.
"""

import contextlib
import datetime
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from matuto import store as store_module

# Beside the store, under one fixed name: one name is what makes the replacement
# a single atomic rename rather than a second file that could survive a crash.
SNAPSHOT_FILENAME = "pre-update-snapshot.sqlite3"
SNAPSHOT_WHAT = "Pre-update snapshot"
# The copy carries its own label in store_meta, so it can be tied to the
# recorded update instead of being trusted by its fixed name alone.
LABEL_RELEASE_KEY = "snapshot_release_version"
LABEL_TAKEN_KEY = "snapshot_taken_at"
TEMPORARY_PREFIX = ".pre-update-snapshot-"


class SnapshotError(Exception):
    """One refusal or failure, naming the snapshot or the reason it failed."""


@dataclass(frozen=True)
class Snapshot:
    """One verified pre-update snapshot and its label."""

    path: Path
    taken_at: str
    release_version: str
    schema_version: int
    size_bytes: int


def snapshot_path(store_path):
    """The fixed name the one pre-update snapshot is kept under, beside the store."""
    return Path(store_path).parent / SNAPSHOT_FILENAME


def take(opened, release_version):
    """The verified pre-update snapshot for this release version, taken or held.

    `opened` is an open `matuto.store.Store`, and the copy is made from its
    connection with SQLite's backup API, never by copying the file. The
    connection must not hold a transaction: a store mid-way through a write is
    not consistently copyable and SQLite's backup cannot progress against the
    caller's own write lock, so an open transaction is refused. A snapshot
    already recorded, or a verified copy carrying this update's own label, is
    returned untouched, which is what a retried unfinished update gets.
    Otherwise the store is copied, verified, atomically put in the snapshot's
    place and only then recorded in the update-state block. A failed save or
    verification raises SnapshotError with the store and the previous snapshot
    unchanged.
    """
    if not isinstance(release_version, str) or not release_version:
        raise SnapshotError("a snapshot needs the Matuto release version it is taken for")
    # A copy of a store another write is mid-way through cannot be consistent, and
    # SQLite's backup cannot make progress against the caller's own write lock, so
    # the transaction has to be finished or rolled back before the snapshot runs.
    if opened.connection().in_transaction:
        raise SnapshotError(
            "cannot take the pre-update snapshot while the store connection holds a "
            "transaction; finish or roll it back first")
    destination = snapshot_path(opened.path)
    _private_directory(destination.parent)
    if os.path.lexists(destination):
        _safe_snapshot(destination)
    _safe_sidecars(destination)
    # A partial copy from a hard interruption is not the snapshot; one updater
    # takes snapshots, so an earlier one's leftover is swept before this copy.
    _discard_leftovers(destination.parent)
    held = _held(opened, destination, release_version)
    if held is not None:
        return held
    return _take(opened, destination, release_version)


def _discard_leftovers(directory):
    """Remove partial copies an interrupted take left under a temporary name."""
    for path in directory.glob(f"{TEMPORARY_PREFIX}*"):
        with contextlib.suppress(OSError):
            path.unlink()


def _held(opened, destination, release_version):
    """The verified copy this update already holds, if one is there.

    The copy's own label must name this update, so a copy another update left in
    the fixed name is never handed out as this update's. When the record did not
    reach a verified copy that already names this update \u2014 an interruption
    between the rename and the record \u2014 its own label is recorded rather than a
    new copy taken, so the retried update keeps the original untouched.
    """
    if not destination.exists():
        return None
    schema_version, stamped_version, stamped_at = _inspect(destination)
    if stamped_version != release_version or not stamped_at:
        return None
    row = opened.connection().execute(
        "SELECT release_version, snapshot_path, snapshot_taken_at FROM update_state"
        " WHERE state_id = 1").fetchone()
    if (row is None or row["release_version"] != release_version
            or row["snapshot_path"] != str(destination)
            or row["snapshot_taken_at"] != stamped_at):
        _record(opened, destination, release_version, stamped_at)
    return _snapshot(destination, stamped_at, release_version, schema_version)


def _take(opened, destination, release_version):
    """Copy, verify and atomically install the snapshot, then record its label."""
    directory = destination.parent
    taken_at = _now()
    try:
        temporary = _temporary(directory)
    except OSError:
        raise SnapshotError(f"cannot create the pre-update snapshot: {destination}") from None
    try:
        _copy_store(opened, temporary, release_version, taken_at)
        schema_version = _inspect(temporary)[0]
        _sync(temporary)
        try:
            os.replace(temporary, destination)
        except OSError:
            raise SnapshotError(
                f"cannot replace the pre-update snapshot: {destination}") from None
    except BaseException:
        # The failed copy never becomes the snapshot, and no partial file stays.
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
    _record(opened, destination, release_version, taken_at)
    return _snapshot(destination, taken_at, release_version, schema_version)


def _temporary(directory):
    """A private, partially written snapshot in the store's own directory."""
    descriptor, name = tempfile.mkstemp(prefix=TEMPORARY_PREFIX, dir=directory)
    os.close(descriptor)
    return Path(name)


def _copy_store(opened, temporary, release_version, taken_at):
    """Copy the open store into `temporary` and stamp the copy with its label."""
    try:
        with contextlib.closing(store_module._connect(temporary)) as target:
            opened.connection().backup(target)
            target.executemany(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                ((LABEL_RELEASE_KEY, release_version), (LABEL_TAKEN_KEY, taken_at)))
    except sqlite3.Error:
        raise SnapshotError(f"cannot save the pre-update snapshot: {temporary}") from None


def _inspect(path):
    """A verified copy's schema version and the label it carries, if any."""
    _safe_sidecars(path)
    try:
        connection = store_module._readonly_connect(path)
    except sqlite3.Error:
        raise SnapshotError(f"the pre-update snapshot cannot be opened: {path}") from None
    try:
        check = connection.execute("PRAGMA integrity_check").fetchall()
        if len(check) != 1 or check[0][0] != "ok":
            raise SnapshotError(f"the pre-update snapshot is corrupt: {path}")
        stored = connection.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?, 'schema_version')",
            (LABEL_RELEASE_KEY, LABEL_TAKEN_KEY)).fetchall()
    except sqlite3.DatabaseError:
        raise SnapshotError(f"the pre-update snapshot is not a Matuto store: {path}") from None
    finally:
        connection.close()
    label = {row["key"]: row["value"] for row in stored}
    try:
        return int(label["schema_version"]), label.get(LABEL_RELEASE_KEY), label.get(LABEL_TAKEN_KEY)
    except (KeyError, TypeError, ValueError):
        raise SnapshotError(
            f"the pre-update snapshot records no readable schema version: {path}") from None


def _sync(path):
    """Put the verified copy on disk before the rename that publishes it."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise SnapshotError(f"cannot save the pre-update snapshot: {path}") from None


def _snapshot(path, taken_at, release_version, schema_version):
    try:
        size = path.stat().st_size
    except OSError:
        raise SnapshotError(f"cannot read the pre-update snapshot: {path}") from None
    return Snapshot(path=path, taken_at=taken_at, release_version=release_version,
                    schema_version=schema_version, size_bytes=size)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _record(opened, destination, release_version, taken_at):
    """Record the label in the one update-state row, preserving its phase."""
    connection = opened.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "INSERT INTO update_state"
            " (state_id, release_version, phase, snapshot_path, snapshot_taken_at, updated_at)"
            " VALUES (1, ?, '', ?, ?, ?)"
            " ON CONFLICT(state_id) DO UPDATE SET"
            " release_version = excluded.release_version,"
            " snapshot_path = excluded.snapshot_path,"
            " snapshot_taken_at = excluded.snapshot_taken_at,"
            " updated_at = excluded.updated_at",
            (release_version, str(destination), taken_at, taken_at))
        connection.execute("COMMIT")
    except sqlite3.Error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise SnapshotError(f"cannot record the pre-update snapshot: {destination}") from None
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def _private_directory(directory):
    """The store's own directory rule, with this module's refusal type."""
    try:
        store_module._private_directory(directory)
    except store_module.StoreError as refusal:
        raise SnapshotError(str(refusal)) from None


def _safe_snapshot(path):
    """The store file's own rule for the snapshot, with this module's refusal type."""
    try:
        store_module._safe_file(path, SNAPSHOT_WHAT)
    except store_module.StoreError as refusal:
        raise SnapshotError(str(refusal)) from None


def _safe_sidecars(path):
    """The store's own rule for a journal, WAL or shared-memory name beside a copy."""
    try:
        store_module._safe_sidecars(path)
    except store_module.StoreError as refusal:
        raise SnapshotError(str(refusal)) from None
