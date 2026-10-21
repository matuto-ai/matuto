"""Matuto's one SQLite store: the only code in the app that opens the file.

The manager, the lab and `wq.py` reach every record through this module; nothing
else in the package connects to the file. The schema is one block per area in
SCHEMA_BLOCKS, so a ticket that owns an area may change that block before the
first release without touching another's.
"""

import contextlib
import datetime
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from collections.abc import Mapping
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matuto import arguments, installation, recipes
from matuto.matuto_runtime import OPTION_ALIASES

SCHEMA_VERSION = 4
MIGRATIONS = {}
STORE_FILENAME = "store.sqlite3"
MIGRATION_TEMP_PREFIX = ".store-migration-"
CONFIG_ROOT_NAME = "ai-agents-sandbox"
# AUTH-2 fixes login.json in the manager's own directory, so the store sits beside
# it under the one path this module owns.
MANAGER_DIR_NAME = "matuto-manager"
# A file another account, or anyone in this account's group, may read or change is
# refused rather than used: every record, qualification and run lives in it.
GROUP_OR_OTHER = 0o077
# The names SQLite keeps beside the store for a transaction in flight.
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")


class StoreError(Exception):
    """One refusal, naming the path or the versions the operator has to fix."""


class StoreBusyError(StoreError):
    """The updater currently owns the store lock."""


class RecordRefusal(Exception):
    """One malformed record, naming the record and the field to fix."""


class ActiveRunRefusal(Exception):
    """A run that has not ended cannot be deleted: stop it and settle it first."""


# One block per area, in the order a first run fills them. Statements are plain
# DDL: the rules about what a record may say arrive with their own tickets, and
# the rules the database itself keeps are foreign keys and the one trigger.
SCHEMA_BLOCKS = (
    ("store", (
        """CREATE TABLE store_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL)""",
    )),
    ("cards", (
        """CREATE TABLE cards (
    card_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    gpu_identity TEXT NOT NULL UNIQUE,
    vram_ceiling_bytes INTEGER NOT NULL CHECK (vram_ceiling_bytes > 0),
    -- llama.cpp uses -1 for all layers; positive values are numeric limits.
    default_gpu_layers INTEGER NOT NULL CHECK (default_gpu_layers = -1 OR default_gpu_layers > 0),
    smoke_output_tokens INTEGER NOT NULL CHECK (smoke_output_tokens > 0),
    bench_prompt_tokens INTEGER NOT NULL CHECK (bench_prompt_tokens > 0),
    bench_generation_tokens INTEGER NOT NULL CHECK (bench_generation_tokens > 0),
    bench_repetitions INTEGER NOT NULL CHECK (bench_repetitions > 0),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL)""",
    )),
    ("artifacts", (
        """CREATE TABLE artifact_locations (
    location_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL)""",
        # The one root supplies the download destination and native discovery; it
        # is the same directory as its linked artifact location.
        """CREATE TABLE model_storage (
    setting_id INTEGER PRIMARY KEY CHECK (setting_id = 1),
    location_id TEXT NOT NULL REFERENCES artifact_locations(location_id),
    saved_at TEXT NOT NULL)""",
    )),
    ("download origins", (
        """CREATE TABLE download_origins (
    origin_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('repository', 'organisation')),
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    UNIQUE (kind, name))""",
    )),
    ("lab port", (
        """CREATE TABLE lab_port (
    setting_id INTEGER PRIMARY KEY CHECK (setting_id = 1),
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    saved_at TEXT NOT NULL)""",
    )),
    ("argument catalogue", (
        """CREATE TABLE argument_catalogue (
    argument_id TEXT PRIMARY KEY,
    flag TEXT NOT NULL UNIQUE,
    takes_value INTEGER NOT NULL DEFAULT 1 CHECK (takes_value IN (0, 1)),
    description TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '')""",
    )),
    ("speech executables", (
        """CREATE TABLE speech_executables (
    executable_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('synthesis', 'recognition')),
    location_id TEXT NOT NULL REFERENCES artifact_locations(location_id),
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    version TEXT,
    notes TEXT NOT NULL DEFAULT '',
    installed_at TEXT NOT NULL)""",
    )),
    ("acquisition", (
        """CREATE TABLE acquisition_records (
    acquisition_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('download', 'adoption')),
    checksum_status TEXT NOT NULL DEFAULT 'locally-hashed'
        CHECK (checksum_status IN ('published-checksum', 'locally-hashed')),
    origin_id TEXT REFERENCES download_origins(origin_id),
    repository TEXT,
    revision TEXT,
    location_id TEXT NOT NULL REFERENCES artifact_locations(location_id),
    verified_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL)""",
        """CREATE TABLE acquisition_files (
    acquisition_id TEXT NOT NULL REFERENCES acquisition_records(acquisition_id),
    file_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    published_sha256 TEXT,
    computed_sha256 TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (acquisition_id, file_name))""",
        """CREATE TABLE acquisition_verification_failures (
    acquisition_id TEXT PRIMARY KEY REFERENCES acquisition_records(acquisition_id) ON DELETE CASCADE,
    reason TEXT NOT NULL)""",
    )),
    ("profiles", (
        """CREATE TABLE model_profiles (
    profile_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('language', 'synthesis', 'recognition')),
    name TEXT NOT NULL,
    alias TEXT NOT NULL DEFAULT '',
    context_size INTEGER,
    thinking TEXT,
    card_id TEXT REFERENCES cards(card_id),
    speech_executable_id TEXT REFERENCES speech_executables(executable_id),
    published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    -- A recognition profile runs on the CPU, and only a language profile is ever
    -- published or carries an alias the router selects.
    CHECK (kind <> 'recognition' OR card_id IS NULL),
    CHECK (kind <> 'language' OR alias <> ''),
    CHECK (kind = 'language' OR published = 0),
    CHECK (kind <> 'synthesis' OR speech_executable_id IS NOT NULL))""",
        # Two published profiles never share an alias; copies use a distinct alias.
        """CREATE UNIQUE INDEX published_alias ON model_profiles (alias) WHERE published = 1""",
        # Publication is the one rule about removal a foreign key cannot carry, so
        # it lives here too: the database refuses it whichever caller asks.
        """CREATE TRIGGER published_profile_not_removable
BEFORE DELETE ON model_profiles
WHEN OLD.published = 1
BEGIN
    SELECT RAISE(ABORT, 'a published language profile cannot be removed');
END""",
        """CREATE TABLE profile_arguments (
    profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
    tool TEXT NOT NULL CHECK (tool IN ('server', 'smoke', 'bench')),
    position INTEGER NOT NULL CHECK (position >= 0),
    argument_id TEXT REFERENCES argument_catalogue(argument_id),
    flag TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (profile_id, tool, position))""",
        """CREATE INDEX profile_arguments_catalogue ON profile_arguments (argument_id)""",
        # Weights, projector and MTP files are chosen from acquisition records, so
        # the reference to the record and to the file the profile selected are the
        # store's, not a typed path's.
        """CREATE TABLE profile_artifacts (
    profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
    role TEXT NOT NULL CHECK (role IN ('model', 'weights', 'mmproj', 'mtp')),
    acquisition_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    PRIMARY KEY (profile_id, role),
    FOREIGN KEY (acquisition_id, file_name)
        REFERENCES acquisition_files(acquisition_id, file_name))""",
    )),
    ("bundles", (
        # The stated generation load is what a bundle's qualification is measured
        # under, so it is part of the bundle's served configuration and not a
        # note. It arrives with the qualification computation.
        """CREATE TABLE bundles (
    bundle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    load_tokens INTEGER CHECK (load_tokens IS NULL OR load_tokens > 0),
    load_prompt TEXT,
    created_at TEXT NOT NULL)""",
        # One language profile plus the speech profiles that serve voice with it.
        """CREATE TABLE bundle_members (
    bundle_id TEXT NOT NULL REFERENCES bundles(bundle_id),
    role TEXT NOT NULL CHECK (role IN ('language', 'synthesis', 'recognition')),
    profile_id TEXT NOT NULL REFERENCES model_profiles(profile_id),
    PRIMARY KEY (bundle_id, role))""",
        """CREATE INDEX bundle_members_profile ON bundle_members (profile_id)""",
    )),
    ("serving", (
        # Which bundle voice serving started, and where each of its speech parts
        # answers on this machine. One record at a time, because voice and
        # agentic serving are exclusive. A part names a member of that bundle by
        # role, so the profile it resolves to is one the bundle contains; the
        # language profile runs inside the router itself.
        """CREATE TABLE voice_serving (
    voice_serving_id INTEGER PRIMARY KEY CHECK (voice_serving_id = 1),
    bundle_id TEXT NOT NULL UNIQUE REFERENCES bundles(bundle_id))""",
        """CREATE TABLE voice_serving_parts (
    part_role TEXT PRIMARY KEY CHECK (part_role IN ('synthesis', 'recognition')),
    bundle_id TEXT NOT NULL REFERENCES voice_serving(bundle_id),
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    FOREIGN KEY (bundle_id, part_role) REFERENCES bundle_members(bundle_id, role))""",
    )),
    ("build registry", (
        """CREATE TABLE builds (
    build_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('llama-hip', 'llama-vulkan')),
    location TEXT NOT NULL,
    backend TEXT NOT NULL,
    source_revision TEXT,
    target TEXT,
    recipe TEXT,
    toolchain TEXT,
    state TEXT NOT NULL DEFAULT 'registered'
        CHECK (state IN ('registered', 'unavailable', 'external')),
    notes TEXT NOT NULL DEFAULT '')""",
        # One tree a Matuto build installed, with the result of the installation check at its
        # final path. The build job records it, and it survives that job's log, so
        # discovery has the completion evidence RT-6 asks for and an executable at
        # the destination is never proof of success. A completed build records this
        # and registers nothing (RT-7): the `builds` row above is the operator's.
        """CREATE TABLE installations (
    location TEXT PRIMARY KEY,
    recipe TEXT NOT NULL,
    source_revision TEXT,
    target TEXT,
    toolchain TEXT,
    check_state TEXT NOT NULL CHECK (check_state IN ('checked', 'unchecked')),
    check_reason TEXT NOT NULL DEFAULT '',
    check_digests TEXT NOT NULL DEFAULT '{}',
    recorded_at TEXT NOT NULL)""",
        # Each tree appears once with its registry ID, so a second row for
        # one location is refused by the database, whichever caller asks.
        """CREATE UNIQUE INDEX builds_location ON builds (location)""",
    )),
    ("runtime selection", (
        """CREATE TABLE runtime_selection (
    selection_id INTEGER PRIMARY KEY CHECK (selection_id = 1),
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    card_id TEXT NOT NULL REFERENCES cards(card_id),
    backend TEXT NOT NULL,
    saved_at TEXT NOT NULL)""",
    )),
    ("jobs", (
        # Provenance and output digests survive the log's deletion, which the build
        # registry and qualification need (RT-6).
        """CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('build', 'download')),
    unit_name TEXT NOT NULL,
    pinned_request TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    outcome TEXT CHECK (outcome IN ('completed', 'failed', 'cancelled', 'interrupted')),
    provenance TEXT,
    output_digests TEXT,
    log TEXT NOT NULL DEFAULT '',
    log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
    started_at TEXT NOT NULL,
    ended_at TEXT)""",
    )),
    ("runs", (
        # A run keeps the record it ran against as it was then, so a removed
        # record's runs stay browsable and still name what they measured.
        """CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    record_kind TEXT NOT NULL CHECK (record_kind IN ('language', 'bundle')),
    record_id TEXT NOT NULL,
    record_label TEXT NOT NULL,
    fingerprint TEXT,
    build_id TEXT,
    procedure TEXT NOT NULL,
    procedure_revision TEXT NOT NULL,
    unit_name TEXT,
    outcome TEXT CHECK (outcome IN ('completed', 'errored', 'cancelled', 'interrupted')),
    dirty_checkout INTEGER NOT NULL DEFAULT 0 CHECK (dirty_checkout IN (0, 1)),
    summary TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    log_truncated INTEGER NOT NULL DEFAULT 0 CHECK (log_truncated IN (0, 1)),
    started_at TEXT NOT NULL,
    ended_at TEXT)""",
        """CREATE INDEX runs_record ON runs (record_kind, record_id)""",
        """CREATE TABLE checks (
    check_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'ERROR', 'SKIP')),
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, sequence))""",
        """CREATE TABLE run_evidence (
    evidence_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    name TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    body BLOB NOT NULL,
    UNIQUE (run_id, name))""",
    )),
    ("qualification and acceptance", (
        # Qualification is computed from a fingerprint against a record, never a
        # flag on the record, so the record is named here as it was then.
        # build_id is the build the accepted evidence was recorded on, provenance
        # the status warns about rather than part of the fingerprint (PROF-5). It is
        # a plain identity, not a foreign key, so forgetting an inactive build
        # registration leaves its qualification history readable.
        """CREATE TABLE qualification_records (
    qualification_id TEXT PRIMARY KEY,
    record_kind TEXT NOT NULL CHECK (record_kind IN ('language', 'bundle')),
    record_id TEXT NOT NULL,
    record_label TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    build_id TEXT,
    created_at TEXT NOT NULL)""",
        """CREATE INDEX qualification_records_record
    ON qualification_records (record_kind, record_id)""",
        """CREATE TABLE qualification_runs (
    qualification_id TEXT NOT NULL REFERENCES qualification_records(qualification_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    PRIMARY KEY (qualification_id, run_id))""",
        """CREATE TABLE acceptances (
    acceptance_id TEXT PRIMARY KEY,
    qualification_id TEXT NOT NULL REFERENCES qualification_records(qualification_id),
    scope TEXT NOT NULL,
    residual_risks TEXT NOT NULL DEFAULT '',
    accepted_by TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    withdrawn_at TEXT)""",
        # A run any acceptance cites, withdrawn ones included, cannot be deleted.
        """CREATE TABLE acceptance_runs (
    acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    run_fingerprint TEXT NOT NULL,
    build_id TEXT DEFAULT NULL,
    PRIMARY KEY (acceptance_id, run_id))""",
        """CREATE TABLE acceptance_exceptions (
    acceptance_id TEXT NOT NULL REFERENCES acceptances(acceptance_id),
    check_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (acceptance_id, check_name))""",
        # The foreign key protects cited runs; this refusal names the invariant for
        # direct SQL callers too, including after withdrawal.
        """CREATE TRIGGER accepted_run_not_removable
BEFORE DELETE ON runs
WHEN EXISTS (SELECT 1 FROM acceptance_runs WHERE run_id = OLD.run_id)
BEGIN
    SELECT RAISE(ABORT, 'run is cited by acceptance history and cannot be deleted');
END""",
    )),
    ("sessions", (
        # The cookie's digest, never the cookie: a stolen file is no credential.
        # The generation is the login's at sign-in, so a password change or a reset
        # ends every row whose generation no longer matches the stored login.
        """CREATE TABLE sessions (
    token_digest TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    generation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL)""",
        """CREATE INDEX sessions_expiry ON sessions (expires_at)""",
    )),
    ("update state", (
        """CREATE TABLE update_state (
    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
    release_version TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '',
    snapshot_path TEXT,
    snapshot_taken_at TEXT,
    updated_at TEXT NOT NULL)""",
    )),
)

# The tables every Matuto store holds, so an open can refuse a file that lost
# them instead of handing a caller a broken store.
def _migrate_v1_to_v2(connection):
    """Add build provenance and citation protection to the existing v1 store."""
    connection.execute("ALTER TABLE runs ADD COLUMN build_id TEXT")
    connection.execute("ALTER TABLE acceptance_runs ADD COLUMN build_id TEXT DEFAULT NULL")
    trigger = next(statement for _area, statements in SCHEMA_BLOCKS for statement in statements
                   if statement.startswith("CREATE TRIGGER accepted_run_not_removable"))
    connection.execute(trigger)


MIGRATIONS[1] = _migrate_v1_to_v2


