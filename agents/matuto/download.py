"""Transfer one pinned Hugging Face file set as a resumable runner job.

The runner owns admission, the systemd unit, progress and interruption. This
module owns only the request-bound transfer, private staging, full-set checks and
atomic final placement; it never publishes, qualifies or starts inference.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matuto import acquisition, admission, progress, runner, storage, store, verification

RETRIES = 2
POLL_SECONDS = 0.2
JOB_ID = re.compile(r"download-[0-9a-f]{16}")
MARKER_TEMP = re.compile(r"\.matuto-download\.json\.[0-9a-f]{32}\.tmp$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class DownloadError(Exception):
    """A pinned transfer or safe-placement refusal with its reason."""


@dataclass(frozen=True)
class DiscardPreview:
    job_id: str
    files: tuple
    total_size: int
    consequence: str = "The next attempt starts again from nothing."


class DiscardChanged(DownloadError):
    """The confirmation is stale; `preview` is the fresh safe preview."""

    def __init__(self, preview):
        self.preview = preview
        super().__init__(f"staging changed; review the fresh discard preview for {preview.job_id}")


@dataclass(frozen=True)
class Submission:
    start: object
    warning: str = ""


def submit(running, resolved, *, consent=False, acquisition_id=None, api_base=None,
           expected_destination=None, disk_usage=shutil.disk_usage):
    """Submit the resolved request through the shared runner and admission slot."""
    with _job_lock(running):
        connection = running.store.connection()
        destination = storage.download_destination(connection)
        if expected_destination is not None and expected_destination != destination:
            raise DownloadError("model storage changed since Review; resolve the request again")
        origin_id = _origin_id(connection, resolved.repository)
        if origin_id is None:
            raise DownloadError(f"no configured download origin admits {resolved.repository!r}")
        pinned = _pin(resolved, destination, consent, acquisition_id, origin_id=origin_id,
                       api_base=api_base)
        stage = _stage_path(Path(destination), pinned)
        if (_matching_download_jobs(connection, stage, required=False)
                or connection.execute("SELECT 1 FROM acquisition_records WHERE acquisition_id = ?",
                                      (pinned["acquisition_id"],)).fetchone()
                or stage.exists() or stage.is_symlink()):
            raise DownloadError("a download already owns this staging; Resume it or use a new acquisition id")
        require_space(destination, pinned, disk_usage=disk_usage)
        purpose = f"{pinned['repository']}@{pinned['revision']}"
        started = running.start(runner_request(pinned, purpose=purpose))
        warning = ("Serving stays up while this download runs; disk and network activity may slow it."
                   if started.decision == admission.ADMIT
                   and running.serving_state() in (admission.AGENTIC, admission.VOICE) else "")
        return Submission(started, warning)


def resume(running, job_id, *, disk_usage=shutil.disk_usage):
    """Start a new attempt from the job's exact request and retained staging."""
    with _job_lock(running, job_id):
        row = running.store.connection().execute(
            "SELECT pinned_request, outcome, phase FROM jobs WHERE job_id = ? AND kind = ?",
            (job_id, admission.DOWNLOAD)).fetchone()
        if row is None:
            raise DownloadError(f"no download job {job_id!r} is recorded")
        if (row["outcome"] == "completed" or _discarded_phase(row["phase"])
                or _discarding_phase(row["phase"])):
            raise DownloadError(f"download job {job_id!r} has nothing to resume")
        pinned = _read_pin(row["pinned_request"])
        if storage.model_storage(running.store.connection()) != pinned["destination"]:
            raise DownloadError(f"the saved model storage changed from {pinned['destination']}; "
                                "the original download destination must be restored before Resume")
        require_space(pinned["destination"], pinned, disk_usage=disk_usage)
        started = running.start(runner_request(
            pinned, purpose=f"{pinned['repository']}@{pinned['revision']}"))
        warning = ("Serving stays up while this download runs; disk and network activity may slow it."
                   if started.decision == admission.ADMIT
                   and running.serving_state() in (admission.AGENTIC, admission.VOICE) else "")
        return Submission(started, warning)


