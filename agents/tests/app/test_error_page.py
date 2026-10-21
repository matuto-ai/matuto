"""The error page's own behaviour: private, bounded, redacted, escaped, replaced."""

import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import error_page  # noqa: E402

KEY = "sk-fixture-router-api-key"


class ErrorPageTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.home)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_page(self, cause, log_text, key=KEY):
        error_page.write(cause, log_text, key)
        return error_page.page_file().read_text()

    def test_page_is_private_and_names_the_cause_repair_and_update(self):
        self.write_page("the manager refused to open its database", "")

        path = error_page.page_file()
        self.assertEqual(path, self.home / "ai-agents-sandbox" / "matuto-manager" / "error.html")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        text = path.read_text()
        self.assertIn("the manager refused to open its database", text)
        self.assertIn("Repair Matuto", text)
        self.assertIn("Update Matuto", text)

    def test_release_offer_and_stop_commands_are_escaped_copyable_text(self):
        error_page.write(
            "manager unavailable", "", KEY,
            offer={"installedVersion": "2026.10.0", "version": "2026.10.1",
                   "notes": "<script>not markup</script>", "size": 42,
                   "prerequisites": ["openssh-client"], "changesDatabase": True},
            blockers=[{"text": "A run is active", "command":
                       "systemctl --user stop matuto-run-fixture.service"}])
        text = error_page.page_file().read_text()
        self.assertIn("2026.10.1", text)
        self.assertIn("Download size", text)
        self.assertIn("Database migration", text)
        self.assertIn("&lt;script&gt;not markup&lt;/script&gt;", text)
        self.assertNotIn("<script>", text)
        self.assertIn("systemctl --user stop matuto-run-fixture.service", text)

    def test_excerpt_is_bounded_redacted_and_shown_as_text(self):
        special = 6
        lines = [f"log-line-{number:03d}" for number in range(error_page.LOG_LINES * 2 - special)]
        lines += [f"Authorization: Bearer {KEY}", "PI_LOCAL_API_KEY=" + KEY,
                  "password = hunter2", "<button>erase</button><script>alert(1)</script>",
                  "y" * 395 + KEY, "x" * (error_page.LOG_LINE * 3)]

        text = self.write_page("cause", "\n".join(lines))

        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY[:3], text)  # a key straddling the per-line cut is replaced whole
        self.assertNotIn("hunter2", text)
        self.assertNotIn("<button>", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;button&gt;erase&lt;/button&gt;&lt;script&gt;alert(1)&lt;/script&gt;", text)
        # Only the tail of the log survives and no line is longer than the bound.
        self.assertNotIn("log-line-000", text)
        self.assertEqual(text.count("log-line-"), error_page.LOG_LINES - special)
        self.assertIn("x" * error_page.LOG_LINE, text)
        self.assertNotIn("x" * (error_page.LOG_LINE + 1), text)

    def test_secret_bearing_cause_is_redacted_and_a_plain_cause_kept(self):
        text = self.write_page(f"the manager failed: Authorization: Bearer {KEY}", "")
        self.assertNotIn(KEY, text)

        text = self.write_page('the manager failed: password = "fixture horse battery staple"', "")
        self.assertNotIn("fixture horse battery staple", text)

        text = self.write_page("the manager refused to open its database", "")
        self.assertIn("the manager refused to open its database", text)

    def test_page_has_no_button_form_or_refresh(self):
        text = self.write_page("cause", "<script>alert(1)</script>")

        for forbidden in ("<button", "<form", "<input", "<script", 'http-equiv="refresh"'):
            self.assertNotIn(forbidden, text)

    def test_a_second_write_replaces_the_first(self):
        self.write_page("first cause", "first log line")
        self.write_page("second cause", "second log line")

        text = error_page.page_file().read_text()
        self.assertNotIn("first cause", text)
        self.assertNotIn("first log line", text)
        self.assertIn("second cause", text)
        self.assertIn("second log line", text)

    def test_concurrent_page_rewrites_keep_the_highest_signed_list_number(self):
        offer = {"version": "2026.10.1", "withdrawn": False}
        barrier = threading.Barrier(3)
        errors = []

        def write(number, current_offer):
            try:
                barrier.wait()
                error_page.write("refresh", "", KEY, offer=current_offer, list_number=number)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=write, args=(8, None)),
                   threading.Thread(target=write, args=(7, offer))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        consent = error_page.read_update_consent()
        self.assertEqual(consent["listNumber"], 8)
        self.assertIsNone(consent["offer"])


if __name__ == "__main__":
    unittest.main()