def _migrate_v2_to_v3(connection):
    """Retain removed origins for pinned downloads and acquisition history."""
    connection.execute(
        "ALTER TABLE download_origins ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        " CHECK (enabled IN (0, 1))")


MIGRATIONS[2] = _migrate_v2_to_v3


def _migrate_v3_to_v4(connection):
    """Keep the last completed full-set Verify failure visible in the record list."""
    connection.execute(
        "CREATE TABLE acquisition_verification_failures ("
        "acquisition_id TEXT PRIMARY KEY REFERENCES acquisition_records(acquisition_id) ON DELETE CASCADE, "
        "reason TEXT NOT NULL)")


MIGRATIONS[3] = _migrate_v3_to_v4


REQUIRED_TABLES = frozenset(
    statement.split()[2]
    for _area, statements in SCHEMA_BLOCKS
    for statement in statements
    if statement.startswith("CREATE TABLE"))


class Store:
    """One open store. Its connection is the only connection to the file."""

    def __init__(self, path, connection, lock_fd=None):
        self.path = path
        self._connection = connection
        self._lock_fd = lock_fd

    def connection(self):
        """The live connection, with foreign keys enforced on it."""
        return self._connection

    def close(self):
        try:
            self._connection.close()
        finally:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None


def snapshot(opened):
    """Copy a consistent, read-only view without holding a lock on the live store."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        opened.connection().backup(connection)
        connection.execute("PRAGMA query_only = ON")
        return Store(opened.path, connection)
    except BaseException:
        connection.close()
        raise


# Only a language profile and a bundle carry a qualification state; a speech
# profile has a fingerprint that reaches its bundle but no state of its own.
QUALIFICATION_KINDS = ("language", "bundle")
# Any spelling of this option is a language profile's own offload override; the
# card's default_gpu_layers supplies the effective value when it carries none.
GPU_LAYER_CANONICAL = "--gpu-layers"
FINGERPRINT_PREFIX = "sha256:"


def list_argument_catalogue(store):
    connection = store.connection()
    entries = []
    for row in connection.execute(
            "SELECT argument_id, flag, takes_value, description, tools"
            " FROM argument_catalogue ORDER BY flag"):
        entry = dict(row)
        entry["takes_value"] = bool(entry["takes_value"])
        entry["tools"] = entry["tools"].split(",") if entry["tools"] else []
        entry["uses"] = [dict(use) for use in connection.execute(
            "SELECT p.profile_id, p.name, a.tool, a.position, a.flag, a.value"
            " FROM profile_arguments a JOIN model_profiles p USING (profile_id)"
            " WHERE a.argument_id = ? ORDER BY p.name, a.tool, a.position",
            (entry["argument_id"],))]
        entries.append(entry)
    return entries


def save_argument_catalogue(store, entry, *, argument_id=None):
    """Create or edit a catalogue entry without changing any stored profile row."""
    if not isinstance(entry, Mapping):
        raise RecordRefusal("Argument catalogue entry must be an object.")
    flag = entry.get("flag")
    if not isinstance(flag, str) or not re.fullmatch(r"--?[A-Za-z0-9][A-Za-z0-9-]*", flag):
        raise RecordRefusal("Argument catalogue flag must be a valid option such as --parallel.")
    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        raise RecordRefusal("Argument catalogue description is required.")
    takes_value = entry.get("takes_value")
    if not isinstance(takes_value, bool):
        raise RecordRefusal("Argument catalogue 'takes_value' must be true or false.")
    tools = entry.get("tools")
    if (not isinstance(tools, list) or not tools
            or any(tool not in ("server", "smoke", "bench") for tool in tools)
            or len(set(tools)) != len(tools)):
        raise RecordRefusal("Choose one or more valid tools: server, smoke, bench.")
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if argument_id is None:
            argument_id = "arg-" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO argument_catalogue (argument_id, flag, takes_value, description, tools)"
                " VALUES (?, ?, ?, ?, ?)",
                (argument_id, flag, int(takes_value), description.strip(), ",".join(tools)))
        else:
            changed = connection.execute(
                "UPDATE argument_catalogue SET flag = ?, takes_value = ?, description = ?, tools = ?"
                " WHERE argument_id = ?",
                (flag, int(takes_value), description.strip(), ",".join(tools), argument_id)).rowcount
            if not changed:
                raise RecordRefusal("Argument catalogue entry no longer exists; reload and try again.")
        connection.execute("COMMIT")
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise RecordRefusal(f"Argument catalogue flag {flag!r} is already in use.") from None
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return argument_id


def remove_argument_catalogue(store, argument_id):
    """Atomically turn catalogue uses into Custom rows and remove their entry."""
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        entry = connection.execute(
            "SELECT flag FROM argument_catalogue WHERE argument_id = ?", (argument_id,)).fetchone()
        if entry is None:
            raise RecordRefusal("Argument catalogue entry no longer exists; reload and try again.")
        converted = [dict(row) for row in connection.execute(
            "SELECT p.name, p.profile_id, a.tool, a.position, a.flag, a.value"
            " FROM profile_arguments a JOIN model_profiles p USING (profile_id)"
            " WHERE a.argument_id = ? ORDER BY p.name, a.tool, a.position", (argument_id,))]
        connection.execute("UPDATE profile_arguments SET argument_id = NULL WHERE argument_id = ?",
                           (argument_id,))
        connection.execute("DELETE FROM argument_catalogue WHERE argument_id = ?", (argument_id,))
        connection.execute("COMMIT")
        return {"flag": entry["flag"], "converted": converted}
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def read_card(store, card_id):
    """One stored card in card validation's record shape."""
    row = store.connection().execute(
        "SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()
    if row is None:
        raise StoreError(f"no card is stored under {card_id!r}")
    record = dict(row, kind="card")
    if record["default_gpu_layers"] == -1:
        record["default_gpu_layers"] = "all"
    return record


def create_card(store, record):
    """Validate and atomically add one card picked from current GPU detection."""
    if not isinstance(record, Mapping):
        raise RecordRefusal("a card record must be a mapping of its fields")
    saved, _warnings = save_record(store, record)
    return saved


def save_record(store, record, *, record_id=None, help_text=None, selector=None,
                _duplicate_from=None):
    """Create or edit one validated record atomically; an edit keeps its stable ID."""
    kind, label = _record_kind(record)
    id_field = ID_FIELDS[kind]
    identifier = _record_identifier(record, kind, label)
    connection = store.connection()
    outer_transaction = not connection.in_transaction
    if outer_transaction:
        connection.execute("BEGIN IMMEDIATE")
    else:
        connection.execute("SAVEPOINT save_record")
    try:
        existing = None
        if record_id is not None:
            if identifier != record_id:
                _refuse(kind, label, id_field, f"cannot change its stable ID from {record_id!r}")
            existing = _stored_record_exists(connection, kind, record_id)
            if not existing:
                _refuse(kind, label, id_field, f"names no stored {kind} record: {record_id!r}")
            if kind in PICKS:
                stored_kind = connection.execute(
                    "SELECT kind FROM model_profiles WHERE profile_id = ?", (record_id,)).fetchone()["kind"]
                if stored_kind != kind:
                    _refuse(kind, label, id_field,
                            f"names a stored {stored_kind} record, not a {kind} record")
        elif _stored_record_exists(connection, kind, identifier):
            _refuse(kind, label, id_field, "already exists")
        elif kind in ("language", "bundle"):
            retained = connection.execute(
                "SELECT record_label FROM runs WHERE record_kind = ? AND record_id = ?"
                " UNION ALL SELECT record_label FROM qualification_records"
                " WHERE record_kind = ? AND record_id = ? LIMIT 1",
                (kind, identifier, kind, identifier)).fetchone()
            if retained is not None:
                _refuse(kind, label, id_field,
                        f"is still used by retained {kind} evidence for {retained['record_label']!r}")
        candidate = dict(record)
        candidate.pop("qualification", None)
        old = None
        if existing:
            old = read_card(store, record_id) if kind == "card" else _read_record(
                store, kind, record_id)
        elif "created_at" not in candidate:
            candidate["created_at"] = datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds")
        if kind in PICKS and "published" in candidate and not isinstance(candidate["published"], bool):
            _refuse(kind, label, "published", "must be true or false")

        detected_gpus = ()
        device = None
        same_card_gpu = kind == "card" and existing and candidate.get("gpu_identity") == old["gpu_identity"]
        if kind == "card":
            from matuto.cards import detect
            detected_gpus = detect(store)
            if detected_gpus["state"] == "completed":
                device = next((item for item in detected_gpus["devices"]
                               if item["pciAddress"] == candidate.get("gpu_identity")), None)
            if same_card_gpu and (detected_gpus["state"] != "completed" or device is None):
                detected_gpus = {"devices": [old["gpu_identity"]]}
            elif detected_gpus["state"] != "completed":
                _refuse(kind, label, "gpu_identity", "cannot be saved while GPU detection is unavailable")
        warnings = validate_record(candidate, connection, detected_gpus=detected_gpus,
                                   help_text=help_text, selector=selector)
        if kind == "language":
            candidate["arguments"] = arguments.canonicalize_server_aliases(
                candidate.get("arguments") or [], help_text)
        if existing:
            if kind in PICKS and "published" in candidate and candidate["published"] != old["published"]:
                _refuse(kind, label, "published", "is managed by the publishing workflow")
            if kind in PICKS:
                candidate["published"] = old["published"]
            candidate["created_at"] = old["created_at"]
        elif kind in PICKS and candidate.get("published", False):
            _refuse(kind, label, "published", "cannot be set when creating a record")
        if kind == "card":
            # Without detection, an existing ceiling is a bound, not proof that it can increase.
            if device is None or device["vramBytes"] is None:
                if not same_card_gpu or candidate["vram_ceiling_bytes"] > old["vram_ceiling_bytes"]:
                    _refuse(kind, label, "vram_ceiling_bytes",
                            "cannot be checked because detected GPU capacity is unavailable")
            elif candidate["vram_ceiling_bytes"] > device["vramBytes"]:
                if not same_card_gpu or candidate["vram_ceiling_bytes"] > old["vram_ceiling_bytes"]:
                    _refuse(kind, label, "vram_ceiling_bytes",
                            "must not exceed the currently detected GPU capacity")
            holder = connection.execute(
                "SELECT card_id, name FROM cards WHERE gpu_identity = ? AND card_id <> ?",
                (candidate["gpu_identity"], identifier)).fetchone()
            if holder is not None:
                _refuse(kind, label, "gpu_identity",
                        f"is already used by card {holder['name']!r} ({holder['card_id']})")
        if kind in PICKS:
            candidate["arguments"] = sorted(
                (({"tool": row["tool"], "catalogue": row["catalogue"],
                   "value": row.get("value")} if isinstance(row.get("catalogue"), str)
                  and row["catalogue"].strip() else
                  {"tool": row["tool"], "option": row["option"],
                   "value": row.get("value")}) for row in candidate.get("arguments") or []),
                key=lambda row: row["tool"])
            for field, required in PICKS[kind]:
                if not required and candidate.get(field) is None:
                    candidate.pop(field, None)
        elif kind == "bundle":
            candidate.setdefault("load_tokens", None)
            candidate.setdefault("load_prompt", None)
        if existing and candidate == old:
            connection.execute("COMMIT" if outer_transaction else "RELEASE save_record")
            unchanged = read_card(store, record_id) if kind == "card" else read_record(
                store, kind, record_id)
            return unchanged, warnings
        if existing:
            # PROF-7: an edit reaches the live-record rule only once it is known to
            # change something, and it runs on this transaction so a refusal leaves
            # the store and the router files as they were. Imported edits arrive
            # here too, so one refused record refuses the whole document.
            from matuto import live_records
            live_records.refuse_live_change(store, kind, identifier)
        if kind == "card":
            values = dict(candidate, default_gpu_layers=(
                -1 if candidate["default_gpu_layers"] == "all" else candidate["default_gpu_layers"]))
            fields = ("name", "gpu_identity", "vram_ceiling_bytes", "default_gpu_layers",
                      "smoke_output_tokens", "bench_prompt_tokens", "bench_generation_tokens",
                      "bench_repetitions", "notes")
            if existing:
                connection.execute(
                    "UPDATE cards SET " + ", ".join(f"{field} = ?" for field in fields)
                    + " WHERE card_id = ?", tuple(values[field] for field in fields) + (record_id,))
            else:
                connection.execute(
                    "INSERT INTO cards (card_id, name, gpu_identity, vram_ceiling_bytes,"
                    " default_gpu_layers, smoke_output_tokens, bench_prompt_tokens,"
                    " bench_generation_tokens, bench_repetitions, notes, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identifier, *(values[field] for field in fields), candidate["created_at"]))
        elif kind in PICKS:
            if kind == "language" and _duplicate_from is not None:
                original = connection.execute(
                    "SELECT profile_id, name, alias FROM model_profiles"
                    " WHERE profile_id = ? AND kind = 'language'", (_duplicate_from,)).fetchone()
                if original is None:
                    raise StoreError(f"no language profile is stored under {_duplicate_from!r}")
                if candidate["alias"] == original["alias"]:
                    _refuse(kind, label, "alias",
                            f"must differ from original profile {original['name']!r}"
                            f" ({original['profile_id']})")
                holder = connection.execute(
                    "SELECT profile_id, name FROM model_profiles"
                    " WHERE kind = 'language' AND alias = ? AND published = 1",
                    (candidate["alias"],)).fetchone()
                if holder is not None:
                    _refuse(kind, label, "alias",
                            f"is already published by {holder['name']!r} ({holder['profile_id']})")
            if kind == "language" and candidate.get("published"):
                holder = connection.execute(
                    "SELECT profile_id, name FROM model_profiles WHERE alias = ? AND published = 1"
                    " AND profile_id <> ?", (candidate["alias"], identifier)).fetchone()
                if holder is not None:
                    _refuse(kind, label, "alias",
                            f"is already published by {holder['name']!r} ({holder['profile_id']})")
            fields = {
                "language": ("name", "alias", "context_size", "thinking", "card_id", "published", "notes"),
                "synthesis": ("name", "card_id", "speech_executable_id", "published", "notes"),
                "recognition": ("name", "speech_executable_id", "published", "notes"),
            }[kind]
            values = dict(candidate, published=int(candidate.get("published", False)))
            if existing:
                connection.execute(
                    "UPDATE model_profiles SET " + ", ".join(f"{field} = ?" for field in fields)
                    + " WHERE profile_id = ?",
                    tuple(values[field] for field in fields) + (record_id,))
            else:
                columns = ("profile_id", "kind", *fields, "created_at")
                connection.execute(
                    "INSERT INTO model_profiles (" + ", ".join(columns) + ") VALUES ("
                    + ", ".join("?" for _ in columns) + ")",
                    (identifier, kind, *(values[field] for field in fields), candidate["created_at"]))
            connection.execute("DELETE FROM profile_arguments WHERE profile_id = ?", (identifier,))
            for position, row in enumerate(candidate.get("arguments") or []):
                catalogue_flag = row.get("catalogue")
                if isinstance(catalogue_flag, str) and catalogue_flag.strip():
                    flag = catalogue_flag
                    entry = connection.execute(
                        "SELECT argument_id FROM argument_catalogue WHERE flag = ?", (flag,)).fetchone()
                    argument_id = entry["argument_id"]
                else:
                    flag = row["option"]
                    argument_id = None
                connection.execute(
                    "INSERT INTO profile_arguments (profile_id, tool, position, argument_id, flag, value)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (identifier, row["tool"], position, argument_id, flag, row.get("value")))
            connection.execute("DELETE FROM profile_artifacts WHERE profile_id = ?", (identifier,))
            for role, _required in PICKS[kind]:
                pick = candidate.get(role)
                if pick is not None:
                    connection.execute(
                        "INSERT INTO profile_artifacts (profile_id, role, acquisition_id, file_name)"
                        " VALUES (?, ?, ?, ?)", (identifier, role, pick["acquisition_id"], pick["file_name"]))
        else:
            if existing:
                connection.execute("UPDATE bundles SET name = ?, notes = ?, load_tokens = ?,"
                                   " load_prompt = ? WHERE bundle_id = ?",
                                   (candidate["name"], candidate["notes"], candidate.get("load_tokens"),
                                    candidate.get("load_prompt"), record_id))
                connection.execute("DELETE FROM bundle_members WHERE bundle_id = ?", (record_id,))
            else:
                connection.execute("INSERT INTO bundles (bundle_id, name, notes, load_tokens,"
                                   " load_prompt, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                   (identifier, candidate["name"], candidate["notes"],
                                    candidate.get("load_tokens"), candidate.get("load_prompt"),
                                    candidate["created_at"]))
            connection.executemany(
                "INSERT INTO bundle_members (bundle_id, role, profile_id) VALUES (?, ?, ?)",
                ((identifier, role, profile_id) for role, profile_id in candidate["members"].items()))
        connection.execute("COMMIT" if outer_transaction else "RELEASE save_record")
    except sqlite3.IntegrityError as error:
        with contextlib.suppress(sqlite3.Error):
            if outer_transaction:
                connection.execute("ROLLBACK")
            else:
                connection.execute("ROLLBACK TO save_record")
                connection.execute("RELEASE save_record")
        message = str(error)
        if "published_alias" in message or "model_profiles.alias" in message:
            field, reason = "alias", "is already published by another profile"
        elif "gpu_identity" in message:
            field, reason = "gpu_identity", "is already used by another card"
        elif "UNIQUE constraint failed" in message:
            field, reason = id_field, "is already in use"
        else:
            field, reason = id_field, "could not be saved because a stored reference is invalid"
        _refuse(kind, label, field, reason)
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            if outer_transaction:
                connection.execute("ROLLBACK")
            else:
                connection.execute("ROLLBACK TO save_record")
                connection.execute("RELEASE save_record")
        raise
    saved = read_card(store, identifier) if kind == "card" else read_record(store, kind, identifier)
    if outer_transaction and (
            (kind == "language" and (saved.get("published") or
                                     (old and old.get("published")))) or
            (kind == "card" and connection.execute(
                "SELECT 1 FROM model_profiles WHERE kind = 'language' AND published = 1"
                " AND card_id = ?", (identifier,)).fetchone())):
        warnings.extend(_router_omission_warnings(regenerate_router_files(store)))
    return saved, warnings


def duplicate_record(store, record_kind, record_id, new_id, name, *, alias=None,
                     help_text=None, selector=None):
    """Copy a stored record through save validation, without its history or publication."""
    if record_kind not in PICKS and record_kind != "bundle":
        raise StoreError(f"cannot duplicate unsupported record kind {record_kind!r}")
    original = read_record(store, record_kind, record_id)
    original.pop("created_at", None)
    original.pop("qualification", None)
    if record_kind == "language":
        if alias is None:
            raise RecordRefusal(f"language profile record {new_id!r} field 'alias' is required")
        candidate = dict(original, profile_id=new_id, name=name, alias=alias, published=False)
    else:
        if alias is not None:
            raise RecordRefusal(f"{record_kind} record {new_id!r} has no alias field")
        id_field = ID_FIELDS[record_kind]
        candidate = dict(original, **{id_field: new_id}, name=name)
    return save_record(store, candidate, help_text=help_text, selector=selector,
                       _duplicate_from=record_id if record_kind == "language" else None)


DOCUMENT_VERSION = 1


def export_records(store, records=None):
    """Return the portable record document, excluding store-owned state."""
    connection = store.connection()
    if records is None:
        records = [("card", row[0]) for row in connection.execute(
            "SELECT card_id FROM cards ORDER BY card_id")]
        records += [(row["kind"], row["profile_id"]) for row in connection.execute(
            "SELECT profile_id, kind FROM model_profiles ORDER BY profile_id")]
        records += [("bundle", row[0]) for row in connection.execute(
            "SELECT bundle_id FROM bundles ORDER BY bundle_id")]
    result = []
    for kind, identifier in records:
        if kind == "card":
            record = read_card(store, identifier)
        else:
            record = read_record(store, kind, identifier)
        result.append({key: value for key, value in record.items()
                       if key not in ("created_at", "published", "qualification")})
    if not result:
        raise StoreError("no records selected for export")
    return {"version": DOCUMENT_VERSION, "records": result}


def import_records(store, document, *, help_text=None, selector=None):
    """Validate and save a portable document as one all-or-nothing operation."""
    if not isinstance(document, Mapping) or set(document) != {"version", "records"}:
        raise RecordRefusal("document fields must be exactly 'version' and 'records'")
    if type(document["version"]) is not int or document["version"] != DOCUMENT_VERSION:
        raise RecordRefusal(f"document field 'version' must be {DOCUMENT_VERSION}")
    records = document["records"]
    if not isinstance(records, list) or not records:
        raise RecordRefusal("document field 'records' must be a non-empty list")

    indexed = []
    seen = set()
    for record in records:
        kind, label = _record_kind(record)
        _known_fields(record, kind, label)
        for field in record:
            if field not in IMPORT_FIELDS[kind]:
                _refuse(kind, label, field, "is not part of the import format")
        identifier = _record_identifier(record, kind, label)
        identity = ("profile" if kind in PICKS else kind, identifier)
        if identity in seen:
            _refuse(kind, label, ID_FIELDS[kind], "appears more than once in the document")
        seen.add(identity)
        indexed.append((RECORD_KINDS.index(kind), kind, identifier, record))

    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    results = []
    router_indexes = []
    try:
        for _rank, kind, identifier, record in sorted(indexed, key=lambda item: (item[0], item[2])):
            existed = _stored_record_exists(connection, kind, identifier)
            before_changes = connection.total_changes
            saved, warnings = save_record(store, record, record_id=identifier if existed else None,
                                          help_text=help_text, selector=selector)
            action = ("created" if not existed else "edited" if connection.total_changes > before_changes
                      else "unchanged")
            if action != "unchanged" and (
                    (kind == "language" and saved.get("published")) or
                    (kind == "card" and connection.execute(
                        "SELECT 1 FROM model_profiles WHERE kind = 'language' AND published = 1"
                        " AND card_id = ?", (identifier,)).fetchone())):
                router_indexes.append(len(results))
            results.append({"kind": kind, "id": identifier, "action": action, "warnings": warnings})
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    if router_indexes:
        warnings = _router_omission_warnings(regenerate_router_files(store))
        results[router_indexes[0]]["warnings"].extend(warnings)
    return results


def _stored_record_exists(connection, kind, identifier):
    table = {"card": "cards", "bundle": "bundles"}.get(kind, "model_profiles")
    column = ID_FIELDS[kind]
    return connection.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (identifier,)).fetchone() is not None


def publish_profile(store, profile_id, *, confirmation=None):
    """Publish one language profile and regenerate its router preset as one change.

    An unqualified profile requires the exact confirmation ``Publish <name> as
    not qualified``. Qualification is checked after taking the write lock, not
    trusted from a preceding read by the caller.
    """
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    preset_path = store.path.parent / "router-presets.ini"
    previous_preset = None
    try:
        previous_preset = _router_preset_snapshot(preset_path)
        profile = _read_record(store, "language", profile_id)
        if profile["published"]:
            raise StoreError(f"language profile {profile['name']!r} is already published")
        state = qualification_state(store, profile)
        if state["state"] != "qualified":
            expected = f"Publish {profile['name']} as not qualified"
            if confirmation != expected:
                raise StoreError(f"{profile['name']} is not qualified; confirmation must name it as not qualified")
        holder = connection.execute(
            "SELECT profile_id, name FROM model_profiles WHERE kind = 'language'"
            " AND published = 1 AND alias = ?", (profile["alias"],)).fetchone()
        if holder is not None:
            _refuse("language", profile["name"], "alias",
                    f"is already published by {holder['name']!r} ({holder['profile_id']})")
        connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = ?", (profile_id,))
        warnings = _router_omission_warnings(regenerate_router_files(store))
        connection.execute("COMMIT")
    except BaseException as error:
        restore_error = None
        if previous_preset is not None:
            try:
                _restore_router_preset(preset_path, previous_preset)
            except BaseException as failure:
                restore_error = failure
        if connection.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
        if restore_error is not None:
            raise StoreError(f"{error}; failed to restore the previous router preset: {restore_error}") from error
        raise
    return read_record(store, "language", profile_id), warnings


def unpublish_profile(store, profile_id):
    """Unpublish one language profile, refusing live records and preserving history."""
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    preset_path = store.path.parent / "router-presets.ini"
    previous_preset = None
    try:
        previous_preset = _router_preset_snapshot(preset_path)
        profile = _read_record(store, "language", profile_id)
        if not profile["published"]:
            raise StoreError(f"language profile {profile['name']!r} is not published; there is nothing to unpublish")
        from matuto import live_records
        live_records.refuse_live_change(store, "language", profile_id)
        connection.execute("UPDATE model_profiles SET published = 0 WHERE profile_id = ?", (profile_id,))
        warnings = _router_omission_warnings(regenerate_router_files(store))
        connection.execute("COMMIT")
    except BaseException as error:
        restore_error = None
        if previous_preset is not None:
            try:
                _restore_router_preset(preset_path, previous_preset)
            except BaseException as failure:
                restore_error = failure
        if connection.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
        if restore_error is not None:
            raise StoreError(f"{error}; failed to restore the previous router preset: {restore_error}") from error
        raise
    return read_record(store, "language", profile_id), warnings


def remove_record(store, record_kind, record_id):
    """Remove an unreferenced card, profile or bundle while keeping its history."""
    if record_kind not in RECORD_KINDS:
        raise StoreError(f"unknown record kind {record_kind!r}")
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        record = read_card(store, record_id) if record_kind == "card" else read_record(
            store, record_kind, record_id)
        # PROF-7: a live record names Overview's Stop serving, which is why the rule
        # runs before the reference rules a removed record would otherwise reach.
        from matuto import live_records
        live_records.refuse_live_change(store, record_kind, record_id)
        holders = []
        if record_kind == "card":
            holders.extend(f"profile {row['name']!r} ({row['profile_id']})" for row in connection.execute(
                "SELECT profile_id, name FROM model_profiles WHERE card_id = ? ORDER BY name", (record_id,)))
            if connection.execute("SELECT 1 FROM runtime_selection WHERE card_id = ?", (record_id,)).fetchone():
                holders.append("runtime selection")
        elif record_kind in PICKS:
            if record.get("published"):
                holders.append("publication")
            holders.extend(f"bundle {row['name']!r} ({row['bundle_id']})" for row in connection.execute(
                "SELECT b.bundle_id, b.name FROM bundle_members m JOIN bundles b USING (bundle_id)"
                " WHERE m.profile_id = ? ORDER BY b.name", (record_id,)))
        elif record_kind == "bundle" and connection.execute(
                "SELECT 1 FROM voice_serving WHERE bundle_id = ?", (record_id,)).fetchone():
            holders.append("voice serving")
        if holders:
            raise RecordRefusal(f"cannot remove {record_kind} record {record_id!r}; held by "
                                + ", ".join(holders))
        table = {"card": "cards", "bundle": "bundles"}.get(record_kind, "model_profiles")
        column = ID_FIELDS[record_kind]
        if record_kind == "bundle":
            connection.execute("DELETE FROM bundle_members WHERE bundle_id = ?", (record_id,))
        elif record_kind in PICKS:
            connection.execute("DELETE FROM profile_arguments WHERE profile_id = ?", (record_id,))
            connection.execute("DELETE FROM profile_artifacts WHERE profile_id = ?", (record_id,))
        connection.execute(f"DELETE FROM {table} WHERE {column} = ?", (record_id,))
        connection.execute("COMMIT")
    except sqlite3.IntegrityError as error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise RecordRefusal(f"cannot remove {record_kind} record {record_id!r}; "
                            f"stored reference refused removal: {error}") from None
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return record


def read_record(store, record_kind, record_id):
    """Read one stored record, including qualification on published languages."""
    record = _read_record(store, record_kind, record_id)
    if record_kind == "language" and record["published"]:
        record["qualification"] = qualification_state(store, record)
    return record


def read_trained_context(store, profile_id):
    """Read the trained context from the selected model's GGUF metadata."""
    from matuto import gguf

    row = store.connection().execute(
        "SELECT l.path AS root, f.file_name FROM profile_artifacts AS a"
        " JOIN acquisition_files AS f USING (acquisition_id, file_name)"
        " JOIN acquisition_records AS r USING (acquisition_id)"
        " JOIN artifact_locations AS l USING (location_id)"
        " WHERE a.profile_id = ? AND a.role = 'model'", (profile_id,)).fetchone()
    if row is None:
        return None
    return gguf.trained_context(Path(row["root"]) / row["file_name"])


def launch_spec(store, record_kind, record_id, *, detected_gpus):
    """Emit one fully resolved, fail-closed launch spec from the store."""
    connection = store.connection()
    selected = connection.execute(
        "SELECT s.build_id, s.card_id, s.backend, b.location, b.recipe, b.state AS build_state,"
        " c.gpu_identity, c.default_gpu_layers, c.smoke_output_tokens,"
        " c.bench_prompt_tokens, c.bench_generation_tokens, c.bench_repetitions,"
        " l.port AS lab_port"
        " FROM runtime_selection AS s JOIN builds AS b USING (build_id)"
        " JOIN cards AS c USING (card_id) LEFT JOIN lab_port AS l ON l.setting_id = 1"
        " WHERE s.selection_id = 1").fetchone()
    if selected is None:
        raise StoreError("No runtime selection is configured in the store.")
    if selected["build_state"] == "unavailable":
        raise StoreError(f"The selected runtime build {selected['build_id']!r} is unavailable.")
    if selected["lab_port"] is None:
        raise StoreError("No lab port is configured in the store.")
    runtime = {name: selected[name] for name in
               ("build_id", "card_id", "backend", "location", "recipe", "gpu_identity")}
    result = {"record": _launch_record(store, record_kind, record_id, detected_gpus,
                                        selected, speech_ports={selected["lab_port"]}),
              "runtime": runtime, "lab_port": selected["lab_port"]}
    return result


def _launch_record(store, kind, identifier, detected_gpus, selected, *, speech_ports):
    record = read_record(store, kind, identifier)
    available, reason = record_availability(record, store.connection(), detected_gpus=detected_gpus)
    if not available:
        raise StoreError(reason or f"{kind} record {identifier!r} is unavailable")
    try:
        configuration = served_configuration(store, record)
    except StoreError as error:
        raise StoreError(str(error)) from None
    current_fingerprint = _configuration_fingerprint(configuration)
    if kind != "bundle":
        from matuto import verification
        ok, reason = verification.launch_check(store.connection(), identifier)
        if not ok:
            raise StoreError(reason)
    result = {"kind": kind, "id": identifier, "name": record["name"],
              "fingerprint": current_fingerprint,
              "availability": {"state": "available", "reason": None}}
    if kind == "bundle":
        result["qualification"] = qualification_state(store, record)
        result["load_tokens"] = record["load_tokens"]
        result["load_prompt"] = record["load_prompt"]
        result["members"] = {
            role: _launch_record(store, member_kind, profile_id, detected_gpus, selected,
                                 speech_ports=speech_ports)
            for role, profile_id in sorted(record["members"].items())
            for member_kind in ("language" if role == "language" else role,)
        }
        return result
    connection = store.connection()
    result["arguments"] = {tool: [] for tool in ("server", "smoke", "bench")}
    raw_arguments = []
    for row in connection.execute(
            "SELECT tool, flag, value FROM profile_arguments WHERE profile_id = ?"
            " ORDER BY tool, position", (identifier,)):
        raw_arguments.append({"tool": row["tool"], "option": row["flag"], "value": row["value"]})
        result["arguments"][row["tool"]].append(row["flag"])
        if row["value"] is not None:
            result["arguments"][row["tool"]].append(row["value"])
    result["argument_rows"] = raw_arguments
    result["files"] = {}
    for row in connection.execute(
            "SELECT a.role, a.file_name, f.computed_sha256, l.path AS root"
            " FROM profile_artifacts AS a"
            " JOIN acquisition_files AS f USING (acquisition_id, file_name)"
            " JOIN acquisition_records AS r USING (acquisition_id)"
            " JOIN artifact_locations AS l USING (location_id)"
            " WHERE a.profile_id = ? ORDER BY a.role", (identifier,)):
        path = Path(row["root"]) / row["file_name"]
        digest = _required_digest(row["computed_sha256"], row["file_name"])
        if not path.is_file():
            raise StoreError(f"{row['role']} file {row['file_name']!r} is missing at {path}")
        acquisition_files = []
        for acquired in connection.execute(
                "SELECT f.file_name, f.computed_sha256 FROM acquisition_files AS f"
                " WHERE f.acquisition_id = (SELECT acquisition_id FROM profile_artifacts"
                " WHERE profile_id = ? AND role = ?)", (identifier, row["role"])):
            acquired_path = Path(row["root"]) / acquired["file_name"]
            acquisition_files.append({"path": str(acquired_path.resolve()),
                                      "sha256": _required_digest(acquired["computed_sha256"],
                                                                  acquired["file_name"])})
        result["files"][row["role"]] = {"path": str(path.resolve()), "sha256": digest,
                                        "acquisition_files": acquisition_files}
    if kind == "language":
        card = connection.execute(
            "SELECT default_gpu_layers, vram_ceiling_bytes, smoke_output_tokens,"
            " bench_prompt_tokens, bench_generation_tokens, bench_repetitions"
            " FROM cards WHERE card_id = ?",
            (record["card_id"],)).fetchone()
        port = selected["lab_port"]
        _server_argv, gpu_override = _server_arguments(record.get("arguments") or (),
                                                       resolve_layers=True)
        gpu_layers = gpu_override if gpu_override is not None else card["default_gpu_layers"]
        result.update({"context_size": record["context_size"], "thinking": record["thinking"],
                       "alias": record["alias"], "gpu_layers": gpu_layers,
                       "model_format": "gguf", "server_output_tokens": record["context_size"],
                       "server_host": "127.0.0.1", "server_port": port,
                       "smoke_output_tokens": card["smoke_output_tokens"],
                       "bench_prompt_tokens": card["bench_prompt_tokens"],
                       "bench_generation_tokens": card["bench_generation_tokens"],
                       "bench_repetitions": card["bench_repetitions"],
                       "vram_ceiling_bytes": card["vram_ceiling_bytes"],
                       "qualification": qualification_state(store, record)})
        return result
    executable = connection.execute(
        "SELECT s.executable_id, s.kind, s.path, s.sha256, s.notes, l.path AS root"
        " FROM speech_executables AS s JOIN artifact_locations AS l USING (location_id)"
        " WHERE s.executable_id = ?", (record["speech_executable_id"],)).fetchone()
    if executable is None:
        raise StoreError(f"Speech executable {record['speech_executable_id']!r} is missing")
    try:
        provenance = json.loads(executable["notes"])
    except (TypeError, ValueError):
        provenance = {}
    backend, recipe = provenance.get("backend"), provenance.get("recipe")
    expected = {"synthesis": ("vulkan", recipes.BREEZE),
                "recognition": ("cpu", recipes.WHISPER)}[kind]
    if executable["kind"] != kind or (backend, recipe) != expected:
        raise StoreError(f"Speech executable {executable['executable_id']!r} has missing or inconsistent recorded backend/recipe identity")
    launch_backend = {"vulkan": "Vulkan", "cpu": "CPU"}[backend]
    executable_path = Path(executable["path"])
    if not executable_path.is_absolute():
        executable_path = Path(executable["root"]) / executable_path
    result["executable"] = {"id": executable["executable_id"],
                            "path": str(executable_path.resolve()),
                            "sha256": executable["sha256"], "backend": launch_backend, "recipe": recipe}
    result["model_path"] = result["files"].get("weights", {}).get("path", "")
    result["model_sha256"] = result["files"].get("weights", {}).get("sha256", "")
    result["host"] = "127.0.0.1"
    result["port"] = _temporary_loopback_port(speech_ports)
    speech_ports.add(result["port"])
    return result


def _temporary_loopback_port(excluded):
    import socket
    for _attempt in range(16):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in excluded:
            return port
    raise StoreError("Could not assign distinct temporary loopback ports for the bundle.")


def _read_record(store, record_kind, record_id):
    """One stored record in validate_record's save form, which the computation takes as given.

    A caller may edit the returned mapping, validate it and compute again without
    saving it, which is how an edit is previewed and how a temporary copy is
    fingerprinted.
    """
    connection = store.connection()
    if record_kind == "bundle":
        row = connection.execute(
            "SELECT bundle_id, name, notes, created_at, load_tokens, load_prompt FROM bundles"
            " WHERE bundle_id = ?", (record_id,)).fetchone()
        if row is None:
            raise StoreError(f"no bundle is stored under {record_id!r}")
        members = {member["role"]: member["profile_id"] for member in connection.execute(
            "SELECT role, profile_id FROM bundle_members WHERE bundle_id = ? ORDER BY role",
            (record_id,))}
        return dict(row, kind="bundle", members=members)
    if record_kind not in PICKS:
        raise StoreError(f"unknown record kind {record_kind!r}")
    row = connection.execute(
        "SELECT * FROM model_profiles WHERE profile_id = ?", (record_id,)).fetchone()
    if row is None:
        raise StoreError(f"no model profile is stored under {record_id!r}")
    if row["kind"] != record_kind:
        raise StoreError(f"record {record_id!r} is a {row['kind']} profile, not {record_kind}")
    # Only the kind's own fields: a recognition profile has no card, a language
    # profile no speech executable.
    record = {field: row[field] for field in RECORD_FIELDS[record_kind] if field in row.keys()}
    record["published"] = bool(row["published"])
    # A row naming a catalogue entry is a catalogue pick, any other a Custom flag.
    record["arguments"] = []
    for argument in connection.execute(
            "SELECT a.tool, a.argument_id, a.flag, a.value, c.flag AS catalogue_flag,"
            " c.takes_value, c.tools FROM profile_arguments a LEFT JOIN argument_catalogue c"
            " ON c.argument_id = a.argument_id WHERE a.profile_id = ? ORDER BY a.tool, a.position",
            (record_id,)):
        still_catalogued = (argument["argument_id"] is not None
                            and argument["catalogue_flag"] == argument["flag"]
                            and argument["tool"] in (argument["tools"] or "").split(",")
                            and bool(argument["takes_value"]) == (argument["value"] is not None))
        record["arguments"].append({
            "tool": argument["tool"],
            "catalogue" if still_catalogued else "option":
                argument["catalogue_flag"] if still_catalogued else argument["flag"],
            "value": argument["value"]})
    for artifact in connection.execute(
            "SELECT role, acquisition_id, file_name FROM profile_artifacts"
            " WHERE profile_id = ? ORDER BY role", (record_id,)):
        record[artifact["role"]] = {"acquisition_id": artifact["acquisition_id"],
                                    "file_name": artifact["file_name"]}
    return record


def router_files(store):
    """Current router inputs, with presets generated only from published profiles.

    The API key is an existing input path; this module never creates or rewrites
    it. Consumer exports are deliberately not generated here.
    """
    from matuto import storage

    connection = store.connection()
    path = store.path.parent / "router-presets.ini"
    profiles = connection.execute(
        "SELECT profile_id, alias FROM model_profiles"
        " WHERE kind = 'language' AND published = 1 ORDER BY alias, profile_id").fetchall()
    by_alias = {}
    for profile in profiles:
        by_alias.setdefault(profile["alias"], []).append(profile)
    sections = []
    reports = []
    for alias, matches in by_alias.items():
        if len(matches) > 1:
            records = [read_record(store, "language", item["profile_id"]) for item in matches]
            reason = "published profiles share this alias: " + ", ".join(
                f"{item['profile_id']} ({item['name']})" for item in records)
            reports.extend({"profile_id": item["profile_id"],
                            "qualification": item["qualification"], "reason": reason}
                           for item in records)
            continue
        profile = read_record(store, "language", matches[0]["profile_id"])
        try:
            section = _router_preset_section(store, profile)
        except StoreError as error:
            reports.append({"profile_id": profile["profile_id"],
                            "qualification": profile["qualification"], "reason": str(error)})
            continue
        sections.append(section)
        reports.append({"profile_id": profile["profile_id"],
                        "qualification": profile["qualification"], "reason": None})
    text = "version = 1\n" + ("\n".join(sections) + "\n" if sections else "")
    key_path = store.path.parent.parent / "pi-local" / "api-key"
    try:
        model_directory = storage.discovery_directory(connection)
        model_directory_reason = None
    except StoreError as error:
        model_directory = None
        model_directory_reason = str(error)
    key_reason = _router_key_reason(key_path)
    return {"presets_path": str(path), "presets": text, "profiles": reports,
            "model_directory": model_directory,
            "model_directory_reason": model_directory_reason,
            "key_path": str(key_path), "key_reason": key_reason}


def _router_key_reason(path):
    """Check the existing credential without returning or changing its bytes."""
    try:
        link = path.lstat()
    except FileNotFoundError:
        return f"the router API key is missing: {path}"
    except OSError:
        return f"the router API key cannot be inspected: {path}"
    if stat.S_ISLNK(link.st_mode):
        return f"the router API key is a link: {path}"
    if not stat.S_ISREG(link.st_mode) or link.st_uid != os.getuid() or link.st_mode & GROUP_OR_OTHER:
        return f"the router API key is unsafe: {path}"
    if link.st_nlink != 1 or not 33 <= link.st_size <= 129:
        return f"the router API key is unsafe: {path}"
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return f"the router API key cannot be opened safely: {path}"
    try:
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_mode & GROUP_OR_OTHER or status.st_nlink != 1
                or (status.st_dev, status.st_ino) != (link.st_dev, link.st_ino)):
            return f"the router API key is unsafe: {path}"
        raw = os.read(descriptor, 130)
    finally:
        os.close(descriptor)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{32,128}\n", raw):
        return f"the router API key is invalid: {path}"
    return None


def _router_omission_warnings(router):
    return [f"published profile {item['profile_id']} was omitted from the router preset: {item['reason']}"
            for item in router["profiles"] if item["reason"]]


def _router_preset_snapshot(path):
    """Read the safe previous preset, preserving its exact bytes for compensation."""
    path = Path(path)
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False, b""
    except OSError:
        raise StoreError(f"cannot inspect the generated router preset: {path}") from None
    if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
            or status.st_mode & GROUP_OR_OTHER or status.st_nlink != 1):
        raise StoreError(f"refusing unsafe generated router preset: {path}")
    try:
        raw = path.read_bytes()
        raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise StoreError(f"cannot read the generated router preset: {path}") from None
    return True, raw


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore_router_preset(path, previous):
    """Compensate a preset write when its matching SQLite transaction fails."""
    path = Path(path)
    if _router_preset_snapshot(path) == previous:
        return
    existed, raw = previous
    try:
        if existed:
            _replace_router_preset(path, raw.decode("utf-8"))
        else:
            path.unlink()
            _fsync_directory(path.parent)
    except (OSError, UnicodeError) as error:
        raise StoreError(f"cannot restore the previous router preset: {path}: {error}") from None


