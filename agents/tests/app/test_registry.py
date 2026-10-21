"""The build registry: register, forget and discover installation trees.

Every rule goes through `matuto.registry`'s Python API against a temporary store
and fixture installation trees, which is the seam the spec's Testing Decisions
name. The tree that has to pass installation checks is compiled here with `cc`; the
others are directories with a server-shaped file, because discovery reads the
store's recorded check and never runs anything.
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
from matuto import installation, recipes, registry  # noqa: E402
from matuto import store as store_module  # noqa: E402

PIN = "b29c606e28a01b1bc8c1351026a0fa6e616bf6c4"
CARD = "card-r9"
LIBRARY = "int fixture_value(void) { return 7; }\n"
# The server marks every run, so a test can tell discovery (which must run
# nothing) from Inspect (which runs installation checks).
SERVER = r"""
#include <stdio.h>
#include <string.h>

extern int fixture_value(void);

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
            for (;;) { }
#endif
            printf("usage: llama-server [options]\n"
                   "  -m FNAME, --model FNAME   model path\n"
                   "  --version                 show version and build info\n");
            return 0;
        }
        if (strcmp(argv[index], "--version") == 0) {
            mark();
            printf("version: 1 (build 1, commit " COMMIT_VALUE ")\n");
            return 0;
        }
    }
    mark();
    printf("%d\n", fixture_value());
    return 0;
}
"""
# A server that answers only from its own directory, so a check run from the
# tree passes and one run from an unrelated directory does not.
CWD_ONLY = r"""
#include <stdio.h>
#include <string.h>
#include <unistd.h>

extern int fixture_value(void);

int main(int argc, char **argv) {
    for (int index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--help") == 0) {
            if (access("cwd-only.txt", F_OK) != 0) { return 1; }
            printf("usage: llama-server [options]\n"
                   "  -m FNAME, --model FNAME   model path\n"
                   "  --version                 show version and build info\n");
            return 0;
        }
        if (strcmp(argv[index], "--version") == 0) {
            printf("version: 1 (build 1, commit " COMMIT_VALUE ")\n");
            return 0;
        }
    }
    printf("%d\n", fixture_value());
    return 0;
}
"""


class WriteLog:
    """The store's connection, remembering every statement that is not a read."""

    def __init__(self, connection):
        self._connection = connection
        self.writes = []

    def execute(self, sql, *arguments):
        if not sql.lstrip().upper().startswith("SELECT"):
            self.writes.append(sql)
        return self._connection.execute(sql, *arguments)

    def executemany(self, sql, arguments):
        self.writes.append(sql)
        return self._connection.executemany(sql, arguments)


