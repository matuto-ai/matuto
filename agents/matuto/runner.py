"""The one runner: every run, build and download as a transient systemd user unit.

Every launcher in the package submits here, the browser's routes and the CLI's
commands alike, so there is no second way to start work. Each start decides with
`matuto.admission`'s pure rules from the state as it is, records the admitted work
in the store in the same transaction that decided it, and only then starts the
unit. The units are the user manager's, not the manager process's, so closing the
browser or restarting the manager leaves work running.

Nothing here trusts a reading on its own. A unit is only an end when it
positively says it has finished: `deactivating` is still stopping, a unit that
cannot be asked about is unknown and never ended, and a signal is not evidence
that anything exited, so every end is confirmed by reading the unit again. Each end
is recorded once, by a conditional write, so a Stop racing a completion keeps the
completion and reports it. An operation in flight is a small file beside the
unit's log: a launch records the process that owns it and a Stop records the moment
it began, so every reader agrees on the cause of an end, a suspended launcher keeps
its slot, and a completion that finished before a Stop's first signal is kept rather
than rewritten as a cancellation.

While a unit runs, everything it writes goes into one private directory this runner
created for it: the unit's own log and an `evidence` directory handed to the command
as `MATUTO_RUN_EVIDENCE_DIR`. When the end is recorded, that run's summary, checks,
artifacts and bounded, redacted log go into the store in the same transaction as the
end itself, and the directory is removed only after that transaction committed. A
move that fails rolls back and leaves the directory for the next reader to retry, so
no run is ever half-moved and nothing is ever written into the checkout.

The host evidence for all of it is the fake `systemd-run`, `systemctl` and `script`
in `agents/tests/app/test_runner.py`, which record calls, simulate unit states and
run the command for real where the exit status is the point.

shortcut: the serving state and the card's holder are read through callables the
launcher supplies; until serving status and the router supply them the defaults read
unknown, and unknown refuses rather than admits.
"""

import fcntl
import hashlib
import json
import mimetypes
import os
import shlex
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from matuto import admission
from matuto import progress as progress_module
from matuto import redaction
from matuto import storage
from matuto import store

RUNNING = "running"
LAUNCHING = "launching"
UNKNOWN = "unknown"
COMPLETED, CANCELLED, INTERRUPTED = "completed", "cancelled", "interrupted"

# A stopped unit is answered within this many seconds before the runner sends
# SIGKILL itself, and then again before it gives up on the stop; a Stop never waits
# on a unit that ignores SIGTERM.
GRACEFUL_SECONDS = 20.0
POLL_SECONDS = 0.2
# A launch whose unit has not appeared by now was abandoned by a manager that died
# mid-launch, which is recorded errored rather than left holding the slot for ever.
LAUNCH_PENDING_SECONDS = 120
LOG_LIMIT = 65536
CALL_TIMEOUT = 60

# One private directory per work holds its unit's own log and the evidence directory
# its procedure is handed. The runner creates the pair before the unit starts and
# removes it only after the store has committed that work's end, so while a procedure
# runs its evidence is nowhere but here.
LOG_NAME = "log"
EVIDENCE_DIRECTORY = "evidence"
SUMMARY_NAME = "summary.txt"
CHECKS_NAME = "checks.json"
EVIDENCE_ENV = "MATUTO_RUN_EVIDENCE_DIR"
CHECK_STATUSES = ("PASS", "FAIL", "ERROR", "SKIP")
# One artifact's own cap, so a procedure that writes without bound cannot fill the
# store; the run_evidence mark says the bytes kept are not the whole file.
EVIDENCE_LIMIT = 1024 * 1024

# The six properties one unit is read by. The monotonic state-change time orders
# completion against a Stop without wall-clock rounding or daylight-saving changes.
LOAD_STATE, ACTIVE_STATE, SUB_STATE, RESULT, EXIT_STATUS, END_MOMENT = (
    "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus",
    "StateChangeTimestampMonotonic")
NOT_FOUND = "not-found"
EXITED = "exited"
ALIVE = ("activating", "active", "deactivating", "reloading")

# What this runner is doing to a unit right now, each name a file beside the unit's
# log, so that a second runner reading the store reaches the same conclusion about
# the cause of an end. A launch and a Stop are separate files: a Stop that reaches
# work whose unit is still on its way does not erase the launch that is in flight.
STOPPING = "stopping"

_RUN_SELECT = (
    "SELECT run_id AS work_id, procedure AS kind, procedure || ' for ' || record_label"
    " AS purpose, record_kind, record_id, record_label, fingerprint, procedure_revision,"
    " unit_name AS unit, started_at, outcome, ended_at, log, log_truncated FROM runs")
_JOB_SELECT = (
    "SELECT job_id AS work_id, kind, pinned_request AS purpose, '' AS record_kind, '' AS"
    " record_id, '' AS record_label, '' AS fingerprint, '' AS procedure_revision,"
    " unit_name AS unit, started_at, outcome, ended_at, log, log_truncated, phase FROM jobs")


@dataclass(frozen=True)
class Target:
    """The stored record a run measures, with the fingerprint it had then."""

    kind: str
    id: str
    label: str
    fingerprint: str


@dataclass(frozen=True)
class Request:
    """One request to start work: what it is, what it is for and how to run it.

    `kind` is the admission kind — one of `matuto.admission`'s run kinds for a run,
    `build` or `download` for the shared jobs. `purpose` is the text a refusal
    names, and `command` is the argv the unit runs.
    """

    kind: str
    purpose: str
    command: tuple
    pty: bool = False
    record_kind: str = ""
    record_id: str = ""
    record_label: str = ""
    fingerprint: str = ""
    procedure_revision: str = ""
    dirty_checkout: bool = False
    pinned_request: str = ""
    destination: str = ""

    @classmethod
    def run(cls, procedure, purpose, command, target, procedure_revision, *, pty=False,
            dirty_checkout=False):
        """A run of one procedure against one stored record at its fingerprint."""
        return cls(kind=procedure, purpose=purpose, command=tuple(command), pty=pty,
                   record_kind=target.kind, record_id=target.id, record_label=target.label,
                   fingerprint=target.fingerprint, procedure_revision=procedure_revision,
                   dirty_checkout=dirty_checkout)

    @classmethod
    def job(cls, kind, purpose, command, pinned_request="", *, destination=""):
        """A pinned job; downloads also carry the confirmed model-storage destination."""
        if kind not in (admission.BUILD, admission.DOWNLOAD):
            raise ValueError(f"{kind} is not a shared job kind.")
        return cls(kind=kind, purpose=purpose, command=tuple(command),
                   pinned_request=pinned_request or " ".join(command), destination=destination)


@dataclass(frozen=True)
class Work:
    """One recorded work as the runner and the pages read it.

    `outcome` is what the store holds and stays empty until one end is recorded;
    `state` is what is true now, so a systemd that does not answer reads `unknown`
    and never `completed`.
    """

    work_id: str
    kind: str
    purpose: str
    unit: str
    started_at: str
    outcome: str = ""
    state: str = RUNNING
    record_kind: str = ""
    record_id: str = ""
    record_label: str = ""
    fingerprint: str = ""
    procedure_revision: str = ""
    log: str = ""
    log_truncated: bool = False
    ended_at: str = ""


@dataclass(frozen=True)
class Start:
    """The answer to one start request: the decision, why, and what was recorded."""

    decision: str
    reason: str = ""
    work: Work = None


class EvidenceError(Exception):
    """A run's evidence was not moved: the store is unchanged and its directory stays."""


class Runner:
    """The one runner. Every launcher builds one and calls `start`, `stop`, `works`."""

    def __init__(self, config_root=None, *, serving_state=None, card_holder=None,
                 card_holder_with_connection=None, graceful=GRACEFUL_SECONDS,
                 sleep=time.sleep, opened_store=None):
        self.store = opened_store or store.open_store(config_root)
        self._owns_store = opened_store is None
        # A launch holds the write lock for as long as systemd-run may take, so a
        # second request waits for the truth rather than failing on the lock.
        self.store.connection().execute(f"PRAGMA busy_timeout = {2 * CALL_TIMEOUT}000")
        # Both defaults read unknown, which refuses: a launcher that cannot see the
        # serving state or the card's holder has not earned an admission.
        self.serving_state = serving_state or (lambda: admission.SERVING_UNKNOWN)
        self.card_holder = card_holder or (lambda: admission.Card(admission.CARD_UNKNOWN))
        self.card_holder_with_connection = card_holder_with_connection
        # The root the router key is read from at each progress read, never shown.
        self.config_root = config_root
        self.graceful = graceful
        self.sleep = sleep
        self.launch = self._start_unit
        self.logs = store.store_directory(config_root) / "work"
        # The router key is read here only so a line of a unit's log that carries it
        # is replaced whole; nothing returns it to a caller.
        self.log_key = redaction.router_key(config_root)

    def close(self):
        if self._owns_store:
            self.store.close()

    # ---- starting ---------------------------------------------------------

    def start(self, request):
        """Decide, record and start. The decision and the record are one transaction."""
        if request.kind not in admission.SLOT_KINDS:
            # Serving starts belong to the router's own launcher; this runner
            # starts the work that holds the slot.
            return Start(admission.REFUSE, f"{request.kind} is not work this runner starts.")
        if not self._answers():
            return Start(admission.REFUSE,
                         f"systemd did not answer for this user, so the {request.kind} for "
                         f"{request.purpose} is refused rather than started.")
        connection = self.store.connection()
        work_id = None
        ended = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            if request.kind == admission.DOWNLOAD:
                try:
                    destination = storage.download_destination(connection)
                except store.StoreError as error:
                    connection.execute("ROLLBACK")
                    return Start(admission.REFUSE, str(error))
                if request.destination != destination:
                    connection.execute("ROLLBACK")
                    return Start(admission.REFUSE,
                                 f"The confirmed download destination {request.destination!r} is"
                                 f" no longer the saved model storage {destination!r}; review the"
                                 " download again before starting.")
            # What has already ended is settled first, in this same transaction, so a
            # finished unit never refuses the next request until an unrelated reader
            # happens to look at it.
            _, ended = self._settle(connection)
            ended = ended + self._leftover(connection)
            serving = self.serving_state()
            card_holder = (self.card_holder_with_connection(connection)
                           if self.card_holder_with_connection else self.card_holder())
            decision = admission.decide(request.kind, request.purpose, _current_work(connection),
                                        serving, card_holder)
            if decision.outcome != admission.ADMIT:
                # A refusal records nothing, so it leaves no pending entry behind.
                connection.execute("ROLLBACK")
                return Start(decision.outcome, decision.reason)
            work_id, unit = _record(connection, request, _now())
            # Written before the row is visible, so a reader never sees this work
            # without knowing that its unit is still on its way.
            self._mark(work_id, LAUNCHING)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            if work_id is not None:
                self._clear_all(work_id)
            raise
        self._discard(ended)
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        settled = self.work(work_id)
        if settled.outcome:
            # A reader settled this request while it waited to be launched, so there
            # is no work left to start and no unit that belongs to this runner.
            return Start(admission.REFUSE,
                         f"the request was settled as {settled.outcome} while it waited to "
                         "be launched; nothing was started.", settled)
        self._prepare(work_id)
        reading = self.launch(request, work_id, unit)
        if reading.returncode == 0:
            return Start(admission.ADMIT, "", self._launched(work_id, unit))
        # A non-zero launch can still have created the unit (for example, Ctrl-C
        # interrupted systemd-run after the user manager accepted it). The unit state,
        # not the launcher's status alone, decides whether work exists.
        answer = self._unit_state(unit)
        if answer is None:
            return Start(admission.ADMIT,
                         "the launch did not complete and systemd would not say; the work "
                         "keeps the slot until it can be told apart.",
                         replace(self.work(work_id), state=UNKNOWN))
        if answer[LOAD_STATE] != NOT_FOUND:
            if _alive(answer):
                return Start(admission.ADMIT, "", self._launched(work_id, unit))
            # systemd-run can return the procedure's nonzero exit status after the
            # unit already finished; settle it now so its outcome and evidence persist.
            work = self._settle_end(work_id, _ended_outcome(
                request.kind, answer[RESULT], answer[EXIT_STATUS]))
            return Start(admission.ADMIT, "", work)
        # A unit that never started is not running work: it is recorded errored, which
        # frees the slot for the next request.
        outcome = _failure_outcome(request.kind)
        work = self._settle_end(work_id, outcome)
        return Start(admission.ADMIT, f"the unit did not start; the work is recorded {outcome}.",
                     work)

    def _launched(self, work_id, unit):
        """The work as it stands once its unit exists, whoever launched it.

        A Stop that arrived while the unit was on its way is honoured here, and a
        request a reader settled in the meantime has its unit ended and confirmed
        rather than left running unrecorded.
        """
        work = self.work(work_id)
        if work.outcome:
            self._end_unit(unit)
            return work
        self._clear(work_id, LAUNCHING)
        if self._mark_of(work_id)[0] == STOPPING:
            return self.stop(work_id)
        return work

    # ---- stopping ---------------------------------------------------------

    def stop(self, work_id):
        """End the whole unit once, gracefully then forcibly after a bounded wait."""
        work = self.work(work_id)
        if work is None or work.outcome:
            return work
        lock = self._lock_log(work_id)
        try:
            return self._stop_locked(work_id)
        finally:
            self._unlock_log(lock)

    def _stop_locked(self, work_id):
        """Stop after acquiring this work's lock, rechecking its recorded outcome."""
        work = self.work(work_id)
        if work is None or work.outcome:
            return work
        answer = self._unit_state(work.unit)
        if answer is None:
            return replace(work, state=UNKNOWN)
        if answer[LOAD_STATE] == NOT_FOUND:
            if self._launching(work_id):
                # Its unit has not appeared yet, so there is nothing here to end: the
                # request to stop is kept, and the launcher stops the unit if it comes
                # back. The work keeps the slot until one of them happens.
                self._mark(work_id, STOPPING)
                return replace(self.work(work_id), state=LAUNCHING)
            return self._settle_end(work_id, INTERRUPTED)
        if not _alive(answer):
            operation, since = self._mark_of(work_id)
            # Only a stopping mark supplies a cancellation instant; a launch mark is
            # unrelated to when this Stop took effect.
            return self._settle_end(work_id, _settled_end(
                work.kind, answer, since if operation == STOPPING else None))
        # Record the monotonic instant immediately before the first signal. Wall-clock
        # timestamps lose both subsecond order and a stable timezone across DST changes.
        since = self._mark(work_id, STOPPING)
        answer, signalled = self._end_unit(work.unit)
        if answer is None:
            return replace(self.work(work_id), state=UNKNOWN)
        if answer[LOAD_STATE] == NOT_FOUND:
            return self._settle_end(work_id, INTERRUPTED)
        if _alive(answer):
            # Still running, or refusing to say: the work keeps the slot.
            return self.work(work_id)
        outcome = (_ended_outcome(work.kind, answer[RESULT], answer[EXIT_STATUS])
                   if signalled is False else _settled_end(work.kind, answer, since))
        return self._settle_end(work_id, outcome)

    # ---- reading the store back -------------------------------------------

    def live_works(self):
        """Unfinished work that is live or unknown, without settling store records."""
        live = []
        for work in unfinished(self.store.connection()):
            answer = self._unit_state(work.unit)
            if (answer is None or _alive(answer)
                    or answer[LOAD_STATE] == NOT_FOUND and self._launching(work.work_id)):
                live.append(work)
        return live

    def works(self):
        """Every recorded work, newest first, with its end settled first.

        This is the one place a launcher looks, after a manager restart as much as
        in a live page: it finds the units the store names, records each end once,
        and answers `unknown` for every unit systemd will not talk about.
        """
        connection = self.store.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            silent, ended = self._settle(connection)
            ended += self._leftover(connection)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        self._discard(ended)
        return [replace(entry, state=UNKNOWN) if entry.work_id in silent else entry
                for entry in self._every_work()]

    def work(self, work_id):
        """One recorded work, or None. No writes."""
        return _work(self.store.connection(), work_id)

    def output(self, work):
        """This unit's bounded output tail, as the store records it on completion."""
        return self._log(work.work_id)[0]

    def output_chunk(self, work, offset):
        """Read the next bounded byte chunk from a run's append-only output log."""
        path = self._log_path(work.work_id)
        try:
            with path.open("rb") as body:
                body.seek(offset)
                chunk = body.read(LOG_LIMIT)
        except OSError:
            chunk = work.log.encode("utf-8") if offset == 0 else b""
        return chunk, offset + len(chunk)

    def progress(self, work):
        """One work's progress: phase, step, times and the tail of its own log.

        Read-only: it settles no end and writes nothing, so the same answer comes
        back after a manager restart. It reads only what the job recorded — its own
        report and its own log, never the journal. A unit systemd will not talk
        about, a report or log that cannot be read, and a tail whose router key
        cannot be read so it cannot be certified clean, all read unknown rather than
        an old phase or a job that finished.
        """
        if work is None:
            return None
        elapsed, duration = progress_module.times(work.started_at, work.ended_at)
        answer = self._unit_state(work.unit)
        if answer is None:
            return progress_module.Progress(
                work.work_id, UNKNOWN, elapsed=elapsed, duration=duration,
                unknown=f"systemd did not answer about {work.unit}, so its progress is unknown.")
        try:
            reported = progress_module.latest(self._progress_path(work.work_id))
        except OSError:
            return progress_module.Progress(
                work.work_id, UNKNOWN, elapsed=elapsed, duration=duration,
                unknown="the job's own progress report could not be read.")
        phase, step = reported or ("", "")
        if _alive(answer):
            state = RUNNING
        elif work.outcome:
            state = work.outcome
        else:
            # The unit is gone and no end is recorded yet: the phase it last
            # reported is stale, so it is not shown as the current one.
            return progress_module.Progress(
                work.work_id, UNKNOWN, elapsed=elapsed, duration=duration,
                unknown=f"the unit for {work.work_id} is gone with no recorded end.")
        # No tail is shown unless the key that must never appear in it was read.
        key = progress_module.router_key(self.config_root)
        if not key:
            return progress_module.Progress(
                work.work_id, UNKNOWN, elapsed=elapsed, duration=duration,
                unknown="the router API key could not be read, so the job's tail is unknown.")
        try:
            if work.outcome:
                body, truncated = work.log, work.log_truncated
            else:
                body, truncated = self._own_log(work.work_id)
        except OSError:
            return progress_module.Progress(
                work.work_id, UNKNOWN, elapsed=elapsed, duration=duration,
                unknown="the job's own log could not be read.")
        return progress_module.Progress(
            work.work_id, state, phase=phase, step=step, elapsed=elapsed,
            duration=duration, log=progress_module.tail(body, key, truncated),
            log_truncated=truncated)

    def _progress_path(self, work_id):
        return self.logs / f"{work_id}.progress"

    def _every_work(self):
        connection = self.store.connection()
        rows = [connection.execute(_RUN_SELECT).fetchall(),
                connection.execute(_JOB_SELECT).fetchall()]
        return sorted((_work_from(row) for table in rows for row in table),
                      key=lambda work: work.started_at, reverse=True)

    # ---- settling the store against the host ------------------------------

    def _settle(self, connection):
        """Record the end of every recorded work that has ended. Each end once.

        Answers the recorded work whose unit systemd would not talk about, so the
        caller reports those as unknown rather than as anything else, and the work
        whose end this call recorded, so the caller removes those private directories
        only after the transaction that keeps their evidence has committed.
        """
        silent, ended = set(), []
        open_rows = _open_work(connection)
        nested_units = {row["unit"] for row in open_rows if row["kind"] == "context_search"}
        open_rows.sort(key=lambda row: (row["unit"] not in nested_units,
                                        row["kind"] == "context_search", row["started_at"]))
        for row in open_rows:
            work_id, unit, kind = row["work_id"], row["unit"], row["kind"]
            lock = self._lock_log(work_id, blocking=False)
            if lock is None:
                silent.add(work_id)
                continue
            try:
                # Keep Stop from signaling between the host observation and the
                # conditional outcome write; a live Stop owns its evidence.
                answer = self._unit_state(unit)
                operation, since = self._mark_of(work_id)
                if answer is None:
                    silent.add(work_id)
                    continue
                if operation == STOPPING:
                    if _alive(answer):
                        continue
                    if answer[LOAD_STATE] == NOT_FOUND and self._launching(work_id):
                        continue
                    if answer[LOAD_STATE] == NOT_FOUND:
                        outcome = INTERRUPTED
                    else:
                        outcome = _settled_end(kind, answer, since)
                    outcome = _nested_rung_outcome(connection, work_id, kind, answer, outcome, self)
                    if self._record_end(connection, work_id, outcome):
                        ended.append(work_id)
                    continue
                if answer[LOAD_STATE] == NOT_FOUND:
                    if self._launching(work_id):
                        continue
                    outcome = (_failure_outcome(kind) if operation == LAUNCHING
                               else INTERRUPTED)
                    outcome = _nested_rung_outcome(connection, work_id, kind, answer, outcome, self)
                    if self._record_end(connection, work_id, outcome):
                        ended.append(work_id)
                    continue
                if _alive(answer):
                    self._clear(work_id, LAUNCHING)
                    continue
                outcome = _ended_outcome(kind, answer[RESULT], answer[EXIT_STATUS])
                outcome = _nested_rung_outcome(connection, work_id, kind, answer, outcome, self)
                if self._record_end(connection, work_id, outcome):
                    ended.append(work_id)
            finally:
                self._unlock_log(lock)
        return silent, ended

    def _launching(self, work_id):
        """Whether a launch that has not yet been seen is still in flight.

        The age is only consulted once the launcher that owns it is gone: a process
        suspended between the recorded row and its unit is slow, not finished, and its
        slot is not reclaimed while it can still start that unit. The owner is named
        by its process number and the kernel's start time for it, because a number
        alone is reused by an unrelated process after logout or reboot.
        """
        try:
            path = self.logs / f"{work_id}.{LAUNCHING}"
            fields = path.read_text().split()
            age = time.time() - path.stat().st_mtime
        except (OSError, IndexError):
            return False
        return age < LAUNCH_PENDING_SECONDS or _process_alive(fields[1:3], fields[3:4])

    def _lock_log(self, work_id, *, blocking=True):
        """Lock reconciliation and Stop on the unit's existing output file.

        The file is inside the work's own private directory, so the lock, the log it
        protects and the evidence beside it live and end together.
        """
        lock = self._log_path(work_id)
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = lock.open("a")
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    @staticmethod
    def _unlock_log(lock):
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()

    def _mark(self, work_id, operation):
        """Record what this runner is doing to the unit right now, and when it began.

        A launch and a Stop are separate files, so a Stop that reaches work whose unit
        is still on its way does not erase the fact that the launch is in flight. A
        launch also records the process that owns it, so an old launch can tell a
        suspended launcher from one that died.
        """
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        if operation == STOPPING:
            stamp = str(time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000)
            body = stamp
        else:
            stamp = _stamp()
            body = f"{stamp} {os.getpid()} {_process_identity(os.getpid())} {_boot_id()}"
        (self.logs / f"{work_id}.{operation}").write_text(body)
        return stamp

    def _mark_of(self, work_id):
        """The operation in flight and the moment it began, as another runner left it.

        A Stop outranks a launch: work that is being cancelled is being cancelled
        whether or not its unit has appeared yet.
        """
        for operation in (STOPPING, LAUNCHING):
            try:
                return operation, (self.logs / f"{work_id}.{operation}").read_text().split()[0]
            except (OSError, IndexError):
                continue
        return None, ""

    def _clear(self, work_id, operation):
        """Drop this mark, but only while it still names the same operation.

        An observation taken before a Stop wrote its mark must not erase it: the other
        reader has to reach the same conclusion about the cause of the end.
        """
        (self.logs / f"{work_id}.{operation}").unlink(missing_ok=True)

    def _clear_all(self, work_id):
        for operation in (LAUNCHING, STOPPING):
            self._clear(work_id, operation)

    def _prepare(self, work_id):
        """The private directory this work's unit writes its evidence into.

        One directory per work, owned by the operator and closed to group and others,
        holding the unit's own log and an `evidence` directory the procedure is handed
        through `MATUTO_RUN_EVIDENCE_DIR`. It is never under the checkout, and it is
        removed only after the store has committed the work's end.
        """
        directory = self._work_directory(work_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        evidence = directory / EVIDENCE_DIRECTORY
        evidence.mkdir(exist_ok=True, mode=0o700)
        os.chmod(evidence, 0o700)
        return evidence

    def _work_directory(self, work_id):
        return self.logs / work_id

    def _log_path(self, work_id):
        return self._work_directory(work_id) / LOG_NAME

    def _evidence_dir(self, work_id):
        return self._work_directory(work_id) / EVIDENCE_DIRECTORY

    def _discard(self, work_ids):
        """Remove the private directories whose evidence the store now keeps.

        Called only after the transaction that recorded those ends committed, so a
        move that failed leaves the directory in place to be tried again. A directory
        that will not go now is left for the next reader rather than failing the read:
        the store already keeps its evidence, so a failed removal costs disk, not data.
        """
        for work_id in work_ids:
            try:
                shutil.rmtree(self._work_directory(work_id))
            except OSError:
                continue

    def _leftover(self, connection):
        """The private directories of work the store no longer keeps open.

        A process that died between committing a run's end and removing its directory
        leaves the directory behind, and so does a run the operator deleted through the
        store: neither is open, so the next call that settles the store finishes the
        move. A directory whose work is still open is left alone, and a directory is
        always created after its row is committed, so none is ever removed under a
        launch that is still on its way.
        """
        if not self.logs.is_dir():
            return []
        open_ids = {row["work_id"] for row in _open_work(connection)}
        return sorted(directory.name for directory in self.logs.iterdir()
                      if directory.is_dir() and directory.name not in open_ids)

    def _evidence(self, work_id):
        """The procedure's own evidence as the store keeps it: summary, checks, artifacts.

        The procedure writes `summary.txt` and a `checks.json` list of `{name, status,
        detail}`, and every other file is an artifact, under whatever subdirectory it
        wrote. An artifact is bounded like the log, with the truncation mark saying the
        bytes in the store are not the whole file, so a procedure that writes without
        bound cannot fill the store. A document this cannot read refuses the whole move,
        so a malformed run is never half-moved.
        """
        directory = self._evidence_dir(work_id)
        summary, checks, artifacts = "", [], []
        run = self.store.connection().execute(
            "SELECT procedure, unit_name FROM runs WHERE run_id = ?", (work_id,)).fetchone()
        if run is not None and run["procedure"] == "context_search":
            _purge_wq_private(directory)
            _recover_context_search(directory, run["unit_name"], self.store.connection())
        elif run is not None and run["procedure"] in {"wq", "bundle"}:
            parent = self.store.connection().execute(
                "SELECT run_id FROM runs WHERE procedure='context_search' AND unit_name=?"
                " AND run_id != ? ORDER BY started_at DESC LIMIT 1",
                (run["unit_name"], work_id)).fetchone()
            if parent is not None:
                for rung in (self._evidence_dir(parent["run_id"]) / "rungs").glob("*"):
                    try:
                        marker = json.loads((rung / "context-rung.json").read_text())
                    except (OSError, ValueError):
                        continue
                    if marker.get("run_id") == work_id:
                        directory = rung
                        break
            if run["procedure"] == "wq":
                _purge_wq_private(directory)
        for path in sorted(directory.rglob("*")) if directory.is_dir() else []:
            if not path.is_file():
                continue
            name = str(path.relative_to(directory))
            if name == SUMMARY_NAME:
                summary = path.read_text("utf-8", "replace")
            elif name == CHECKS_NAME:
                checks = _checks(work_id, path.read_text("utf-8", "replace"))
            else:
                with path.open("rb") as handle:
                    body = handle.read(EVIDENCE_LIMIT + 1)
                truncated = len(body) > EVIDENCE_LIMIT
                body = body[:EVIDENCE_LIMIT]
                artifacts.append((name, mimetypes.guess_type(name)[0] or "",
                                  len(body), hashlib.sha256(body).hexdigest(), int(truncated),
                                  body))
        if run is not None and run["procedure"] == "wq" and not checks:
            report = directory / "wq" / "report.json"
            if report.is_file():
                try:
                    rows = json.loads(report.read_text("utf-8"))["results"]
                    checks = _checks(work_id, json.dumps([
                        {"name": row["id"], "status": row["status"],
                         "detail": row.get("reason", "")} for row in rows]))
                except (OSError, TypeError, ValueError, KeyError):
                    raise EvidenceError(
                        f"the evidence for {work_id} was not moved into the store: "
                        "WQ report.json has no readable check results") from None
        return summary, checks, artifacts

    def _record_end(self, connection, work_id, outcome):
        """Record the one end of this work inside the caller's transaction.

        The conditional write is what makes a Stop racing a completion harmless:
        whichever writer reaches the row first ends it, and the other reads back the
        outcome that was kept rather than writing over it. A run's whole evidence —
        summary, checks, artifacts and its capped, redacted log — is written in that
        same transaction, so a failure anywhere leaves the store exactly as it was and
        the run's private directory in place to be moved when a later reader finds the
        unit ended. Answers whether this call recorded the end; a call that lost the
        race writes nothing and leaves the directory to the writer that won it. Jobs
        keep only their log here; build and download jobs own their artifacts and output
        digests.
        """
        table = _table_of(connection, work_id)
        if table is None:
            return False
        body, truncated = self._log(work_id, strict=True)
        ended_at = _now()
        if table == "runs":
            try:
                summary, checks, artifacts = self._evidence(work_id)
                recorded = store.write_run_evidence(
                    connection, work_id, outcome=outcome, ended_at=ended_at, summary=summary,
                    log=body, log_truncated=truncated, checks=checks, artifacts=artifacts)
            except EvidenceError:
                raise
            except Exception as error:
                raise EvidenceError(
                    f"the evidence for {work_id} was not moved into the store and its "
                    f"directory {self._work_directory(work_id)} is left in place: {error}"
                ) from None
            if not recorded:
                return False
        else:
            # shortcut: only a job's log is kept here; its own artifacts are removed
            # with its directory, and build and download jobs write their provenance and
            # output digests through the store when their procedures produce them.
            connection.execute(
                "UPDATE jobs SET outcome = ?, ended_at = ?, log = ?, log_truncated = ?"
                " WHERE job_id = ? AND outcome IS NULL",
                (outcome, ended_at, body, int(truncated), work_id))
        self._clear_all(work_id)
        return True

    def _settle_end(self, work_id, outcome):
        """Record this work's end in its own transaction and return it."""
        connection = self.store.connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            parent = connection.execute(
                "SELECT procedure, unit_name FROM runs WHERE run_id = ?", (work_id,)).fetchone()
            ended = []
            if parent is not None and parent["procedure"] == "context_search":
                children = connection.execute(
                    "SELECT run_id, procedure FROM runs WHERE unit_name=? AND outcome IS NULL AND run_id != ?",
                    (parent["unit_name"], work_id)).fetchall()
                for child in children:
                    child_outcome = _nested_rung_outcome(
                        connection, child["run_id"], child["procedure"], None, outcome, self)
                    if self._record_end(connection, child["run_id"], child_outcome):
                        ended.append(child["run_id"])
            recorded = self._record_end(connection, work_id, outcome)
            if recorded:
                ended.append(work_id)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        self._discard(ended)
        return self.work(work_id)

    # ---- the host ---------------------------------------------------------

    def _end_unit(self, unit):
        """Signal the whole control group, confirming both signal delivery and its end.

        The `-fail` target distinguishes a unit that already has no processes from a
        successful signal, so a concurrent natural completion keeps its own outcome.
        """
        term = self._systemctl("kill", "--kill-who=all", "--job-mode=fail", "--signal=SIGTERM", unit)
        if term.returncode != 0:
            no_process = term.answered and any(
                message in term.stderr.lower()
                for message in ("no such process", "no matching processes to kill"))
            return self._unit_state(unit), False if no_process else None
        answer = self._await_end(unit)
        if answer is not None and _alive(answer):
            killed = self._systemctl("kill", "--kill-who=all", "--job-mode=fail", "--signal=SIGKILL", unit)
            if killed.returncode == 0:
                answer = self._await_end(unit)
        return answer, True

    def _answers(self):
        """Whether this user's systemd answers at all. Silence admits nothing."""
        return self._systemctl("list-units", "--no-legend", "--no-pager").returncode == 0

    def _unit_state(self, unit):
        """One unit's systemd properties, or None when systemd does not answer."""
        result = self._systemctl(
            "show", unit, *(f"--property={key}" for key in (LOAD_STATE, ACTIVE_STATE, SUB_STATE,
                                                           RESULT, EXIT_STATUS, END_MOMENT)))
        if result.returncode != 0:
            # systemd before 230 names the unit instead of answering about it, which
            # is the same fact as LoadState=not-found rather than silence.
            if f"Unit {unit} could not be found" in result.stderr:
                return _properties(f"LoadState={NOT_FOUND}")
            return None
        properties = _properties(result.stdout)
        return None if not properties[LOAD_STATE] else properties

    def _await_end(self, unit):
        """Wait out the bounded grace, answering None when systemd goes silent."""
        waited = 0.0
        while waited < self.graceful:
            answer = self._unit_state(unit)
            if answer is None or answer[LOAD_STATE] == NOT_FOUND or not _alive(answer):
                return answer
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        return self._unit_state(unit)

    def _start_unit(self, request, work_id, unit):
        """Start the unit. Answers whether `systemd-run` itself reported a status."""
        log = str(self._log_path(work_id))
        command = list(request.command)
        # ExitType=cgroup is what "the unit ended" means here: systemd holds the unit
        # running while any process of its control group is left, so a command that
        # leaves a child behind cannot report itself done. RemainAfterExit then keeps
        # that finished unit loaded, so its result stays readable and a completed run
        # is never mistaken for an interrupted one; CollectMode=inactive keeps a failed
        # unit for the same reason.
        options = ["--user", f"--unit={unit}", "--property=Type=exec",
                   f"--setenv={progress_module.FILE_VARIABLE}={self._progress_path(work_id)}",
                   "--property=ExitType=cgroup", "--property=RemainAfterExit=yes",
                   "--property=KillMode=control-group", "--property=CollectMode=inactive",
                   # The one hand-off of the private directory: the procedure writes
                   # its summary, checks and artifacts there and nowhere else.
                   f"--setenv={EVIDENCE_ENV}={self._evidence_dir(work_id)}"]
        if request.pty:
            # A unit whose command needs a terminal gets one, and `script -e` keeps
            # the command's own exit status, which `script` without it loses.
            command = ["script", "-q", "-e", "-c", shlex.join(request.command), log]
        else:
            options += [f"--property=StandardOutput=append:{log}",
                        f"--property=StandardError=append:{log}"]
        return self._call("systemd-run", *options, "--", *command)

    def _systemctl(self, *arguments):
        # Every call is the user manager's, which is what makes the work outlive the
        # manager process and end at logout.
        return self._call("systemctl", "--user", *arguments)

    def _call(self, program, *arguments):
        """Run one host command and answer, including whether it answered at all.

        A timeout and a missing program are one fact, distinct from a status the
        command reported: a read that never came back tells a reader nothing, while a
        launch that never came back may still have been accepted by the user manager.
        """
        try:
            env = os.environ.copy() if program == "systemctl" else None
            if env is not None:
                env["LC_ALL"] = "C"
            finished = subprocess.run([program, *arguments], capture_output=True,
                                      text=True, timeout=CALL_TIMEOUT, check=False, env=env)
        except (subprocess.SubprocessError, OSError):
            return _UNANSWERED
        return Reading(finished.returncode, finished.stdout, finished.stderr)

    def _log(self, work_id, *, strict=False):
        """The tail of this unit's own output, bounded, redacted, with truncation marked.

        Only the tail is read, so a run whose output grew without bound — temporary
        serving has no natural end — costs the bound rather than the whole file. A
        partial first line is dropped before redaction because its start may contain
        a secret. Redaction can expand the text, so the final bound keeps the newest
        whole lines, which include the diagnostic that says how the work ended.

        Settlement refuses an unreadable log so the only copy stays beside the run;
        display reads stay lossy. An absent log is empty either way.
        """
        try:
            tail, truncated = self._own_log(work_id)
        except FileNotFoundError:
            return "", False
        except OSError as error:
            if not strict:
                return "", False
            raise EvidenceError(
                f"the log for {work_id} could not be read, so its evidence was not moved "
                f"into the store and its directory {self._work_directory(work_id)} is "
                f"left in place: {error}") from None
        lines = tail.split("\n")
        if truncated and lines:
            lines = lines[1:]
        text, cut = _bounded(redaction.redact("\n".join(lines), key=self.log_key))
        return text, truncated or cut

    def _own_log(self, work_id):
        """Read this unit's bounded log without following or blocking on files."""
        path = self._log_path(work_id)
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError(f"the log is not a private regular file: {path}")
            size = info.st_size
            if size > LOG_LIMIT:
                os.lseek(descriptor, size - LOG_LIMIT, os.SEEK_SET)
            tail = bytearray()
            while len(tail) < LOG_LIMIT:
                chunk = os.read(descriptor, LOG_LIMIT - len(tail))
                if not chunk:
                    break
                tail.extend(chunk)
        finally:
            os.close(descriptor)
        return bytes(tail).decode("utf-8", "replace"), size > LOG_LIMIT


def _recover_context_search(directory, unit_name, connection):
    """Give an abruptly ended search a durable no-winner result and its settled rungs."""
    result_path = directory / "context-search.json"
    try:
        json.loads(result_path.read_text())
        return
    except (OSError, ValueError):
        pass
    rung_rows = connection.execute(
        "SELECT run_id, fingerprint, outcome, summary FROM runs WHERE unit_name=?"
        " AND procedure IN ('wq','bundle') ORDER BY started_at", (unit_name,)).fetchall()
    rungs = []
    for row in rung_rows:
        marker_path, marker = None, {}
        for path in sorted((directory / "rungs").glob("*")):
            if not path.is_dir() or path.is_symlink():
                continue
            candidate = path / "context-rung.json"
            try:
                current = json.loads(candidate.read_text())
            except (OSError, ValueError):
                continue
            if current.get("run_id") == row["run_id"]:
                marker_path, marker = candidate, current
                break
        checks = [{"name": check["name"], "status": check["status"], "detail": check["detail"]}
                  for check in connection.execute(
                      "SELECT name, status, detail FROM checks WHERE run_id=? ORDER BY sequence",
                      (row["run_id"],))]
        if marker_path is not None:
            marker.update(outcome=row["outcome"], checks=checks)
            marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n")
        rungs.append({"context": marker.get("context_size"),
                      "label": marker.get("label"), "run_id": row["run_id"],
                      "fingerprint": row["fingerprint"], "outcome": row["outcome"],
                      "checks": checks})
    if not (directory / "checks.json").exists():
        (directory / "checks.json").write_text(json.dumps([{
            "name": "context search", "status": "SKIP",
            "detail": "The context search ended before it could name a winner."}]) + "\n")
    if not (directory / "summary.txt").exists():
        (directory / "summary.txt").write_text(
            "The context search ended before completion and names no winner.\n")
    result_path.write_text(json.dumps({
        "outcome": "no winner", "winner": None, "checked": False,
        "reason": "The context search ended before completion and names no winner.",
        "runs": [row["run_id"] for row in rung_rows], "rungs": rungs},
        indent=2, sort_keys=True) + "\n")


def _purge_wq_private(directory):
    """Delete WQ request/response working data before direct or nested evidence moves."""
    directory = Path(directory)
    roots = [directory]
    rungs = directory / "rungs"
    if rungs.is_dir():
        roots.extend(path for path in rungs.iterdir() if path.is_dir() and not path.is_symlink())
    for root in roots:
        wq = root / "wq"
        for raw in wq.glob("private-*"):
            if raw.is_dir() and not raw.is_symlink():
                shutil.rmtree(raw)
        private_home = root / "wq-private-home"
        if private_home.is_dir() and not private_home.is_symlink():
            shutil.rmtree(private_home)


def _bounded(text, limit=LOG_LIMIT):
    """The last `limit` UTF-8 bytes of `text`, whole lines, and whether it was cut."""
    body = text.encode("utf-8")
    if len(body) <= limit:
        return text, False
    body = body[len(body) - limit:]
    cut = body.find(b"\n")
    return body[cut + 1:].decode("utf-8", "replace") if cut != -1 else "", True


@dataclass(frozen=True)
class Reading:
    """What one host command said: its status, its output, and whether it said it."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    answered: bool = True


_UNANSWERED = Reading(1, "", "", answered=False)


def _now():
    """One instant, precise, for the store's own columns."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _checks(work_id, text):
    """The run's checks as the store's columns, refusing a document it cannot read.

    A procedure that writes this file has already done the work, so a malformed
    document is a reason to keep the directory and say so rather than to lose the
    run: the caller's transaction rolls back and the next reader tries again.
    """
    try:
        document = json.loads(text)
        checks = []
        for sequence, entry in enumerate(document):
            if entry["status"] not in CHECK_STATUSES:
                raise ValueError("status")
            checks.append((sequence, str(entry["name"]), entry["status"],
                           str(entry.get("detail", ""))))
    except (TypeError, ValueError, KeyError, AttributeError):
        raise EvidenceError(
            f"the evidence for {work_id} was not moved into the store: {CHECKS_NAME} is not "
            f"a list of {{name, status, detail}} checks with a status in "
            f"{', '.join(CHECK_STATUSES)}") from None
    return checks


def _stamp():
    """A readable timestamp for operation records that are not compared for order."""
    return datetime.now().replace(microsecond=0).isoformat(timespec="seconds")


def _alive(answer):
    """Whether a unit still has work of its own to do.

    A unit that exited and stayed loaded is `active (exited)` and is done; anything
    that is still starting, stopping or reloading is not, because its control group
    may still hold processes.
    """
    if answer[ACTIVE_STATE] == "active":
        return answer[SUB_STATE] != EXITED
    return answer[ACTIVE_STATE] in ALIVE


def _properties(text):
    """systemd's `key=value` lines as a mapping, every wanted key present."""
    found = {key: "" for key in (LOAD_STATE, ACTIVE_STATE, SUB_STATE, RESULT, EXIT_STATUS,
                                 END_MOMENT)}
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in found:
            found[name] = value.strip()
    return found


def unfinished(connection):
    """The store's unsettled work, read without settling or opening another store."""
    rows = [connection.execute(_RUN_SELECT).fetchall(),
            connection.execute(_JOB_SELECT).fetchall()]
    return [work for table in rows for work in (_work_from(row) for row in table)
            if not work.outcome]


def _current_work(connection):
    """The one work holding the slot, as `matuto.admission` names it, or None."""
    for row in _open_work(connection):
        return admission.Work(row["kind"], row["purpose"])
    return None


def _open_work(connection):
    """Every recorded work with no end yet, in one shape across both tables."""
    rows = [connection.execute(_RUN_SELECT + " WHERE outcome IS NULL").fetchall(),
            connection.execute(_JOB_SELECT + " WHERE outcome IS NULL").fetchall()]
    return sorted((row for table in rows for row in table), key=lambda row: row["started_at"])


def _work_from(row):
    outcome = row["outcome"] or ""
    if row["kind"] == admission.DOWNLOAD and "phase" in row.keys():
        try:
            if json.loads(row["phase"]).get("phase") == "discarded":
                outcome = "discarded"
        except (AttributeError, TypeError, ValueError):
            pass
    return Work(work_id=row["work_id"], kind=row["kind"], purpose=row["purpose"],
                unit=row["unit"], started_at=row["started_at"], outcome=outcome,
                state=outcome or RUNNING, record_kind=row["record_kind"],
                record_id=row["record_id"], record_label=row["record_label"],
                fingerprint=row["fingerprint"], procedure_revision=row["procedure_revision"],
                log=row["log"] or "", log_truncated=bool(row["log_truncated"]),
                ended_at=row["ended_at"] or "")


def _work(connection, work_id):
    """The recorded work as the launcher reads it, or None."""
    row = connection.execute(_RUN_SELECT + " WHERE run_id = ?", (work_id,)).fetchone()
    if row is None:
        row = connection.execute(_JOB_SELECT + " WHERE job_id = ?", (work_id,)).fetchone()
    return None if row is None else _work_from(row)


def _table_of(connection, work_id):
    """Which table holds this work, or None when the store holds none."""
    for table, column in (("runs", "run_id"), ("jobs", "job_id")):
        if connection.execute(f"SELECT 1 FROM {table} WHERE {column} = ?",
                              (work_id,)).fetchone() is not None:
            return table
    return None


def _record(connection, request, started_at):
    """The one row for this work: kind, purpose, unit, start time and no end yet."""
    token = uuid.uuid4().hex[:16]
    work_id = f"{request.kind}-{token}"
    unit = f"matuto-{request.kind}-{token}.service"
    if request.kind in admission.RUN_KINDS:
        connection.execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, fingerprint,"
            " build_id, procedure, procedure_revision, dirty_checkout, unit_name, started_at)"
            " VALUES (?, ?, ?, ?, ?, (SELECT build_id FROM runtime_selection WHERE selection_id = 1),"
            " ?, ?, ?, ?, ?)",
            (work_id, request.record_kind, request.record_id, request.record_label,
             request.fingerprint, request.kind, request.procedure_revision,
             int(request.dirty_checkout), unit, started_at))
    else:
        connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (work_id, request.kind, unit, request.pinned_request, started_at))
    return work_id, unit


def _nested_rung_outcome(connection, work_id, kind, answer, outcome, runner):
    """A nested rung ends with its parent; an unmarked abrupt end is interrupted."""
    if outcome == CANCELLED:
        return outcome
    if kind not in ("wq", "bundle"):
        return outcome
    child = connection.execute("SELECT unit_name FROM runs WHERE run_id=?", (work_id,)).fetchone()
    if child is None:
        return outcome
    parent = connection.execute(
        "SELECT run_id FROM runs WHERE procedure='context_search' AND unit_name=?"
        " AND run_id != ? ORDER BY started_at DESC LIMIT 1",
        (child["unit_name"], work_id)).fetchone()
    if parent is None:
        return outcome
    operation, since = runner._mark_of(parent["run_id"])
    if operation == STOPPING and answer is not None:
        return _settled_end(kind, answer, since)
    return INTERRUPTED


def _settled_end(kind, answer, since):
    """Keep a completion only when its unit transition preceded the Stop request."""
    if not since:
        return _ended_outcome(kind, answer[RESULT], answer[EXIT_STATUS])
    try:
        requested, ended = int(since), int(answer[END_MOMENT])
    except (TypeError, ValueError):
        return CANCELLED
    if ended < requested:
        return _ended_outcome(kind, answer[RESULT], answer[EXIT_STATUS])
    return CANCELLED


def _boot_id():
    """The kernel boot this process identity belongs to."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _process_identity(pid):
    """The kernel's start time for a process, which a reused number does not keep."""
    try:
        # /proc/<pid>/stat's comm field can hold spaces, so the fields after the
        # closing parenthesis are the only safe place to count from.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def _process_alive(fields, boot=()):
    """Whether this process incarnation is still alive in the recorded boot."""
    if not boot or not boot[0] or boot[0] != _boot_id():
        return False
    try:
        pid = int(fields[0])
    except (IndexError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return fields[1:] == [_process_identity(pid)]


def _ended_outcome(kind, result, exit_status):
    """What a unit's own result records: a clean exit completes, anything else errors."""
    if result == "success" and exit_status in ("0", ""):
        return COMPLETED
    return _failure_outcome(kind)


def _failure_outcome(kind):
    return "failed" if kind in (admission.BUILD, admission.DOWNLOAD) else "errored"