def regenerate_router_files(store):
    """Atomically replace the private router preset from a serialized store snapshot."""
    connection = store.connection()
    nested = connection.in_transaction
    connection.execute("SAVEPOINT regenerate_router_files" if nested else "BEGIN IMMEDIATE")
    path = store.path.parent / "router-presets.ini"
    previous = None
    changed = False
    output = None
    try:
        output = router_files(store)
        path = Path(output["presets_path"])
        previous = _router_preset_snapshot(path)
        changed = previous != (True, output["presets"].encode("utf-8"))
        if changed:
            _replace_router_preset(path, output["presets"])
        connection.execute("RELEASE regenerate_router_files" if nested else "COMMIT")
    except BaseException as error:
        restore_error = None
        if changed and previous is not None:
            try:
                _restore_router_preset(path, previous)
            except BaseException as failure:
                restore_error = failure
        if connection.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                if nested:
                    connection.execute("ROLLBACK TO regenerate_router_files")
                    connection.execute("RELEASE regenerate_router_files")
                else:
                    connection.execute("ROLLBACK")
        if restore_error is not None:
            raise StoreError(f"{error}; failed to restore the previous router preset: {restore_error}") from error
        raise
    return {key: value for key, value in output.items() if key != "presets"}


def _router_preset_section(store, profile):
    alias = profile["alias"]
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", alias):
        raise StoreError(f"published profile {profile['profile_id']!r} has an invalid alias")
    configuration = served_configuration(store, profile)
    files = profile["model"]
    model_path = _profile_file_path(store, files)
    _check_profile_files(store, files, model_path)
    predict = str(configuration["context"])
    lines = [f"[{alias}]", f"model = {model_path}",
             f"ctx-size = {configuration['context']}",
             f"n-gpu-layers = {configuration['gpu_layers']}"]
    for role, option in (("mmproj", "mmproj"), ("mtp", "spec-draft-model")):
        pick = profile.get(role)
        if pick is None:
            continue
        file_path = _profile_file_path(store, pick)
        _check_profile_files(store, pick, file_path)
        if role == "mmproj":
            lines.append(f"mmproj = {file_path}")
        else:
            lines.extend(("spec-type = draft-mtp", f"spec-draft-model = {file_path}"))
    for flag, value in configuration["server_arguments"]:
        option, separator, inline_value = flag.partition("=")
        canonical = OPTION_ALIASES.get(option, option)
        if separator and value is None:
            value = inline_value
        option = canonical
        if canonical == "--n-predict":
            predict = value if value is not None else inline_value
            if not isinstance(predict, str) or not predict or len(predict.splitlines()) != 1:
                raise StoreError(f"published profile {profile['profile_id']!r} has an invalid n-predict value")
            continue
        if canonical in {"--model", "--gpu-layers", "--ctx-size",
                         "--alias", "--api-key", "--api-key-file", "--host", "--port",
                         "--models-dir", "--models-max", "--models-preset"}:
            raise StoreError(f"published profile {profile['profile_id']!r} sets router-owned option {option}")
        if canonical in {"--mmproj", "--spec-draft-model", "--spec-type"}:
            raise StoreError(f"published profile {profile['profile_id']!r} sets a file option owned by its artifact picks: {option}")
        if not option.startswith("--") or not re.fullmatch(r"--[A-Za-z0-9][A-Za-z0-9-]*", option):
            raise StoreError(f"published profile {profile['profile_id']!r} has invalid server option {option!r}")
        key = option[2:]
        value = "true" if value is None or (value == "" and not separator) else value
        if not isinstance(value, str) or not value or len(value.splitlines()) != 1:
            raise StoreError(f"published profile {profile['profile_id']!r} has an invalid value for {option}")
        lines.append(f"{key} = {value}")
    lines.insert(4, f"n-predict = {predict}")
    return "\n".join(lines)


