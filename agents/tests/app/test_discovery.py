"""The package offers a command added as its own file, without editing any other file."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
PACKAGE = AGENTS / "matuto"


class CommandDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        shutil.copytree(PACKAGE, self.tree / "matuto")

    def entry(self, *arguments):
        return subprocess.run([sys.executable, str(self.tree / "matuto" / "cli.py"), *arguments],
                              capture_output=True, text=True)

    def test_added_command_file_is_offered_and_run(self):
        (self.tree / "matuto" / "commands" / "sample.py").write_text(
            "def run(argv):\n    print('sample ran ' + ' '.join(argv))\n")

        listed = self.entry()
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("sample", listed.stdout)

        offered = self.entry("sample", "one", "two")
        self.assertEqual(offered.returncode, 0, offered.stderr)
        self.assertEqual(offered.stdout.strip(), "sample ran one two")

    def test_unknown_command_is_refused(self):
        unknown = self.entry("sample")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown command: sample", unknown.stderr)


if __name__ == "__main__":
    unittest.main()