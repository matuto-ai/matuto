"""The one rule for the log tail Matuto keeps: bounded by its caller, redacted here.

`matuto.runner` bounds a unit's own output and hands the tail to this module before
it stores it with the run's evidence; the progress tail shown while work runs
uses the same decision, so what an operator reads and what the store keeps cannot
disagree. The rule is the manager's own: a line that carries the router API key, or
that names or assigns a secret, is replaced whole rather than shown in part.
"""

import re

from matuto import store

REDACTED = "[redacted]"
# A line that mentions or assigns a secret is replaced whole. The words are matched
# on their own as well as around `=` or `:` because a credential is named in prose
# ("using the api key below") as often as it is assigned; a measurement line saying
# tokens per second loses its place to that caution rather than leaving a key behind.
SECRET_LINE = re.compile(r"(?i)(api.?key|authorization|bearer|token|secret|password)")
SECRET_ASSIGN = re.compile(
    r"(?i)\b[a-z0-9_]*(?:secret|password|api_key|apikey|token)[a-z0-9_]*\s*[=:]")


def router_key(config_root=None):
    """The router API key from the path CUT-4 keeps, or empty when it cannot be read.

    Nothing returns this key to a caller: it is read only so a log line carrying it
    is recognized and replaced whole.
    """
    path = store.store_directory(config_root).parent / "pi-local" / "api-key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clean(line):
    """One entry on one line, without control characters and without its edges."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(line)).strip()


def secret(line, key=""):
    """Whether one cleaned entry carries the key or names or assigns a secret."""
    return bool(key and key in line) or bool(SECRET_LINE.search(line)) or bool(SECRET_ASSIGN.search(line))


def redact(text, *, key=""):
    """One log text with every credential-carrying line replaced whole.

    Each line is cleaned first, so the decision is made on the line that would be
    shown, and redaction runs on the whole line, so a line is never kept in part
    around a secret. Splitting and rejoining on `\\n` leaves the caller's own line
    structure and trailing newline exactly as they were.

    `key` is a credential whose literal value must never appear, such as the router
    API key; a caller that has none passes an empty string rather than skipping the
    redaction.
    """
    text = str(text)
    if not text:
        return text
    return "\n".join(
        REDACTED if secret(clean(line), key) else clean(line) for line in text.split("\n"))
