"""The installation check, through its own Python API, against fixture trees.

The fixtures are compiled here with `cc`, the way `runtime_fixture.py` does, and
then renamed to a final path the way a build job installs one. Every case runs
the check from a directory unrelated to the installation.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

import matuto_runtime  # noqa: E402
from matuto import installation, recipes  # noqa: E402

PIN = "b29c606e28a01b1bc8c1351026a0fa6e616bf6c4"
REPORTED = "b29c606e2"
OTHER_PIN = "deadbeef" * 5
GGML_PIN = "36da57138425487184aa1da2eee2cde155909c6f"
# The server each recipe installs and the library each fixture builds, written
# out from the install probe rather than read from the implementation.
SERVERS = {
    recipes.LLAMA_HIP: "bin/llama-server",
    recipes.LLAMA_VULKAN: "bin/llama-server",
    recipes.BREEZE: "breeze-server",
    recipes.WHISPER: "bin/whisper-server",
}
HELP = """usage: server [options]
  -m FNAME, --model FNAME   model path
  -h, --help                show this help message and exit
  --version                 show version and build info
  --list-devices            list devices
  --host HOST               bind host
  --port PORT               bind port
"""
VERSION = "version: 4096 (build 10218, commit b29c606e2)"
LIBRARY_SOURCE = "int fixture_value(void) { return 7; }\n"
# A linked library with a file name that does not end in `.so`, calling into a
# library only the unrelated working directory provides (review F2).
NEEDING_SOURCE = "extern int plant_value(void);\nint fixture_value(void) { return plant_value(); }\n"
# A harmless planted library: it writes to the marker file when it is loaded.
PLANT_SOURCE = """#include <stdio.h>
#include <stdlib.h>

int plant_value(void) { return 3; }

__attribute__((constructor)) static void planted(void) {
    const char *path = getenv("PLANT_MARKER");
    if (!path) return;
    FILE *file = fopen(path, "a");
    if (file) { fprintf(file, "planted\\n"); fclose(file); }
}
"""
SERVER_SOURCE = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>

extern int fixture_value(void);

static const char *HELP_TEXT = HELP_TEXT_VALUE;
static const char *VERSION_TEXT = VERSION_TEXT_VALUE;
static const char *MARKER = MARKER_VALUE;

static void mark(void) {
    FILE *file = fopen(MARKER, "a");
    if (file) { fprintf(file, "ran\n"); fclose(file); }
}

int main(int argc, char **argv) {
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--help") == 0) {
            mark();
#ifdef HANG
            for (;;) sleep(1);
#endif
#ifdef FLOOD
            for (long line = 0; line < 200000; line++)
                printf("flood flood flood flood flood flood flood flood flood flood\n");
            return 0;
#endif
            printf("%s\n", HELP_TEXT);
            return HELP_EXIT;
        }
        if (strcmp(argv[index], "--version") == 0) {
            mark();
            printf("%s\n", VERSION_TEXT);
            return VERSION_EXIT;
        }
        if (strcmp(argv[index], "--list-devices") == 0) {
            mark();
            printf("Available devices:\n  Vulkan0 AMD Radeon (1000 MiB, 900 MiB free)\n");
            return DEVICE_EXIT;
        }
        if (strncmp(argv[index], "--no-such-option", 16) == 0) {
            mark();
            printf("unknown argument: %s\n", argv[index]);
            return UNKNOWN_EXIT;
        }
    }
    mark();
    printf("%d\n", fixture_value());
    return 0;
}
"""