class RegistryTestCase(unittest.TestCase):
    """A temporary store, a temporary build location and one card to select."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="registry-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.data = self.home / "data"
        self.root = self.data / "ai-agents-sandbox" / "builds"
        self.root.mkdir(parents=True, mode=0o700)
        self.marker = self.home / "ran.txt"
        self.store = store_module.open_store(self.config)
        self.addCleanup(self.store.close)
        self.connection = self.store.connection()
        self.connection.execute(
            "INSERT INTO cards (card_id, name, gpu_identity, vram_ceiling_bytes,"
            " default_gpu_layers, smoke_output_tokens, bench_prompt_tokens,"
            " bench_generation_tokens, bench_repetitions, notes, created_at)"
            " VALUES (?, 'R9700', 'gfx1201', 34359738368, 99, 256, 512, 128, 3, '',"
            " '2026-10-10T00:00:00+00:00')", (CARD,))

    def tree(self, name):
        """One installation-shaped tree under the build location."""
        path = self.root / name
        (path / "bin").mkdir(parents=True, mode=0o700)
        server = path / "bin" / "llama-server"
        server.write_text("#!/bin/sh\n")
        server.chmod(0o755)
        return path

    def compile_tree(self, name, *, pin=PIN, source=SERVER, rpath="$ORIGIN", hang=False):
        """A tree whose server and library the installation check accepts."""
        path = self.root / name
        (path / "bin").mkdir(parents=True, mode=0o700)
        (path / "library.c").write_text(LIBRARY)
        self.compile("-shared", "-fPIC", "-Wl,-soname,libggml-base.so",
                     str(path / "library.c"), "-o", str(path / "bin" / "libggml-base.so"))
        (path / "server.c").write_text(source)
        self.compile(f"-Wl,-rpath,{rpath}", "-Wl,--enable-new-dtags",
                     f"-DMARKER_VALUE={json.dumps(str(self.marker))}",
                     f"-DCOMMIT_VALUE={json.dumps(pin)}",
                     *(["-DHANG"] if hang else []),
                     str(path / "server.c"), f"-L{path / 'bin'}", "-lggml-base",
                     "-o", str(path / "bin" / "llama-server"))
        return path

    def compile(self, *arguments):
        subprocess.run(["cc", "-Wall", "-Werror", *arguments], check=True, capture_output=True)

    def record(self, path, *, state=registry.CHECKED, reason="", recipe=recipes.LLAMA_HIP,
               target="gfx1201", revision=PIN, toolchain="rocm 7.0, gcc 15"):
        """The installation record the build job writes for one tree it installed."""
        self.connection.execute(
            "INSERT INTO installations (location, recipe, source_revision, target, toolchain,"
            " check_state, check_reason, check_digests, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '2026-10-10T00:00:00+00:00')",
            (str(path), recipe, revision, target, toolchain, state, reason))

    def select(self, build_id):
        self.connection.execute(
            "INSERT INTO runtime_selection (selection_id, build_id, card_id, backend, saved_at)"
            " VALUES (1, ?, ?, 'hip', '2026-10-10T00:00:00+00:00')", (build_id, CARD))

    def selection(self):
        row = self.connection.execute(
            "SELECT * FROM runtime_selection WHERE selection_id = 1").fetchone()
        return None if row is None else dict(row)

    def builds(self):
        return self.connection.execute("SELECT * FROM builds ORDER BY build_id").fetchall()

    def entries(self):
        return {entry["location"]: entry for entry in registry.listing(self.connection, self.root)}


class RecordingTest(RegistryTestCase):
    def test_a_recorded_installation_registers_selects_and_qualifies_nothing(self):
        path = self.tree("gfx1201/b29c606e28a0")

        self.record(path)

        self.assertEqual(self.builds(), [])
        self.assertIsNone(self.selection())
        self.assertEqual(self.entries()[str(path)]["status"], "discovered")


class RegisterTest(RegistryTestCase):
    def test_register_records_the_id_the_label_and_the_recorded_provenance(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)

        entry = registry.register(self.connection, "hip-b10918", str(path), "ROCm 7.0")

        row = self.connection.execute(
            "SELECT * FROM builds WHERE build_id = 'hip-b10918'").fetchone()
        self.assertEqual(row["location"], str(path))
        self.assertEqual(row["label"], "ROCm 7.0")
        self.assertEqual(row["kind"], "llama-hip")
        self.assertEqual(row["backend"], "hip")
        self.assertEqual(row["source_revision"], PIN)
        self.assertEqual(row["target"], "gfx1201")
        self.assertEqual(row["recipe"], recipes.LLAMA_HIP)
        self.assertEqual(row["toolchain"], "rocm 7.0, gcc 15")
        self.assertEqual(row["state"], "registered")
        self.assertEqual(entry["status"], "ready")
        self.assertEqual(entry["check"]["state"], "checked")
        # Register selects and runs nothing.
        self.assertIsNone(self.selection())

    def test_an_omitted_label_is_the_id(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)

        entry = registry.register(self.connection, "hip", str(path))

        self.assertEqual(entry["label"], "hip")

    def test_registering_the_same_id_for_the_same_tree_changes_only_the_label(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path), "first")

        entry = registry.register(self.connection, "hip", str(path), "second")

        self.assertEqual(entry["label"], "second")
        rows = self.builds()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["location"], str(path))
        self.assertEqual(rows[0]["source_revision"], PIN)
        self.assertEqual(rows[0]["state"], "registered")

    def test_a_registered_tree_whose_files_are_gone_is_still_relabelled(self):
        # Forget already tolerates a selected entry whose files are missing, so a
        # label correction must not depend on the tree still being there.
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path), "first")
        shutil.rmtree(path)

        entry = registry.register(self.connection, "hip", str(path), "second")

        self.assertEqual(entry["label"], "second")
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(self.builds()[0]["location"], str(path))

    def test_registering_an_id_that_names_another_tree_is_refused_naming_it(self):
        first = self.tree("gfx1201/first")
        self.record(first)
        registry.register(self.connection, "hip", str(first))
        second = self.tree("gfx1201/second")
        self.record(second)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "hip", str(second))

        self.assertIn(str(first), str(refused.exception))
        rows = self.builds()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["location"], str(first))

    def test_a_second_id_for_the_same_tree_is_refused_naming_the_first(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "hip-second", str(path))

        self.assertIn("hip", str(refused.exception))
        self.assertEqual(len(self.builds()), 1)

    def test_a_tree_without_a_recorded_passed_check_is_not_registered(self):
        bare = self.tree("gfx1201/bare")
        unchecked = self.tree("gfx1201/unchecked")
        self.record(unchecked, state=registry.UNCHECKED, reason="the RUNPATH carries an empty entry")

        with self.assertRaises(store_module.StoreError) as unrecorded:
            registry.register(self.connection, "hip", str(bare))
        self.assertIn("no Matuto build recorded an installation", str(unrecorded.exception))
        with self.assertRaises(store_module.StoreError) as failed:
            registry.register(self.connection, "hip", str(unchecked))
        self.assertIn("empty entry", str(failed.exception))
        self.assertEqual(self.builds(), [])

    def test_a_missing_tree_is_not_registered(self):
        path = self.tree("gfx1201/gone")
        self.record(path)
        shutil.rmtree(path)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "hip", str(path))

        self.assertIn("missing", str(refused.exception))
        self.assertEqual(self.builds(), [])

    def test_a_tree_whose_server_is_gone_is_not_registered(self):
        path = self.tree("gfx1201/serverless")
        self.record(path)
        (path / "bin" / "llama-server").unlink()

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "hip", str(path))

        self.assertIn("server is missing", str(refused.exception))
        self.assertEqual(self.builds(), [])

    def test_a_tree_whose_server_is_not_runnable_is_not_registered(self):
        path = self.tree("gfx1201/not-runnable")
        self.record(path)
        (path / "bin" / "llama-server").chmod(0o644)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "hip", str(path))

        self.assertIn("server is missing", str(refused.exception))
        self.assertEqual(self.builds(), [])

    def test_malformed_ids_labels_and_locations_are_refused(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)

        for bad in ("", " hip", "-hip", "hip/x", "hip x", "h" * 65, 7, None):
            with self.assertRaises(store_module.StoreError, msg=repr(bad)):
                registry.register(self.connection, bad, str(path))
        for bad in ("two\nlines", "one\0line", "x" * 201):
            with self.assertRaises(store_module.StoreError, msg=repr(bad)):
                registry.register(self.connection, "hip", str(path), bad)
        for bad in ("relative/path", str(path) + "\0", str(self.root / ".." / "escape")):
            with self.assertRaises(store_module.StoreError, msg=repr(bad)):
                registry.register(self.connection, "hip", bad)
        self.assertEqual(self.builds(), [])


class ForgetTest(RegistryTestCase):
    def test_forget_refuses_the_selected_entry_even_when_its_files_are_missing(self):
        path = self.tree("gfx1201/selected")
        self.record(path)
        registry.register(self.connection, "hip", str(path))
        self.select("hip")
        shutil.rmtree(path)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.forget(self.connection, "hip")

        self.assertIn("hip", str(refused.exception))
        self.assertIn("select another build first", str(refused.exception))
        self.assertEqual(len(self.builds()), 1)
        self.assertEqual(self.selection()["build_id"], "hip")

    def test_an_allowed_forget_leaves_the_files_and_evidence_and_rediscovers_the_tree(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))

        location = registry.forget(self.connection, "hip")

        self.assertEqual(location, str(path))
        self.assertEqual(self.builds(), [])
        self.assertTrue((path / "bin" / "llama-server").is_file())
        recorded = self.connection.execute("SELECT count(*) FROM installations").fetchone()[0]
        self.assertEqual(recorded, 1)
        self.assertEqual(self.entries()[str(path)]["status"], "discovered")

    def test_forget_refuses_an_unknown_id_and_names_what_is_registered(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))

        with self.assertRaises(store_module.StoreError) as refused:
            registry.forget(self.connection, "vulkan")
        self.assertIn("hip", str(refused.exception))

        registry.forget(self.connection, "hip")
        with self.assertRaises(store_module.StoreError) as empty:
            registry.forget(self.connection, "hip")
        self.assertIn("no builds are registered at all", str(empty.exception))


class DiscoveryTest(RegistryTestCase):
    def test_a_recorded_passed_check_does_not_make_a_tree_without_a_runnable_server_discovered(self):
        path = self.tree("gfx1201/not-runnable")
        self.record(path)
        (path / "bin" / "llama-server").chmod(0o644)

        entry = self.entries()[str(path)]

        self.assertEqual(entry["status"], "found but unchecked")
        self.assertIn("server is missing", entry["reason"])

    def test_a_recorded_passed_check_does_not_make_an_old_layout_leftover_discovered(self):
        # Only the recipe's own server counts, so a plain `llama-server` at the
        # root of a tree whose `bin/` server is gone is not a complete install.
        path = self.tree("gfx1201/old-layout")
        self.record(path)
        (path / "bin" / "llama-server").unlink()
        (path / "llama-server").write_text("#!/bin/sh\n")
        (path / "llama-server").chmod(0o755)

        entry = self.entries()[str(path)]

        self.assertEqual(entry["status"], "found but unchecked")
        self.assertIn(str(path / "bin" / "llama-server"), entry["reason"])

    def test_only_a_recorded_passed_check_makes_a_tree_an_installation(self):
        checked = self.tree("gfx1201/checked")
        self.record(checked)
        unchecked = self.tree("gfx1201/unchecked")
        self.record(unchecked, state=registry.UNCHECKED, reason="the RUNPATH carries an empty entry")
        bare = self.tree("gfx1201/bare")

        entries = self.entries()

        self.assertEqual(entries[str(checked)]["status"], "discovered")
        self.assertEqual(entries[str(unchecked)]["status"], "found but unchecked")
        self.assertIn("empty entry", entries[str(unchecked)]["reason"])
        self.assertEqual(entries[str(bare)]["status"], "found but unchecked")
        discovered = [entry["location"] for entry in entries.values() if entry["status"] == "discovered"]
        self.assertEqual(discovered, [str(checked)])

    def test_a_tree_that_is_gone_is_not_discovered(self):
        path = self.tree("gfx1201/gone")
        self.record(path)
        shutil.rmtree(path)

        self.assertNotIn(str(path), self.entries())

    def test_discovery_runs_nothing_from_any_tree(self):
        path = self.compile_tree("gfx1201/b29c606e28a0")
        self.record(path)

        self.entries()

        self.assertFalse(self.marker.exists(), "discovery ran an executable from a tree")

    def test_an_old_checkout_build_stays_where_it_is_and_is_found_but_unchecked(self):
        path = self.root / "gfx1201" / "b29c606e28a0"
        (path / "bin").mkdir(parents=True, mode=0o700)
        (path / "bin" / "llama-server").write_text("#!/bin/sh\n")

        entry = self.entries()[str(path)]

        self.assertEqual(entry["status"], "found but unchecked")
        self.assertIsNone(entry["build_id"])
        self.assertTrue(path.is_dir(), "discovery moved the old build")
        self.assertEqual(self.builds(), [])

    def test_staging_outside_the_build_location_never_appears(self):
        staging = self.data / "ai-agents-sandbox" / "build-staging" / "gfx1201" / "b29c606e28a0"
        (staging / "bin").mkdir(parents=True, mode=0o700)
        (staging / "bin" / "llama-server").write_text("#!/bin/sh\n")

        self.assertEqual(registry.listing(self.connection, self.root), [])

    def test_the_listing_shows_a_registered_tree_once_with_its_registry_id(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path), "ROCm 7.0")

        entries = self.entries()

        self.assertEqual(list(entries), [str(path)])
        entry = entries[str(path)]
        self.assertEqual(entry["status"], "ready")
        self.assertEqual(entry["build_id"], "hip")
        self.assertEqual(entry["label"], "ROCm 7.0")
        self.assertEqual(entry["recipe"], recipes.LLAMA_HIP)
        self.assertEqual(entry["source_revision"], PIN)

    def test_a_selected_registered_tree_is_shown_selected(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))
        self.select("hip")

        entry = self.entries()[str(path)]

        self.assertEqual(entry["status"], "selected")
        self.assertTrue(entry["selected"])


class InspectTest(RegistryTestCase):
    def test_inspect_runs_the_check_and_records_its_result(self):
        path = self.compile_tree("gfx1201/b29c606e28a0")
        self.record(path)

        result = registry.inspect(self.connection, str(path))

        self.assertEqual(result.state, installation.STATE_CHECKED, result.reason)
        self.assertTrue(self.marker.exists(), "the check ran the tree's server")
        row = self.connection.execute(
            "SELECT * FROM installations WHERE location = ?", (str(path),)).fetchone()
        self.assertEqual(row["check_state"], registry.CHECKED)
        self.assertEqual(json.loads(row["check_digests"])["bin/llama-server"]["sha256"],
                         result.digests["bin/llama-server"]["sha256"])
        # Inspect registers and selects nothing.
        self.assertEqual(self.builds(), [])
        self.assertIsNone(self.selection())

    def test_inspect_records_a_failed_check_with_its_reason(self):
        path = self.tree("gfx1201/broken")
        self.record(path)

        result = registry.inspect(self.connection, str(path))

        self.assertEqual(result.state, installation.STATE_UNCHECKED)
        self.assertTrue(result.reason)
        row = self.connection.execute(
            "SELECT * FROM installations WHERE location = ?", (str(path),)).fetchone()
        self.assertEqual(row["check_state"], registry.UNCHECKED)
        self.assertEqual(row["check_reason"], result.reason)

    def test_inspect_checks_from_a_directory_unrelated_to_the_tree(self):
        # A caller standing in the tree must not certify an installation that
        # only works from its own directory (RT-5).
        path = self.compile_tree("gfx1201/cwd", source=CWD_ONLY)
        (path / "cwd-only.txt").write_text("only here\n")
        self.record(path)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(path)

        result = registry.inspect(self.connection, str(path))

        self.assertEqual(result.state, installation.STATE_UNCHECKED,
                         "the caller's working directory certified the tree")
        self.assertIn("help probe failed", result.reason)

    def test_inspect_refuses_a_tree_matuto_did_not_install(self):
        path = self.tree("gfx1201/old")

        with self.assertRaises(store_module.StoreError) as refused:
            registry.inspect(self.connection, str(path))

        self.assertIn("no Matuto build recorded an installation", str(refused.exception))
        self.assertFalse(self.marker.exists())


class AvailabilityTest(RegistryTestCase):
    def test_a_registered_tree_is_available_until_its_check_or_its_files_fail(self):
        path = self.compile_tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))

        available, reason = registry.availability(self.connection, "hip")
        self.assertTrue(available)
        self.assertIsNone(reason)

        (path / "bin" / "libggml-base.so").unlink()
        registry.inspect(self.connection, str(path))
        available, reason = registry.availability(self.connection, "hip")
        self.assertFalse(available)
        self.assertIn("did not pass", reason)

        shutil.rmtree(path)
        available, reason = registry.availability(self.connection, "hip")
        self.assertFalse(available)
        self.assertIn("files are missing", reason)

    def test_an_unregistered_id_is_unavailable_and_no_build_is_substituted(self):
        available, reason = registry.availability(self.connection, "hip")

        self.assertFalse(available)
        self.assertIn("hip", reason)

    def test_a_registration_recorded_unavailable_is_unavailable(self):
        path = self.tree("gfx1201/b29c606e28a0")
        self.record(path)
        registry.register(self.connection, "hip", str(path))
        self.connection.execute("UPDATE builds SET state = 'unavailable' WHERE build_id = 'hip'")

        available, reason = registry.availability(self.connection, "hip")

        self.assertFalse(available)
        self.assertIn("unavailable", reason)

    def test_a_registration_with_no_recorded_check_stays_registered_and_ineligible(self):
        # The shape an entry registered as external metadata has: present
        # and recorded, with no installation check to make it eligible yet.
        path = self.tree("gfx1201/external")
        self.connection.execute(
            "INSERT INTO builds VALUES ('external', 'external', 'llama-vulkan', ?, 'vulkan',"
            " NULL, NULL, NULL, NULL, 'external', '')", (str(path),))

        entry = self.entries()[str(path)]

        self.assertEqual(entry["status"], "registered")
        self.assertIn("no installation check is recorded", entry["reason"])
        available, reason = registry.availability(self.connection, "external")
        self.assertFalse(available)
        self.assertIn("no installation check is recorded", reason)


class ExternalBuildTest(RegistryTestCase):
    def external(self, path, *, recipe=recipes.LLAMA_HIP, label="External build"):
        return registry.register_external(self.connection, "external", str(path), recipe, label)

    def consented_inspection(self):
        preview = registry.inspect_external(self.connection, "external")
        self.assertTrue(preview["needs_consent"])
        return registry.inspect_external(self.connection, "external",
                                         trust_digest=preview["consent_digest"])

    def test_registration_records_unknown_external_metadata_and_runs_nothing(self):
        path = self.compile_tree("gfx1201/external")

        entry = self.external(path)

        row = self.connection.execute("SELECT * FROM builds WHERE build_id = 'external'").fetchone()
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(row["state"], "external")
        self.assertIsNone(row["source_revision"])
        self.assertIsNone(row["target"])
        self.assertIsNone(row["toolchain"])
        self.assertEqual(entry["check"]["state"], registry.UNCHECKED)
        self.assertFalse(registry.availability(self.connection, "external")[0])
        self.assertFalse(self.marker.exists(), "external registration executed the tree")
        self.assertIsNone(self.selection())

    def test_ordinary_inspect_refuses_an_external_registration_without_execution(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.inspect(self.connection, str(path))

        self.assertIn("unknown source provenance", str(refused.exception))
        self.assertFalse(self.marker.exists(), "ordinary Inspect executed an external tree")
        self.assertFalse(registry.availability(self.connection, "external")[0])

    def test_external_registration_after_forget_starts_unavailable_again(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)
        self.consented_inspection()
        self.marker.unlink()
        registry.forget(self.connection, "external")

        self.assertEqual(self.entries()[str(path)]["status"], "found but unchecked")
        with self.assertRaises(store_module.StoreError) as refused:
            registry.register(self.connection, "ordinary", str(path))
        self.assertIn("external trust flow", str(refused.exception))
        entry = self.external(path)
        self.assertEqual(entry["status"], "unavailable")
        self.assertFalse(registry.availability(self.connection, "external")[0])
        self.assertFalse(self.marker.exists(), "registration after Forget executed the tree")

    def test_external_id_stays_bound_to_its_tree_and_can_only_be_relabeled(self):
        first = self.compile_tree("gfx1201/external")
        second = self.compile_tree("gfx1201/other")
        self.external(first)

        with self.assertRaises(store_module.StoreError) as refused:
            registry.register_external(self.connection, "external", str(second), recipes.LLAMA_HIP)
        self.assertIn(str(first), str(refused.exception))

        entry = registry.register_external(self.connection, "external", str(first),
                                           recipes.LLAMA_HIP, "Renamed external")
        self.assertEqual(entry["location"], str(first))
        self.assertEqual(entry["label"], "Renamed external")
        self.assertEqual(entry["state"], "external")
        self.assertFalse(self.marker.exists())

    def test_external_inspection_shows_static_findings_before_consent_and_runs_nothing(self):
        path = self.compile_tree("gfx1201/unsafe", rpath="$ORIGIN:")
        self.external(path)

        result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertFalse(result["needs_consent"])
        self.assertIn("empty RUNPATH", result["reason"])
        self.assertFalse(self.marker.exists(), "static loader refusal ran the server")
        self.assertFalse(registry.availability(self.connection, "external")[0])

    def test_clean_external_tree_requires_consent_then_becomes_ready_unknown(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)

        preview = registry.inspect_external(self.connection, "external")
        self.assertTrue(preview["needs_consent"])
        self.assertFalse(self.marker.exists(), "showing static findings ran the server")
        self.assertFalse(registry.availability(self.connection, "external")[0])

        checked = registry.inspect_external(self.connection, "external",
                                             trust_digest=preview["consent_digest"])

        self.assertEqual(checked["state"], registry.CHECKED, checked["reason"])
        self.assertTrue(self.marker.exists(), "consented inspection did not run the fixture")
        row = self.connection.execute("SELECT * FROM builds WHERE build_id = 'external'").fetchone()
        self.assertEqual(row["state"], "external")
        self.assertIsNone(row["source_revision"])
        self.assertIsNotNone(checked["observations"]["build"])
        self.assertTrue(registry.availability(self.connection, "external")[0])
        self.assertEqual(registry.listing(self.connection, self.root)[0]["status"], "ready")

    def test_tree_change_after_findings_requires_fresh_consent(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)
        preview = registry.inspect_external(self.connection, "external")
        (path / "bin" / "llama-server").write_bytes(
            (path / "bin" / "llama-server").read_bytes() + b"changed")

        stale = registry.inspect_external(self.connection, "external",
                                          trust_digest=preview["consent_digest"])

        self.assertTrue(stale["needs_consent"])
        self.assertIn("consent does not match", stale["reason"])
        self.assertFalse(self.marker.exists(), "stale consent ran a changed tree")
        self.assertFalse(registry.availability(self.connection, "external")[0])

    def test_consent_for_one_tree_does_not_authorize_another(self):
        first = self.compile_tree("gfx1201/first")
        second = self.compile_tree("gfx1201/second")
        registry.register_external(self.connection, "first", str(first), recipes.LLAMA_HIP)
        registry.register_external(self.connection, "second", str(second), recipes.LLAMA_HIP)
        preview = registry.inspect_external(self.connection, "first")

        result = registry.inspect_external(self.connection, "second",
                                           trust_digest=preview["consent_digest"])

        self.assertTrue(result["needs_consent"])
        self.assertIn("consent does not match", result["reason"])
        self.assertFalse(self.marker.exists(), "one tree's consent executed another tree")

    def test_consent_digest_is_rechecked_inside_the_executable_check(self):
        path = self.compile_tree("gfx1201/external")
        replacement = self.compile_tree("gfx1201/replacement",
                                        source=SERVER.replace("version: 1", "version: 2"))
        self.external(path)
        preview = registry.inspect_external(self.connection, "external")
        check = installation.check

        def replace_then_check(location, recipe, **options):
            shutil.copy2(replacement / "bin" / "llama-server", path / "bin" / "llama-server")
            return check(location, recipe, **options)

        with mock.patch.object(installation, "check", side_effect=replace_then_check):
            result = registry.inspect_external(self.connection, "external",
                                               trust_digest=preview["consent_digest"])

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("changed after its static findings", result["reason"])
        self.assertFalse(self.marker.exists(), "replacement code ran between consent and probe")

    def test_external_file_change_revokes_eligibility_without_running_code(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)
        self.consented_inspection()
        self.marker.unlink()
        with (path / "bin" / "llama-server").open("ab") as server:
            server.write(b"changed")

        available, reason = registry.availability(self.connection, "external")

        self.assertFalse(available)
        self.assertIn("changed since", reason)
        self.assertFalse(self.marker.exists(), "eligibility check executed the changed tree")

    def test_external_probe_failure_stays_unavailable_and_can_be_repaired(self):
        path = self.compile_tree("gfx1201/external", source=CWD_ONLY)
        self.external(path)
        failed = self.consented_inspection()
        self.assertEqual(failed["state"], registry.UNCHECKED)
        self.assertIn("help probe failed", failed["reason"])
        self.assertFalse(registry.availability(self.connection, "external")[0])

        path = self.compile_tree("gfx1201/replacement")
        # Keep the same registered tree and repair its server in place.
        destination = self.root / "gfx1201" / "external" / "bin" / "llama-server"
        shutil.copy2(path / "bin" / "llama-server", destination)
        repaired = self.consented_inspection()
        self.assertEqual(repaired["state"], registry.CHECKED, repaired["reason"])
        self.assertTrue(registry.availability(self.connection, "external")[0])

    def test_external_inspection_refuses_foreign_permission_modes(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)
        server = path / "bin" / "llama-server"
        server.chmod(0o775)

        result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("writable by another account", result["reason"])
        self.assertFalse(self.marker.exists())

    def test_external_file_owned_by_another_account_is_refused_before_execution(self):
        path = self.compile_tree("gfx1201/external")
        self.external(path)

        foreign_uid = os.getuid() + 1
        with mock.patch("matuto.installation.os.getuid", return_value=foreign_uid):
            result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("belongs to another account", result["reason"])
        self.assertFalse(self.marker.exists())

    def test_external_check_uses_a_working_directory_outside_the_tree(self):
        path = self.compile_tree("gfx1201/external", source=CWD_ONLY)
        (path / "cwd-only.txt").write_text("works only from the tree\n")
        self.external(path)
        preview = registry.inspect_external(self.connection, "external")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(path)

        result = registry.inspect_external(self.connection, "external",
                                           trust_digest=preview["consent_digest"])

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("help probe failed", result["reason"])
        self.assertFalse(registry.availability(self.connection, "external")[0])

    def test_external_probe_timeout_stays_unavailable(self):
        path = self.compile_tree("gfx1201/external", hang=True)
        self.external(path)

        with mock.patch.object(matuto_runtime, "PROBE_SECONDS", 0.2):
            preview = registry.inspect_external(self.connection, "external")
            result = registry.inspect_external(self.connection, "external",
                                               trust_digest=preview["consent_digest"])

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("timed out", result["reason"])
        self.assertFalse(registry.availability(self.connection, "external")[0])

    def test_external_absolute_runpath_is_reported_without_running(self):
        path = self.compile_tree("gfx1201/external", rpath="$ORIGIN:/opt/elsewhere")
        self.external(path)

        result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("does not start with $ORIGIN", result["reason"])
        self.assertFalse(self.marker.exists())

    def test_external_outside_runpath_is_reported_without_running(self):
        path = self.compile_tree("gfx1201/external", rpath="$ORIGIN/../..")
        shutil.copy2(path / "bin" / "libggml-base.so", path.parent / "libggml-base.so")
        self.external(path)

        result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("does not resolve inside", result["reason"])
        self.assertFalse(self.marker.exists())

    def test_external_library_link_out_is_refused_without_running(self):
        path = self.compile_tree("gfx1201/external")
        outside = self.home / "outside.so"
        shutil.copy2(path / "bin" / "libggml-base.so", outside)
        library = path / "bin" / "libggml-base.so"
        library.unlink()
        library.symlink_to(outside)
        self.external(path)

        result = registry.inspect_external(self.connection, "external")

        self.assertEqual(result["state"], registry.UNCHECKED)
        self.assertIn("link out of the installation", result["reason"])
        self.assertFalse(self.marker.exists())


class NoClearSelectionTest(RegistryTestCase):
    def test_no_registry_operation_writes_the_runtime_selection(self):
        selected = self.tree("gfx1201/selected")
        other = self.tree("gfx1201/other")
        self.record(selected)
        self.record(other, recipe=recipes.LLAMA_VULKAN, target=None)
        registry.register(self.connection, "hip", str(selected))
        registry.register(self.connection, "vulkan", str(other))
        self.select("hip")
        before = self.selection()
        logged = WriteLog(self.connection)

        registry.register(logged, "vulkan", str(other), "relabelled")
        registry.inspect(logged, str(other))
        registry.forget(logged, "vulkan")
        with self.assertRaises(store_module.StoreError):
            registry.forget(logged, "hip")

        self.assertFalse([statement for statement in logged.writes
                          if "runtime_selection" in statement],
                         "a registry operation wrote the runtime selection")
        self.assertEqual(self.selection(), before)
        self.assertEqual(self.selection()["build_id"], "hip")


if __name__ == "__main__":
    unittest.main()
