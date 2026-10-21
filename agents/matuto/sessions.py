"""Browser sessions: start one after a password check, keep it seven days, end it.

A session lives only in the store's sessions block, tied to the login generation
that created it. Whether a token is valid is decided at each check from the store,
`login.json` and the wall-clock time, so a manager restart, a Matuto update or a
reboot changes nothing. A password change or a `reset-login` gives the login a new
generation, which ends every session at once; signing out ends only one.
"""

import datetime
import hashlib
import secrets
import sqlite3

from matuto import login, store

SESSION_LIFETIME = datetime.timedelta(days=7)
NO_SESSION = "the username or password is wrong"


class SessionError(Exception):
    """A refused session operation, with a message the operator can be shown."""


class SessionUnavailable(SessionError):
    """Session validity could not be checked without changing authentication."""


def start_session(username, password, *, now=None, config_root=None):
    """Start a session after a successful password check, returning (token, expiry).

    A missing or unusable login is refused with `login.LoginError` so the caller can
    tell it from a wrong pair. The generation comes from the same check, so a
    password change racing this call cannot store a session for the old password.
    """
    generation = login.verified_generation(username, password)
    if generation is None:
        raise SessionError(NO_SESSION)
    moment = _moment(now)
    expires_at = moment + SESSION_LIFETIME
    token = secrets.token_urlsafe(32)
    opened = store.open_store(config_root)
    try:
        opened.connection().execute(
            "INSERT INTO sessions (token_digest, username, generation, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_digest(token), username, generation, _text(moment), _text(expires_at)))
    finally:
        opened.close()
    return token, expires_at


def session_is_valid(token, *, now=None, config_root=None):
    """Whether this token is a live session for the current login.

    Unknown and altered tokens, a session past its expiry, one whose generation the
    login no longer has, and no login at all are all refused alike. Validation reads
    the committed session without opening the store for recovery. If it cannot
    verify the row, SessionUnavailable preserves the cookie for a later retry.
    """
    if not isinstance(token, str) or not token:
        return False
    moment = _moment(now)
    try:
        generation = login.login_generation()
    except login.LoginError:
        return False
    row = _session_row(token, config_root)
    if row is None or row["generation"] != generation:
        return False
    try:
        return moment < _read_time(row["expires_at"])
    except (TypeError, ValueError):
        return False


def session_username(token, *, now=None, config_root=None):
    """Return the username bound to a live session token, or an empty string."""
    if not isinstance(token, str) or not token:
        return ""
    try:
        generation = login.login_generation()
    except login.LoginError:
        return ""
    row = _session_row(token, config_root)
    try:
        if row is None or row["generation"] != generation or _moment(now) >= _read_time(row["expires_at"]):
            return ""
        return row["username"]
    except (TypeError, ValueError):
        return ""


def sign_out(token, *, config_root=None):
    """End this one session, leaving every other session alone."""
    if not isinstance(token, str) or not token:
        return False
    opened = store.open_store(config_root)
    try:
        cursor = opened.connection().execute(
            "DELETE FROM sessions WHERE token_digest = ?", (_digest(token),))
        return cursor.rowcount > 0
    finally:
        opened.close()


def _session_row(token, config_root):
    try:
        return store.read_session_at(store.store_path(config_root), _digest(token))
    except (store.StoreError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise SessionUnavailable("session validity is temporarily unavailable") from error


def _digest(token):
    """The store's key for a token: the cookie is never written down."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _moment(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _text(moment):
    return moment.isoformat()


def _read_time(value):
    moment = datetime.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment
