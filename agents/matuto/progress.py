"""Job progress: the phase and step a unit reports, and the tail of its own log.

The runner gives every unit one reporting interface: the path in the
`MATUTO_PROGRESS_FILE` environment variable, set for every work when its unit
starts, browser-started or terminal-started alike. A unit appends one line per
update, its phase and current step separated by a tab, and a reader takes the last
line the unit wrote. An absent file means the unit has reported nothing yet; a file
that cannot be read fails closed to its caller rather than reading as a job that
has said nothing.

A tail is the unit's own log and never the journal: bounded in bytes read, lines
and line length, with control characters removed, and a line that carries the
router API key or names or assigns a secret is replaced whole, so a secret never
appears even in part.
"""

import datetime
import os
import re
import stat
import time
from dataclasses import dataclass

from matuto import store

FILE_VARIABLE = "MATUTO_PROGRESS_FILE"
# The reported tail's bounds: read at most this many bytes, show this many lines,
# and cut a line at this many characters, as the manager's own logs are bounded.
TAIL_BYTES = 64 * 1024
TAIL_LINES = 60
TAIL_LINE = 400
REDACTED = "[redacted]"
# The manager's own log rule: an entry that carries the router API key, or that
# mentions or assigns a secret, is replaced whole rather than shown in part.
SECRET_LINE = re.compile(r"(?i)(api.?key|authorization|bearer|token|secret|password)")
SECRET_ASSIGN = re.compile(r"(?i)\b[a-z0-9_]*(?:secret|password|api_key|apikey|token)[a-z0-9_]*\s*[=:]")


@dataclass(frozen=True)
class Progress:
    """One work's progress as its unit reported it and the runner recorded it.

    `state` is `unknown` when a read failed; `unknown` then names why and the phase,
    step and log stay empty, so a lost read never shows as an old phase or a job
    that finished. `elapsed` runs from the recorded start and `duration` is the
    recorded total once the work has ended.
    """

    work_id: str
    state: str
    phase: str = ""
    step: str = ""
    elapsed: float = 0.0
    duration: float = 0.0
    log: str = ""
    log_truncated: bool = False
    unknown: str = ""


def report(phase, step, path=None):
    """Record one unit's latest phase and current step through the one interface.

    A unit the runner did not start has no file and records nothing rather than
    failing: progress is a report, never a reason to end work.
    """
    target = path or os.environ.get(FILE_VARIABLE)
    if not target:
        return
    prefix = os.environ.get("MATUTO_PROGRESS_PREFIX")
    if prefix:
        step = f"{prefix}, {step}"
    try:
        with open(target, "a", encoding="utf-8") as body:
            body.write(f"{_one_line(phase)[:TAIL_LINE]}\t{_one_line(step)[:TAIL_LINE]}\n")
    except OSError:
        # A report that cannot be written must never end the job that made it.
        return


def latest(path):
    """The last phase and step the unit reported, or None when it reported none.

    Only the end of the file is read, so a report that grew without bound costs the
    bound. A file that cannot be read raises, and the caller reports unknown rather
    than pretending the unit said nothing.
    """
    try:
        # O_NONBLOCK is what lets the regular-file check below reject a FIFO
        # instead of waiting for a writer that may never come.
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError(f"the progress report is not a private regular file: {path}")
        size = info.st_size
        if size > TAIL_BYTES:
            os.lseek(descriptor, size - TAIL_BYTES, os.SEEK_SET)
        raw = b""
        while len(raw) < TAIL_BYTES:
            chunk = os.read(descriptor, TAIL_BYTES - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    text = raw.decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        phase, separator, step = line.partition("\t")
        if separator:
            return phase, step
    return None


def tail(log_text, key, truncated=False):
    """The bounded, redacted tail of one unit's own log, one entry per line.

    Redaction runs on the whole line before the display cut, so a key that would
    straddle the cut is replaced rather than shown in part. A tail whose first line
    lost its start to the byte window is dropped for the same reason.
    """
    lines = str(log_text).splitlines()
    if truncated and lines:
        lines = lines[1:]
    excerpt = []
    for raw_line in lines[-TAIL_LINES:]:
        line = _one_line(raw_line)
        if not line:
            continue
        excerpt.append(REDACTED if _secret(line, key) else line[:TAIL_LINE])
    return "\n".join(excerpt)


def times(started_at, ended_at=""):
    """Elapsed and, once ended, total duration in seconds, from the recorded times."""
    start = _instant(started_at)
    if start is None:
        return 0.0, 0.0
    elapsed = max(time.time() - start, 0.0)
    end = _instant(ended_at) if ended_at else None
    if end is None:
        return elapsed, 0.0
    duration = max(end - start, 0.0)
    return duration, duration


def router_key(config_root=None):
    """The router API key from the path CUT-4 keeps, or empty when it cannot be read.

    Nothing returns this key to a caller: it is read only so a log line carrying it
    is recognized and replaced.
    """
    path = store.store_directory(config_root).parent / "pi-local" / "api-key"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024:
            return ""
        raw = os.read(descriptor, 1025)
    except OSError:
        return ""
    finally:
        os.close(descriptor)
    return raw.decode("utf-8", "replace").strip()


def _secret(line, key):
    """Whether one entry carries the router API key or names or assigns a secret."""
    return bool(key and key in line) or bool(SECRET_LINE.search(line)) or bool(SECRET_ASSIGN.search(line))


def _one_line(text):
    """One report or log entry on one line, without control characters."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text)).strip()


def _instant(text):
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None
