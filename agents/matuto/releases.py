"""Read the signed Matuto release list and check release files.

The operator signs one JSON list of every release so far with the passphrase
protected release key, using OpenSSH's ``ssh-keygen``:

    ssh-keygen -Y sign -f release-key -n matuto-release release-list.json

The list is a JSON object with exactly two keys:

    {
      "number": 4,
      "releases": [
        {
          "version": "1.0.0",
          "commit": "<40 lowercase hex characters>",
          "size": 12345678,
          "sha256": "<64 lowercase hex characters>",
          "notes": "What this release changes.",
          "prerequisites": ["openssh-client"],
          "changes_database": false,
          "withdrawn": false
        }
      ]
    }

``number`` only rises. ``read_list`` verifies the detached signature against the
caller's trusted public key before it parses anything, refuses a list that is
missing a field or otherwise malformed, and marks a list numbered below the
highest already accepted as stale, so it brings no news and does not replace
what is known. A withdrawn release is never installable. ``check_release_file``
matches a release file against its entry's size and SHA-256 before anything
reads its contents. Only the trusted key the caller supplies is ever used; a key
carried in a list or a release is never consulted. See README.md for the package
conventions.
"""

import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass

# The namespace and principal the list is signed and verified with. The
# principal is the caller's, not the signing key's comment, so a key exchange
# needs no signing change.
SIGNATURE_NAMESPACE = "matuto-release"
SIGNED_PRINCIPAL = "matuto-release"

COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# No leading zeros, so two spellings never name one release.
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
PUBLIC_KEY_TYPE = re.compile(r"(?:ssh|ecdsa|sk)-[A-Za-z0-9@.\-]+")

LIST_KEYS = ("number", "releases")
RELEASE_KEYS = ("version", "commit", "size", "sha256", "notes", "prerequisites",
                "changes_database", "withdrawn")


class ReleaseError(Exception):
    """A refused release-list or release-file check, with a reason to show."""


@dataclass(frozen=True)
class Release:
    """One release as the signed list describes it."""

    version: str
    commit: str
    size: int
    sha256: str
    notes: str
    prerequisites: tuple
    changes_database: bool
    withdrawn: bool

    @property
    def installable(self):
        return not self.withdrawn


@dataclass(frozen=True)
class ReleaseList:
    """A verified list: its rising number and every release it names."""

    number: int
    releases: tuple

    def is_stale(self, accepted_number=None):
        """Whether this list sits below the highest number already accepted."""
        if accepted_number is None:
            return False
        if isinstance(accepted_number, bool) or not isinstance(accepted_number, int) or accepted_number < 0:
            raise ReleaseError(
                f"the accepted list number {accepted_number!r} is not a non-negative integer")
        return self.number < accepted_number


@dataclass(frozen=True)
class ListReading:
    """A verified list, and whether it is a stale copy of what is already known."""

    release_list: ReleaseList
    stale: bool

    def offered(self):
        """What this reading may offer: nothing at all when it is a stale copy.

        A replayed list numbered below the accepted one must not revive a
        release the accepted list withdrew, so only a fresh reading offers, and
        a withdrawn release is never among them.
        """
        if self.stale:
            return ()
        return tuple(release for release in self.release_list.releases if release.installable)


def read_list(document, signature, trusted_key, *, accepted_number=None):
    """Verify a signed list against the trusted key and parse it.

    `document` is the list's bytes and `signature` its detached ``.sig`` bytes;
    `trusted_key` is the release public key the caller recorded at bootstrap, as
    text or bytes. A list that does not verify, is malformed or names a release
    twice is refused before any of its content is used. A verified list numbered
    below `accepted_number` is returned marked stale, so the caller keeps the
    state it already has.
    """
    document = _as_bytes(document)
    verify_signature(document, signature, trusted_key)
    parsed = _parse_list(document)
    return ListReading(parsed, parsed.is_stale(accepted_number))


def verify_signature(document, signature, trusted_key):
    """Check the document's OpenSSH signature against the caller's trusted key.

    Nothing is returned on success; every failure raises `ReleaseError` with the
    reason, and nothing is treated as verified. A missing or failing
    `ssh-keygen` is a failure like any other.
    """
    allowed = _allowed_signers(trusted_key)
    document = _as_bytes(document)
    signature = _as_bytes(signature)
    with tempfile.TemporaryDirectory(prefix="matuto-release-") as directory:
        allowed_path = os.path.join(directory, "allowed_signers")
        signature_path = os.path.join(directory, "release-list.sig")
        _write_private(allowed_path, allowed)
        _write_private(signature_path, signature)
        try:
            completed = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", allowed_path, "-I", SIGNED_PRINCIPAL,
                 "-n", SIGNATURE_NAMESPACE, "-s", signature_path],
                input=document, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise ReleaseError(
                "ssh-keygen is not installed, so the release list signature cannot be checked") from None
        except OSError as error:
            raise ReleaseError(f"ssh-keygen could not be run: {error}") from error
    if completed.returncode != 0:
        raise ReleaseError(f"the release list signature does not verify: {_reason(completed)}")