class TreeBuilder(unittest.TestCase):
    """A renamed fixture tree, built once per case under a temporary root."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="installation-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.working = self.root / "elsewhere"
        self.working.mkdir()
        self.marker = self.root / "ran.txt"
        self.installation = self.root / "final" / "llama-hip" / REPORTED

    def compile(self, *arguments):
        subprocess.run(["cc", "-Wall", "-Werror", *arguments], check=True, capture_output=True)

    def build(self, *, recipe=recipes.LLAMA_HIP, library="bin/libggml-base.so",
              rpath="$ORIGIN", link_library=True, drop_library=False, help_text=HELP,
              version=VERSION, version_exit=0, help_exit=0, hang=False, flood=False,
              unknown_exit=0, device_exit=0, soname=None, link_argument=None, server_link=(),
              library_rpath=None, library_source=LIBRARY_SOURCE, library_link=()):
        """Compile one fixture tree, then rename it to its final path."""
        server = SERVERS[recipe]
        tree = self.root / "staging"
        (tree / server).parent.mkdir(parents=True, exist_ok=True)
        (tree / library).parent.mkdir(parents=True, exist_ok=True)
        (tree / "library.c").write_text(library_source)
        self.compile("-shared", "-fPIC", f"-Wl,-soname,{soname or Path(library).name}",
                     *([f"-Wl,-rpath,{library_rpath}", "-Wl,--enable-new-dtags"]
                       if library_rpath is not None else []),
                     str(tree / "library.c"), *library_link, "-o", str(tree / library))
        if link_library:
            (tree / "server.c").write_text(SERVER_SOURCE)
            defines = [f"-DHELP_TEXT_VALUE={json.dumps(help_text)}",
                       f"-DVERSION_TEXT_VALUE={json.dumps(version)}",
                       f"-DMARKER_VALUE={json.dumps(str(self.marker))}",
                       f"-DUNKNOWN_EXIT={unknown_exit}", f"-DVERSION_EXIT={version_exit}",
                       f"-DDEVICE_EXIT={device_exit}", f"-DHELP_EXIT={help_exit}"]
            self.compile(*defines, *(["-DHANG"] if hang else []), *(["-DFLOOD"] if flood else []),
                         f"-Wl,-rpath,{rpath}", "-Wl,--enable-new-dtags", str(tree / "server.c"),
                         f"-L{tree / Path(library).parent}",
                         link_argument or f"-l{Path(library).name[3:-3]}", *server_link,
                         "-o", str(tree / server))
        if drop_library:
            (tree / library).unlink()
        self.installation.parent.mkdir(parents=True, exist_ok=True)
        tree.rename(self.installation)
        return self.installation

    def outcome(self, tree, recipe=recipes.LLAMA_HIP, **arguments):
        arguments.setdefault("working_directory", str(self.working))
        return installation.check(str(tree), recipe, **arguments)

    def ran(self):
        return self.marker.exists()


class CheckedInstallationTest(TreeBuilder):
    def test_a_clean_renamed_tree_is_checked_from_an_unrelated_directory(self):
        tree = self.build()

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertEqual(result.server, "bin/llama-server")
        self.assertIn("bin/llama-server", result.digests)
        self.assertIn("bin/libggml-base.so", result.digests)
        self.assertEqual(len(result.digests["bin/llama-server"]["sha256"]), 64)
        self.assertTrue(self.ran(), "the probes ran the server")
        libraries = {name: (path, inside) for name, path, inside in result.libraries}
        self.assertEqual(libraries["libggml-base.so"][1], True)
        self.assertTrue(str(tree) in libraries["libggml-base.so"][0])

    def test_device_lines_are_observations_and_never_proof(self):
        tree = self.build()

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertIn("Vulkan0", " ".join(result.observations["devices"]))
        self.assertTrue(any("does not show that inference uses a GPU" in limit for limit in result.limits))
        self.assertTrue(any("model readiness" in limit for limit in result.limits))
        self.assertNotIn("GPU", result.reason)
        self.assertNotIn("ready", result.reason)

    def test_the_reported_commit_is_verified_and_unknown_provenance_stays_an_observation(self):
        tree = self.build()

        pinned = self.outcome(tree, source_commit=PIN)
        self.assertTrue(pinned.identity["source"]["verified"])
        self.assertEqual(pinned.identity["source"]["reported"], REPORTED)

        unknown = self.outcome(tree)
        self.assertEqual(unknown.state, installation.STATE_CHECKED, unknown.reason)
        self.assertIsNone(unknown.identity["source"]["commit"])
        self.assertEqual(unknown.identity["source"]["reported"], REPORTED)
        self.assertFalse(unknown.identity["source"]["verified"])

    def test_a_radv_warning_alone_does_not_fail_the_check(self):
        tree = self.build(help_text=HELP + "WARNING: radv is not a conformant Vulkan implementation\n")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertIn(installation.RADV_WARNING, result.observations["warnings"])

    def test_git_metadata_verifies_the_source_and_the_submodule_commits(self):
        tree = self.build(recipe=recipes.BREEZE, library="third_party/ggml/src/libggml-base.so",
                          rpath="$ORIGIN/third_party/ggml/src", help_text="--help\n")
        (tree / ".git").mkdir()
        (tree / ".git" / "HEAD").write_text(PIN + "\n")
        (tree / ".git" / "modules" / "third_party" / "ggml").mkdir(parents=True)
        (tree / ".git" / "modules" / "third_party" / "ggml" / "HEAD").write_text(GGML_PIN + "\n")

        result = self.outcome(tree, recipes.BREEZE, source_commit=PIN,
                              submodules={"third_party/ggml": GGML_PIN})

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertTrue(result.identity["source"]["verified"])
        self.assertTrue(result.identity["submodules"]["third_party/ggml"]["verified"])

    def test_a_pin_the_installation_cannot_record_leaves_it_unchecked(self):
        # A renamed build tree without a checkout records no commit of its own,
        # and neither does the executable report one, so nothing observed the pin.
        tree = self.build(recipe=recipes.BREEZE, library="third_party/ggml/src/libggml-base.so",
                          rpath="$ORIGIN/third_party/ggml/src", help_text="--help\n")

        result = self.outcome(tree, recipes.BREEZE, source_commit=PIN,
                              submodules={"third_party/ggml": GGML_PIN})

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn(GGML_PIN, result.reason)
        self.assertFalse(result.identity["source"]["verified"])
        self.assertIsNone(result.identity["source"]["recorded"])

    def test_the_build_job_handoff_verifies_a_pin_the_tree_cannot_record(self):
        tree = self.build(recipe=recipes.BREEZE, library="third_party/ggml/src/libggml-base.so",
                          rpath="$ORIGIN/third_party/ggml/src", help_text="--help\n")

        result = self.outcome(tree, recipes.BREEZE, source_commit=PIN,
                              submodules={"third_party/ggml": GGML_PIN}, verified_checkout=True)

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertTrue(result.identity["source"]["verified"])
        self.assertEqual(result.identity["source"]["channel"], "build checkout")
        submodule = result.identity["submodules"]["third_party/ggml"]
        self.assertTrue(submodule["verified"])
        self.assertEqual(submodule["channel"], "build checkout")

    def test_a_library_in_the_working_directory_is_not_loaded(self):
        tree = self.build()
        (self.working / "planted.c").write_text(
            "#include <unistd.h>\n"
            "__attribute__((constructor)) static void stop(void) { _exit(9); }\n"
            "int fixture_value(void) { return 99; }\n")
        planted = self.working / "libggml-base.so"
        self.compile("-shared", "-fPIC", "-Wl,-soname,libggml-base.so",
                     str(self.working / "planted.c"), "-o", str(planted))

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        libraries = {name: (path, inside) for name, path, inside in result.libraries}
        self.assertEqual(libraries["libggml-base.so"][1], True)
        self.assertTrue(str(tree) in libraries["libggml-base.so"][0])
        self.assertNotEqual(libraries["libggml-base.so"][0], str(planted))
        self.assertTrue(self.ran(), "the server ran and the planted library was not loaded")


class LoaderRejectionTest(TreeBuilder):
    def test_an_empty_rpath_entry_is_rejected_before_anything_runs(self):
        tree = self.build(rpath="$ORIGIN:")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertEqual(result.loader_findings[0].file, "bin/llama-server")
        self.assertEqual(result.loader_findings[0].entry, "")
        self.assertIn("empty", result.loader_findings[0].reason)
        self.assertFalse(self.ran(), "a rejected tree never ran its server")

    def test_an_absolute_entry_outside_the_tree_is_rejected_naming_it(self):
        tree = self.build(rpath="$ORIGIN:/opt/elsewhere")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertEqual(result.loader_findings[0].file, "bin/llama-server")
        self.assertEqual(result.loader_findings[0].entry, "/opt/elsewhere")
        self.assertIn("/opt/elsewhere", result.reason)

    def test_a_library_in_another_directory_must_resolve_inside_the_tree(self):
        tree = self.build(library="lib/libggml-base.so", rpath="$ORIGIN")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(any(finding.entry == "libggml-base.so" for finding in result.loader_findings))
        self.assertIn("does not resolve inside", result.reason)

    def test_a_library_reachable_outside_the_tree_is_rejected(self):
        # $ORIGIN/../.. escapes the installation, so the copy outside it wins.
        tree = self.build(rpath="$ORIGIN/../..")
        shutil.copy2(tree / "bin" / "libggml-base.so", tree.parent / "libggml-base.so")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(any(finding.file == "bin/llama-server" and finding.entry == "libggml-base.so"
                            for finding in result.loader_findings))
        self.assertIn("does not resolve inside", result.reason)
        self.assertFalse(self.ran(), "a rejected tree never ran its server")

    def test_a_missing_linked_library_is_rejected(self):
        tree = self.build(drop_library=True)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(any(finding.entry == "libggml-base.so" for finding in result.loader_findings))

    def test_a_missing_executable_is_rejected(self):
        tree = self.build()
        (tree / "bin" / "llama-server").unlink()

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("bin/llama-server", result.reason)
        self.assertIn("absent", result.reason)

    def test_a_commit_mismatch_is_rejected(self):
        tree = self.build(version="version: 4096 (build 10218, commit " + OTHER_PIN[:9] + ")")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn(OTHER_PIN[:9], result.reason)
        self.assertIn(PIN, result.reason)

    def test_a_submodule_commit_mismatch_is_rejected(self):
        tree = self.build(recipe=recipes.BREEZE, library="third_party/ggml/src/libggml-base.so",
                          rpath="$ORIGIN/third_party/ggml/src", help_text="--help\n")
        (tree / ".git" / "modules" / "third_party" / "ggml").mkdir(parents=True)
        (tree / ".git" / "modules" / "third_party" / "ggml" / "HEAD").write_text(OTHER_PIN + "\n")

        result = self.outcome(tree, recipes.BREEZE, source_commit=PIN,
                              submodules={"third_party/ggml": GGML_PIN})

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("third_party/ggml", result.reason)
        self.assertIn(GGML_PIN, result.reason)

    def test_loader_findings_report_statically_and_run_nothing(self):
        tree = self.build(rpath="$ORIGIN:")

        findings = installation.loader_findings(str(tree), recipes.LLAMA_HIP)

        self.assertTrue(findings)
        self.assertFalse(self.ran(), "the static check executed nothing")

    def test_an_unknown_recipe_or_malformed_pin_is_refused(self):
        tree = self.build()
        with self.assertRaises(ValueError):
            installation.check(str(tree), "not a recipe")
        with self.assertRaises(ValueError):
            installation.check(str(tree), recipes.LLAMA_HIP, source_commit=REPORTED)
        with self.assertRaises(ValueError):
            installation.check(str(tree), recipes.LLAMA_HIP, submodules={"../ggml": GGML_PIN})


class ProbeFailureTest(TreeBuilder):
    def test_a_whisper_server_needs_the_model_option_in_its_help_text(self):
        tree = self.build(recipe=recipes.WHISPER, help_text="usage: whisper-server [options]\n"
                                                            "  -h, --help  show help\n", unknown_exit=0)

        result = self.outcome(tree, recipes.WHISPER, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("--model", result.reason)
        self.assertTrue(self.ran(), "the help probe ran; only the capability was missing")
        # The trap the check must not fall into: the unknown option exits 0.
        self.assertEqual(subprocess.run([str(tree / "bin" / "whisper-server"), "--no-such-option"],
                                        capture_output=True).returncode, 0)

    def test_a_dependency_that_is_a_path_is_refused_before_anything_runs(self):
        # A DT_NEEDED holding a slash is a pathname the loader reads against the
        # child's working directory, behind RUNPATH, so a planted library wins.
        tree = self.build(library="bin/sub/libggml-base.so", soname="sub/libggml-base.so")
        (self.working / "sub").mkdir()
        (self.working / "planted.c").write_text(PLANT_SOURCE)
        self.compile("-shared", "-fPIC", "-Wl,-soname,sub/libggml-base.so",
                     str(self.working / "planted.c"), "-o",
                     str(self.working / "sub" / "libggml-base.so"))

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(any(finding.entry == "sub/libggml-base.so" for finding in result.loader_findings),
                        result.loader_findings)
        self.assertFalse(self.ran())

    def test_a_linked_library_without_a_so_name_is_still_checked(self):
        # The tree walk matches `*.so`, but a library a server links must be
        # held to the loader rule whatever its file name is.
        (self.working / "planted.c").write_text(PLANT_SOURCE)
        self.compile("-shared", "-fPIC", "-Wl,-soname,libplant.so",
                     str(self.working / "planted.c"), "-o", str(self.working / "libplant.so"))
        tree = self.build(library="bin/libggml-base", soname="libggml-base",
                          link_argument="-l:libggml-base", library_rpath="$ORIGIN:",
                          library_source=NEEDING_SOURCE,
                          library_link=[f"-L{self.working}", "-lplant"],
                          server_link=[f"-Wl,-rpath-link,{self.working}"])

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(any(finding.file == "bin/libggml-base" and finding.entry == ""
                            for finding in result.loader_findings), result.loader_findings)
        self.assertFalse(self.ran())

    def test_a_help_probe_that_fails_leaves_the_installation_unchecked(self):
        tree = self.build(help_exit=1)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("help probe failed", result.reason)

    def test_a_probe_past_its_deadline_leaves_the_installation_unchecked(self):
        tree = self.build(hang=True)

        with mock.patch.object(matuto_runtime, "PROBE_SECONDS", 0.3):
            result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("timed out", result.reason)

    def test_a_flooding_probe_leaves_the_installation_unchecked(self):
        tree = self.build(flood=True)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("limit", result.reason)

    def test_a_failing_version_probe_leaves_the_installation_unchecked(self):
        tree = self.build(version_exit=3)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("version probe failed", result.reason)

    @unittest.skipIf(os.geteuid() == 0, "root reads a file with no permissions")
    def test_a_device_probe_that_fails_leaves_the_installation_unchecked(self):
        tree = self.build(device_exit=4)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("device probe failed", result.reason)

    def test_an_unreadable_library_leaves_the_installation_unchecked(self):
        tree = self.build()
        library = tree / "bin" / "libggml-base.so"
        library.chmod(0)
        self.addCleanup(library.chmod, 0o755)

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("libggml-base.so", result.reason)

    def test_a_library_that_is_a_link_out_of_the_tree_is_refused(self):
        tree = self.build()
        outside = self.root / "outside"
        outside.mkdir()
        self.compile("-shared", "-fPIC", "-Wl,-soname,libggml-base.so",
                     str(tree / "library.c"), "-o", str(outside / "libggml-base.so"))
        library = tree / "bin" / "libggml-base.so"
        library.unlink()
        library.symlink_to(outside / "libggml-base.so")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("link out of the installation", result.reason)

    def test_an_executable_that_is_a_link_out_of_the_tree_is_refused(self):
        tree = self.build()
        server = tree / "bin" / "llama-server"
        server.unlink()
        server.symlink_to("/bin/true")

        result = self.outcome(tree, source_commit=PIN)

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertIn("bin/llama-server", result.reason)
