"""Acquired files and their identity: hash the whole set, judge it, verify at launch.

One acquisition record is the unit of identity. Every required file keeps its
source-relative name, its size, the published checksum when the source published
one, the computed SHA-256 and the time it was verified. `verify_record` hashes
every shard and every sidecar and records the set only when every file is present
at its recorded size and no published checksum mismatches; a file the publisher
published no checksum for needs the operator's consent, and a mismatch is never
consented. `launch_check` is the launch-time subset: the first model file and the
sidecars a profile selected. Nothing here starts a download or writes a profile
pick; the download, adoption and relocation callers own their own records and
call these rules.

A verification that reaches a verdict always leaves the record telling the truth:
a set it could not complete loses the record's completeness, so the store's own
selectability refuses it, and a set it completes carries the new time and digests.
Only a verification that never reached a verdict writes nothing, and no expected
digest is ever replaced silently.
"""

import contextlib
import datetime
import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from matuto import store

# The record's own checksum_status values, and the label an operator reads for a
# file the publisher published no checksum for (ACQ-6).
PUBLISHED = "published-checksum"
LOCALLY_HASHED = "locally-hashed"
LABEL_PUBLISHED = "published checksum matched"
LABEL_NO_PUBLISHED = "locally hashed; no published checksum"

VERIFIED = "verified"
MISSING = "missing"
UNREADABLE = "unreadable"
WRONG_SIZE = "wrong-size"
MISMATCHED = "mismatched"
CHANGED = "changed"
NEEDS_CONSENT = "needs-consent"

# One read at a time, whatever the file's size: a 95 GB shard is hashed in 1 MiB
# blocks and never held whole.
BLOCK = 1_048_576


class VerificationError(Exception):
    """A refused verification action, with a message the operator can be shown."""


@dataclass(frozen=True)
class FileCheck:
    """One required file as a verification found it, and why it is not usable."""

    file_name: str
    state: str
    reason: str = ""
    computed_sha256: str = ""


@dataclass(frozen=True)
class Verification:
    """One acquisition record's whole set as a single verification judged it."""

    acquisition_id: str
    complete: bool
    reason: str = ""
    label: str = ""
    checksum_status: str = ""
    verified_at: str = ""
    files: tuple = ()


def verify_record(connection, acquisition_id, *, consent=False, now=None, expected_files=None,
                  on_progress=None):
    """Hash every required file of one acquisition record and judge the whole set.

    Returns a `Verification` and records each file's computed SHA-256, each
    file's verification time, the record's verification time and its checksum
    status only when every file is present at its recorded size and no published
    checksum mismatches. Every other answer leaves the record telling the truth
    instead: a set that cannot be completed loses the record's completeness, so
    the store refuses a later pick of it and `launch_check` refuses it, and every
    digest already recorded stays as it was. `consent` is the operator's explicit
    "locally hashed; no published checksum" for the files of this set that carry no
    published checksum; it applies to none of the others. `expected_files`, when
    supplied by a caller that safely opened local files, binds each hash to those
    open file descriptors so a replaced path cannot redirect the read. `on_progress`,
    when provided, receives the current file, bytes actually hashed, and recorded size.

    The record needs no profile and no download job: this is the only way an
    acquisition record becomes complete, and it works on every record in the store.
    """
    record, rows, snapshot = _read_identity(connection, acquisition_id)
    if record is None:
        raise VerificationError(f"no acquisition record {acquisition_id!r} in the store")
    consented = record["checksum_status"] == LOCALLY_HASHED and record["verified_at"] is not None
    result = _verify_files(acquisition_id, Path(record["root"]), rows, consent=consent,
                           consented=consented, now=now, expected_files=expected_files,
                           on_progress=on_progress)
    if not result.files:
        return result
    if not result.complete:
        _record_failure(connection, acquisition_id, snapshot, result.reason)
        return result
    if not _record_results(connection, acquisition_id, result.files, result.checksum_status,
                           result.verified_at, snapshot):
        return Verification(acquisition_id, False,
                            f"{acquisition_id} changed while it was being verified;"
                            " nothing was recorded", files=result.files)
    return result


def verify_files(acquisition_id, root, files, *, consent=False, now=None,
                 expected_files=None, on_progress=None):
    """Judge a pinned file set without storing it, for atomic download placement.

    `files` has the download pin's name, size and published SHA-256. The returned
    result can be committed with the acquisition record only after every file has
    been hashed, so readers never hold a store write lock while hashing.
    """
    rows = [{"file_name": item["name"], "size_bytes": item["size"],
             "published_sha256": item["sha256"], "computed_sha256": "", "verified_at": None}
            for item in sorted(files, key=lambda file: file["name"])]
    return _verify_files(acquisition_id, Path(root), rows, consent=consent, consented=False,
                         now=now, expected_files=expected_files, on_progress=on_progress)