def preview_discard(running, job_id):
    """Preview every safe file that confirmation would remove."""
    with _job_lock(running, job_id):
        running.works()
        connection = running.store.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            preview = _discard_preview(connection, running, job_id)
            connection.execute("COMMIT")
            return preview
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def confirm_discard(running, preview):
    """Recheck and remove the exact previewed files, then retain a discarded history row."""
    if not isinstance(preview, DiscardPreview):
        raise DownloadError("discard confirmation requires its preview")
    with _job_lock(running, preview.job_id):
        running.works()
        connection = running.store.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            fresh = _discard_preview(connection, running, preview.job_id)
            if fresh != preview:
                connection.execute("ROLLBACK")
                raise DiscardChanged(fresh)
            row = connection.execute(
                "SELECT pinned_request FROM jobs WHERE job_id = ?", (preview.job_id,)).fetchone()
            pinned = _read_pin(row["pinned_request"])
            stage = _stage_path(Path(pinned["destination"]), pinned)
            phase = json.dumps({"phase": "discarding"})
            moment = _now()
            for attempt in _matching_download_jobs(connection, stage):
                connection.execute(
                    "UPDATE jobs SET outcome = COALESCE(outcome, 'interrupted'),"
                    " ended_at = COALESCE(ended_at, ?), phase = ? WHERE job_id = ?",
                    (moment, phase, attempt["job_id"]))
            # Durable intent distinguishes restart recovery from ordinary empty staging.
            connection.execute("COMMIT")

            _remove_staging(Path(pinned["destination"]), stage, pinned, fresh.files)

            connection.execute("BEGIN IMMEDIATE")
            final = _discard_preview(connection, running, preview.job_id)
            if final.files:
                connection.execute("ROLLBACK")
                raise DiscardChanged(final)
            # Keep the v1 outcome enum; phase carries the distinct discard terminal state.
            moment = _now()
            for attempt in _matching_download_jobs(connection, stage):
                connection.execute(
                    "UPDATE jobs SET outcome = COALESCE(outcome, 'interrupted'),"
                    " ended_at = COALESCE(ended_at, ?), phase = ? WHERE job_id = ?",
                    (moment, json.dumps({"phase": "discarded"}), attempt["job_id"]))
            connection.execute("COMMIT")
            return fresh
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def runner_request(pinned, *, purpose=None, argv=None):
    """Build a runner request from the immutable resolution and saved destination."""
    encoded = json.dumps(pinned, sort_keys=True, separators=(",", ":"))
    command = tuple(argv) if argv is not None else (
        sys.executable, str(Path(__file__).absolute()), "download")
    return runner.Request.job(admission.DOWNLOAD,
                              purpose or f"{pinned['repository']}@{pinned['revision']}",
                              command, pinned_request=encoded,
                              destination=pinned["destination"])


def run(argv):
    """Entry point for the transient unit: execute its recorded download job."""
    if len(argv) != 1:
        print("usage: download.py download", file=sys.stderr)
        return 2
    opened = None
    try:
        job_id = job_id_from_environment()
        opened = store.open_store()
        opened.connection().execute("PRAGMA busy_timeout = 20000")
        return execute(opened.connection(), job_id)
    except (OSError, sqlite3.Error, store.StoreError, DownloadError,
            verification.VerificationError) as error:
        print(f"download: {error}", file=sys.stderr)
        return 1
    finally:
        if opened is not None:
            opened.close()


def job_id_from_environment(environment=None):
    """Resolve the unit's job id from the runner's private evidence directory."""
    environment = os.environ if environment is None else environment
    evidence = environment.get(runner.EVIDENCE_ENV)
    if not evidence:
        raise DownloadError("this process was not started by the runner")
    job_id = Path(evidence).parent.name
    if not JOB_ID.fullmatch(job_id):
        raise DownloadError("the runner did not provide a download job id")
    return job_id


def execute(connection, job_id, *, popen=subprocess.Popen, report=progress.report,
            echo=print, sleep=time.sleep):
    """Transfer and verify one stored request; incomplete data stays private."""
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise DownloadError(f"{job_id!r} is not a download job id")
    row = connection.execute(
        "SELECT pinned_request FROM jobs WHERE job_id = ? AND kind = ?",
        (job_id, admission.DOWNLOAD)).fetchone()
    if row is None:
        raise DownloadError(f"the store records no download job {job_id!r}")
    pinned = _read_pin(row["pinned_request"])
    root = Path(pinned["destination"])
    if storage.model_storage(connection) != str(root):
        raise DownloadError(f"the saved model storage changed from {root}; review the download")
    stage = _stage_path(root, pinned)
    final = _final_path(root, pinned)
    _set_phase(connection, job_id, "download", report)
    try:
        placed = _owns_placement(final, pinned)
        source = final if placed else stage
        if not placed:
            _private_tree_directory(root, stage)
            for index, item in enumerate(pinned["files"], 1):
                _transfer(stage, item, index, len(pinned["files"]), connection, job_id,
                          popen=popen, report=report, echo=echo, sleep=sleep)
            _add_discovery_aliases(stage, pinned)
        _set_phase(connection, job_id, "verification", report)
        result = _record_and_verify(connection, job_id, pinned, source, already_placed=placed,
                                    report=report)
        if result.complete:
            echo(f"download {job_id} completed and verified as {result.acquisition_id}")
        elif "needs the operator's consent" in result.reason:
            echo(f"download {job_id} transferred; verification needs consent: {result.reason}")
        else:
            raise DownloadError(result.reason or "the complete file set did not verify")
        return 0
    except (DownloadError, OSError, verification.VerificationError, sqlite3.Error) as error:
        _set_phase(connection, job_id, "failed", report)
        echo(f"download {job_id} failed: {error}")
        return 1


