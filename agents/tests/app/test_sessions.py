"""The session module's own behaviour: start, seven days, sign out, and every end."""

import datetime
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login, sessions, store  # noqa: E402

SECOND = datetime.timedelta(seconds=1)
SIGNED_IN = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        login.create_login("operator", "first-secret", "first-secret")

    def start(self, password="first-secret", now=SIGNED_IN):
        return sessions.start_session("operator", password, now=now)

    def stored_rows(self):
        opened = store.open_store()
        try:
            return opened.connection().execute("SELECT * FROM sessions").fetchall()
        finally:
            opened.close()

    def test_a_correct_password_starts_a_session_valid_for_seven_days(self):
        token, expiry = self.start()

        self.assertEqual(expiry, SIGNED_IN + sessions.SESSION_LIFETIME)
        self.assertTrue(sessions.session_is_valid(token, now=SIGNED_IN))
        almost = SIGNED_IN + sessions.SESSION_LIFETIME - SECOND
        self.assertTrue(sessions.session_is_valid(token, now=almost))
        after = SIGNED_IN + sessions.SESSION_LIFETIME + SECOND
        self.assertFalse(sessions.session_is_valid(token, now=after))

    def test_session_username_comes_only_from_a_live_token(self):
        token, _expiry = self.start()

        self.assertEqual(sessions.session_username(token, now=SIGNED_IN), "operator")
        self.assertEqual(sessions.session_username("altered-token", now=SIGNED_IN), "")
        self.assertEqual(sessions.session_username(
            token, now=SIGNED_IN + sessions.SESSION_LIFETIME), "")

    def test_a_session_is_valid_in_a_new_process(self):
        token, _expiry = self.start(now=datetime.datetime.now(datetime.timezone.utc))
        probe = ("from matuto import sessions; import sys;"
                 "sys.exit(0 if sessions.session_is_valid(sys.argv[1]) else 3)")

        result = subprocess.run([sys.executable, "-B", "-c", probe, token],
                                cwd=AGENTS, env={**os.environ}, capture_output=True, text=True)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_store_keeps_only_a_digest_and_the_login_generation(self):
        token, expiry = self.start()

        rows = self.stored_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_digest"], hashlib.sha256(token.encode("utf-8")).hexdigest())
        self.assertNotEqual(rows[0]["token_digest"], token)
        self.assertEqual(rows[0]["username"], "operator")
        self.assertEqual(rows[0]["generation"], login.login_generation())
        self.assertEqual(rows[0]["expires_at"], expiry.isoformat())

    def test_a_wrong_password_starts_nothing(self):
        for password in ("second-secret", ""):
            with self.subTest(password=password):
                with self.assertRaises(sessions.SessionError):
                    self.start(password=password)

        self.assertEqual(self.stored_rows(), [])

    def test_starting_without_a_login_is_refused_distinctly(self):
        login.reset_login()

        with self.assertRaises(login.LoginError):
            self.start()

    def test_sign_out_ends_only_that_session(self):
        first, _ = self.start()
        second, _ = self.start()

        self.assertTrue(sessions.sign_out(first))
        self.assertFalse(sessions.session_is_valid(first, now=SIGNED_IN))
        self.assertTrue(sessions.session_is_valid(second, now=SIGNED_IN))
        self.assertFalse(sessions.sign_out(first))

    def test_a_password_change_ends_every_session(self):
        first, _ = self.start()
        second, _ = self.start()

        login.change_password("first-secret", "second-secret", "second-secret")

        self.assertFalse(sessions.session_is_valid(first, now=SIGNED_IN))
        self.assertFalse(sessions.session_is_valid(second, now=SIGNED_IN))
        replacement, _ = sessions.start_session("operator", "second-secret", now=SIGNED_IN)
        self.assertTrue(sessions.session_is_valid(replacement, now=SIGNED_IN))

    def test_a_reset_ends_every_session_and_a_new_login_revives_none(self):
        token, _ = self.start()

        login.reset_login()
        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))

        login.create_login("operator", "first-secret", "first-secret")
        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))
        fresh, _ = self.start()
        self.assertTrue(sessions.session_is_valid(fresh, now=SIGNED_IN))

    def test_unknown_and_altered_tokens_are_refused(self):
        token, _ = self.start()
        altered = token[:-1] + ("a" if token[-1] != "a" else "b")

        self.assertFalse(sessions.session_is_valid("unknown-token", now=SIGNED_IN))
        self.assertFalse(sessions.session_is_valid(token + "x", now=SIGNED_IN))
        self.assertFalse(sessions.session_is_valid(altered, now=SIGNED_IN))
        self.assertFalse(sessions.session_is_valid("", now=SIGNED_IN))

    def test_store_verification_errors_do_not_expire_a_browser_session(self):
        token, _ = self.start()
        os.chmod(store.store_path(), 0o644)
        with self.assertRaises(sessions.SessionUnavailable):
            sessions.session_is_valid(token, now=SIGNED_IN)

        os.chmod(store.store_path(), 0o600)
        for error in (OSError("unreadable"), sqlite3.OperationalError("read failed")):
            with self.subTest(error=error), mock.patch.object(
                    sessions.store, "read_session_at", side_effect=error):
                with self.assertRaises(sessions.SessionUnavailable):
                    sessions.session_is_valid(token, now=SIGNED_IN)

    def test_session_lookup_refuses_unsafe_directory_and_journal(self):
        token, _ = self.start()
        path = store.store_path()
        os.chmod(path.parent, 0o777)
        try:
            with self.assertRaises(sessions.SessionUnavailable):
                sessions.session_is_valid(token, now=SIGNED_IN)
        finally:
            os.chmod(path.parent, 0o700)
        journal = path.with_name(path.name + "-journal")
        journal.symlink_to(path.parent / "missing")
        try:
            with self.assertRaises(sessions.SessionUnavailable):
                sessions.session_is_valid(token, now=SIGNED_IN)
            self.assertTrue(journal.is_symlink())
        finally:
            journal.unlink()
        self.assertTrue(sessions.session_is_valid(token, now=SIGNED_IN))

    def test_session_reads_committed_state_while_another_writer_is_active(self):
        token, _ = self.start()
        opened = store.open_store()
        self.addCleanup(opened.close)
        connection = opened.connection()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM sessions")
        try:
            self.assertTrue(sessions.session_is_valid(token, now=SIGNED_IN))
            self.assertEqual(sessions.session_username(token, now=SIGNED_IN), "operator")
            self.assertTrue(connection.in_transaction)
        finally:
            connection.execute("COMMIT")
        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))
        self.assertEqual(sessions.session_username(token, now=SIGNED_IN), "")

    def test_no_session_is_valid_when_its_stored_expiry_is_unreadable(self):
        token, _ = self.start()
        opened = store.open_store()
        try:
            opened.connection().execute("UPDATE sessions SET expires_at = 'not-a-time'")
        finally:
            opened.close()

        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))

    def test_no_session_is_valid_when_the_login_file_is_missing(self):
        token, _ = self.start()
        login.login_file().unlink()

        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))

    def test_no_session_is_valid_when_the_login_file_is_unsafe(self):
        token, _ = self.start()
        os.chmod(login.login_file(), 0o644)

        self.assertFalse(sessions.session_is_valid(token, now=SIGNED_IN))