def _verify_files(acquisition_id, root, rows, *, consent, consented, now,
                  expected_files, on_progress):
    if not rows:
        return Verification(acquisition_id, False, f"{acquisition_id} names no required file")
    files = tuple(_check_file(root, row, consent, consented,
                              (expected_files or {}).get(row["file_name"]), on_progress)
                  for row in rows)
    refused = next((check for check in files if check.state != VERIFIED), None)
    if refused is not None:
        return Verification(acquisition_id, False, refused.reason, files=files)
    unpublished = any(row["published_sha256"] is None for row in rows)
    status = LOCALLY_HASHED if unpublished else PUBLISHED
    return Verification(acquisition_id, True,
                        label=LABEL_NO_PUBLISHED if unpublished else LABEL_PUBLISHED,
                        checksum_status=status, verified_at=_stamp(now), files=files)


def accept_replacement(connection, acquisition_id, file_name, replacement_id, *, now=None):
    """Take replacement bytes for one file as a new artifact identity beside the old.

    The old record keeps its recorded digests and every profile still picks it,
    but it stops being selectable: its bytes no longer match what it recorded, and
    no expected digest is ever changed silently (ACQ-6). The new record copies the
    old one's source, revision, location and files, with the replacement file's
    freshly computed size and SHA-256, so it is a separate identity the operator
    has to verify and select. Bytes that contradict a published checksum are never
    accepted here: that is not a replacement, it is another file.
    """
    record = _record(connection, acquisition_id)
    if record is None:
        raise VerificationError(f"no acquisition record {acquisition_id!r} in the store")
    if _record(connection, replacement_id) is not None:
        raise VerificationError(f"{replacement_id!r} already names an acquisition record")
    rows = {row["file_name"]: row for row in connection.execute(
        "SELECT * FROM acquisition_files WHERE acquisition_id = ?", (acquisition_id,))}
    snapshot = (tuple(record), {name: (entry["size_bytes"], entry["computed_sha256"],
                                       entry["verified_at"])
                                for name, entry in rows.items()})
    row = rows.get(file_name)
    if row is None:
        raise VerificationError(
            f"acquisition record {acquisition_id!r} names no file {file_name!r}")
    path = Path(record["root"]) / file_name
    read = _read_digest(path)
    if read is None:
        raise VerificationError(f"{file_name} could not be read whole at {path}")
    digest, size = read
    published = row["published_sha256"]
    if published is not None and published != digest:
        raise VerificationError(
            f"{file_name} of {acquisition_id!r} contradicts the published checksum "
            f"{published}; those bytes are refused, not accepted as a replacement")
    if digest == row["computed_sha256"]:
        raise VerificationError(
            f"{file_name} still matches the SHA-256 recorded for it; there is nothing to replace")
    moment = _stamp(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if _snapshot(connection, acquisition_id) != snapshot:
            # Another Matuto verified or replaced this record while this one was
            # reading the bytes; the identity it would copy is no longer the one
            # it read, so it writes nothing.
            connection.execute("ROLLBACK")
            raise VerificationError(
                f"acquisition record {acquisition_id!r} changed while the replacement was"
                " being read; nothing was written")
        # The new identity records no verification of its own: the copied digests
        # are the old identity's, so only a fresh Verify may complete it.
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " origin_id, repository, revision, location_id, verified_at, notes, created_at)"
            " SELECT ?, name, source, checksum_status, origin_id, repository, revision,"
            " location_id, NULL, notes, ? FROM acquisition_records WHERE acquisition_id = ?",
            (replacement_id, moment, acquisition_id))
        connection.executemany(
            "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes,"
            " published_sha256, computed_sha256, verified_at) VALUES (?, ?, ?, ?, ?, NULL)",
            [(replacement_id, name, size if name == file_name else entry["size_bytes"],
              entry["published_sha256"], digest if name == file_name else entry["computed_sha256"])
             for name, entry in sorted(rows.items())])
        connection.execute(
            "UPDATE acquisition_records SET verified_at = NULL WHERE acquisition_id = ?",
            (acquisition_id,))
        connection.execute(
            "INSERT INTO acquisition_verification_failures (acquisition_id, reason) VALUES (?, ?)"
            " ON CONFLICT(acquisition_id) DO UPDATE SET reason = excluded.reason",
            (acquisition_id, f"{file_name} was accepted as replacement bytes; this record is unusable."))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return replacement_id


def launch_check(connection, profile_id):
    """(ok, reason) for a launch: the first model file and the selected sidecars.

    A record the last verification could not complete refuses here before
    anything is hashed, because a discovered mismatch blocks use however intact
    the files this launch reads are. Then only the files the profile picked are
    hashed, each against the SHA-256 the acquisition record holds for it, and a
    missing or changed one refuses the launch. The other shards of the same record
    are deliberately not rehashed here: their integrity comes from the download
    and the full-set Verify (ACQ-6, ACQ-8), and a launch must not hash roughly
    95 GB before it starts.
    """
    profile = connection.execute(
        "SELECT kind FROM model_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    if profile is None:
        return False, f"no profile {profile_id!r} in the store"
    for field, required in store.PICKS.get(profile["kind"], ()):
        pick = connection.execute(
            "SELECT f.file_name AS file_name, f.computed_sha256 AS sha256,"
            " f.verified_at AS file_verified, r.verified_at AS record_verified, l.path AS root"
            " FROM profile_artifacts AS a"
            " JOIN acquisition_files AS f ON f.acquisition_id = a.acquisition_id"
            "  AND f.file_name = a.file_name"
            " JOIN acquisition_records AS r ON r.acquisition_id = a.acquisition_id"
            " JOIN artifact_locations AS l ON l.location_id = r.location_id"
            " WHERE a.profile_id = ? AND a.role = ?",
            (profile_id, field)).fetchone()
        if pick is None:
            if required:
                return False, f"its {field} names no file of the store"
            continue
        if pick["record_verified"] is None or pick["file_verified"] is None:
            return False, (f"its {field} file {pick['file_name']!r} is in an acquisition"
                           " record no verification has completed")
        path = Path(pick["root"]) / pick["file_name"]
        if not path.is_file():
            return False, f"its {field} file {pick['file_name']!r} is missing at {path}"
        read = _read_digest(path)
        if read is None:
            return False, f"its {field} file {pick['file_name']!r} could not be read whole at {path}"
        if read[0] != pick["sha256"]:
            return False, (f"its {field} file {pick['file_name']!r} does not match the "
                           f"SHA-256 recorded for it at {path}")
    return True, ""


def _check_file(root, row, consent, consented, expected_fd=None, on_progress=None):
    """One required file: what it is, and the first reason it is not usable."""
    name = row["file_name"]
    path = root / name
    if on_progress:
        on_progress(name, 0, row["size_bytes"])
    if expected_fd is None and not path.is_file():
        return FileCheck(name, MISSING, f"{name} is missing at {path}")
    report_progress = (lambda transferred: on_progress(name, transferred, row["size_bytes"])) if on_progress else None
    read = (_read_digest(path, on_progress=report_progress) if expected_fd is None
            else _read_digest(path, expected_fd=expected_fd, on_progress=report_progress))
    if read is None:
        return FileCheck(name, UNREADABLE, f"{name} could not be read whole at {path}")
    digest, size = read
    if size != row["size_bytes"]:
        return FileCheck(name, WRONG_SIZE,
                         f"{name} is {size} bytes, not the recorded {row['size_bytes']}")
    published = row["published_sha256"]
    if published is not None and published != digest:
        return FileCheck(name, MISMATCHED,
                         f"{name} does not match the published checksum {published}", digest)
    # What was recorded is what the operator accepted last time, so bytes that
    # differ from it are a replacement to accept explicitly, never a success.
    if row["computed_sha256"] and row["computed_sha256"] != digest:
        return FileCheck(name, CHANGED,
                         f"{name} no longer matches the SHA-256 recorded for it", digest)
    if published is None and not consent and not (consented and row["verified_at"]):
        return FileCheck(name, NEEDS_CONSENT,
                         f"{name} has no published checksum; it needs the operator's consent"
                         f' to "{LABEL_NO_PUBLISHED}"', digest)
    return FileCheck(name, VERIFIED, "", digest)


def _read_digest(path, expected_fd=None, on_progress=None):
    """(hex SHA-256, size) for one file, or None when it is not usable.

    Read in bounded blocks, and refused rather than trusted when the file is not
    an ordinary one, cannot be read to its end, or changed while it was read: a
    digest of bytes that were not there for the whole read proves nothing.
    """
    digest = hashlib.sha256()
    read_bytes = 0
    try:
        handle = (open(path, "rb") if expected_fd is None
                  else os.fdopen(os.dup(expected_fd), "rb"))
        with handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            for block in iter(lambda: handle.read(BLOCK), b""):
                digest.update(block)
                read_bytes += len(block)
                if on_progress:
                    on_progress(read_bytes)
            after = os.fstat(handle.fileno())
    except OSError:
        return None
    if read_bytes != after.st_size:
        return None
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        return None
    return digest.hexdigest(), after.st_size


def _record(connection, acquisition_id):
    """The stored acquisition record with its artifact location's root, or None."""
    return connection.execute(
        "SELECT r.*, l.path AS root, v.reason AS verification_reason"
        " FROM acquisition_records AS r"
        " JOIN artifact_locations AS l ON l.location_id = r.location_id"
        " LEFT JOIN acquisition_verification_failures AS v USING (acquisition_id)"
        " WHERE r.acquisition_id = ?", (acquisition_id,)).fetchone()


def _read_identity(connection, acquisition_id):
    """The record, its file rows, and the exact state a later write compares.

    One read of each, so the state this verification judges is the state its
    write is compared against: whatever another Matuto records between the two
    is a change this verification must refuse, never a baseline it adopts
    silently. Returns (None, [], None) for a record the store does not hold.
    """
    record = _record(connection, acquisition_id)
    if record is None:
        return None, [], None
    rows = connection.execute(
        "SELECT file_name, size_bytes, published_sha256, computed_sha256, verified_at"
        " FROM acquisition_files WHERE acquisition_id = ? ORDER BY file_name",
        (acquisition_id,)).fetchall()
    return record, rows, (tuple(record), _file_state(rows))


def _file_state(rows):
    return {row["file_name"]: (row["size_bytes"], row["computed_sha256"], row["verified_at"])
            for row in rows}


def _snapshot(connection, acquisition_id):
    """What the record and its file rows say now, read again under the write lock.

    A writer compares this with the state it hashed: a verification that hashed
    an older record cannot overwrite what another Matuto recorded in the
    meantime, nor re-complete a record another one ended while it was hashing.
    """
    record = _record(connection, acquisition_id)
    files = {row["file_name"]: (row["size_bytes"], row["computed_sha256"], row["verified_at"])
             for row in connection.execute(
                 "SELECT file_name, size_bytes, computed_sha256, verified_at"
                 " FROM acquisition_files WHERE acquisition_id = ?", (acquisition_id,))}
    return (tuple(record) if record is not None else None, files)


def _record_failure(connection, acquisition_id, snapshot, reason):
    """End the record's completeness after a verification that could not complete it.

    A discovered mismatch blocks use, so the record stops being selectable and
    every profile launch from it refuses, even though its files may still hash
    clean. Per-file digests and successful verification times stay unchanged;
    the failure reason is stored until a later successful Verify replaces it.
    """
    nested = getattr(connection, "in_transaction", False)
    if nested:
        connection.execute("SAVEPOINT matuto_verification")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if _snapshot(connection, acquisition_id) != snapshot:
            # Another Matuto ended or completed this record while this one was
            # reading the bytes; that newer verdict is the one that stands.
            connection.execute("ROLLBACK TO matuto_verification" if nested else "ROLLBACK")
            if nested:
                connection.execute("RELEASE matuto_verification")
            return
        connection.execute(
            "UPDATE acquisition_records SET verified_at = NULL WHERE acquisition_id = ?",
            (acquisition_id,))
        connection.execute(
            "INSERT INTO acquisition_verification_failures (acquisition_id, reason) VALUES (?, ?)"
            " ON CONFLICT(acquisition_id) DO UPDATE SET reason = excluded.reason",
            (acquisition_id, reason))
        connection.execute("RELEASE matuto_verification" if nested else "COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK TO matuto_verification" if nested else "ROLLBACK")
            if nested:
                connection.execute("RELEASE matuto_verification")
        raise


def _record_results(connection, acquisition_id, files, status, verified_at, snapshot):
    """The one write a successful verification makes, in one transaction.

    The snapshot is compared under the write lock, so two Matutoes verifying the
    same record cannot each record over the other, and a failure part-way rolls
    the whole thing back, leaving the record and its files exactly as they were.
    """
    nested = getattr(connection, "in_transaction", False)
    if nested:
        connection.execute("SAVEPOINT matuto_verification")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        if _snapshot(connection, acquisition_id) != snapshot:
            connection.execute("ROLLBACK TO matuto_verification" if nested else "ROLLBACK")
            if nested:
                connection.execute("RELEASE matuto_verification")
            return False
        connection.executemany(
            "UPDATE acquisition_files SET computed_sha256 = ?, verified_at = ?"
            " WHERE acquisition_id = ? AND file_name = ?",
            [(check.computed_sha256, verified_at, acquisition_id, check.file_name)
             for check in files])
        connection.execute(
            "UPDATE acquisition_records SET verified_at = ?, checksum_status = ?"
            " WHERE acquisition_id = ?", (verified_at, status, acquisition_id))
        connection.execute(
            "DELETE FROM acquisition_verification_failures WHERE acquisition_id = ?", (acquisition_id,))
        connection.execute("RELEASE matuto_verification" if nested else "COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK TO matuto_verification" if nested else "ROLLBACK")
            if nested:
                connection.execute("RELEASE matuto_verification")
        raise
    return True


def _stamp(now=None):
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