def _check_profile_files(store, pick, selected_path):
    connection = store.connection()
    siblings = {row["file_name"]: row["verified_at"] for row in connection.execute(
        "SELECT file_name, verified_at FROM acquisition_files WHERE acquisition_id = ?",
        (pick["acquisition_id"],))}
    if pick["file_name"] not in siblings or siblings[pick["file_name"]] is None:
        raise StoreError(f"file {pick['file_name']!r} has no complete acquisition record")
    shards = _shard_family(pick["file_name"], siblings)
    names = [name for _shard, name in shards] if shards else [pick["file_name"]]
    for name in names:
        if siblings.get(name) is None:
            raise StoreError(f"required shard {name!r} has no complete acquisition record")
        path = selected_path if name == pick["file_name"] else _profile_file_path(
            store, {"acquisition_id": pick["acquisition_id"], "file_name": name})
        if not path.is_file() or not os.access(path, os.R_OK):
            raise StoreError(f"required file {name!r} is unavailable at {path}")
        if len(str(path).splitlines()) != 1:
            raise StoreError(f"required file {name!r} has an unsafe preset path")


def _profile_file_path(store, pick):
    row = store.connection().execute(
        "SELECT l.path FROM acquisition_records AS r"
        " JOIN artifact_locations AS l USING (location_id)"
        " JOIN acquisition_files AS f USING (acquisition_id)"
        " WHERE r.acquisition_id = ? AND f.file_name = ?"
        " AND r.verified_at IS NOT NULL AND f.verified_at IS NOT NULL",
        (pick["acquisition_id"], pick["file_name"])).fetchone()
    if row is None:
        raise StoreError(f"file {pick['file_name']!r} has no complete acquisition record")
    filename = Path(pick["file_name"])
    if filename.is_absolute() or ".." in filename.parts or not Path(row["path"]).is_absolute():
        raise StoreError(f"file {pick['file_name']!r} has no safe absolute acquisition path")
    return Path(row["path"]) / filename


