"""The shared log-tail redaction rule, on its own.

The same rule is what the runner stores with a run's evidence and what the job
progress tail shows, so it is checked directly as well as through the runner.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import redaction  # noqa: E402  the package is not on the path yet


class RedactionTest(unittest.TestCase):
    def test_a_secret_named_or_assigned_is_replaced_whole(self):
        for line in ("api_key=supersecret", "api key: supersecret",
                     "Authorization: Bearer abc", "the token is below",
                     "secret", "my password: hunter2"):
            with self.subTest(line=line):
                self.assertEqual(redaction.redact(line), redaction.REDACTED)

    def test_the_router_key_is_replaced_wherever_it_appears(self):
        self.assertEqual(redaction.redact("GET /models?key=abc123", key="abc123"),
                         redaction.REDACTED)

    def test_an_ordinary_measurement_line_is_kept_whole(self):
        text = "load_tensors: layer 1 assigned\nbench repetition 3 of 5\n"
        self.assertEqual(redaction.redact(text), text)

    def test_the_router_key_is_read_from_the_path_cut_4_keeps(self):
        root = Path(tempfile.mkdtemp(prefix="redaction-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertEqual(redaction.router_key(root), "")
        key_file = root / "ai-agents-sandbox" / "pi-local" / "api-key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text("abc123\n")
        self.assertEqual(redaction.router_key(root), "abc123")

    def test_control_characters_are_cleaned_and_the_line_structure_survives(self):
        self.assertEqual(redaction.redact("a\x1b[31mb\n\nc\n"), "a [31mb\n\nc\n")


if __name__ == "__main__":
    unittest.main()