def verify_pending(connection, acquisition_id, *, consent=False):
    """Reverify an unconsented staged download and place it only after consent."""
    pinned = None
    for row in connection.execute("SELECT pinned_request FROM jobs WHERE kind = ?",
                                  (admission.DOWNLOAD,)):
        candidate = _read_pin(row["pinned_request"])
        if candidate["acquisition_id"] == acquisition_id:
            pinned = candidate
            break
    if pinned is None:
        raise DownloadError(f"no pinned download request exists for {acquisition_id!r}")
    root = Path(pinned["destination"])
    stage, final = _stage_path(root, pinned), _final_path(root, pinned)
    record = connection.execute(
        "SELECT r.location_id, l.path FROM acquisition_records r"
        " JOIN artifact_locations l USING(location_id)"
        " WHERE r.acquisition_id = ? AND r.source = 'download'", (acquisition_id,)).fetchone()
    if record is None:
        raise DownloadError(f"no downloaded acquisition {acquisition_id!r} is recorded")
    already_placed = _owns_placement(final, pinned)
    source = final if already_placed else stage
    descriptors = {}
    moved = False
    connection.execute("BEGIN IMMEDIATE")
    try:
        if Path(record["path"]) == final:
            connection.execute("ROLLBACK")
            return verification.verify_record(connection, acquisition_id, consent=consent)
        if Path(record["path"]) not in (stage, final) or (Path(record["path"]) == final and not already_placed):
            raise DownloadError(f"the staged files for {acquisition_id!r} are not at their pinned path")
        for item in pinned["files"]:
            descriptor = os.open(source.joinpath(*item["name"].split("/")),
                                 os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size != item["size"]:
                os.close(descriptor)
                raise DownloadError(f"staged file {item['name']!r} changed before verification")
            descriptors[item["name"]] = descriptor
        result = verification.verify_record(connection, acquisition_id, consent=consent,
                                            expected_files=descriptors)
        if result.complete:
            if not already_placed:
                if final.exists() or final.is_symlink():
                    raise DownloadError(f"{final} already exists; no model file is overwritten")
                for descriptor in descriptors.values():
                    os.fsync(descriptor)
                _write_placement_marker(stage, pinned)
                os.rename(stage, final)
                moved = True
                _sync_directory(root)
            connection.execute("UPDATE artifact_locations SET path = ? WHERE location_id = ?",
                               (str(final), record["location_id"]))
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if moved and final.exists() and not stage.exists():
            os.rename(final, stage)
        raise
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def _pin(resolved, destination, consent, acquisition_id, *, origin_id, api_base=None):
    """Serialize only resolver-produced addresses, names, checksums and sizes."""
    if type(consent) is not bool:
        raise DownloadError("no-published-checksum consent must be true or false")
    repository, revision, files = (getattr(resolved, "repository", None),
                                  getattr(resolved, "revision", None),
                                  getattr(resolved, "files", None))
    if not isinstance(repository, str) or not isinstance(revision, str) or not isinstance(files, tuple):
        raise DownloadError("the request is not a resolved Hugging Face file set")
    entries = []
    for file in files:
        name, size, checksum, url = (getattr(file, "name", None), getattr(file, "size", None),
                                     getattr(file, "sha256", None), getattr(file, "url", None))
        if api_base is not None:
            production = urlsplit(url)
            item_url = api_base.rstrip("/") + production.path
        else:
            item_url = url
        item = {"name": name, "size": size, "sha256": checksum, "url": item_url}
        _validate_file(item, repository, revision, test_base=api_base)
        entries.append(item)
    if (not entries or len({item["name"] for item in entries}) != len(entries)
            or getattr(resolved, "total_size", None) != sum(item["size"] for item in entries)):
        raise DownloadError("the resolved file set is empty, inconsistent or contains duplicate names")
    if not isinstance(destination, str) or not Path(destination).is_absolute():
        raise DownloadError("the download destination is not an absolute model storage path")
    if acquisition_id is None:
        acquisition_id = "acq-" + uuid.uuid4().hex
    if not isinstance(acquisition_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", acquisition_id):
        raise DownloadError("the acquisition record id is malformed")
    if not isinstance(origin_id, str) or not origin_id:
        raise DownloadError("the resolved repository has no configured download origin")
    return {"repository": repository, "revision": revision, "destination": destination,
            "origin_id": origin_id, "consent": consent, "acquisition_id": acquisition_id,
            "files": entries, "total_size": sum(item["size"] for item in entries),
            **({"test_base": api_base} if api_base is not None else {})}


def _read_pin(text):
    try:
        pinned = json.loads(text)
        required = {"repository", "revision", "destination", "origin_id", "consent",
                    "acquisition_id", "files", "total_size"}
        if not isinstance(pinned, dict) or set(pinned) not in (required, required | {"test_base"}):
            raise ValueError
        if not isinstance(pinned["files"], list) or not pinned["files"]:
            raise ValueError
        for item in pinned["files"]:
            if not isinstance(item, dict) or set(item) != {"name", "size", "sha256", "url"}:
                raise ValueError
            _validate_file(item, pinned["repository"], pinned["revision"],
                           test_base=pinned.get("test_base"))
        if (not isinstance(pinned["origin_id"], str) or not pinned["origin_id"]
                or type(pinned["consent"]) is not bool or type(pinned["total_size"]) is not int
                or pinned["total_size"] != sum(item["size"] for item in pinned["files"])
                or not Path(pinned["destination"]).is_absolute()
                or len({item["name"] for item in pinned["files"]}) != len(pinned["files"])):
            raise ValueError
        return pinned
    except (TypeError, ValueError, KeyError):
        raise DownloadError("the pinned download request is malformed") from None


def _validate_file(item, repository, revision, *, test_base=None):
    name, size, checksum, url = (item["name"], item["size"], item["sha256"], item["url"])
    if (not isinstance(name, str) or name.startswith("/") or "\\" in name
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise DownloadError("the resolved request contains an unsafe file name")
    if type(size) is not int or size < 0:
        raise DownloadError(f"the resolved size for {name!r} is invalid")
    if checksum is not None and (not isinstance(checksum, str)
                                 or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
        raise DownloadError(f"the published SHA-256 for {name!r} is invalid")
    parts = repository.split("/") if isinstance(repository, str) else []
    if len(parts) != 2 or not all(REPOSITORY_PART.fullmatch(part) for part in parts):
        raise DownloadError("the resolved repository name is malformed")
    if not isinstance(revision, str) or not COMMIT.fullmatch(revision):
        raise DownloadError("the resolved revision is not a full immutable commit")
    parsed = urlsplit(url) if isinstance(url, str) else None
    expected = ("/" + "/".join(parts) + "/resolve/" + revision + "/"
                + "/".join(quote(part, safe="") for part in name.split("/")))
    parsed_test = urlsplit(test_base) if isinstance(test_base, str) else None
    test_host = (parsed_test is not None
                 and parsed_test.hostname in ("127.0.0.1", "::1", "localhost")
                 and parsed_test.username is None and parsed_test.password is None
                 and not parsed_test.query and not parsed_test.fragment
                 and parsed_test.path in ("", "/"))
    test_address = (test_host and parsed.scheme == parsed_test.scheme == "http"
                    and parsed.hostname == parsed_test.hostname and parsed.port == parsed_test.port
                    and parsed.username is None and parsed.password is None
                    and not parsed.query and not parsed.fragment and parsed.path == expected)
    production_address = (parsed is not None and parsed.scheme == "https"
                          and parsed.hostname == "huggingface.co"
                          and parsed.username is None and parsed.password is None
                          and parsed.port is None and not parsed.query and not parsed.fragment
                          and parsed.path == expected)
    if not (production_address or test_address):
        raise DownloadError(f"the resolved address for {name!r} is not pinned to Hugging Face")


def _discard_preview(connection, running, job_id):
    row = connection.execute(
        "SELECT kind, pinned_request FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None or row["kind"] != admission.DOWNLOAD:
        raise DownloadError(f"no download job {job_id!r} is recorded")
    pinned = _read_pin(row["pinned_request"])
    stage = _stage_path(Path(pinned["destination"]), pinned)
    attempts = _matching_download_jobs(connection, stage)
    for attempt in attempts:
        if _discarded_phase(attempt["phase"]):
            raise DownloadError(f"download job {job_id!r} was already discarded")
        if attempt["outcome"] == "completed":
            raise DownloadError(f"download {attempt['job_id']} completed; there is nothing to discard")
        answer = running._unit_state(attempt["unit_name"])
        if answer is None:
            raise DownloadError(f"systemd state for download {attempt['job_id']} is unknown; refusing discard")
        if runner._alive(answer) or running._launching(attempt["job_id"]):
            raise DownloadError(f"download {attempt['job_id']} may still write to its staging; refusing discard")
        if (answer[runner.LOAD_STATE] != runner.NOT_FOUND
                and runner._ended_outcome(admission.DOWNLOAD, answer[runner.RESULT],
                                          answer[runner.EXIT_STATUS]) == runner.COMPLETED):
            raise DownloadError(f"download {attempt['job_id']} completed; there is nothing to discard")
    if connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = ? LIMIT 1",
            (pinned["acquisition_id"],)).fetchone():
        raise DownloadError(f"download {job_id!r} already has an acquisition record; refusing discard")
    entries = _inspect_discard_tree(Path(pinned["destination"]), stage, pinned)
    if (not any(name in {item["name"] for item in pinned["files"]} for name, _size in entries)
            and not any(_discarding_phase(attempt["phase"]) for attempt in attempts)):
        raise DownloadError(f"download job {job_id!r} has no partial files to discard")
    _refuse_recorded_files(connection, stage, entries)
    return DiscardPreview(job_id, tuple(entries), sum(size for _name, size in entries))


def _matching_download_jobs(connection, stage, *, required=True):
    """All original or resumed attempts that use this exact staging tree."""
    matches = []
    for row in connection.execute(
            "SELECT job_id, unit_name, pinned_request, outcome, phase FROM jobs WHERE kind = ?",
            (admission.DOWNLOAD,)):
        pinned = _read_pin(row["pinned_request"])
        if _stage_path(Path(pinned["destination"]), pinned) == stage:
            matches.append(row)
    if required and not matches:
        raise DownloadError(f"no recorded download owns staging {stage}")
    return matches


def _refuse_recorded_files(connection, stage, entries):
    candidates = {}
    for name, _size in entries:
        path = stage / name
        candidates[os.path.abspath(path)] = name
        candidates[os.path.realpath(path)] = name
    for row in connection.execute(
            "SELECT l.path, f.file_name FROM acquisition_records r"
            " JOIN artifact_locations l USING(location_id)"
            " JOIN acquisition_files f USING(acquisition_id)"):
        recorded = os.path.join(row["path"], row["file_name"])
        for path in (os.path.abspath(recorded), os.path.realpath(recorded)):
            if path in candidates:
                raise DownloadError(f"staging file {candidates[path]!r} is named by an acquisition record")


def _inspect_discard_tree(root, stage, pinned):
    """Fail closed unless the private tree contains only this request's own files."""
    try:
        stage.relative_to(root)
    except ValueError:
        raise DownloadError(f"refusing staging outside model storage: {stage}") from None
    current = root
    for part in stage.relative_to(root).parts:
        current = current / part
        try:
            status = current.lstat()
        except FileNotFoundError:
            if current == stage:
                return []
            raise DownloadError(f"cannot inspect recorded staging directory {current}") from None
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise DownloadError(f"refusing unsafe recorded staging directory {current}")
    requested = {item["name"] for item in pinned["files"]}
    aliases = {_discovery_alias(name) for name in requested
               if "/" in name and Path(name).suffix.casefold() == ".gguf"}
    known_files = requested | aliases
    known_files.add(".matuto-download.json")
    prefixes = {""}
    for name in known_files:
        parts = name.split("/")
        prefixes.update("/".join(parts[:index]) for index in range(1, len(parts)))
    found, entries = set(), []
    try:
        root_status = root.lstat()
    except OSError as error:
        raise DownloadError(f"cannot inspect model storage {root}: {error}") from None
    if not stat.S_ISDIR(root_status.st_mode) or root_status.st_uid != os.getuid():
        raise DownloadError(f"refusing unsafe model storage directory {root}")
    marker = stage / ".matuto-download.json"
    if marker.exists() or marker.is_symlink():
        if ".matuto-download.json" not in requested and not _owns_placement(stage, pinned):
            raise DownloadError(f"refusing unrecognized placement marker in {stage}")
    for directory, dirs, names in os.walk(stage, topdown=True, followlinks=False):
        parent = Path(directory)
        relative_parent = parent.relative_to(stage).as_posix()
        if relative_parent == ".":
            relative_parent = ""
        for name in list(dirs):
            path = parent / name
            relative = f"{relative_parent}/{name}".lstrip("/")
            try:
                status = path.lstat()
            except OSError as error:
                raise DownloadError(f"cannot inspect staging entry {relative!r}: {error}") from None
            if (not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid()
                    or relative not in prefixes):
                raise DownloadError(f"refusing unsafe or unrecorded staging entry {relative!r}")
        for name in names:
            path = parent / name
            relative = f"{relative_parent}/{name}".lstrip("/")
            try:
                status = path.lstat()
            except OSError as error:
                raise DownloadError(f"cannot inspect staging entry {relative!r}: {error}") from None
            if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                    or (relative not in known_files and not MARKER_TEMP.fullmatch(name))):
                raise DownloadError(f"refusing unsafe or unrecorded staging entry {relative!r}")
            found.add(relative)
            entries.append((relative, status.st_size))
    return sorted(entries)


def _remove_staging(root, stage, pinned, entries):
    """Delete through no-follow directory descriptors, not re-resolved paths."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    try:
        root_status = os.fstat(root_fd)
        if root_status.st_uid != os.getuid():
            raise DownloadError(f"refusing unsafe model storage directory {root}")
        try:
            downloads_fd = _open_owned_directory(root_fd, ".matuto-downloads", flags)
        except FileNotFoundError:
            return
        try:
            try:
                stage_fd = _open_owned_directory(downloads_fd, stage.name, flags)
            except FileNotFoundError:
                return
            try:
                for relative, expected_size in entries:
                    parts = relative.split("/")
                    with ExitStack() as parents:
                        parent_fd = stage_fd
                        for part in parts[:-1]:
                            parent_fd = parents.enter_context(
                                _owned_directory(parent_fd, part, flags))
                        with _regular_file(parent_fd, parts[-1], expected_size):
                            os.unlink(parts[-1], dir_fd=parent_fd)
            finally:
                os.close(stage_fd)
            directories = {"/".join(parts[:index]) for item in pinned["files"]
                           for parts in [item["name"].split("/")]
                           for index in range(1, len(parts))}
            for relative in sorted(directories, key=lambda name: name.count("/"), reverse=True):
                parts = relative.split("/")
                try:
                    with ExitStack() as parents:
                        parent_fd = downloads_fd
                        for part in (stage.name, *parts[:-1]):
                            parent_fd = parents.enter_context(
                                _owned_directory(parent_fd, part, flags))
                        os.rmdir(parts[-1], dir_fd=parent_fd)
                except FileNotFoundError:
                    continue
            os.rmdir(stage.name, dir_fd=downloads_fd)
        finally:
            os.close(downloads_fd)
    finally:
        os.close(root_fd)


@contextmanager
def _owned_directory(parent_fd, name, flags):
    descriptor = _open_owned_directory(parent_fd, name, flags)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _open_owned_directory(parent_fd, name, flags):
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
        os.close(descriptor)
        raise DownloadError(f"refusing unsafe staging directory {name!r}")
    return descriptor


@contextmanager
def _regular_file(parent_fd, name, expected_size):
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
    try:
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_size != expected_size):
            raise DownloadError(f"refusing changed staging file {name!r}")
        yield descriptor
    finally:
        os.close(descriptor)


def _discarded_phase(phase):
    try:
        return json.loads(phase).get("phase") == "discarded"
    except (AttributeError, TypeError, ValueError):
        return False


def _discarding_phase(phase):
    try:
        return json.loads(phase).get("phase") == "discarding"
    except (AttributeError, TypeError, ValueError):
        return False


@contextmanager
def _job_lock(running, job_id=None):
    """Serialize Submit, Resume and Discard through the runner's private work area."""
    if job_id is not None and (not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id)):
        raise DownloadError(f"{job_id!r} is not a download job id")
    logs = running.logs
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(logs, 0o700)
    path = logs / "download-operations.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
            os.close(descriptor)
            raise DownloadError(f"refusing unsafe download lock {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        if "descriptor" in locals():
            os.close(descriptor)
        raise DownloadError(f"cannot lock download operation: {error}") from None
    try:
        yield
    finally:
        os.close(descriptor)


def _origin_id(connection, repository):
    """The configured exact-repository or owning-organisation origin."""
    owner = repository.partition("/")[0].casefold()
    rows = connection.execute(
        "SELECT origin_id, kind, name FROM download_origins WHERE enabled = 1").fetchall()
    matches = [row for row in rows if
               (row["kind"] == "repository" and row["name"].casefold() == repository.casefold())
               or (row["kind"] == "organisation" and row["name"].casefold() == owner)]
    return matches[0]["origin_id"] if matches else None


def _stage_path(root, pinned):
    identity = json.dumps({key: pinned[key] for key in
                           ("repository", "revision", "destination", "acquisition_id", "files")},
                          sort_keys=True, separators=(",", ":")).encode()
    return root / ".matuto-downloads" / hashlib.sha256(identity).hexdigest()


def _final_path(root, pinned):
    """Keep revisions distinct in the single directory level native discovery scans."""
    owner, repository = pinned["repository"].split("/")
    return root / f"{owner}@{repository}@{pinned['revision']}"


def _marker_bytes(pinned):
    identity = json.dumps(pinned, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps({"acquisition_id": pinned["acquisition_id"],
                       "request_sha256": hashlib.sha256(identity).hexdigest()},
                      sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _owns_placement(path, pinned):
    """Accept a final directory for recovery only when its private marker matches."""
    if path.is_symlink() or not path.is_dir():
        return False
    marker = path / ".matuto-download.json"
    try:
        descriptor = os.open(marker, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_mode & 0o077:
            return False
        return os.read(descriptor, 1024) == _marker_bytes(pinned)
    finally:
        os.close(descriptor)


def _write_placement_marker(stage, pinned):
    marker = stage / ".matuto-download.json"
    if marker.exists() or marker.is_symlink():
        if _owns_placement(stage, pinned):
            return
        raise DownloadError(f"refusing an unrecognized placement marker in {stage}")
    source_names = {item["name"] for item in pinned["files"]}
    for leftover in stage.iterdir():
        if MARKER_TEMP.fullmatch(leftover.name) and leftover.name not in source_names:
            leftover.unlink()
    while True:
        temporary = stage / f".matuto-download.json.{uuid.uuid4().hex}.tmp"
        if temporary.name not in source_names and not temporary.exists() and not temporary.is_symlink():
            break
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                             0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(_marker_bytes(pinned))
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, marker, follow_symlinks=False)
        except FileExistsError:
            if not _owns_placement(stage, pinned):
                raise DownloadError(f"refusing an unrecognized placement marker in {stage}") from None
        directory = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise DownloadError(f"cannot persist the placement recovery marker: {error}") from None
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _add_discovery_aliases(stage, pinned):
    """Hard-link nested GGUFs at the depth the native scanner enumerates."""
    names = {item["name"] for item in pinned["files"]}
    for item in pinned["files"]:
        name = item["name"]
        if "/" not in name or Path(name).suffix.casefold() != ".gguf":
            continue
        source = stage.joinpath(*name.split("/"))
        alias_name = _discovery_alias(name)
        if alias_name in names:
            raise DownloadError(f"the requested file set conflicts with discovery alias {alias_name!r}")
        alias = stage / alias_name
        if alias.is_symlink():
            raise DownloadError(f"refusing unsafe discovery alias {alias.name!r}")
        if alias.exists():
            if not alias.is_file():
                raise DownloadError(f"refusing unsafe discovery alias {alias.name!r}")
            alias.unlink()
        try:
            os.link(source, alias, follow_symlinks=False)
        except OSError as error:
            raise DownloadError(f"cannot expose nested model file {name!r} to native discovery: {error}") from None


def _discovery_alias(name):
    """Keep llama.cpp's split suffix and projector marker in nested-file aliases."""
    parent, basename = name.rsplit("/", 1)
    parent_hash = hashlib.sha256(parent.encode()).hexdigest()
    shard = re.search(r"-(?P<index>[0-9]{5})-of-(?P<count>[0-9]{5})\.gguf$", basename)
    if shard:
        stem_hash = hashlib.sha256(basename[:shard.start()].encode()).hexdigest()
        return f"matuto-{parent_hash}-{stem_hash}{basename[shard.start():]}"
    digest = hashlib.sha256(name.encode()).hexdigest()
    if "mmproj" in basename.casefold():
        return f"matuto-{parent_hash}-{digest}-mmproj.gguf"
    return f"matuto-{parent_hash}-{digest}.gguf"


def _transfer(stage, item, index, count, connection, job_id, *, popen, report, echo, sleep,
              fresh=False):
    part = stage.joinpath(*item["name"].split("/"))
    _private_tree_directory(stage, part.parent)
    if part.is_symlink() or (part.exists() and not part.is_file()):
        raise DownloadError(f"retained staging file {item['name']!r} is not a regular file")
    retained = part.exists()
    if part.exists() and (part.stat().st_size > item["size"] or item["sha256"] is None):
        part.unlink()
    if part.exists() and part.stat().st_size == item["size"]:
        digest, size = _hash(part)
        if size == item["size"] and (item["sha256"] is None or digest == item["sha256"]):
            _report(connection, job_id, "download", item["name"], size, item["size"], report)
            return
        part.unlink()
    argv = ["curl", "--disable", "--fail", "--location", "--silent", "--show-error",
            "--netrc-file", os.devnull, "--connect-timeout", "30", "--retry", str(RETRIES),
            "--retry-all-errors", "--retry-delay", "1", "--max-filesize", str(item["size"]), "--proto",
            "=https,http" if pinned_test_url(item["url"]) else "=https", "--proto-redir",
            "=https,http" if pinned_test_url(item["url"]) else "=https",
            "--continue-at", "-", "--output", str(part), item["url"]]
    echo(f"Downloading {item['name']} ({index} of {count})")
    try:
        process = popen(argv, stdout=None, stderr=None)
    except OSError as error:
        raise DownloadError(f"curl could not start for {item['name']}: {error}") from None
    previous = -1
    while process.poll() is None:
        try:
            transferred = part.stat().st_size
        except FileNotFoundError:
            transferred = 0
        if transferred != previous:
            _report(connection, job_id, "download", item["name"], transferred,
                    item["size"], report)
            previous = transferred
        sleep(POLL_SECONDS)
    if process.returncode != 0:
        raise DownloadError(f"curl failed for {item['name']} after {RETRIES + 1} attempts "
                            f"(exit {process.returncode}); partial data is retained")
    digest, size = _hash(part)
    if size != item["size"]:
        raise DownloadError(f"{item['name']} transferred {size} bytes, expected {item['size']}; "
                            "partial data is retained")
    if item["sha256"] is not None and digest != item["sha256"]:
        if fresh:
            raise DownloadError(f"{item['name']} does not match the published SHA-256 after a fresh transfer")
        if not retained:
            raise DownloadError(f"{item['name']} does not match the published SHA-256; "
                                "the staged copy is retained for Resume")
        part.unlink()
        echo(f"Discarding an invalid retained copy of {item['name']} and retrying from byte zero")
        _transfer(stage, item, index, count, connection, job_id, popen=popen, report=report,
                  echo=echo, sleep=sleep, fresh=True)
        return
    _report(connection, job_id, "download", item["name"], size, item["size"], report)


def _record_and_verify(connection, job_id, pinned, stage, *, already_placed=False,
                       report=progress.report):
    """Verify outside the store transaction, then record and place the whole set atomically."""
    root = Path(pinned["destination"])
    final = _final_path(root, pinned)
    if final.exists() and not already_placed:
        raise DownloadError(f"{final} already exists; no model file is overwritten")
    if final.is_symlink():
        raise DownloadError(f"{final} already exists; no model file is overwritten")
    if already_placed and not _owns_placement(final, pinned):
        raise DownloadError(f"{final} is not the recorded download's recoverable placement")
    existing = connection.execute(
        "SELECT l.path FROM acquisition_records r JOIN artifact_locations l USING(location_id)"
        " WHERE r.acquisition_id = ?", (pinned["acquisition_id"],)).fetchone()
    if existing:
        if Path(existing["path"]) == final and already_placed:
            return verification.verify_record(connection, pinned["acquisition_id"],
                consent=pinned["consent"],
                on_progress=_verification_progress(connection, job_id, report))
        raise DownloadError(f"acquisition record {pinned['acquisition_id']!r} already exists")

    descriptors = {}
    moved = False
    try:
        for item in pinned["files"]:
            path = stage.joinpath(*item["name"].split("/"))
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_size != item["size"]:
                os.close(descriptor)
                raise DownloadError(f"staged file {item['name']!r} changed before verification")
            descriptors[item["name"]] = descriptor

        result = verification.verify_files(
            pinned["acquisition_id"], stage, pinned["files"], consent=pinned["consent"],
            expected_files=descriptors,
            on_progress=_verification_progress(connection, job_id, report))
        waiting_for_consent = (not pinned["consent"] and
                               "needs the operator's consent" in result.reason)
        if not result.complete and not waiting_for_consent:
            raise DownloadError(result.reason or "the complete file set did not verify")

        _private_tree_directory(root, final.parent)
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
                "SELECT 1 FROM acquisition_records WHERE acquisition_id = ?",
                (pinned["acquisition_id"],)).fetchone():
            raise DownloadError(f"acquisition record {pinned['acquisition_id']!r} already exists")
        location_id = "loc-" + uuid.uuid4().hex
        location = final if result.complete else Path(stage)
        verified_at = result.verified_at if result.complete else None
        checksum_status = result.checksum_status if result.complete else verification.PUBLISHED
        connection.execute(
            "INSERT INTO artifact_locations (location_id, path, notes, created_at) VALUES (?, ?, '', ?)",
            (location_id, str(location), _now()))
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " origin_id, repository, revision, location_id, verified_at, notes, created_at)"
            " VALUES (?, ?, 'download', ?, ?, ?, ?, ?, ?, '', ?)",
            (pinned["acquisition_id"], pinned["repository"] + "@" + pinned["revision"],
             checksum_status, pinned["origin_id"], pinned["repository"], pinned["revision"],
             location_id, verified_at, _now()))
        checks = {item.file_name: item for item in result.files}
        for item in pinned["files"]:
            checked = checks[item["name"]]
            connection.execute(
                "INSERT INTO acquisition_files (acquisition_id, file_name, size_bytes,"
                " published_sha256, computed_sha256, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pinned["acquisition_id"], item["name"], item["size"], item["sha256"],
                 checked.computed_sha256 if result.complete else "", verified_at))
        if result.complete and not already_placed:
            if final.exists() or final.is_symlink():
                raise DownloadError(f"{final} already exists; no model file is overwritten")
            for descriptor in descriptors.values():
                os.fsync(descriptor)
            _write_placement_marker(stage, pinned)
            os.rename(stage, final)
            moved = True
            _sync_directory(root)
        connection.execute("COMMIT")
        return result
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if moved and final.exists() and not stage.exists():
            os.rename(final, stage)
        raise
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def require_space(destination, pinned, *, disk_usage=shutil.disk_usage):
    """Refuse before launch unless the saved root can hold the remaining bytes."""
    root = Path(destination)
    stage = _stage_path(root, pinned)
    required = 0
    reclaim = 0
    try:
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            raise DownloadError(f"model storage {root} is not accessible for downloading")
        if stage.is_symlink() or (stage.exists() and not stage.is_dir()):
            raise DownloadError(f"refusing unsafe staging directory {stage}")
        if _owns_placement(_final_path(root, pinned), pinned):
            return 0
        for item in pinned["files"]:
            part = stage.joinpath(*item["name"].split("/"))
            retained = 0
            if part.is_file() and not part.is_symlink():
                size = part.stat().st_size
                if item["sha256"] is None or size > item["size"]:
                    reclaim += size
                elif 0 <= size < item["size"]:
                    retained = size
                elif size == item["size"]:
                    digest, actual_size = _hash(part)
                    if actual_size == item["size"] and digest == item["sha256"]:
                        retained = size
                    else:
                        reclaim += size
            required += item["size"] - retained
        free = disk_usage(root).free
    except DownloadError:
        raise
    except OSError as error:
        raise DownloadError(f"cannot check download space at {root}: {error}") from None
    if free + reclaim < required:
        raise DownloadError(f"not enough free space at {root}: {required - free - reclaim} bytes short "
                            f"for the remaining transfer and staging")
    return required


def pinned_test_url(url):
    """Whether the stored test-only URL is a loopback HTTP fixture."""
    parsed = urlsplit(url)
    return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "::1", "localhost")


def _verification_progress(connection, job_id, report):
    last_file, last_time = None, 0.0

    def update(name, transferred, total):
        nonlocal last_file, last_time
        now = time.monotonic()
        if name != last_file or transferred >= total or now - last_time >= 1.0:
            _report(connection, job_id, "verification", name, transferred, total, report)
            last_file, last_time = name, now

    return update


def _report(connection, job_id, phase, name, transferred, total, report):
    step = f"{name}: {transferred} of {total} bytes"
    connection.execute("UPDATE jobs SET phase = ? WHERE job_id = ?",
                       (json.dumps({"phase": phase, "file": name,
                                    "transferred": transferred, "total": total}), job_id))
    report(phase, step)


def _set_phase(connection, job_id, phase, report):
    report(phase, "")
    connection.execute("UPDATE jobs SET phase = ? WHERE job_id = ?",
                       (json.dumps({"phase": phase}), job_id))


def _private_tree_directory(root, path):
    """Create/check each owned descendant without traversing a symbolic link."""
    root = Path(root)
    path = Path(path)
    try:
        path.relative_to(root)
    except ValueError:
        raise DownloadError(f"refusing path outside model storage: {path}") from None
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            status = current.lstat()
        except OSError as error:
            raise DownloadError(f"cannot inspect staging directory {current}: {error}") from None
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
            raise DownloadError(f"refusing unsafe staging directory {current}")
        if current == path:
            os.chmod(current, 0o700)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _hash(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as body:
        for block in iter(lambda: body.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
