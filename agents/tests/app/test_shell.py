"""The shell renders the approved page list and loads each page's sections from their own file."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
PACKAGE = AGENTS / "matuto"
SECTION = ('PAGE = "overview"\nNAME = "probe"\nTITLE = "Probe section"\n\n\n'
           'def html(status):\n    return "<p>probe for " + status["name"] + "</p>"\n')


class ShellRenderTest(unittest.TestCase):
    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)
        shutil.copytree(PACKAGE, self.tree / "matuto")

    def render(self, status="{}", csrf='""'):
        rendered = subprocess.run(
            [sys.executable, "-B", "-c",
             f"import sys; from matuto.shell import render; sys.stdout.write(render({status}, {csrf}))"],
            cwd=self.tree, capture_output=True, text=True)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        return rendered.stdout

    def test_page_list_renders_in_the_approved_order(self):
        html = self.render()
        pages = re.findall(r'<a href="#([a-z-]+)" data-page', html)
        self.assertEqual(pages, ["overview", "model-files", "profiles", "runs", "runtime",
                                 "settings", "console"])

    def test_console_hint_follows_the_status_the_shell_is_given(self):
        self.assertIn('<small class="hint" hidden="">needs agentic serving</small>', self.render())
        self.assertIn('<small class="hint">needs agentic serving</small>',
                      self.render('{"console": {"usable": False}}'))
        self.assertIn('<small class="hint" hidden="">needs agentic serving</small>',
                      self.render('{"console": {"usable": True}}'))

    def test_section_file_loads_under_its_page_without_editing_the_shell(self):
        (self.tree / "matuto" / "page" / "probe.py").write_text(SECTION)
        html = self.render('{"name": "the app"}')
        self.assertIn("Probe section", html)
        self.assertIn("<p>probe for the app</p>", html)
        self.assertLess(html.index("Probe section"), html.index('id="page-model-files"'))

    def test_a_status_value_cannot_close_the_page_script(self):
        html = self.render('{"console": {"usable": False, "reason": "</script><script>x</script>"}}')
        self.assertEqual(html.count("</script>"), self.render().count("</script>"))
        self.assertNotIn("</script><script>x", html)


if __name__ == "__main__":
    unittest.main()