def _parse_list(document):
    """Parse and validate a list's fields; the signature is checked separately.

    Every release must carry version, commit, size, SHA-256, notes,
    prerequisites, database-change flag and withdrawn mark, and the list its one
    rising number. A missing, extra, mistyped or duplicate field is refused.
    """
    try:
        parsed = json.loads(_as_bytes(document).decode("utf-8"), object_pairs_hook=_field_pairs)
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseError(f"the release list is not JSON: {error}") from None
    if not isinstance(parsed, dict):
        raise ReleaseError("the release list is not a JSON object")
    _exact_keys(parsed, LIST_KEYS, "the release list")
    number = parsed["number"]
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise ReleaseError(f"the release list number {number!r} is not a non-negative integer")
    entries = parsed["releases"]
    if not isinstance(entries, list) or not entries:
        raise ReleaseError("the release list carries no releases")
    seen = set()
    parsed_releases = []
    for entry in entries:
        release = _release(entry)
        if release.version in seen:
            raise ReleaseError(f"the release list names version {release.version} twice")
        seen.add(release.version)
        parsed_releases.append(release)
    return ReleaseList(number, tuple(parsed_releases))


def check_release_file(path, release):
    """Refuse a release file whose size or SHA-256 does not match its list entry.

    The size is read from the directory entry first, so a truncated file is
    refused before its contents are read. Nothing is returned on success.
    """
    try:
        status = os.stat(path)
    except OSError as error:
        raise ReleaseError(f"the release file cannot be read: {path}") from error
    if not stat.S_ISREG(status.st_mode):
        raise ReleaseError(f"the release file is not an ordinary file: {path}")
    if status.st_size != release.size:
        raise ReleaseError(
            f"the release file is {status.st_size} bytes, but the list says {release.size}: {path}")
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseError(f"the release file cannot be read: {path}") from error
    if digest.hexdigest() != release.sha256:
        raise ReleaseError(
            f"the release file's SHA-256 does not match the list for {release.version}: {path}")


def _release(entry):
    if not isinstance(entry, dict):
        raise ReleaseError("a release entry is not a JSON object")
    _exact_keys(entry, RELEASE_KEYS, "a release entry")
    version = entry["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ReleaseError(f"the release version {version!r} is not MAJOR.MINOR.PATCH")
    commit = entry["commit"]
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ReleaseError(f"the release commit {commit!r} is not a full 40-character commit")
    size = entry["size"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseError(f"the release size {size!r} is not a non-negative byte count")
    sha256 = entry["sha256"]
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        raise ReleaseError(f"the release SHA-256 {sha256!r} is not 64 hexadecimal characters")
    notes = entry["notes"]
    if not isinstance(notes, str):
        raise ReleaseError("the release notes are not text")
    prerequisites = entry["prerequisites"]
    if not isinstance(prerequisites, list):
        raise ReleaseError("the release prerequisites are not a list")
    for item in prerequisites:
        if not isinstance(item, str) or not item:
            raise ReleaseError(f"a release prerequisite {item!r} is not a name")
    changes_database = entry["changes_database"]
    withdrawn = entry["withdrawn"]
    if not isinstance(changes_database, bool) or not isinstance(withdrawn, bool):
        raise ReleaseError("a release's database-change and withdrawn marks are not booleans")
    return Release(version, commit, size, sha256, notes, tuple(prerequisites),
                   changes_database, withdrawn)


def _exact_keys(mapping, required, where):
    missing = [key for key in required if key not in mapping]
    if missing:
        raise ReleaseError(f"{where} is missing {', '.join(missing)}")
    extra = [key for key in mapping if key not in required]
    if extra:
        raise ReleaseError(f"{where} carries an unknown field {', '.join(extra)}")


def _field_pairs(pairs):
    """One JSON object, refusing a name that appears twice in it.

    `json.loads` otherwise keeps only the last of duplicate names, so a signed
    list could carry two `withdrawn` marks or two list numbers and be read as
    the attacker-chosen one. The object hook sees every pair before that.
    """
    fields = {}
    for name, value in pairs:
        if name in fields:
            raise ReleaseError(f"the release list names the field {name!r} twice in one object")
        fields[name] = value
    return fields


def _allowed_signers(trusted_key):
    """The one-line allowed-signers file built from the trusted public key alone."""
    fields = _as_text(trusted_key).split()
    if len(fields) < 2 or not PUBLIC_KEY_TYPE.fullmatch(fields[0]):
        raise ReleaseError("the trusted release key is not an OpenSSH public key")
    keytype, blob = fields[0], fields[1]
    try:
        decoded = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError):
        raise ReleaseError("the trusted release key is not an OpenSSH public key") from None
    if not decoded:
        raise ReleaseError("the trusted release key is not an OpenSSH public key")
    return f"{SIGNED_PRINCIPAL} {keytype} {blob}\n"


def _reason(completed):
    message = (completed.stderr or completed.stdout or b"").decode("utf-8", "replace").strip()
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    return lines[-1] if lines else f"ssh-keygen exited {completed.returncode}"


def _write_private(path, content):
    if isinstance(content, str):
        content = content.encode("utf-8")
    with open(path, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(content)


def _as_bytes(value):
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise ReleaseError(f"expected bytes or text, not {type(value).__name__}")


def _as_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ReleaseError("the trusted release key is not text") from None
    raise ReleaseError(f"the trusted release key is not text, but {type(value).__name__}")