def _replace_router_preset(path, text):
    descriptor = None
    temporary = None
    try:
        if path.exists() or path.is_symlink():
            existing = path.lstat()
            if (not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid()
                    or existing.st_mode & GROUP_OR_OTHER or existing.st_nlink != 1):
                raise StoreError(f"refusing unsafe generated router preset: {path}")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except StoreError:
        raise
    except OSError:
        raise StoreError(f"cannot atomically write the generated router preset: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def served_configuration(store, record):
    """The values that decide what one record launches, or a refusal naming what is missing.

    Only served values are here: names, notes, aliases, smoke and bench
    measurement settings, a card's name and VRAM ceiling, and the llama.cpp build
    are never part of it. Each required file contributes its recorded SHA-256,
    never a path, directory or acquisition-record ID, so identical bytes at
    another location keep the configuration and other bytes replace it.
    """
    kind = record.get("kind")
    if kind == "language":
        return _language_served_configuration(store, record)
    if kind in ("synthesis", "recognition"):
        return _speech_served_configuration(store, record)
    if kind == "bundle":
        return _bundle_served_configuration(store, record)
    raise StoreError(f"unknown record kind {kind!r}, so no served configuration")


def fingerprint(store, record):
    """One record's fingerprint, or None when its configuration cannot be computed.

    The canonical form fixes key and argument order, so the same configuration
    gives the same digest in every process and after the store is reopened,
    whatever order the database returns its rows in. A configuration that cannot
    be computed in full gives no fingerprint; served_configuration and
    qualification_state name the reason.
    """
    try:
        return _configuration_fingerprint(served_configuration(store, record))
    except StoreError:
        return None


def _configuration_fingerprint(configuration):
    canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return FINGERPRINT_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def qualification_state(store, record):
    """Computed qualification from the non-withdrawn acceptances, never stored.

    A language profile or bundle is qualified exactly when an accepted
    qualification record matches the fingerprint of its current served
    configuration; otherwise it is a candidate, marked when an earlier
    fingerprint of it was accepted. A qualified record whose accepted evidence
    was recorded on another build than the active one stays qualified and carries
    a warning. A served configuration that cannot be computed in full gives no
    fingerprint, never reads as qualified and names the reason.
    """
    kind = record.get("kind")
    if kind not in QUALIFICATION_KINDS:
        raise StoreError(f"a {kind or 'unknown'} record has no qualification state of its own")
    record_id = record.get("bundle_id") if kind == "bundle" else record.get("profile_id")
    if not record_id:
        raise StoreError(f"a {kind} record without an ID has no qualification state")
    try:
        current = _configuration_fingerprint(served_configuration(store, record))
    except StoreError as error:
        return {"state": "unavailable", "reason": str(error)}
    accepted = _accepted_qualifications(store, kind, record_id)
    matching = [entry for entry in accepted if entry["fingerprint"] == current]
    if matching:
        state = {"state": "qualified", "fingerprint": current}
        warning = _different_build_warning(store, matching)
        if warning:
            state["warning"] = warning
        return state
    return {"state": "candidate", "fingerprint": current,
            "changed_since_qualification": bool(accepted)}


def accept(store, record_kind, record_id, run_ids, scope, exceptions, residual_risks,
           accepted_by, *, now=None):
    """Atomically accept completed evidence for one current language profile or bundle."""
    connection = store.connection()
    if record_kind not in QUALIFICATION_KINDS:
        raise StoreError(f"a {record_kind!r} record cannot be accepted")
    if not isinstance(record_id, str) or not record_id.strip():
        raise StoreError("acceptance needs a record ID")
    if not isinstance(scope, str) or not scope.strip():
        raise StoreError("acceptance scope must be a non-empty string")
    if not isinstance(residual_risks, str):
        raise StoreError("acceptance residual risks must be a string")
    if not isinstance(accepted_by, str) or not accepted_by.strip():
        raise StoreError("acceptance needs the signed-in login username")
    if not isinstance(run_ids, (list, tuple)) or not run_ids:
        raise StoreError("acceptance needs at least one cited run")
    if any(not isinstance(run_id, str) or not run_id.strip() for run_id in run_ids):
        raise StoreError("each cited run needs a run ID")
    if len(set(run_ids)) != len(run_ids):
        raise StoreError("a cited run may appear only once")
    if not isinstance(exceptions, Mapping):
        raise StoreError("named exceptions must map failed check names to reasons")
    for name, reason in exceptions.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(reason, str) or not reason.strip():
            raise StoreError("each named exception needs a check name and a non-empty reason")

    connection.execute("BEGIN IMMEDIATE")
    try:
        record = _read_record(store, record_kind, record_id)
        current_fingerprint = fingerprint(store, record)
        if current_fingerprint is None:
            try:
                served_configuration(store, record)
            except StoreError as error:
                raise StoreError(f"cannot accept {record_kind} {record_id!r}: {error}") from None
            raise StoreError(f"cannot accept {record_kind} {record_id!r}: its fingerprint is unavailable")

        cited = []
        all_failed_checks = set()
        for run_id in run_ids:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise StoreError(f"run {run_id!r} cannot be cited: it is not recorded")
            if run["record_kind"] != record_kind or run["record_id"] != record_id:
                raise StoreError(f"run {run_id!r} cannot be cited: it belongs to another record")
            if run["outcome"] != "completed":
                raise StoreError(f"run {run_id!r} cannot be cited: its outcome is {run['outcome']!r}, not completed")
            if run["dirty_checkout"]:
                raise StoreError(f"run {run_id!r} cannot be cited: it came from a dirty checkout")
            if not run["fingerprint"] or run["fingerprint"] != current_fingerprint:
                raise StoreError(f"run {run_id!r} cannot be cited: its fingerprint does not match the record's current fingerprint")
            if not run["build_id"]:
                raise StoreError(f"run {run_id!r} cannot be cited: its llama.cpp build was not recorded")
            checks = connection.execute(
                "SELECT name, status FROM checks WHERE run_id = ? ORDER BY sequence", (run_id,)).fetchall()
            if run["procedure"] in ("smoke", "bundle") and any(
                    check["status"] != "PASS" for check in checks):
                raise StoreError(f"run {run_id!r} cannot be cited: required {run['procedure']} checks did not all pass")
            cited.append((run, checks))
            all_failed_checks.update(check["name"] for check in checks
                                     if check["status"] in ("FAIL", "ERROR"))

        if record_kind == "language":
            smoke = [checks for run, checks in cited if run["procedure"] == "smoke"]
            bench = [checks for run, checks in cited if run["procedure"] == "bench"]
            if not any(checks and all(check["status"] == "PASS" for check in checks)
                       for checks in smoke):
                raise StoreError("language acceptance needs a cited smoke run whose checks passed")
            if not bench:
                raise StoreError("language acceptance needs a cited completed bench run")
        else:
            bundles = [checks for run, checks in cited if run["procedure"] == "bundle"]
            if not any(checks and all(check["status"] == "PASS" for check in checks)
                       for checks in bundles):
                raise StoreError("bundle acceptance needs a cited bundle run whose checks passed")

        if set(exceptions) != all_failed_checks:
            missing = sorted(all_failed_checks - set(exceptions))
            extra = sorted(set(exceptions) - all_failed_checks)
            detail = []
            if missing:
                detail.append("failed checks need named exceptions: " + ", ".join(missing))
            if extra:
                detail.append("exceptions name no failed cited check: " + ", ".join(extra))
            raise StoreError("acceptance refused: " + "; ".join(detail))

        active = connection.execute(
            "SELECT acceptances.*, qualification_records.record_kind AS record_kind,"
            " qualification_records.record_id AS record_id,"
            " qualification_records.record_label AS record_label,"
            " qualification_records.fingerprint AS fingerprint,"
            " qualification_records.build_id AS build_id,"
            " qualification_records.created_at AS created_at"
            " FROM acceptances JOIN qualification_records USING (qualification_id)"
            " WHERE qualification_records.record_kind = ? AND qualification_records.record_id = ?"
            " AND qualification_records.fingerprint = ? AND acceptances.withdrawn_at IS NULL"
            " ORDER BY acceptances.accepted_at, acceptances.acceptance_id LIMIT 1",
            (record_kind, record_id, current_fingerprint)).fetchone()
        if active is not None:
            existing_runs = {row["run_id"] for row in connection.execute(
                "SELECT run_id FROM acceptance_runs WHERE acceptance_id = ?", (active["acceptance_id"],))}
            existing_exceptions = {row["check_name"]: row["reason"] for row in connection.execute(
                "SELECT check_name, reason FROM acceptance_exceptions WHERE acceptance_id = ?",
                (active["acceptance_id"],))}
            if (active["scope"] != scope or active["residual_risks"] != residual_risks
                    or active["accepted_by"] != accepted_by or existing_runs != set(run_ids)
                    or existing_exceptions != dict(exceptions)):
                raise StoreError(
                    f"{record_kind} {record_id!r} already has active acceptance "
                    f"{active['acceptance_id']!r} for this fingerprint")
            accepted_state = qualification_state(store, record)
            connection.execute("COMMIT")
            result = dict(active, run_ids=list(run_ids), exceptions=dict(exceptions))
            if accepted_state.get("warning"):
                result["warning"] = accepted_state["warning"]
            return result

        accepted_at = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        qualification_id = "qualification-" + uuid.uuid4().hex
        acceptance_id = "acceptance-" + uuid.uuid4().hex
        build_ids = {run["build_id"] for run, _checks in cited}
        qualification_build = next(iter(build_ids)) if len(build_ids) == 1 else None
        row = connection.execute(
            "INSERT INTO qualification_records (qualification_id, record_kind, record_id,"
            " record_label, fingerprint, build_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " RETURNING *",
            (qualification_id, record_kind, record_id, record.get("name", record_id),
             current_fingerprint, qualification_build, accepted_at)).fetchone()
        for run, _checks in cited:
            connection.execute("INSERT INTO qualification_runs VALUES (?, ?)",
                               (qualification_id, run["run_id"]))
        connection.execute(
            "INSERT INTO acceptances VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (acceptance_id, qualification_id, scope, residual_risks, accepted_by, accepted_at))
        for run, _checks in cited:
            connection.execute("INSERT INTO acceptance_runs VALUES (?, ?, ?, ?)",
                               (acceptance_id, run["run_id"], run["fingerprint"], run["build_id"]))
        connection.executemany(
            "INSERT INTO acceptance_exceptions VALUES (?, ?, ?)",
            ((acceptance_id, name, reason) for name, reason in exceptions.items()))
        accepted_state = qualification_state(store, record)
        connection.execute("COMMIT")
        result = dict(row, acceptance_id=acceptance_id, scope=scope,
                    residual_risks=residual_risks, accepted_by=accepted_by,
                    accepted_at=accepted_at, withdrawn_at=None,
                    run_ids=list(run_ids), exceptions=dict(exceptions))
        if accepted_state.get("warning"):
            result["warning"] = accepted_state["warning"]
        return result
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def withdraw(store, acceptance_id, *, now=None):
    """Withdraw an acceptance without deleting its history or cited-run references."""
    if not isinstance(acceptance_id, str) or not acceptance_id.strip():
        raise StoreError("withdrawal needs an acceptance ID")
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            "SELECT * FROM acceptances WHERE acceptance_id = ?", (acceptance_id,)).fetchone()
        if row is None:
            raise StoreError(f"acceptance {acceptance_id!r} is not recorded")
        if row["withdrawn_at"] is None:
            withdrawn_at = now or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            connection.execute("UPDATE acceptances SET withdrawn_at = ? WHERE acceptance_id = ?",
                               (withdrawn_at, acceptance_id))
        connection.execute("COMMIT")
        return dict(connection.execute(
            "SELECT * FROM acceptances WHERE acceptance_id = ?", (acceptance_id,)).fetchone())
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def refuse_accepted_run_deletion(store, run_id):
    """Name the acceptance blocking deletion before a caller removes a run."""
    row = store.connection().execute(
        "SELECT acceptance_id FROM acceptance_runs WHERE run_id = ?"
        " ORDER BY acceptance_id LIMIT 1", (run_id,)).fetchone()
    if row is not None:
        raise StoreError(
            f"run {run_id!r} cannot be deleted: acceptance {row['acceptance_id']!r} cites it")

def preview_edit(store, before, after):
    """Describe an unsaved record edit and the records whose fingerprints it changes."""
    kind = after.get("kind")
    if before.get("kind") != kind or kind not in (
            "language", "synthesis", "recognition", "bundle", "card"):
        raise StoreError("the before and after records must have the same supported kind")
    if _record_identity(kind, before) != _record_identity(kind, after):
        raise StoreError("an edit preview must keep the record's identity")

    changed = _preview_values(store, kind, before, after)
    before_reason = after_reason = None
    if kind != "card":
        for record, label in ((before, "before"), (after, "after")):
            try:
                served_configuration(store, record)
            except StoreError as error:
                if label == "before":
                    before_reason = str(error)
                else:
                    after_reason = str(error)
    reason = after_reason or before_reason
    is_unchanged = before == after
    if kind == "card":
        affected = _card_preview_affected(store, before, after)
        state = None
    else:
        state = (qualification_state(store, after)
                 if kind in QUALIFICATION_KINDS else None)
        affected = (_profile_preview_affected(store, kind, before, after, force=bool(changed))
                    if kind in ("language", "synthesis", "recognition") else [])
    return {"kind": "unavailable" if after_reason else "unchanged" if is_unchanged else
            "cosmetic-only" if not changed else "served-change",
            "changed_values": changed, "state": state, "reason": reason,
            "affected": affected}


def _record_identity(kind, record):
    return record.get("bundle_id" if kind == "bundle" else
                      "card_id" if kind == "card" else "profile_id")


def _preview_values(store, kind, before, after):
    if kind == "card":
        fields = (("gpu_identity", "Physical GPU"),
                  ("default_gpu_layers", "Default GPU layers"))
        return [{"field": key, "label": label, "old": before.get(key), "new": after.get(key)}
                for key, label in fields if before.get(key) != after.get(key)]
    try:
        old = served_configuration(store, before)
        old_error = None
    except StoreError as error:
        old, old_error = None, str(error)
    try:
        new = served_configuration(store, after)
        new_error = None
    except StoreError as error:
        new, new_error = None, str(error)
    if old_error or new_error:
        if old_error and new_error:
            return _uncomputable_preview_values(kind, before, after)
        return [{"field": "served_configuration", "label": "Served configuration",
                 "old": {"unavailable": old_error} if old_error else old,
                 "new": {"unavailable": new_error} if new_error else new}]
    if old == new and kind != "bundle":
        return []
    labels = {
        "context": "Context size", "thinking": "Thinking",
        "gpu_layers": "GPU layers", "card_gpu": "Card GPU",
        "speech_executable": "Speech executable", "weights": "Weights",
        "members": "Bundle members", "stated_load": "Bundle load",
    }
    changed = []
    for key in sorted(old.keys() | new.keys()):
        old_value, new_value = old.get(key), new.get(key)
        if old_value == new_value:
            continue
        if key == "server_arguments":
            old_by_flag = {row[0]: row for row in old_value or ()}
            new_by_flag = {row[0]: row for row in new_value or ()}
            for flag in sorted(old_by_flag.keys() | new_by_flag.keys()):
                old_row, new_row = old_by_flag.get(flag), new_by_flag.get(flag)
                if old_row != new_row:
                    changed.append({"field": f"server_arguments.{flag}", "label": flag,
                                    "old": old_row, "new": new_row})
            old_order = [row[0] for row in old_value or ()]
            new_order = [row[0] for row in new_value or ()]
            if old_order != new_order and old_by_flag.keys() == new_by_flag.keys():
                changed.append({"field": "server_argument_order", "label": "Argument order",
                                "old": old_order, "new": new_order})
        elif key == "files":
            for role in sorted((old_value or {}).keys() | (new_value or {}).keys()):
                old_files, new_files = (old_value or {}).get(role), (new_value or {}).get(role)
                if old_files != new_files:
                    old_pick, new_pick = before.get(role), after.get(role)
                    changed.append({"field": role, "label": f"{role.title()} file",
                                    "old": {"file_name": old_pick.get("file_name"),
                                            "sha256": old_files} if old_pick else None,
                                    "new": {"file_name": new_pick.get("file_name"),
                                            "sha256": new_files} if new_pick else None})
        elif key == "gpu_layers":
            changed.append({"field": key, "label": labels[key],
                            "old": "all" if old_value == -1 else old_value,
                            "new": "all" if new_value == -1 else new_value})
        elif key == "speech_executable":
            changed.append({"field": "speech_executable_id", "label": labels[key],
                            "old": before.get("speech_executable_id"),
                            "new": after.get("speech_executable_id")})
        elif key == "members":
            old_members, new_members = before.get("members") or {}, after.get("members") or {}
            for role in sorted((old_value or {}).keys() | (new_value or {}).keys()):
                old_member, new_member = old_members.get(role), new_members.get(role)
                old_pick = old_member.get("profile_id") if isinstance(old_member, Mapping) else old_member
                new_pick = new_member.get("profile_id") if isinstance(new_member, Mapping) else new_member
                if old_pick != new_pick:
                    changed.append({"field": f"members.{role}",
                                    "label": f"{role.title()} profile",
                                    "old": old_pick, "new": new_pick})
                elif old_pick and new_pick:
                    old_record = (old_member if isinstance(old_member, Mapping)
                                  else read_record(store, role, old_pick))
                    new_record = (new_member if isinstance(new_member, Mapping)
                                  else read_record(store, role, new_pick))
                    for member_change in _preview_values(store, role, old_record, new_record):
                        changed.append({
                            "field": f"members.{role}.{member_change['field']}",
                            "label": f"{role.title()} profile: {member_change['label']}",
                            "old": member_change["old"], "new": member_change["new"]})
        else:
            changed.append({"field": key, "label": labels.get(key, key),
                            "old": old_value, "new": new_value})
    if kind == "bundle":
        old_members, new_members = before.get("members") or {}, after.get("members") or {}
        for role in ("language", "synthesis", "recognition"):
            old_member, new_member = old_members.get(role), new_members.get(role)
            old_pick = old_member.get("profile_id") if isinstance(old_member, Mapping) else old_member
            new_pick = new_member.get("profile_id") if isinstance(new_member, Mapping) else new_member
            field = f"members.{role}"
            if old_pick != new_pick and not any(row["field"] == field for row in changed):
                changed.append({"field": field, "label": f"{role.title()} profile",
                                "old": old_pick, "new": new_pick})
    return changed


def _uncomputable_preview_values(kind, before, after):
    fields = {
        "language": (("context_size", "Context size"), ("thinking", "Thinking"),
                     ("card_id", "Card"), ("model", "Model file"),
                     ("mmproj", "Projector file"), ("mtp", "MTP file")),
        "synthesis": (("speech_executable_id", "Speech executable"),
                      ("card_id", "Card"), ("weights", "Weights")),
        "recognition": (("speech_executable_id", "Speech executable"), ("weights", "Weights")),
        "bundle": (("members", "Bundle members"), ("load_tokens", "Load tokens"),
                   ("load_prompt", "Load prompt")),
    }
    changed = [{"field": field, "label": label, "old": before.get(field),
                "new": after.get(field)}
               for field, label in fields[kind] if before.get(field) != after.get(field)]
    old_arguments = {row[0]: row for row in _raw_server_arguments(before)}
    new_arguments = {row[0]: row for row in _raw_server_arguments(after)}
    for flag in sorted(old_arguments.keys() | new_arguments.keys()):
        old_row, new_row = old_arguments.get(flag), new_arguments.get(flag)
        if old_row != new_row:
            changed.append({"field": f"server_arguments.{flag}", "label": flag,
                            "old": old_row, "new": new_row})
    try:
        old_served_arguments, _ = _server_arguments(
            before.get("arguments") or (), resolve_layers=kind == "language")
        new_served_arguments, _ = _server_arguments(
            after.get("arguments") or (), resolve_layers=kind == "language")
    except StoreError:
        pass
    else:
        old_order = [row[0] for row in old_served_arguments]
        new_order = [row[0] for row in new_served_arguments]
        old_flags = {row[0] for row in old_served_arguments}
        new_flags = {row[0] for row in new_served_arguments}
        if old_order != new_order and old_flags == new_flags:
            changed.append({"field": "server_argument_order", "label": "Argument order",
                            "old": old_order, "new": new_order})
    if "card" in before or "card" in after:
        card_fields = ("gpu_identity", "default_gpu_layers") if kind == "language" else (
            ("gpu_identity",) if kind == "synthesis" else ())
        for field in card_fields:
            old_card, new_card = before.get("card") or {}, after.get("card") or {}
            if old_card.get(field) != new_card.get(field):
                changed.append({"field": f"card.{field}", "label": field,
                                "old": old_card.get(field), "new": new_card.get(field)})
    return changed


def _raw_server_arguments(record):
    return [[row.get("catalogue") or row.get("option"), row.get("value")]
            for row in record.get("arguments") or () if row.get("tool") == "server"]


def _profile_preview_affected(store, kind, before, after, force=False):
    profile_id = _record_identity(kind, before)
    affected = []
    connection = store.connection()
    bundles = connection.execute(
        "SELECT DISTINCT bundle_id FROM bundle_members WHERE profile_id = ? ORDER BY bundle_id",
        (profile_id,)).fetchall()
    for row in bundles:
        stored_bundle = read_record(store, "bundle", row["bundle_id"])
        old_bundle = _bundle_with_profile(stored_bundle, kind, profile_id, before)
        new_bundle = _bundle_with_profile(stored_bundle, kind, profile_id, after)
        _append_if_changed(store, affected, "bundle", old_bundle, new_bundle, force=force)
    return affected


def _bundle_with_profile(bundle, kind, profile_id, replacement):
    members = dict(bundle["members"])
    member = members.get(kind)
    member_id = member.get("profile_id") if isinstance(member, Mapping) else member
    if member_id == profile_id:
        members[kind] = replacement
    return dict(bundle, members=members)


def _card_preview_affected(store, before, after):
    card_id = _record_identity("card", before)
    changed_fields = {item["field"] for item in _preview_values(store, "card", before, after)}
    if not changed_fields:
        return []
    force_profiles = "gpu_identity" in changed_fields
    affected = []
    rows = store.connection().execute(
        "SELECT profile_id, kind FROM model_profiles WHERE card_id = ? ORDER BY profile_id",
        (card_id,)).fetchall()
    replacements = {}
    for row in rows:
        stored_profile = read_record(store, row["kind"], row["profile_id"])
        old_profile = dict(stored_profile, card=before)
        new_profile = dict(stored_profile, card=after)
        force = force_profiles
        if not force and "default_gpu_layers" in changed_fields and row["kind"] == "language":
            try:
                force = (_server_arguments(old_profile.get("arguments") or (),
                                           resolve_layers=True)[1] is None)
            except StoreError:
                force = True
        if _append_if_changed(store, affected, row["kind"], old_profile, new_profile,
                              force=force):
            replacements[row["profile_id"]] = (row["kind"], old_profile, new_profile)
    bundle_ids = set()
    for profile_id in replacements:
        bundle_ids.update(row[0] for row in store.connection().execute(
            "SELECT bundle_id FROM bundle_members WHERE profile_id = ?", (profile_id,)))
    for bundle_id in sorted(bundle_ids):
        old_bundle = read_record(store, "bundle", bundle_id)
        new_bundle = dict(old_bundle, members=dict(old_bundle["members"]))
        for profile_id, (kind, old_profile, new_profile) in replacements.items():
            old_bundle = _bundle_with_profile(old_bundle, kind, profile_id, old_profile)
            new_bundle = _bundle_with_profile(new_bundle, kind, profile_id, new_profile)
        _append_if_changed(store, affected, "bundle", old_bundle, new_bundle, force=True)
    return affected


def _append_if_changed(store, affected, kind, before, after, force=False):
    before_fingerprint = fingerprint(store, before)
    after_fingerprint = fingerprint(store, after)
    if before_fingerprint == after_fingerprint and (after_fingerprint is not None or not force):
        return False
    state = (qualification_state(store, after)
             if kind in QUALIFICATION_KINDS else None)
    item = {"kind": kind, "id": _record_identity(kind, after), "state": state}
    if after_fingerprint is None:
        try:
            served_configuration(store, after)
        except StoreError as error:
            item["reason"] = str(error)
    affected.append(item)
    return True


def _language_served_configuration(store, record):
    context = record.get("context_size")
    if context is None:
        raise StoreError("a language profile without a context size has no served configuration")
    card = _card(store, record)
    arguments, gpu_layers = _server_arguments(record.get("arguments") or (), resolve_layers=True)
    if gpu_layers is None:
        gpu_layers = card["default_gpu_layers"]
    return {"kind": "language", "context": context, "thinking": record.get("thinking"),
            "server_arguments": arguments, "gpu_layers": gpu_layers,
            "card_gpu": card["gpu_identity"],
            "files": _required_files(store, record)}


def _speech_served_configuration(store, record):
    kind = record["kind"]
    executable_id = record.get("speech_executable_id")
    if not executable_id:
        raise StoreError(f"a {kind} profile without a speech executable has no served configuration")
    executable = store.connection().execute(
        "SELECT sha256 FROM speech_executables WHERE executable_id = ?",
        (executable_id,)).fetchone()
    if executable is None:
        raise StoreError(f"a {kind} profile names speech executable {executable_id!r}, which is not recorded")
    if not executable["sha256"]:
        raise StoreError(f"the speech executable {executable_id!r} holds no SHA-256")
    arguments, _layers = _server_arguments(record.get("arguments") or (), resolve_layers=False)
    configuration = {"kind": kind, "speech_executable": executable["sha256"],
                     "weights": _required_files(store, record)["weights"],
                     "server_arguments": arguments}
    if kind == "synthesis":
        configuration["card_gpu"] = _card(store, record)["gpu_identity"]
    return configuration


def _bundle_served_configuration(store, record):
    members = record.get("members") or {}
    fingerprints = {}
    for role in ("language", "synthesis", "recognition"):
        member = members.get(role)
        if not member:
            raise StoreError(f"a bundle without its {role} member has no served configuration")
        if isinstance(member, dict):
            # A caller supplies a member as given, so a temporary copy of a bundle
            # (a context-search rung, for example) needs no saved member record.
            if member.get("kind") != role:
                raise StoreError(f"a bundle's {role} member is not a {role} record")
            fingerprints[role] = _configuration_fingerprint(served_configuration(store, member))
        else:
            fingerprints[role] = _configuration_fingerprint(
                served_configuration(store, read_record(store, role, member)))
    if record.get("load_tokens") is None or record.get("load_prompt") is None:
        raise StoreError("a bundle without its stated load has no served configuration")
    return {"kind": "bundle", "members": fingerprints,
            "stated_load": {"tokens": record["load_tokens"], "prompt": record["load_prompt"]}}


def _card(store, record):
    """The record's card served values: explicit values win, otherwise its pointer."""
    explicit = record.get("card")
    if explicit is not None:
        if not explicit.get("gpu_identity") or explicit.get("default_gpu_layers") is None:
            raise StoreError(f"the card of a {record.get('kind')} profile is incomplete")
        return {"gpu_identity": explicit["gpu_identity"],
                "default_gpu_layers": (-1 if explicit["default_gpu_layers"] == "all"
                                       else explicit["default_gpu_layers"])}
    card_id = record.get("card_id")
    if not card_id:
        raise StoreError(f"a {record.get('kind')} profile without its card has no served configuration")
    card = store.connection().execute(
        "SELECT gpu_identity, default_gpu_layers FROM cards WHERE card_id = ?",
        (card_id,)).fetchone()
    if card is None:
        raise StoreError(f"a {record.get('kind')} profile names card {card_id!r}, which is not recorded")
    return {"gpu_identity": card["gpu_identity"], "default_gpu_layers": card["default_gpu_layers"]}


def _server_arguments(rows, resolve_layers):
    """The server rows as positional argv pairs, with a GPU-layer override resolved out."""
    arguments, gpu_layers = [], None
    # List order is argv order, as in the save form; read_record orders by position.
    for row in (row for row in rows if row.get("tool") == "server"):
        flag, value = row.get("catalogue") or row.get("option"), row.get("value")
        # A flag may carry its operand inline, as --gpu-layers=60 does.
        spelling, inline, operand = (flag or "").partition("=")
        if resolve_layers and OPTION_ALIASES.get(spelling, spelling) == GPU_LAYER_CANONICAL:
            gpu_layers = _gpu_layer_count(flag, operand if inline else value)
            continue
        arguments.append([flag, value])
    return arguments, gpu_layers


def _gpu_layer_count(flag, value):
    text = "" if value is None else str(value).strip()
    try:
        # isdigit alone accepts digits int() refuses, such as a superscript two.
        count = int(text) if text.isascii() and text.isdigit() else 0
    except ValueError:  # more digits than int() converts
        count = 0
    if count <= 0:
        raise StoreError(f"a server argument {flag} carries {value!r}, not a positive GPU-layer count")
    return count


def _required_files(store, record):
    """Each selected role's required files, by logical position, never by path.

    A role names one acquisition record and one file the profile picked. For a
    sharded GGUF set every shard of that set is required, so each contributes the
    digest at its shard number; a file outside a sharded set is its own single
    required file. The file the profile did not select, such as a sidecar in the
    same acquisition record, is not part of this configuration.
    """
    files = {}
    for role, required in PICKS[record["kind"]]:
        pick = record.get(role)
        if pick is None:
            if required:
                raise StoreError(f"a {record['kind']} profile with no {role} file"
                                 " has no served configuration")
            continue
        acquisition_id, file_name = pick.get("acquisition_id"), pick.get("file_name")
        if not acquisition_id or not file_name:
            raise StoreError(f"the {role} file names no acquisition record and file")
        files[role] = _artifact_files(store, acquisition_id, file_name, role)
    return files


# The GGUF convention for a model published as shards: <stem>-NNNNN-of-MMMMM.gguf.
_SHARD_NAME = re.compile(r"^(?P<stem>.+)-(?P<index>[0-9]+)-of-(?P<count>[0-9]+)\.gguf$")


def _artifact_files(store, acquisition_id, file_name, role):
    """One role's required files as {shard, sha256} entries, ordered by shard number.

    Only the shard number enters the result, never the file name, directory or
    acquisition-record ID, so identical bytes elsewhere keep the configuration
    while different bytes at a required shard position replace it. Every shard of
    the picked file's set is required, so a missing or undigested shard fails
    closed.
    """
    rows = store.connection().execute(
        "SELECT file_name, computed_sha256 FROM acquisition_files"
        " WHERE acquisition_id = ?", (acquisition_id,)).fetchall()
    recorded = {row["file_name"]: row["computed_sha256"] for row in rows}
    if not recorded:
        raise StoreError(f"the {role} file {file_name!r} has no acquisition record {acquisition_id!r}")
    if file_name not in recorded:
        raise StoreError(f"the {role} file {file_name!r} is not in acquisition record {acquisition_id!r}")
    shards = _shard_family(file_name, recorded)
    if shards is None:
        shards = [(1, file_name)]
    return [{"shard": shard, "sha256": _required_digest(recorded[name], name)}
            for shard, name in shards]


def _shard_family(file_name, recorded):
    """The picked file's whole shard set, or None when it is not one shard of a set."""
    match = _SHARD_NAME.match(file_name)
    if match is None:
        return None
    stem, count = match.group("stem"), int(match.group("count"))
    by_shard = {}
    for name in recorded:
        sibling = _SHARD_NAME.match(name)
        if (sibling is not None and sibling.group("stem") == stem
                and int(sibling.group("count")) == count):
            shard = int(sibling.group("index"))
            # Two names for one position would make its digest depend on row order.
            if shard in by_shard:
                raise StoreError(f"the acquisition record for {file_name!r} holds shard {shard} twice")
            by_shard[shard] = name
    # Checked against the recorded shards, never by building the declared count:
    # an acquired file name can declare any count.
    if len(by_shard) != count or not all(1 <= shard <= count for shard in by_shard):
        raise StoreError(f"the acquisition record for {file_name!r} is missing a required shard")
    return sorted(by_shard.items())


def _required_digest(value, file_name):
    digest = (value or "").strip()
    if not digest:
        raise StoreError(f"the acquisition record for {file_name!r} holds no computed SHA-256")
    return digest


def _accepted_qualifications(store, record_kind, record_id):
    rows = store.connection().execute(
        "SELECT DISTINCT qualification_records.qualification_id,"
        " qualification_records.fingerprint, qualification_records.build_id,"
        " qualification_records.created_at"
        " FROM qualification_records"
        " JOIN acceptances ON acceptances.qualification_id = qualification_records.qualification_id"
        " WHERE qualification_records.record_kind = ? AND qualification_records.record_id = ?"
        " AND acceptances.withdrawn_at IS NULL"
        " ORDER BY qualification_records.created_at, qualification_records.qualification_id",
        (record_kind, record_id)).fetchall()
    return [{"qualification_id": row["qualification_id"], "fingerprint": row["fingerprint"],
             "build_id": row["build_id"]} for row in rows]


def _different_build_warning(store, accepted):
    connection = store.connection()
    qualified_builds = set()
    for entry in accepted:
        builds = connection.execute(
            "SELECT DISTINCT acceptance_runs.build_id FROM acceptance_runs"
            " JOIN acceptances USING (acceptance_id)"
            " WHERE acceptances.qualification_id = ? AND acceptances.withdrawn_at IS NULL",
            (entry["qualification_id"],)).fetchall()
        recorded = {row["build_id"] for row in builds if row["build_id"]}
        qualified_builds.update(recorded or ({entry["build_id"]} if entry["build_id"] else set()))
    selection = connection.execute(
        "SELECT build_id FROM runtime_selection WHERE selection_id = 1").fetchone()
    active = selection["build_id"] if selection else None
    if active is None or not qualified_builds or qualified_builds == {active}:
        return None
    labels = []
    for build_id in sorted(qualified_builds - {active}):
        build = store.connection().execute(
            "SELECT label FROM builds WHERE build_id = ?", (build_id,)).fetchone()
        labels.append(build["label"] if build else build_id)
    return ("the accepted evidence was recorded on " + ", ".join(labels) +
            ", not the active llama.cpp build; results may differ")


def store_directory(config_root=None):
    """The private directory the store file lives in, beside login.json."""
    if config_root is None:
        config_root = (os.environ.get("XDG_CONFIG_HOME")
                       or Path.home() / ".config")
    return Path(config_root) / CONFIG_ROOT_NAME / MANAGER_DIR_NAME


def store_path(config_root=None):
    return store_directory(config_root) / STORE_FILENAME


def open_store(config_root=None, *, exclusive=False, allow_newer=False,
               allow_update_read=False):
    """Open Matuto's store, creating it on first use, or say why it is refused.

    Ordinary callers hold a shared update lock for the connection's lifetime.
    The updater takes the exclusive form after stopping the manager, which waits
    for already-open readers and writers and refuses new ones until it closes.
    Only the independently installed updater may allow a newer schema; it touches
    the stable snapshot, session and update-state tables, not release-owned tables.
    """
    directory = store_directory(config_root)
    _private_directory(directory)
    path = directory / STORE_FILENAME
    if path.exists():
        _safe_file(path)
    lock_fd = _store_lock(directory, exclusive=exclusive)
    connection = None
    try:
        # A journal, WAL or shared-memory name is held to the store file's own rule on
        # every path, creation included: SQLite would otherwise adopt a planted one
        # and silently remove it.
        sidecars = _safe_sidecars(path)
        if not path.exists():
            created = _create_file(path)
        else:
            _safe_file(path)
            created = False
        if not created:
            if not sidecars and path.stat().st_size == 0:
                created = True
            else:
                has_meta, version, empty = _committed_version(path, sidecars)
                if empty:
                    _recover(path, sidecars)
                    created = True
                else:
                    if not (allow_newer and has_meta and version is not None
                            and version > SCHEMA_VERSION):
                        _refuse_version(path, has_meta, version)
                    _recover(path, sidecars)
        connection = _connect(path)
        if created:
            try:
                _create(connection)
            except sqlite3.Error:
                connection.close()
                raise StoreError(f"cannot create the Matuto store: {path}") from None
            except BaseException:
                connection.close()
                raise
        if not exclusive:
            access = _update_access(connection)
            if access == "blocked" and allow_update_read:
                access = "readonly"
            if access == "blocked":
                row = connection.execute(
                    "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
                try:
                    state = json.loads(row[0]) if row and row[0] else {}
                except (TypeError, ValueError):
                    state = {}
                version = state.get("version", "unknown") if isinstance(state, dict) else "unknown"
                stage = state.get("stage", "interrupted") if isinstance(state, dict) else "interrupted"
                pending = state.get("app_install_pending") if isinstance(state, dict) else None
                if isinstance(pending, dict):
                    version = pending.get("version", version)
                connection.close()
                connection = None
                if stage == "maintenance":
                    raise StoreError(f"Matuto update to {version} ended in maintenance; run Repair Matuto before using the store")
                if pending:
                    raise StoreError(f"Matuto update to {version} has not recorded an outcome; wait for it or run Repair Matuto before using the store")
                raise StoreError(f"Matuto update to {version} is in progress ({stage}); wait for it to finish before using the store")
            if access == "readonly":
                connection.close()
                connection = _readonly_connect(path)
                connection.execute("PRAGMA foreign_keys = ON")
        return Store(path, connection, lock_fd)
    except BaseException:
        if connection is not None:
            connection.close()
        os.close(lock_fd)
        raise


def main(argv):
    if len(argv) != 3 or argv[0] != "launch-spec":
        print("Usage: matuto-store launch-spec KIND ID", file=sys.stderr)
        return 2
    try:
        from matuto import matuto_runtime
        opened = open_store(os.environ.get("MATUTO_STORE_CONFIG_ROOT"))
        try:
            devices = matuto_runtime.pci_devices()
            if devices.get("state") != "completed":
                raise StoreError("GPU detection failed; the stored runtime selection cannot be verified.")
            print(json.dumps(launch_spec(opened, argv[1], argv[2],
                                         detected_gpus=devices.get("devices", ())), sort_keys=True))
            return 0
        finally:
            opened.close()
    except (OSError, StoreError, RecordRefusal) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def read_store(config_root=None):
    """Open the store for reading alone: never created, recovered or written.

    None when there is nothing safe to read: no store, a creation interrupted
    before it committed, another writer's journal or WAL beside it, or a file
    this release refuses. Every check and the connection itself are read-only, so
    a writer that starts after this call is left to finish its own work. A reader
    that finds none of that reports it rather than changing the operator's store.
    """
    path = store_path(config_root)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        if any(os.path.lexists(path.parent / (path.name + suffix)) for suffix in SIDECAR_SUFFIXES):
            return None
        status = store_directory(config_root).lstat()
    except OSError:
        # A state directory this account cannot reach holds no store it may read,
        # and answering as if there were none would be the one reading nothing
        # cannot prove. Nothing below changes a file to find that out.
        return None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode) \
            or status.st_mode & GROUP_OR_OTHER or status.st_uid != os.getuid():
        return None
    try:
        lock_fd = _store_lock(path.parent, exclusive=False, blocking=False)
    except StoreError:
        return None
    connection = None
    try:
        _safe_file(path)
        has_meta, version = _read_version(path, readonly=True)
        _refuse_version(path, has_meta, version)
        _check_schema(path, readonly=True)
        connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if _update_access(connection) == "blocked":
            connection.close()
            connection = None
            os.close(lock_fd)
            return None
    except (StoreError, OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        os.close(lock_fd)
        return None
    return Store(path, connection, lock_fd)


def read_sessions(connection):
    """Session rows for the updater's byte-preservation check."""
    return [tuple(row) for row in connection.execute(
        "SELECT token_digest, username, generation, created_at, expires_at"
        " FROM sessions ORDER BY token_digest")]


def read_sessions_at(path):
    """Read sessions from a store file without exposing another DB opener."""
    try:
        connection = _readonly_connect(Path(path))
    except sqlite3.Error:
        raise StoreError(f"cannot open the store to verify browser sessions: {path}") from None
    try:
        return read_sessions(connection)
    except sqlite3.Error:
        raise StoreError(f"cannot read preserved browser sessions: {path}") from None
    finally:
        connection.close()


def read_session_at(path, token_digest):
    """Read one session row without taking the updater lock; never permits writes."""
    path = Path(path)
    _check_private_directory(path.parent)
    _safe_file(path, "Matuto store")
    _safe_sidecars(path)
    try:
        connection = _readonly_connect(path)
    except sqlite3.Error:
        raise StoreError(f"cannot open the store to verify a browser session: {path}") from None
    try:
        row = connection.execute(
            "SELECT username, generation, expires_at FROM sessions WHERE token_digest = ?",
            (token_digest,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        raise StoreError(f"cannot read the browser session: {path}") from None
    finally:
        connection.close()


def discard_staged_migrations(config_root=None):
    """Remove private migration copies left by an updater interrupted before activation."""
    directory = store_directory(config_root)
    _private_directory(directory)
    for path in directory.glob(f"{MIGRATION_TEMP_PREFIX}*"):
        try:
            status = path.lstat()
            if (stat.S_ISREG(status.st_mode) and status.st_nlink == 1
                    and status.st_uid == os.getuid() and not status.st_mode & GROUP_OR_OTHER):
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise StoreError(f"cannot remove an interrupted staged store: {path}") from None


def acquire_update_lock(config_root=None, *, blocking=False):
    """Acquire the exclusive lease that closes the store to every other caller."""
    directory = store_directory(config_root)
    _private_directory(directory)
    return _store_lock(directory, exclusive=True, blocking=blocking)


def read_schema_version(path):
    """Read a store version without opening the database outside this module."""
    try:
        connection = _readonly_connect(Path(path))
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store schema: {path}") from None
    try:
        row = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            raise StoreError(f"the Matuto store records no schema version: {path}")
        return int(row[0])
    except (sqlite3.Error, TypeError, ValueError):
        raise StoreError(f"cannot read the Matuto store schema version: {path}") from None
    finally:
        connection.close()


def read_update_state(path):
    """Read the updater's durable state through the store module only."""
    _safe_file(Path(path), "Matuto store")
    try:
        connection = _readonly_connect(Path(path))
        row = connection.execute(
            "SELECT release_version, phase FROM update_state WHERE state_id = 1").fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        raise StoreError(f"cannot read the update state: {path}") from None
    finally:
        if "connection" in locals():
            connection.close()


def begin_app_install(path, version, list_number, unit_name, *, verified_offer=None):
    """Atomically reserve only the signed offer the operator reviewed."""
    if (not isinstance(version, str) or not version
            or isinstance(list_number, bool) or not isinstance(list_number, int)
            or list_number < 0
            or not isinstance(unit_name, str)
            or not re.fullmatch(r"matuto-update-[0-9a-f]{32}\.service", unit_name)):
        raise StoreError("the reviewed update offer is invalid")
    path = Path(path)
    _safe_file(path, "Matuto store")
    try:
        connection = _connect(path)
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store to start an update: {path}") from None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
        try:
            state = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            state = {}
        offer = state.get("update_offer") if isinstance(state, dict) else None
        pending = state.get("app_install_pending") if isinstance(state, dict) else None
        page_offer = verified_offer if isinstance(verified_offer, dict) else None
        accepted_number = state.get("list_number") if isinstance(state, dict) else None
        reviewed = (page_offer is not None and page_offer.get("version") == version
                    and page_offer.get("withdrawn") is False
                    and isinstance(state, dict)
                    and (accepted_number is None
                         or isinstance(accepted_number, int) and not isinstance(accepted_number, bool)
                         and list_number >= accepted_number))
        saved_offer = (state.get("list_number") == list_number
                       and isinstance(offer, dict) and offer.get("version") == version)
        if (not isinstance(state, dict) or state.get("stage") not in ("complete", "failed")
                or (pending and state.get("stage") != "failed")
                or not (saved_offer or reviewed)):
            connection.execute("ROLLBACK")
            return False
        if reviewed:
            state["list_number"] = list_number
        state["app_install_pending"] = {
            "version": version, "list_number": list_number, "unit_name": unit_name,
            **({"relaunching": True} if pending else {})}
        raw = json.dumps(state, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE update_state SET phase = ?, updated_at = ? WHERE state_id = 1",
            (raw, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
        connection.execute("COMMIT")
        return True
    except BaseException as error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        if isinstance(error, sqlite3.Error):
            raise StoreError(f"cannot reserve the reviewed Matuto update: {path}") from None
        raise
    finally:
        connection.close()


def record_app_install_result(path, *, installed_version, version, list_number,
                              unit_name, stage, reason):
    """Atomically finish only the app-install handoff named by this transient unit."""
    if (not isinstance(installed_version, str) or not installed_version
            or not isinstance(version, str) or not version
            or isinstance(list_number, bool) or not isinstance(list_number, int) or list_number < 0
            or not isinstance(unit_name, str)
            or not re.fullmatch(r"matuto-update-[0-9a-f]{32}\.service", unit_name)
            or stage not in ("complete", "failed") or not isinstance(reason, str)):
        raise StoreError("the app-install outcome is invalid")
    path = Path(path)
    _safe_file(path, "Matuto store")
    try:
        connection = _connect(path)
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store to record the app-install result: {path}") from None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
        try:
            state = json.loads(row[0]) if row and row[0] else None
        except (TypeError, ValueError):
            connection.execute("ROLLBACK")
            raise StoreError(f"cannot read the update state before recording the app-install result: {path}") from None
        if not isinstance(state, dict) or state.get("stage") in (
                "preparing", "prepared", "activating", "verifying", "maintenance"):
            connection.execute("ROLLBACK")
            return False
        pending = state.get("app_install_pending")
        result_unit = state.get("app_install_result_unit")
        if pending is not None:
            if (not isinstance(pending, dict)
                    or pending.get("unit_name") != unit_name
                    or pending.get("version") != version
                    or pending.get("list_number") != list_number):
                connection.execute("ROLLBACK")
                return False
        else:
            if (result_unit not in (None, unit_name)
                    or state.get("version") != version
                    or state.get("list_number") != list_number
                    or result_unit == unit_name):
                connection.execute("ROLLBACK")
                return False
        state.update(version=installed_version, stage="complete",
                     app_install_pending=None, app_install_result_unit=unit_name,
                     install_result={"version": version, "stage": stage, "reason": reason[:500]})
        state.pop("reason", None)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE update_state SET release_version = ?, phase = ?, updated_at = ? WHERE state_id = 1",
            (installed_version, json.dumps(state, sort_keys=True, separators=(",", ":")), now))
        connection.execute("COMMIT")
        return True
    except BaseException as error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        if isinstance(error, sqlite3.Error):
            raise StoreError(f"cannot record the app-install result: {path}") from None
        raise
    finally:
        connection.close()


def write_update_state(path, version, phase, *, list_number=None, details=None):
    """Write durable update progress in the store's update-state record."""
    path = Path(path)
    _safe_file(path, "Matuto store")
    try:
        connection = _connect(path)
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store to record update state: {path}") from None
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
        carried = {}
        if previous and previous[0]:
            try:
                previous_state = json.loads(previous[0])
                if isinstance(previous_state, dict):
                    carried = {key: value for key, value in previous_state.items()
                               if key not in ("version", "stage")}
            except (TypeError, ValueError):
                pass
        carried.update(details or {})
        if list_number is not None:
            carried["list_number"] = list_number
        state = json.dumps({"version": version, "stage": phase, **carried},
                           sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO update_state (state_id, release_version, phase, updated_at)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(state_id) DO UPDATE SET"
            " release_version=excluded.release_version, phase=excluded.phase, updated_at=excluded.updated_at",
            (version, state, now))
        connection.execute("COMMIT")
    except BaseException as error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        if isinstance(error, sqlite3.Error):
            raise StoreError(f"cannot write the Matuto update state: {path}") from None
        raise
    finally:
        connection.close()


def record_update_check(path, details, *, list_number=None):
    """Persist update-check facts without replacing updater identity or lowering list state."""
    if not isinstance(details, Mapping) or {"version", "stage", "list_number"} & details.keys():
        raise StoreError("update-check details cannot replace updater state")
    if (list_number is not None
            and (isinstance(list_number, bool) or not isinstance(list_number, int)
                 or list_number < 0)):
        raise StoreError("the accepted release-list number must be a non-negative integer")
    path = Path(path)
    _safe_file(path, "Matuto store")
    try:
        connection = _connect(path)
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store to record update-check state: {path}") from None
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT phase FROM update_state WHERE state_id = 1").fetchone()
        if row is None:
            raise StoreError(f"cannot record an update check without update state: {path}")
        try:
            state = json.loads(row[0]) if row[0] else {"stage": "complete"}
        except (TypeError, ValueError):
            state = {"stage": "complete" if row[0] == "snapshot verified" else row[0]}
        if not isinstance(state, dict):
            state = {"stage": row[0]}
        accepted = state.get("list_number")
        if (accepted is not None
                and (isinstance(accepted, bool) or not isinstance(accepted, int) or accepted < 0)):
            raise StoreError("the stored release-list number is invalid")
        if list_number is not None and accepted is not None and list_number < accepted:
            connection.execute("COMMIT")
            return False
        state.update(details)
        if list_number is not None:
            state["list_number"] = list_number
        connection.execute(
            "UPDATE update_state SET phase = ?, updated_at = ? WHERE state_id = 1",
            (json.dumps(state, sort_keys=True, separators=(",", ":")), now))
        connection.execute("COMMIT")
        return True
    except BaseException as error:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        if isinstance(error, sqlite3.Error):
            raise StoreError(f"cannot write update-check state: {path}") from None
        raise
    finally:
        connection.close()


def _update_access(connection):
    """Return read-only or blocked while an update has not reached a safe endpoint."""
    row = connection.execute("SELECT phase FROM update_state WHERE state_id = 1").fetchone()
    if row is None or not row[0]:
        return "writable"
    try:
        phase = json.loads(row[0])
    except (TypeError, ValueError):
        return "writable" if row[0] == "snapshot verified" else "blocked"
    if not isinstance(phase, dict):
        return "blocked"
    stage = phase.get("stage")
    pending = phase.get("app_install_pending")
    if stage == "verifying":
        return "readonly"
    if pending:
        if stage == "failed" and isinstance(pending, dict) and not pending.get("relaunching"):
            return "writable"
        if (stage == "complete" and isinstance(pending, dict)
                and pending.get("version") == phase.get("version")
                and pending.get("list_number") == phase.get("list_number")):
            return "writable"
        return "blocked"
    if stage in ("complete", "failed"):
        return "writable"
    return "blocked"


def _store_lock(directory, *, exclusive, blocking=False):
    """Lock out store users while an update replaces the active release and DB."""
    path = directory / ".update.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != os.getuid() or status.st_mode & GROUP_OR_OTHER):
            raise StoreError(f"the Matuto update lock is unsafe: {path}")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            raise StoreBusyError("a Matuto update is in progress; try again when it finishes") from None
        return descriptor
    except StoreError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise StoreError(f"cannot lock the Matuto store for an update: {error}") from None


def _private_directory(directory):
    """A directory only this account may read, write or search, created if absent."""
    if not directory.exists():
        try:
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
        except FileExistsError:
            # Another Matuto created it first, or something else holds the name;
            # either way the checks below judge what is really there.
            pass
        except OSError:
            raise StoreError(f"cannot create the Matuto state directory: {directory}") from None
    _check_private_directory(directory)


def _check_private_directory(directory):
    """Check an existing state directory without creating or changing it."""
    try:
        status = directory.lstat()
    except OSError:
        raise StoreError(f"cannot read the Matuto state directory: {directory}") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise StoreError(f"Matuto state directory is not a directory: {directory}")
    _refuse_shared(status, "Matuto state directory", directory)
    _refuse_foreign(status, "Matuto state directory", directory)


def _safe_file(path, what="Matuto store", *, missing_ok=False):
    """The store file, if it is this account's own 0600 regular file."""
    try:
        status = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise StoreError(f"cannot read the {what.lower()}: {path}") from None
    except OSError:
        raise StoreError(f"cannot read the {what.lower()}: {path}") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise StoreError(f"{what} is not a regular file: {path}")
    _refuse_shared(status, what, path)
    _refuse_foreign(status, what, path)
    return True


def _refuse_shared(status, what, path):
    if status.st_mode & GROUP_OR_OTHER:
        mode = stat.S_IMODE(status.st_mode)
        raise StoreError(
            f"{what} is readable or writable by group or others (mode {mode:04o}); "
            f"restore it to this account only: {path}")


def _refuse_foreign(status, what, path):
    if status.st_uid != os.getuid():
        raise StoreError(f"{what} belongs to another account: {path}")


def _create_file(path):
    # Created 0600 before SQLite opens it: SQLite gives the journal, WAL and
    # shared-memory files the database file's own mode, so the file's mode is
    # what the rest of the store's permissions follow.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        # Another Matuto created it first, so this open is not the creating one.
        _safe_file(path)
        return False
    except OSError:
        raise StoreError(f"cannot create the Matuto store: {path}") from None
    os.close(descriptor)
    return True


def _safe_sidecars(path):
    """The journal, WAL and shared-memory names beside the store, if they are safe.

    lstat also refuses dangling links. A writer may remove its journal at any
    moment, so an absent name is harmless; an unsafe name is still refused.
    """
    sidecars = [path.parent / (path.name + suffix)
                for suffix in SIDECAR_SUFFIXES]
    return [sidecar for sidecar in sidecars
            if _safe_file(sidecar, f"Matuto store {sidecar.name}", missing_ok=True)]


def _committed_version(path, sidecars):
    """What the store has committed, read without writing to the operator's file.

    Returns (has_meta, version, empty). A present journal is replayed on a
    disposable copy, because replaying it on the original would change a store
    this release may have to refuse untouched. `empty` says replaying left no
    database at all: a creation interrupted before its transaction committed.
    The recovered view is validated here too, so refused input is never recovered.
    """
    if not sidecars:
        has_meta, version = _read_version(path, readonly=True)
        if has_meta and version == SCHEMA_VERSION:
            _check_schema(path, readonly=True)
        return has_meta, version, False
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / path.name
        shutil.copyfile(path, copy)
        os.chmod(copy, 0o600)
        for sidecar in sidecars:
            shutil.copyfile(sidecar, Path(scratch) / sidecar.name)
        has_meta, version = _read_version(copy, readonly=False)
        empty = copy.stat().st_size == 0
        if has_meta and version == SCHEMA_VERSION:
            _check_schema(copy, readonly=False)
        return has_meta, version, empty


def _readonly_connect(path):
    """A connection that judges a SQLite file as it sits, never repairing it.

    Read-only in SQLite's own sense: a hot journal is not replayed and no page
    is written, so a copy being verified is never changed by being opened.
    """
    connection = sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _read_version(path, readonly):
    """The schema version the file records, and whether it has store_meta at all."""
    try:
        if readonly:
            connection = _readonly_connect(path)
        else:
            connection = sqlite3.connect(str(path))
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store: {path}") from None
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        # Only the probe for the version table is judged busy-or-corrupt here: a
        # file without store_meta is not a Matuto store, and telling the operator
        # to try again would never help.
        has_meta = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'store_meta'").fetchone()
    except sqlite3.OperationalError:
        raise StoreError(
            f"the Matuto store is in use by another Matuto and did not answer; "
            f"try again: {path}") from None
    except sqlite3.DatabaseError:
        raise StoreError(
            f"this file is not a Matuto store, or is corrupt; it was left unchanged: {path}") from None
    try:
        if has_meta is None:
            return False, None
        stored = connection.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.DatabaseError:
        raise StoreError(
            f"this file is not a Matuto store, or is corrupt; it was left unchanged: {path}") from None
    finally:
        connection.close()
    try:
        version = int(stored[0]) if stored is not None else None
    except (TypeError, ValueError):
        version = None
    return True, version


def _refuse_version(path, has_meta, version):
    """Refuse a store this release does not read, leaving its records untouched."""
    if not has_meta:
        raise StoreError(f"this file is not a Matuto store; it was left unchanged: {path}")
    if version is None:
        raise StoreError(f"this Matuto store records no readable schema version: {path}")
    if version == SCHEMA_VERSION:
        return
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"this Matuto store has schema version {version}, newer than the "
            f"{SCHEMA_VERSION} this Matuto reads; it was left unchanged, so install "
            f"a newer Matuto or move this store aside: {path}")
    raise StoreError(
        f"this Matuto store has schema version {version}, older than the "
        f"{SCHEMA_VERSION} this Matuto reads; migrating it belongs to a Matuto "
        f"update: {path}")


def _check_schema(path, readonly=True):
    """Refuse a store that lost its tables or is corrupt, leaving it untouched."""
    try:
        if readonly:
            connection = _readonly_connect(path)
        else:
            connection = sqlite3.connect(str(path))
    except sqlite3.Error:
        raise StoreError(f"cannot open the Matuto store: {path}") from None
    try:
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = ", ".join(sorted(REQUIRED_TABLES - names))
        if missing:
            raise StoreError(
                f"this Matuto store is missing its {missing} table(s); "
                f"it was left unchanged: {path}")
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise StoreError(f"this Matuto store is corrupt; it was left unchanged: {path}")
    except sqlite3.DatabaseError:
        raise StoreError(f"this Matuto store is corrupt; it was left unchanged: {path}") from None
    finally:
        connection.close()


def _recover(path, sidecars):
    """Finish an interrupted write SQLite left its journal for, if there is one.

    A reader may otherwise see the interrupted transaction's own pages, and a
    read-only connection cannot replay them. Taking the write lock and giving it
    straight back rolls the journal back without committing anything.
    """
    if not sidecars:
        return False
    try:
        connection = _connect(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()
    except sqlite3.Error:
        raise StoreError(
            f"cannot finish an interrupted write to the Matuto store: {path}") from None
    return True


def _connect(path):
    # Autocommit, so a caller owns its own transactions; the schema's own
    # creation is the one transaction this module writes itself.
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _create(connection):
    """The whole current schema and its version, in one transaction."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            # Another Matuto created the schema between our check and this
            # transaction; the file is the store either way.
            connection.execute("ROLLBACK")
            return
        for _area, statements in SCHEMA_BLOCKS:
            for statement in statements:
                connection.execute(statement)
        connection.executemany(
            "INSERT INTO store_meta (key, value) VALUES (?, ?)",
            (("schema_version", str(SCHEMA_VERSION)), ("created_at", now)))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise


def stage_migrations(config_root=None):
    """Return a verified, private forward-migrated copy without changing the store.

    Each MIGRATIONS entry maps a source version to one callable that migrates it
    to the next version. Callables use the supplied connection without committing
    their own transaction. A process interruption can leave only a private temp
    file; the next call discards it and starts again from the original.
    """
    directory = store_directory(config_root)
    _private_directory(directory)
    source = directory / STORE_FILENAME
    _safe_file(source)
    sidecars = _safe_sidecars(source)
    if sidecars:
        raise StoreError(f"cannot stage migrations while the store has an unfinished write: {source}")
    has_meta, version = _read_version(source, readonly=True)
    _refuse_migration_source(source, has_meta, version)
    _discard_migration_temps(directory)

    if version > SCHEMA_VERSION:
        raise StoreError(
            f"cannot stage schema version {version} down to {SCHEMA_VERSION}: downgrades are not supported")
    steps = []
    for step in range(version, SCHEMA_VERSION):
        migration = MIGRATIONS.get(step)
        if not callable(migration):
            raise StoreError(f"no schema migration is registered from version {step} to {step + 1}")
        steps.append((step, migration))

    descriptor, name = tempfile.mkstemp(prefix=MIGRATION_TEMP_PREFIX, dir=directory)
    os.close(descriptor)
    staged = Path(name)
    try:
        with contextlib.closing(_readonly_connect(source)) as original:
            with contextlib.closing(_connect(staged)) as copy:
                original.backup(copy)
        for step, migration in steps:
            _apply_migration(staged, step, migration)
        _verify_migrated_copy(staged, SCHEMA_VERSION)
        _safe_file(staged, "Staged Matuto store")
        return staged
    except BaseException as error:
        _discard_migration_temp(staged)
        if isinstance(error, StoreError):
            raise
        raise StoreError(f"cannot stage schema migrations from version {version}: {error}") from None


def _refuse_migration_source(path, has_meta, version):
    if not has_meta or version is None:
        raise StoreError(f"this file is not a Matuto store or has no readable schema version: {path}")
    # Older layouts need not have today's required tables yet, but the source
    # must still be a readable SQLite database before it is backed up.
    try:
        connection = _readonly_connect(path)
        try:
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            result = connection.execute("PRAGMA integrity_check").fetchall()
            if len(result) != 1 or result[0][0] != "ok":
                raise StoreError(f"the source Matuto store is corrupt: {path}")
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise StoreError(f"the source Matuto store is corrupt: {path}") from None


def _apply_migration(path, version, migration):
    connection = None
    try:
        connection = _connect(path)
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration(connection)
            connection.execute(
                "UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
                (str(version + 1),))
            if connection.execute(
                    "SELECT changes()").fetchone()[0] != 1:
                raise sqlite3.DatabaseError("schema version record is missing")
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if len(integrity) != 1 or integrity[0][0] != "ok":
                raise sqlite3.IntegrityError("integrity_check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.IntegrityError("foreign_key_check found a broken reference")
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise
    except BaseException as error:
        raise StoreError(
            f"schema migration {version} to {version + 1} failed: {error}") from None
    finally:
        if connection is not None:
            connection.close()


def _verify_migrated_copy(path, target_version):
    try:
        connection = _readonly_connect(path)
        try:
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            integrity = connection.execute("PRAGMA integrity_check").fetchall()
            if len(integrity) != 1 or integrity[0][0] != "ok":
                raise StoreError(f"staged schema copy failed SQLite integrity_check: {path}")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise StoreError(f"staged schema copy failed SQLite foreign_key_check: {path}")
            row = connection.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
            if row is None or row[0] != str(target_version):
                raise StoreError(f"staged schema copy does not record version {target_version}: {path}")
        finally:
            connection.close()
        _check_schema(path, readonly=True)
    except sqlite3.DatabaseError:
        raise StoreError(f"cannot verify staged schema copy: {path}") from None


def _discard_migration_temps(directory):
    for path in directory.glob(f"{MIGRATION_TEMP_PREFIX}*"):
        _discard_migration_temp(path)


def _discard_migration_temp(path):
    for candidate in (path, *(Path(str(path) + suffix) for suffix in SIDECAR_SUFFIXES)):
        if not os.path.lexists(candidate):
            continue
        _safe_file(candidate, "Staged Matuto store")
        candidate.unlink()


# ------------------------------------------------------------------ records
#
# One validation every save and import calls, and the availability check beside
# it. Both read only: a malformed record is refused before anything is written,
# and a change outside Matuto is reported without touching the record. Argument
# rows go through the launch validator, so a save enforces what a launch enforces.

RECORD_KINDS = ("card", "language", "synthesis", "recognition", "bundle")
ID_FIELDS = {"card": "card_id", "language": "profile_id", "synthesis": "profile_id",
             "recognition": "profile_id", "bundle": "bundle_id"}
# A field outside its kind's set is refused, never ignored: a card on a
# recognition profile, or weights on a language profile, is a mistake to name.
# `created_at` is the store's own column and is accepted so an export round-trips.
RECORD_FIELDS = {
    "card": frozenset(("kind", "card_id", "name", "notes", "created_at", "gpu_identity",
                       "vram_ceiling_bytes", "default_gpu_layers", "smoke_output_tokens",
                       "bench_prompt_tokens", "bench_generation_tokens", "bench_repetitions")),
    "language": frozenset(("kind", "profile_id", "name", "notes", "created_at", "alias",
                           "context_size", "thinking", "card_id", "published", "arguments",
                           "model", "mmproj", "mtp")),
    "synthesis": frozenset(("kind", "profile_id", "name", "notes", "created_at", "card_id",
                            "speech_executable_id", "published", "arguments", "weights")),
    "recognition": frozenset(("kind", "profile_id", "name", "notes", "created_at",
                              "speech_executable_id", "published", "arguments", "weights")),
    "bundle": frozenset(("kind", "bundle_id", "name", "notes", "created_at", "members",
                         "load_tokens", "load_prompt")),
}
# The files each profile kind launches with: a language profile a model and
# optional projector and MTP head, a speech profile its weights.
PICKS = {"language": (("model", True), ("mmproj", False), ("mtp", False)),
         "synthesis": (("weights", True),),
         "recognition": (("weights", True),)}
BUNDLE_ROLES = ("language", "synthesis", "recognition")
IMPORT_FIELDS = {kind: fields - {"created_at", "published"}
                 for kind, fields in RECORD_FIELDS.items()}


def validate_record(record, connection, *, detected_gpus=(), help_text=None, selector=None):
    """Refuse a malformed record, naming the record and the field.

    `record` is one card, model profile or bundle as a save or an import holds
    it: a mapping whose `kind` names what it is. A missing required field, a
    field another kind carries, a value of the wrong type, a pick that is not a
    complete acquisition record and an argument row the launch validator refuses all raise
    RecordRefusal. Returns the unadvertised-option warnings the launch validator reports for an
    otherwise accepted record. Writes nothing.
    """
    kind, label = _record_kind(record)
    if "qualification" in record:
        record = {key: value for key, value in record.items() if key != "qualification"}
    _known_fields(record, kind, label)
    _record_identifier(record, kind, label)
    _text(record, kind, label, "name", required=True)
    _notes(record, kind, label)
    if "created_at" in record:
        _text(record, kind, label, "created_at", required=True)
    if kind == "card":
        _validate_card(record, detected_gpus, label)
        return []
    if kind == "language":
        return _validate_language(record, connection, label, help_text, selector)
    if kind in ("synthesis", "recognition"):
        _validate_speech(record, connection, label, kind)
        return []
    _validate_bundle(record, connection, label)
    return []


def record_availability(record, connection, *, detected_gpus=()):
    """(available, reason) for a stored record, reading what is true now.

    Reports a required file missing at its location, a build or speech executable
    changed or removed outside Matuto, an undetected card and a profile on a card
    other than the active card. It writes nothing and substitutes nothing, so the
    record's stored picks and its fingerprint stay as they are.
    """
    kind = record.get("kind")
    if kind not in RECORD_KINDS:
        return False, f"unknown record kind {kind!r}"
    identities = _gpu_identities(detected_gpus)
    if kind == "card":
        identity = record.get("gpu_identity")
        if identity not in identities:
            return False, f"its GPU {identity!r} is not detected"
        return True, None
    if kind == "bundle":
        return _bundle_availability(record, connection, identities)
    missing = _missing_pick(connection, record, kind)
    if missing is not None:
        return False, missing
    if kind in ("language", "synthesis"):
        available, reason = _card_availability(connection, record.get("card_id"), identities)
        if not available:
            return False, reason
    if kind in ("synthesis", "recognition"):
        reason = _executable_availability(connection, record.get("speech_executable_id"))
        if reason is not None:
            return False, reason
    if kind == "language":
        reason = _build_availability(connection)
        if reason is not None:
            return False, reason
    return True, None


def _record_kind(record):
    if not isinstance(record, Mapping):
        raise RecordRefusal("a record must be a mapping of its fields")
    kind = record.get("kind")
    if kind not in RECORD_KINDS:
        raise RecordRefusal(f"record of unknown kind {kind!r}: field 'kind'")
    for field in (ID_FIELDS[kind], "name"):
        label = record.get(field)
        if isinstance(label, str) and label.strip():
            return kind, label
    return kind, "<unnamed>"


def _refuse(kind, label, field, reason):
    raise RecordRefusal(f"{kind} record {label!r} field {field!r} {reason}")


def _record_identifier(record, kind, label):
    field = ID_FIELDS[kind]
    identifier = _text(record, kind, label, field, required=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", identifier):
        _refuse(kind, label, field, "is malformed")
    return identifier


def _known_fields(record, kind, label):
    for field in record:
        if field not in RECORD_FIELDS[kind]:
            _refuse(kind, label, field, f"does not belong to a {kind} record")


def _text(record, kind, label, field, *, required):
    value = record.get(field)
    if value is None:
        if required:
            _refuse(kind, label, field, "is required")
        return None
    if not isinstance(value, str) or not value.strip():
        _refuse(kind, label, field, "must be a non-empty string")
    return value


def _notes(record, kind, label):
    # PROF-1: every record has a notes field, so a missing or null one is a
    # missing required part rather than the schema's own empty default.
    if record.get("notes") is None:
        _refuse(kind, label, "notes", "is required")
    if not isinstance(record["notes"], str):
        _refuse(kind, label, "notes", "must be a string")
    return record["notes"]


def _whole_number(record, kind, label, field):
    value = record.get(field)
    if value is None:
        _refuse(kind, label, field, "is required")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _refuse(kind, label, field, "must be a positive whole number")
    return value


def _flag(record, kind, label, field):
    value = record.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        _refuse(kind, label, field, "must be true or false")
    return value


def _unpublished(record, kind, label):
    # PROF-1: only a language profile is ever published.
    if _flag(record, kind, label, "published"):
        _refuse(kind, label, "published", "only a language profile may be published")


def _validate_card(record, detected_gpus, label):
    kind = "card"
    identity = _text(record, kind, label, "gpu_identity", required=True)
    if identity not in _gpu_identities(detected_gpus):
        _refuse(kind, label, "gpu_identity",
                f"names a GPU detection does not report: {identity!r}")
    for field in ("vram_ceiling_bytes", "smoke_output_tokens", "bench_prompt_tokens",
                  "bench_generation_tokens", "bench_repetitions"):
        _whole_number(record, kind, label, field)
    layers = record.get("default_gpu_layers")
    if layers != "all" and (isinstance(layers, bool) or not isinstance(layers, int) or layers <= 0):
        _refuse(kind, label, "default_gpu_layers", "must be 'all' or a positive whole number")


def _validate_language(record, connection, label, help_text, selector):
    kind = "language"
    _text(record, kind, label, "alias", required=True)
    _whole_number(record, kind, label, "context_size")
    _text(record, kind, label, "thinking", required=True)
    _card_id(record, connection, kind, label)
    _flag(record, kind, label, "published")
    _picks(record, connection, kind, label)
    rows = _argument_rows(connection, record, kind, label)
    catalogue = [{"flag": entry["flag"], "takesValue": bool(entry["takes_value"])}
                 for entry in connection.execute(
                     "SELECT flag, takes_value FROM argument_catalogue")]
    try:
        checked = arguments.save_checks(rows, catalogue, selector=selector, help_text=help_text)
    except arguments.Refusal as refusal:
        raise RecordRefusal(f"{kind} record {label!r} {refusal}") from None
    return checked["warnings"]


def _validate_speech(record, connection, label, kind):
    # A speech profile's rows are its launcher's own arguments, not llama.cpp's,
    # so they are held to the store's row shape but not to the language launcher's option rules:
    # The language launcher refuses the options a llama.cpp launcher owns, which a speech runtime
    # may legitimately use. The speech launcher owns its own accepted options.
    if kind == "synthesis":
        _card_id(record, connection, kind, label)
    _speech_executable_id(record, connection, kind, label)
    _picks(record, connection, kind, label)
    _argument_rows(connection, record, kind, label)
    _unpublished(record, kind, label)


def _validate_bundle(record, connection, label):
    kind = "bundle"
    # The stated load is optional here; a bundle without it has no fingerprint.
    if record.get("load_tokens") is not None:
        _whole_number(record, kind, label, "load_tokens")
    _text(record, kind, label, "load_prompt", required=False)
    members = record.get("members")
    if members is None:
        _refuse(kind, label, "members", "is required")
    if not isinstance(members, Mapping):
        _refuse(kind, label, "members", "must map each bundle role to a profile")
    for role in BUNDLE_ROLES:
        if members.get(role) is None:
            _refuse(kind, label, f"members.{role}", "is required")
    for role, profile_id in members.items():
        field = f"members.{role}"
        if role not in BUNDLE_ROLES:
            _refuse(kind, label, field, "is not a bundle role")
        if not isinstance(profile_id, str) or not profile_id.strip():
            _refuse(kind, label, field, "must name a profile in the store")
        row = connection.execute("SELECT kind FROM model_profiles WHERE profile_id = ?",
                                 (profile_id,)).fetchone()
        if row is None:
            _refuse(kind, label, field, f"names no profile in the store: {profile_id!r}")
        if row["kind"] != role:
            _refuse(kind, label, field,
                    f"names a {row['kind']} profile, not a {role} one")


def _card_id(record, connection, kind, label):
    card_id = _text(record, kind, label, "card_id", required=True)
    if connection.execute("SELECT 1 FROM cards WHERE card_id = ?", (card_id,)).fetchone() is None:
        _refuse(kind, label, "card_id", f"names no card in the store: {card_id!r}")
    return card_id


def list_speech_executables(connection):
    """List the installed speech executables with their store-owned provenance."""
    records = []
    for row in connection.execute(
            "SELECT s.executable_id, s.kind, l.path AS root, s.path, s.sha256, s.version,"
            " s.notes, s.installed_at FROM speech_executables AS s"
            " JOIN artifact_locations AS l USING (location_id)"
            " ORDER BY s.installed_at, s.executable_id"):
        record = dict(row)
        try:
            record["provenance"] = json.loads(record.pop("notes"))
        except (TypeError, ValueError):
            record["provenance"] = {}
        records.append(record)
    return records


def record_speech_executable(connection, *, executable_id, kind, root, path, sha256,
                             version, provenance, installed_at):
    """Record one checked speech executable inside the caller's transaction."""
    if kind not in ("synthesis", "recognition"):
        raise StoreError(f"unsupported speech executable kind: {kind!r}")
    root = os.fspath(root)
    path = os.fspath(path)
    existing = connection.execute(
        "SELECT location_id FROM artifact_locations WHERE path = ?", (root,)).fetchone()
    if existing is None:
        location_id = f"loc-{executable_id}"
        connection.execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at)"
            " VALUES (?, ?, '', ?)", (location_id, root, installed_at))
    else:
        location_id = existing["location_id"]
    connection.execute(
        "INSERT INTO speech_executables"
        " (executable_id, kind, location_id, path, sha256, version, notes, installed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (executable_id, kind, location_id, path, sha256, version,
         json.dumps(provenance, sort_keys=True), installed_at))


def _speech_executable_id(record, connection, kind, label):
    executable_id = _text(record, kind, label, "speech_executable_id", required=True)
    row = connection.execute("SELECT kind FROM speech_executables WHERE executable_id = ?",
                             (executable_id,)).fetchone()
    if row is None:
        _refuse(kind, label, "speech_executable_id",
                f"names no installed speech executable: {executable_id!r}")
    if row["kind"] != kind:
        _refuse(kind, label, "speech_executable_id",
                f"names a {row['kind']} executable, not a {kind} one")
    return executable_id


def _picks(record, connection, kind, label):
    for field, required in PICKS[kind]:
        _pick(record, connection, kind, label, field, required)


def _pick(record, connection, kind, label, field, required):
    pick = record.get(field)
    if pick is None:
        if required:
            _refuse(kind, label, field, "is required")
        return None
    if not isinstance(pick, Mapping) or set(pick) - {"acquisition_id", "file_name"}:
        _refuse(kind, label, field,
                "must be an acquisition-record pick of acquisition_id and file_name, "
                "never a typed path or digest")
    acquisition_id = pick.get("acquisition_id")
    file_name = pick.get("file_name")
    if not isinstance(acquisition_id, str) or not acquisition_id.strip():
        _refuse(kind, label, field, "must name an acquisition record in the store")
    if not isinstance(file_name, str) or not file_name.strip():
        _refuse(kind, label, field, "must name a file in the acquisition record")
    row = connection.execute(
        "SELECT r.verified_at AS record_verified, f.verified_at AS file_verified"
        " FROM acquisition_files AS f"
        " JOIN acquisition_records AS r ON r.acquisition_id = f.acquisition_id"
        " WHERE f.acquisition_id = ? AND f.file_name = ?",
        (acquisition_id, file_name)).fetchone()
    if row is None:
        _refuse(kind, label, field,
                f"names no file of acquisition record {acquisition_id!r}: {file_name!r}")
    if row["record_verified"] is None or row["file_verified"] is None:
        _refuse(kind, label, field,
                f"names an incomplete acquisition record {acquisition_id!r}")
    return pick


def _argument_rows(connection, record, kind, label):
    rows = record.get("arguments")
    if rows is None:
        # A language profile is accepted only with its per-tool rows; a speech
        # profile's own launcher arguments stay optional here.
        if kind == "language":
            _refuse(kind, label, "arguments", "is required")
        return []
    if not isinstance(rows, list):
        _refuse(kind, label, "arguments", "must be a list of argument rows")
    catalogue = {entry["flag"] for entry in connection.execute(
        "SELECT flag FROM argument_catalogue")}
    for position, row in enumerate(rows, 1):
        where = f"arguments row {position}"
        if not isinstance(row, Mapping):
            _refuse(kind, label, where, "must be a mapping of tool, flag and value")
        for key in row:
            if key not in ("tool", "catalogue", "option", "value"):
                _refuse(kind, label, f"{where} {key}", "is not an argument field")
        if row.get("tool") not in arguments.TOOLS:
            _refuse(kind, label, where, f"must name one of {', '.join(arguments.TOOLS)}")
        for key in ("catalogue", "option", "value"):
            if row.get(key) is not None and not isinstance(row[key], str):
                _refuse(kind, label, f"{where} {key}", "must be a string")
        # A row has to name the flag it will store: a catalogue entry, or a
        # Custom flag the operator typed. Whitespace names neither.
        pick = row.get("catalogue")
        custom = row.get("option")
        if not isinstance(pick, str) or not pick.strip():
            pick = None
        if not isinstance(custom, str) or not custom.strip():
            custom = None
        if pick is None and custom is None:
            _refuse(kind, label, where,
                    "must name an argument-catalogue entry or a Custom flag")
        if pick is not None and pick not in catalogue:
            _refuse(kind, label, f"{where} catalogue",
                    f"names no argument-catalogue entry: {pick!r}")
    return rows


def _gpu_identities(detected_gpus):
    """The GPU identities in a detection result, a device list or plain names."""
    if isinstance(detected_gpus, Mapping):
        detected_gpus = detected_gpus.get("devices", ())
    identities = set()
    for device in detected_gpus or ():
        if isinstance(device, str):
            identities.add(device)
        elif isinstance(device, Mapping):
            identities.add(device.get("pciAddress") or device.get("identity"))
    identities.discard(None)
    return identities


def _missing_pick(connection, record, kind):
    for field, _required in PICKS[kind]:
        pick = record.get(field)
        if not isinstance(pick, Mapping):
            continue
        path = _artifact_path(connection, pick.get("acquisition_id"), pick.get("file_name"))
        if path is None:
            return f"its {field} acquisition record is missing from the store"
        if not os.path.isfile(path):
            return f"its {field} file {pick.get('file_name')!r} is missing at {path}"
    return None


def _artifact_path(connection, acquisition_id, file_name):
    row = connection.execute(
        "SELECT l.path AS root FROM acquisition_records AS r"
        " JOIN artifact_locations AS l ON l.location_id = r.location_id"
        " JOIN acquisition_files AS f ON f.acquisition_id = r.acquisition_id"
        " WHERE r.acquisition_id = ? AND f.file_name = ?",
        (acquisition_id, file_name)).fetchone()
    if row is None:
        return None
    return Path(row["root"]) / file_name


def _bundle_availability(record, connection, identities):
    members = record.get("members")
    if not isinstance(members, Mapping):
        return False, "its members are missing"
    for role in BUNDLE_ROLES:
        profile = _stored_profile(connection, members.get(role))
        if profile is None:
            return False, f"its {role} profile is missing from the store"
        available, reason = record_availability(profile, connection, detected_gpus=identities)
        if not available:
            return False, f"its {role} profile {members.get(role)!r}: {reason}"
    return True, None


def _stored_profile(connection, profile_id):
    """One stored profile as the fields availability reads, or None."""
    if not isinstance(profile_id, str) or not profile_id:
        return None
    row = connection.execute(
        "SELECT profile_id, kind, card_id, speech_executable_id FROM model_profiles"
        " WHERE profile_id = ?", (profile_id,)).fetchone()
    if row is None:
        return None
    record = dict(row)
    for artifact in connection.execute(
            "SELECT role, acquisition_id, file_name FROM profile_artifacts"
            " WHERE profile_id = ?", (profile_id,)):
        record[artifact["role"]] = {"acquisition_id": artifact["acquisition_id"],
                                    "file_name": artifact["file_name"]}
    return record


def _card_availability(connection, card_id, identities):
    if not isinstance(card_id, str) or not card_id:
        return False, "it names no card"
    row = connection.execute("SELECT gpu_identity FROM cards WHERE card_id = ?",
                             (card_id,)).fetchone()
    if row is None:
        return False, f"its card {card_id!r} is missing from the store"
    if row["gpu_identity"] not in identities:
        return False, f"its card {card_id!r} is not detected"
    active = connection.execute(
        "SELECT card_id FROM runtime_selection WHERE selection_id = 1").fetchone()
    if active is None:
        return False, f"its card {card_id!r} is not the active card, and none is selected"
    if active["card_id"] != card_id:
        return False, f"its card {card_id!r} is not the active card {active['card_id']!r}"
    return True, None


def _executable_availability(connection, executable_id):
    if not isinstance(executable_id, str) or not executable_id:
        return "it names no speech executable"
    row = connection.execute(
        "SELECT s.path AS path, s.sha256 AS sha256, l.path AS root"
        " FROM speech_executables AS s"
        " JOIN artifact_locations AS l ON l.location_id = s.location_id"
        " WHERE s.executable_id = ?", (executable_id,)).fetchone()
    if row is None:
        return f"its speech executable {executable_id!r} is missing from the store"
    path = Path(row["path"])
    if not path.is_absolute():
        path = Path(row["root"]) / path
    if not os.path.isfile(path):
        return f"its speech executable {executable_id!r} is missing at {path}"
    if not os.access(path, os.X_OK):
        return f"its speech executable {executable_id!r} is not executable at {path}"
    if _digest_of(path) != row["sha256"]:
        return f"its speech executable {executable_id!r} changed at {path}"
    return None


def _build_availability(connection):
    # A language profile runs under the selected llama.cpp build. The build is not
    # part of the record or its fingerprint, so another registered build keeps it
    # available: Use only warns (PROF-5). A build whose server is gone from its
    # installation was removed outside Matuto, so it is unavailable here; the registry
    # owns the registry's own switch and check rules.
    row = connection.execute(
        "SELECT b.build_id AS build_id, b.recipe AS recipe, b.state AS state,"
        " b.location AS location"
        " FROM runtime_selection AS s JOIN builds AS b ON b.build_id = s.build_id"
        " WHERE s.selection_id = 1").fetchone()
    if row is None:
        return "no llama.cpp build is selected"
    if row["state"] == "unavailable":
        return f"the runtime build {row['build_id']!r} is unavailable"
    server = Path(row["location"]) / installation.SERVER_PATHS.get(
        row["recipe"], installation.SERVER_PATHS[recipes.LLAMA_HIP])
    if not _executable(server):
        return f"the runtime build {row['build_id']!r} is missing its server at {server}"
    return None


def _executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _digest_of(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def write_run_evidence(connection, run_id, *, outcome, ended_at, summary, log, log_truncated,
                       checks, artifacts):
    """One finished run's whole evidence, written inside the caller's transaction.

    The end is written conditionally, so a Stop racing a completion keeps the outcome
    that won and inserts no second copy of the evidence. Answers whether this call was
    the one that recorded the end; a caller whose write lost the race writes nothing
    else. `checks` are (sequence, name, status, detail) rows and `artifacts` are
    (name, media_type, size_bytes, sha256, truncated, body) rows.
    """
    written = connection.execute(
        "UPDATE runs SET outcome = ?, ended_at = ?, summary = ?, log = ?, log_truncated = ?"
        " WHERE run_id = ? AND outcome IS NULL",
        (outcome, ended_at, summary, log, int(log_truncated), run_id)).rowcount
    if not written:
        return False
    for sequence, name, status, detail in checks:
        connection.execute(
            "INSERT INTO checks (run_id, sequence, name, status, detail) VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, name, status, detail))
    for name, media_type, size_bytes, sha256, truncated, body in artifacts:
        connection.execute(
            "INSERT INTO run_evidence (run_id, name, media_type, size_bytes, sha256, truncated,"
            " body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, name, media_type, size_bytes, sha256, int(truncated), body))
    return True


def read_run_evidence(store, run_id):
    """One run's stored record, checks, summary, log and artifacts, or None.

    The record is the run's own row, so a download always names what it came from;
    its summary and log are lifted out beside the checks and artifacts every reader
    wants the same way.
    """
    connection = store.connection()
    row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    record = {key: row[key] for key in row.keys()}
    summary, log = record.pop("summary"), record.pop("log")
    record["log_truncated"] = bool(record["log_truncated"])
    record["dirty_checkout"] = bool(record["dirty_checkout"])
    checks = [{"sequence": row["sequence"], "name": row["name"], "status": row["status"],
               "detail": row["detail"]}
              for row in connection.execute(
                  "SELECT sequence, name, status, detail FROM checks WHERE run_id = ?"
                  " ORDER BY sequence", (run_id,))]
    artifacts = [{"name": row["name"], "media_type": row["media_type"],
                  "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                  "truncated": bool(row["truncated"]), "body": bytes(row["body"])}
                 for row in connection.execute(
                     "SELECT name, media_type, size_bytes, sha256, truncated, body"
                     " FROM run_evidence WHERE run_id = ? ORDER BY name", (run_id,))]
    return {"record": record, "checks": checks, "summary": summary, "log": log,
            "artifacts": artifacts}


def run_archive(store, run_id):
    """One run's whole evidence as one downloadable file, or None when it is not stored.

    The archive names the run in `record.json` and holds its checks, summary, log and
    every artifact, so it can be attached to an issue by hand. It is built in memory:
    a download writes nothing to the checkout or anywhere else.
    """
    evidence = read_run_evidence(store, run_id)
    if evidence is None:
        return None
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("record.json", json.dumps(evidence["record"], indent=2, sort_keys=True))
        bundle.writestr("checks.json", json.dumps(evidence["checks"], indent=2, sort_keys=True))
        bundle.writestr("summary.txt", evidence["summary"])
        bundle.writestr("log.txt", evidence["log"])
        for artifact in evidence["artifacts"]:
            bundle.writestr(f"evidence/{artifact['name']}", artifact["body"])
    return archive.getvalue()


def delete_run(store, run_id):
    """Remove one run and every part of its evidence, in one transaction.

    A run that has not ended is refused: its row is what holds its slot and names its
    unit, so removing it would let a second run start while the first still runs. Stop
    the run and let its end settle first. A run a qualification record or an acceptance
    cites is still referenced, so the database refuses to remove it; naming that refusal
    to the operator is the acceptance and withdrawal rule, not this one.
    """
    connection = store.connection()
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT outcome FROM runs WHERE run_id = ?",
                                 (run_id,)).fetchone()
        if row is not None and row["outcome"] is None:
            raise ActiveRunRefusal(
                f"the run {run_id} has not ended, so its evidence is not in the store to "
                "delete; stop the run and let its end settle first")
        connection.execute("DELETE FROM run_evidence WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM checks WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
