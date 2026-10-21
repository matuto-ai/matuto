"""The login module's own behaviour: one login, its hash, its generation, and refusal."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import login  # noqa: E402

CLI = AGENTS / "matuto" / "cli.py"


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.file = login.login_file()

    def stored(self):
        return json.loads(self.file.read_text())

    def test_creation_stores_the_login_protected_and_holds_no_password(self):
        generation = login.create_login("operator", "first-secret", "first-secret")

        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.file, self.home / "ai-agents-sandbox" / "matuto-manager" / "login.json")
        self.assertNotIn("first-secret", self.file.read_text())
        stored = self.stored()
        self.assertEqual(stored["username"], "operator")
        self.assertEqual(stored["generation"], generation)
        self.assertEqual(len(bytes.fromhex(stored["salt"])), 16)

    def test_check_succeeds_only_for_the_stored_pair(self):
        login.create_login("operator", "first-secret", "first-secret")

        self.assertTrue(login.check_login("operator", "first-secret"))
        self.assertFalse(login.check_login("operator", "second-secret"))
        self.assertFalse(login.check_login("someone", "first-secret"))

    def test_check_reports_a_missing_login_distinctly(self):
        with self.assertRaises(login.LoginError) as refusal:
            login.check_login("operator", "first-secret")
        self.assertEqual(str(refusal.exception), login.NO_LOGIN)

    def test_creation_refuses_over_an_existing_login(self):
        login.create_login("operator", "first-secret", "first-secret")
        before = self.file.read_bytes()

        with self.assertRaises(login.LoginError):
            login.create_login("someone", "other-secret", "other-secret")
        self.assertEqual(self.file.read_bytes(), before)

    def test_creation_refuses_an_empty_value_or_a_mismatched_confirmation(self):
        for username, password, confirmation in (("", "secret", "secret"), ("operator", "", ""),
                                                  ("operator", "secret", "secret-two")):
            with self.subTest(username=username):
                with self.assertRaises(login.LoginError):
                    login.create_login(username, password, confirmation)
        self.assertFalse(self.file.exists())

    def test_change_password_keeps_the_username_and_changes_the_generation(self):
        login.create_login("operator", "first-secret", "first-secret")
        first = self.stored()["generation"]

        generation = login.change_password("first-secret", "second-secret", "second-secret")

        self.assertEqual(self.stored()["username"], "operator")
        self.assertNotEqual(generation, first)
        self.assertTrue(login.check_login("operator", "second-secret"))
        self.assertFalse(login.check_login("operator", "first-secret"))

    def test_a_failed_change_leaves_the_file_byte_for_byte_unchanged(self):
        login.create_login("operator", "first-secret", "first-secret")
        before = self.file.read_bytes()

        for current, new, confirmation in (("wrong", "second-secret", "second-secret"),
                                           ("first-secret", "", ""),
                                           ("first-secret", "second-secret", "secret-two")):
            with self.subTest(current=current):
                with self.assertRaises(login.LoginError):
                    login.change_password(current, new, confirmation)
                self.assertEqual(self.file.read_bytes(), before)

    def test_reset_deletes_the_login_and_the_next_login_gets_its_own_generation(self):
        first = login.create_login("operator", "first-secret", "first-secret")

        self.assertTrue(login.reset_login())
        self.assertFalse(self.file.exists())
        with self.assertRaises(login.LoginError):
            login.check_login("operator", "first-secret")

        second = login.create_login("operator", "first-secret", "first-secret")
        self.assertNotEqual(second, first)

    def test_no_login_ever_reuses_a_generation(self):
        first = login.create_login("operator", "first-secret", "first-secret")
        second = login.change_password("first-secret", "first-secret", "first-secret")
        login.reset_login()
        third = login.create_login("operator", "first-secret", "first-secret")

        self.assertEqual(len({first, second, third}), 3)
        self.assertEqual(self.stored()["generation"], third)

    def test_any_non_empty_username_round_trips(self):
        username = "u" * 65_536
        login.create_login(username, "password", "password")
        self.assertTrue(login.check_login(username, "password"))
        self.assertFalse(login.check_login(username[:-1], "password"))

    def test_an_unsafe_login_file_is_refused_rather_than_used(self):
        target = self.home / "elsewhere.json"
        target.write_text('{"username": "attacker", "salt": "00", "hash": "00", "generation": 1}\n')
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(target)
        with self.assertRaises(login.LoginError) as refusal:
            login.check_login("attacker", "")
        self.assertIn("link", str(refusal.exception))

        self.file.unlink()
        login.create_login("operator", "first-secret", "first-secret")
        self.file.chmod(0o644)
        with self.assertRaises(login.LoginError) as refusal:
            login.check_login("operator", "first-secret")
        self.assertIn("group or others", str(refusal.exception))

    def test_reset_removes_a_broken_login_link_so_creation_works_again(self):
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(self.home / "absent.json")

        self.assertTrue(login.reset_login())
        self.assertFalse(os.path.lexists(self.file))
        login.create_login("operator", "secret", "secret")
        self.assertTrue(login.check_login("operator", "secret"))

    def test_an_interrupted_replacement_leaves_the_old_login_complete(self):
        login.create_login("operator", "first-secret", "first-secret")
        before = self.file.read_bytes()

        with mock.patch.object(login.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(login.LoginError):
                login.change_password("first-secret", "second-secret", "second-secret")
        self.assertEqual(self.file.read_bytes(), before)
        self.assertTrue(login.check_login("operator", "first-secret"))
        self.assertEqual([name for name in os.listdir(self.file.parent) if name != login.LOGIN_NAME], [])

    def test_a_reset_in_flight_is_not_undone_by_an_earlier_change(self):
        login.create_login("original", "old-secret", "old-secret")
        entered, release = threading.Event(), threading.Event()
        refusals, real_write = [], login._write

        def blocking_write(descriptor, record, create_only):
            entered.set()
            release.wait(10)
            real_write(descriptor, record, create_only)

        def change():
            try:
                login.change_password("old-secret", "changed-secret", "changed-secret")
            except login.LoginError as error:
                refusals.append(str(error))

        with mock.patch.object(login, "_write", blocking_write):
            worker = threading.Thread(target=change)
            worker.start()
            self.assertTrue(entered.wait(10))
            recovery = threading.Thread(target=login.reset_login)
            recovery.start()
            recovery.join(0.5)
            blocked = recovery.is_alive()
            release.set()
            worker.join(10)
            recovery.join(10)

        # The change held the login for its whole read-check-write, so the reset
        # either waited behind it or ran after it; it never lost the recovery.
        self.assertTrue(blocked)
        self.assertEqual(refusals, [])
        self.assertFalse(self.file.exists())
        login.create_login("replacement", "recovered", "recovered")
        self.assertTrue(login.check_login("replacement", "recovered"))
        self.assertFalse(login.check_login("original", "changed-secret"))


class ResetLoginCommandTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_the_command_deletes_the_login(self):
        login.create_login("operator", "first-secret", "first-secret")

        done = subprocess.run([sys.executable, str(CLI), "reset-login"], capture_output=True, text=True)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(login.login_file().exists())

    def test_the_command_refuses_arguments(self):
        done = subprocess.run([sys.executable, str(CLI), "reset-login", "now"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("takes no arguments", done.stderr)


if __name__ == "__main__":
    unittest.main()