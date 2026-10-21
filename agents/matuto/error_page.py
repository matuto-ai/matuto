"""The Matuto error page: one private local file saying why the manager is down.

A caller supplies the cause and the manager's log text; this module bounds,
redacts and escapes both, so neither can put markup, links or buttons on the
page. The page sits at one fixed private path (0600 in a private directory), has
no buttons or forms and never refreshes itself, and each write replaces the whole
previous page. The Matuto app-menu entry owns when to write and open it;
see START-3 in docs/specs/matuto-browser-operator-app.md and CONTEXT.md,
"Error page".
"""

import base64
import contextlib
import fcntl
import html
import json
import os
import re
import stat
import tempfile
from string import Template

from matuto.store import store_directory

PAGE_NAME = "error.html"
# The excerpt is a quick read beside the cause, not the manager's whole journal.
LOG_LINES = 60
LOG_BYTES = 64 * 1024
LOG_LINE = 400
REDACTED = "[redacted]"

# The manager's own log rule: an entry that carries the router API key, or that
# mentions or assigns a secret, is replaced whole rather than shown in part. The
# cause is one entry too, so a cause naming a secret is replaced whole as well.
SECRET_LINE = re.compile(r"(?i)(api.?key|authorization|bearer|token|secret|password)")
SECRET_ASSIGN = re.compile(r"(?i)\b[a-z0-9_]*(?:secret|password|api_key|apikey|token)[a-z0-9_]*\s*[=:]")

PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
$consent
<title>Matuto is not running</title>
</head>
<body>
<h1>Matuto is not running</h1>
<p>$cause</p>
<h2>What Repair Matuto would do</h2>
<p>Repair Matuto keeps the installed version: it finishes an interrupted update, otherwise reinstalls the same version when installed files are missing or altered, regenerates Matuto's own files, and restarts only the manager.</p>
<h2>What Update Matuto would do</h2>
<p>Update Matuto installs a newer published release, after a verified snapshot of the database. It refuses while serving or a job is running.</p>
$offer
$blockers
<h2>Manager log</h2>
<pre>$log</pre>
</body>
</html>
""")


class ErrorPageError(Exception):
    """A refused write, naming the path the operator has to fix."""


def page_file():
    """The one fixed path of the error page, beside login.json."""
    return store_directory() / PAGE_NAME


def write(cause, log_text, key, *, offer=None, blockers=(), list_number=None):
    """Replace the page with the cause, redacted log and optional current offer."""
    path = page_file()
    _private_directory(path.parent)
    with _write_lock(path.parent):
        previous = read_update_consent()
        previous_number = previous.get("listNumber") if previous else None
        if isinstance(previous_number, int) and not isinstance(previous_number, bool):
            if list_number is None:
                offer = previous.get("offer")
                blockers = previous.get("blockers", ())
                list_number = previous_number
            elif (not isinstance(list_number, int) or isinstance(list_number, bool)
                  or list_number < previous_number):
                offer, blockers, list_number = None, (), previous_number
        _replace(path, _render(cause, log_text, key, offer, blockers, list_number))
    return path


@contextlib.contextmanager
def _write_lock(directory):
    """Serialize watermark checks with page replacement across updater processes."""
    path = directory / ".error-page.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_mode & 0o077 or status.st_nlink != 1):
            raise ErrorPageError(f"the error-page lock is not private: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except ErrorPageError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except OSError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise ErrorPageError(f"cannot lock the error page: {path}") from None
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def install_consent():
    """Hold the page-writer lock while the caller validates and reserves its offer."""
    path = page_file()
    _private_directory(path.parent)
    with _write_lock(path.parent):
        yield read_update_consent()


def read_update_consent():
    """Read the release and blocker facts embedded in the exact page shown to the operator."""
    path = page_file()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        status = os.fstat(descriptor)
        if (not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid()
                or status.st_mode & 0o077 or status.st_nlink != 1 or status.st_size > 131_072):
            return None
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            page = source.read(131_073).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    match = re.search(r'<meta name="matuto-update-consent" content="([A-Za-z0-9_-]+)">', page)
    if not match:
        return None
    try:
        token = match.group(1)
        facts = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    if (not isinstance(facts, dict) or not isinstance(facts.get("blockers"), list)
            or (facts.get("listNumber") is not None
                and (isinstance(facts["listNumber"], bool) or not isinstance(facts["listNumber"], int)))):
        return None
    return facts


def _render(cause, log_text, key, offer, blockers, list_number):
    cause = str(cause)
    if _secret(cause, key):
        cause = REDACTED
    offer_html = ""
    if offer:
        fields = (
            ("Installed version", offer.get("installedVersion")),
            ("Offered version", offer.get("version")),
            ("Release notes", offer.get("notes")),
            ("Download size", offer.get("size")),
            ("Prerequisites", ", ".join(offer.get("prerequisites", ())) or "None"),
            ("Database migration", "Yes" if offer.get("changesDatabase") else "No"),
        )
        rows = "".join(f"<dt>{html.escape(str(name))}</dt><dd>{html.escape(str(value))}</dd>"
                       for name, value in fields if value is not None)
        offer_html = f"<h2>Newer release</h2><dl>{rows}</dl>"
    blocker_html = ""
    if blockers:
        entries = "".join(
            "<li>" + html.escape(str(item.get("text", "")))
            + (" <code>" + html.escape(str(item["command"])) + "</code>"
               if item.get("command") else "") + "</li>"
            for item in blockers)
        blocker_html = f"<h2>What must stop first</h2><ul>{entries}</ul>"
    consent_offer = ({key: value for key, value in offer.items() if key != "installedVersion"}
                     if isinstance(offer, dict) else offer)
    facts = json.dumps({"offer": consent_offer, "listNumber": list_number,
                        "blockers": list(blockers)}, sort_keys=True, separators=(",", ":"))
    token = base64.urlsafe_b64encode(facts.encode("utf-8")).decode("ascii").rstrip("=")
    consent = f'<meta name="matuto-update-consent" content="{token}">'
    return PAGE.substitute(cause=html.escape(cause), offer=offer_html, blockers=blocker_html,
                           consent=consent, log=html.escape(_excerpt(log_text, key)))


def _secret(line, key):
    """Whether one entry carries the router API key or names or assigns a secret."""
    return bool(key and key in line) or bool(SECRET_LINE.search(line)) or bool(SECRET_ASSIGN.search(line))


def _excerpt(log_text, key):
    """The bounded, redacted tail of the manager's log, one entry per line.

    Redaction runs on the whole line before the display truncation: a key that
    straddles the per-line cut must be replaced, never shown in part.
    """
    raw = str(log_text).encode("utf-8", "replace")
    text = raw[-LOG_BYTES:].decode("utf-8", "replace")
    lines = text.splitlines()
    if len(raw) > LOG_BYTES and lines:
        # The first line lost its start to the byte window, so a key in it cannot
        # be recognized; drop it rather than show part of one.
        lines = lines[1:]
    excerpt = []
    for raw_line in lines[-LOG_LINES:]:
        line = re.sub(r"[\x00-\x1f\x7f]+", " ", raw_line).strip()
        if not line:
            continue
        if _secret(line, key):
            line = REDACTED
        else:
            line = line[:LOG_LINE]
        excerpt.append(line)
    return "\n".join(excerpt).encode("utf-8", "replace")[-LOG_BYTES:].decode("utf-8", "replace")


def _private_directory(directory):
    """A directory only this account may read, write or search, created if absent."""
    if not directory.exists():
        try:
            directory.mkdir(parents=True, mode=0o700)
            os.chmod(directory, 0o700)
        except FileExistsError:
            # Another Matuto created it first; the checks below judge what is there.
            pass
        except OSError:
            raise ErrorPageError(f"cannot create the Matuto directory: {directory}") from None
    try:
        status = directory.lstat()
    except OSError:
        raise ErrorPageError(f"cannot read the Matuto directory: {directory}") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ErrorPageError(f"the Matuto directory is not a directory: {directory}")
    if status.st_uid != os.getuid() or status.st_mode & 0o077:
        raise ErrorPageError(f"the Matuto directory is not private: {directory}")


def _replace(path, document):
    """Write the page in one step, so an interrupted write leaves one whole page."""
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _sync_directory(path.parent)
    except OSError:
        raise ErrorPageError(f"cannot write the error page: {path}") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _sync_directory(directory):
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
