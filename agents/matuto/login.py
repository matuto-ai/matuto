"""The one Matuto login: create it, check it, change the password, reset it.

`login.json` sits beside the old manager's files and is written 0600, salted and
hashed with scrypt from the standard library. The login generation changes with
every password change and every reset, so session code can end every session.
"""

import contextlib
import errno
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile

from matuto.matuto_runtime import InspectionError, directory_is_safe, protected_open

LOGIN_NAME = "login.json"
# N=2**14 with r=8 costs about 16 MiB and tens of milliseconds per check: the
# login keeps out other local accounts, and an online guess is already slow.
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "maxmem": 64 << 20}
NO_LOGIN = "no login is set"


class LoginError(Exception):
    """A refused login operation, with a message the operator can be shown."""


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def login_directory():
    return config_home() / "ai-agents-sandbox" / "matuto-manager"


def login_file():
    return login_directory() / LOGIN_NAME


def _generation():
    """A login generation no earlier login had.

    A wall clock cannot promise that: it repeats, and an operator or an NTP step
    can move it back. Randomness of this size can promise it, and nothing needs
    two generations to be ordered, only to differ.
    """
    return secrets.token_hex(16)


def _hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT).hex()


def _open_directory():
    """A descriptor on the login's own directory, or why it cannot be used."""
    directory = login_directory()
    try:
        descriptor = protected_open(directory, directory=True, follow_links=True)
    except FileNotFoundError:
        raise LoginError(NO_LOGIN) from None
    except (InspectionError, OSError) as error:
        raise LoginError(f"cannot open the login directory: {directory}") from error
    if not directory_is_safe(os.fstat(descriptor)):
        os.close(descriptor)
        raise LoginError(f"the login directory is not a directory: {directory}")
    return descriptor


@contextlib.contextmanager
def _locked():
    """One exclusive lock over reading the login, checking it and writing it back.

    A password change reads the login, spends tens of milliseconds hashing the
    current password, and only then commits. Without this lock a terminal
    reset-login landing in that window would be undone by the change, restoring
    the very login the operator just recovered from.
    """
    directory = login_directory()
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as error:
        raise LoginError(f"cannot create the login directory: {directory}") from error
    descriptor = _open_directory()
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield descriptor
    finally:
        os.close(descriptor)


def _read(descriptor):
    """The stored login, or why it cannot be used.

    `login.json` is the credential itself, so unlike the other protected settings
    it is refused unless it is this account's own regular file that neither group
    nor others can reach, and a link is refused outright: following one would let
    whoever placed it choose the credential this account accepts.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(LOGIN_NAME, flags, dir_fd=descriptor)
    except FileNotFoundError:
        raise LoginError(NO_LOGIN) from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise LoginError(f"the login file is a link: {login_file()}") from None
        raise LoginError(f"cannot open the login file: {login_file()}") from None
    with os.fdopen(handle, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise LoginError(f"the login file is not an ordinary file: {login_file()}")
        if status.st_uid != os.getuid():
            raise LoginError(f"the login file belongs to another account: {login_file()}")
        if status.st_mode & 0o077:
            raise LoginError(f"the login file is readable or writable by group or others: {login_file()}")
        # The whole file is read: its size is whatever username the operator
        # typed, and refusing a long one would be an input rule the login contract does not ask
        # for. It is this account's own 0600 file, already judged above.
        raw = handle.read()
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LoginError(f"the login file does not hold a login: {login_file()}") from None
    if not isinstance(record, dict) or not {"username", "salt", "hash", "generation"} <= set(record):
        raise LoginError(f"the login file does not hold a login: {login_file()}")
    return record


def _write(descriptor, record, create_only):
    """Replace the login in one step, so an interrupted write leaves one whole file.

    `create_only` links the finished temporary under the final name instead of
    replacing it, and a name that already exists fails: creating a login never
    replaces one, not even when two of them race.
    """
    temporary = None
    try:
        try:
            with tempfile.NamedTemporaryFile(dir=f"/proc/self/fd/{descriptor}", prefix=".login.", delete=False) as output:
                temporary = output.name
                os.fchmod(output.fileno(), 0o600)
                output.write((json.dumps(record, sort_keys=True) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
            if create_only:
                os.link(temporary, LOGIN_NAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            else:
                os.replace(temporary, LOGIN_NAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.fsync(descriptor)
        except FileExistsError:
            raise LoginError("a login already exists") from None
        except OSError as error:
            raise LoginError(f"cannot write the login file: {login_file()}") from error
    finally:
        # Unlinked before the descriptor closes: the temporary lives under /proc/self/fd.
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _exists(descriptor):
    try:
        os.stat(LOGIN_NAME, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def create_login(username, password, confirmation):
    """Store the one login, and refuse when there already is one."""
    if not username or not password:
        raise LoginError("a username and a password are required")
    if password != confirmation:
        raise LoginError("the passwords do not match")
    salt = secrets.token_bytes(16)
    record = {"username": username, "salt": salt.hex(), "hash": _hash(password, salt),
              "generation": _generation()}
    with _locked() as descriptor:
        if _exists(descriptor):
            raise LoginError("a login already exists")
        _write(descriptor, record, create_only=True)
    return record["generation"]


def verified_generation(username, password):
    """The login's generation when the stored pair is this one, otherwise None.

    The password check and the generation come from the same read, so a password
    change landing between them cannot store a session for a password that just
    stopped being valid. A missing or unusable login is refused distinctly, as
    `check_login` does.
    """
    descriptor = _open_directory()
    try:
        record = _read(descriptor)
    finally:
        os.close(descriptor)
    if username != record["username"]:
        return None
    if not hmac.compare_digest(_hash(password, bytes.fromhex(record["salt"])), record["hash"]):
        return None
    return record["generation"]


def check_login(username, password):
    """Whether the stored pair is this one; a missing login says so distinctly."""
    return verified_generation(username, password) is not None


def login_generation():
    """The current login's generation, or why no login can supply one."""
    descriptor = _open_directory()
    try:
        return _read(descriptor)["generation"]
    finally:
        os.close(descriptor)


def current_username():
    """The username in the one protected login record."""
    descriptor = _open_directory()
    try:
        return _read(descriptor)["username"]
    finally:
        os.close(descriptor)


def change_password(current_password, new_password, confirmation):
    """Store a new hash for the same username and change the login generation."""
    if not new_password:
        raise LoginError("a new password is required")
    if new_password != confirmation:
        raise LoginError("the passwords do not match")
    salt = secrets.token_bytes(16)
    with _locked() as descriptor:
        record = _read(descriptor)
        if not hmac.compare_digest(_hash(current_password, bytes.fromhex(record["salt"])), record["hash"]):
            raise LoginError("the current password is wrong")
        record["salt"] = salt.hex()
        record["hash"] = _hash(new_password, salt)
        record["generation"] = _generation()
        _write(descriptor, record, create_only=False)
    return record["generation"]


def reset_login():
    """Delete the login, so the next visit shows Create login.

    The pathname is unlinked, never what it points at: a login that is a broken
    link is still the credential entry creation has to be able to replace.
    """
    if not login_directory().exists():
        return False
    with _locked() as descriptor:
        try:
            os.unlink(LOGIN_NAME, dir_fd=descriptor)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise LoginError(f"cannot delete the login file: {login_file()}") from error
        os.fsync(descriptor)
    return True