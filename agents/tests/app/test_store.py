"""Matuto's one SQLite file: what the store module creates, keeps and refuses.

Everything here goes through the module's own Python API against a temporary
home directory, which is the seam the spec's Testing Decisions name.
"""

import contextlib
import hashlib
import io
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
PACKAGE = AGENTS / "matuto"
sys.path.insert(0, str(AGENTS))

from matuto import cli as cli_module  # noqa: E402  the package is not on the path yet
from matuto import store as store_module  # noqa: E402  the package is not on the path yet

NOW = "2026-10-09T16:00:00+00:00"


def mode_of(path):
    return stat.S_IMODE(path.lstat().st_mode)


def digest_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StoreTestCase(unittest.TestCase):
    """A temporary config root standing in for the operator's home directory."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.directory = store_module.store_directory(self.config)
        self.path = store_module.store_path(self.config)

    def open_store(self):
        store = store_module.open_store(self.config)
        self.addCleanup(store.close)
        return store

    def fill(self, connection):
        """One connected record of every kind the schema names."""
        connection.executescript(
            """
            INSERT INTO cards VALUES ('card-r9', 'Radeon AI PRO R9700', 'gfx1201',
                34359738368, 99, 16, 4096, 512, 5, 'the loud one', '2026-10-01T00:00:00+00:00');
            INSERT INTO artifact_locations VALUES ('loc-models', '/var/lib/matuto/models',
                'the download root', '2026-10-01T00:00:00+00:00');
            INSERT INTO model_storage VALUES (1, 'loc-models', '2026-10-01T00:00:00+00:00');
            INSERT INTO download_origins (origin_id, kind, name, notes, created_at)
                VALUES ('origin-unsloth', 'organisation', 'unsloth',
                '', '2026-10-01T00:00:00+00:00');
            INSERT INTO lab_port VALUES (1, 18890, '2026-10-01T00:00:00+00:00');
            INSERT INTO argument_catalogue VALUES ('arg-single-turn', '--single-turn', 0,
                'one exchange per prompt', 'smoke,bench', 'seeded from the examples');
            INSERT INTO speech_executables VALUES ('exe-breeze', 'synthesis', 'loc-models',
                '/var/lib/matuto/models/breeze/bin/breeze', 'a' * 64, '0.4.0', '',
                '2026-10-01T00:00:00+00:00');
            INSERT INTO acquisition_records VALUES ('acq-qwen36', 'Qwen3.6 35B-A3B UD-Q4_K_M',
                'download', 'published-checksum', 'origin-unsloth', 'unsloth/Qwen3.6-35B-GGUF',
                'a483e9e6cbd595906af30beda3187c2663a1118d', 'loc-models',
                '2026-10-02T00:00:00+00:00', '', '2026-10-02T00:00:00+00:00');
            INSERT INTO acquisition_files VALUES ('acq-qwen36', 'Qwen3.6-35B-A3B-Q4_K_M.gguf',
                20000000000, 'b0651e28' || substr(hex(randomblob(28)), 1, 56),
                'c' || substr(hex(randomblob(31)), 1, 63), '2026-10-02T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-coder', 'language', 'Qwen3.6 Coder 192K',
                'qwen3-coder', 131072, 'high', 'card-r9', NULL, 0, 'tuned for the router',
                '2026-10-03T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-breeze', 'synthesis', 'Breeze on the R9',
                '', NULL, NULL, 'card-r9', 'exe-breeze', 0, 'voice', '2026-10-03T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-whisper', 'recognition', 'Whisper on the CPU',
                '', NULL, NULL, NULL, NULL, 0, '', '2026-10-03T00:00:00+00:00');
            INSERT INTO profile_arguments VALUES ('prof-coder', 'server', 0, NULL,
                '--parallel', '1');
            INSERT INTO profile_arguments VALUES ('prof-coder', 'bench', 0, 'arg-single-turn',
                '--single-turn', NULL);
            INSERT INTO profile_artifacts VALUES ('prof-coder', 'model', 'acq-qwen36',
                'Qwen3.6-35B-A3B-Q4_K_M.gguf');
            INSERT INTO bundles VALUES ('bundle-voice', 'Voice 192K', 'the voice bundle',
                512, 'Describe the sea in one short paragraph.', '2026-10-04T00:00:00+00:00');
            INSERT INTO bundle_members VALUES ('bundle-voice', 'language', 'prof-coder');
            INSERT INTO bundle_members VALUES ('bundle-voice', 'synthesis', 'prof-breeze');
            INSERT INTO bundle_members VALUES ('bundle-voice', 'recognition', 'prof-whisper');
            INSERT INTO builds VALUES ('build-hip', 'ROCm 7.0', 'llama-hip',
                '/var/lib/matuto/builds/hip-b10918', 'hip', 'b10918-de699957b', 'gfx1201',
                'llama-hip', 'rocm 7.0, gcc 15', 'registered', '');
            INSERT INTO runtime_selection VALUES (1, 'build-hip', 'card-r9', 'hip',
                '2026-10-05T00:00:00+00:00');
            INSERT INTO installations VALUES ('/var/lib/matuto/builds/hip-b10918',
                'llama.cpp HIP', 'b10918de699957b0d0e5a0d4e4b4b7a1a3c2b1d0', 'gfx1201',
                'rocm 7.0, gcc 15', 'checked', '', '{}', '2026-10-05T00:00:00+00:00');
            INSERT INTO jobs VALUES ('job-build-hip', 'build', 'matuto-build-hip.service',
                'llama-hip@b10918', 'linking', 'completed',
                'source b10918-de699957b, recipe llama-hip, toolchain rocm 7.0',
                '[{"name": "llama-server", "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}]',
                'log tail', 1, '2026-10-05T00:00:00+00:00', '2026-10-05T01:00:00+00:00');
            INSERT INTO runs VALUES ('run-smoke-1', 'language', 'prof-coder', 'Qwen3.6 Coder 192K',
                'fp-9c1', 'build-hip', 'smoke', '2026.10.1', 'matuto-lab-smoke.service', 'completed', 0,
                'one passed smoke', 'smoke log', 0, '2026-10-06T00:00:00+00:00',
                '2026-10-06T00:05:00+00:00');
            INSERT INTO checks VALUES (NULL, 'run-smoke-1', 0, 'exact reply', 'PASS', '');
            INSERT INTO run_evidence VALUES (NULL, 'run-smoke-1', 'report.md', 'text/markdown',
                11, 'd' || substr(hex(randomblob(31)), 1, 63), 0, x'2323736d6f6b65206f6b');
            INSERT INTO qualification_records VALUES ('qual-1', 'language', 'prof-coder',
                'Qwen3.6 Coder 192K', 'fp-9c1', 'build-hip', '2026-10-06T00:06:00+00:00');
            INSERT INTO qualification_runs VALUES ('qual-1', 'run-smoke-1');
            INSERT INTO acceptances VALUES ('acc-1', 'qual-1', 'agentic serving', '',
                'operator', '2026-10-06T00:07:00+00:00', NULL);
            INSERT INTO acceptance_runs VALUES ('acc-1', 'run-smoke-1', 'fp-9c1', 'build-hip');
            INSERT INTO acceptance_exceptions VALUES ('acc-1', 'WQ-09', 'retrieval is good enough');
            INSERT INTO sessions VALUES ('digest-abc', 'operator', 'gen-1',
                '2026-10-07T00:00:00+00:00', '2026-10-14T00:00:00+00:00');
            INSERT INTO update_state VALUES (1, '2026.9.1', 'snapshot verified',
                '/var/lib/matuto/snapshots/2026.9.1.sqlite3', '2026-10-08T00:00:00+00:00',
                '2026-10-08T00:00:00+00:00');
            """)


class CreationTest(StoreTestCase):
    def test_acceptance_and_withdrawal_are_not_cli_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            accepted = cli_module.main(["matuto", "accept"])
            withdrawn = cli_module.main(["matuto", "withdraw"])

        self.assertEqual((accepted, withdrawn), (2, 2))

    def test_first_use_creates_a_private_file_recording_its_schema_version(self):
        self.assertFalse(self.directory.exists())
        store = self.open_store()

        self.assertEqual(store.path, self.directory / "store.sqlite3")
        self.assertEqual(mode_of(self.directory), 0o700)
        self.assertEqual(mode_of(self.path), 0o600)
        recorded = store.connection().execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()
        self.assertEqual(recorded[0], str(store_module.SCHEMA_VERSION))

    def test_journal_files_beside_the_store_are_private_too(self):
        self.open_store()
        other = sqlite3.connect(str(self.path), isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO cards VALUES ('card-second', 'n', 'gfx1100', 1, 1, 1, 1, 1, 1, '', '')")
        journals = [path for path in self.directory.iterdir()
                    if path.name.startswith(store_module.STORE_FILENAME + "-")]
        self.assertEqual([path.name for path in journals], [self.path.name + "-journal"])
        for journal in journals:
            self.assertEqual(mode_of(journal), 0o600)

    def test_the_same_path_is_reopened_and_keeps_its_records(self):
        first = self.open_store()
        self.fill(first.connection())
        first.close()

        second = self.open_store()
        self.assertEqual(second.path, self.path)
        self.assertEqual(
            second.connection().execute("SELECT name FROM cards").fetchone()[0],
            "Radeon AI PRO R9700")

    def test_foreign_keys_are_enforced_on_every_connection_the_module_opens(self):
        first = self.open_store()
        self.fill(first.connection())
        self.assertEqual(first.connection().execute("PRAGMA foreign_keys").fetchone()[0], 1)
        first.close()

        second = self.open_store()
        self.assertEqual(second.connection().execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_every_record_kind_is_stored_and_read_back(self):
        store = self.open_store()
        connection = store.connection()
        self.fill(connection)

        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        for table in ("cards", "model_profiles", "profile_arguments", "profile_artifacts",
                      "bundles", "bundle_members", "argument_catalogue", "artifact_locations",
                      "model_storage", "download_origins", "lab_port", "runtime_selection",
                      "builds", "installations", "speech_executables", "acquisition_records",
                      "acquisition_files",
                      "jobs", "runs", "checks", "run_evidence", "qualification_records",
                      "qualification_runs", "acceptances", "acceptance_runs",
                      "acceptance_exceptions", "sessions", "update_state"):
            self.assertIn(table, tables)
            self.assertGreaterEqual(
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1,
                f"{table} did not keep its record")
        argv = connection.execute(
            "SELECT flag, value FROM profile_arguments "
            "WHERE profile_id = 'prof-coder' AND tool = 'server' AND position = 0").fetchone()
        self.assertEqual(tuple(argv), ("--parallel", "1"))
        job = connection.execute(
            "SELECT provenance, output_digests FROM jobs WHERE job_id = 'job-build-hip'").fetchone()
        self.assertEqual(tuple(job), (
            "source b10918-de699957b, recipe llama-hip, toolchain rocm 7.0",
            '[{"name": "llama-server", "sha256": '
            '"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}]'))
        for table in ("cards", "model_profiles", "bundles"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            self.assertIn("notes", columns)

    def test_the_schema_holds_no_integration_settings_and_no_consumer_exports(self):
        store = self.open_store()
        names = [row[0] for row in store.connection().execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")]
        schema = " ".join(names).lower()
        for absent in ("integration", "consumer", "export", "api_key", "thinking_preference"):
            self.assertNotIn(absent, schema)

    def test_only_the_store_module_opens_the_file(self):
        for source in PACKAGE.rglob("*.py"):
            if source.name == "store.py":
                continue
            self.assertIsNone(re.search(r"\bconnect\(", source.read_text()),
                              f"{source.name} opens Matuto's store itself")


class ReferenceEnforcementTest(StoreTestCase):
    def test_removing_a_referenced_record_is_refused_and_changes_nothing(self):
        store = self.open_store()
        connection = store.connection()
        self.fill(connection)

        for statement in ("DELETE FROM cards WHERE card_id = 'card-r9'",
                          "DELETE FROM model_profiles WHERE profile_id = 'prof-coder'",
                          "DELETE FROM bundles WHERE bundle_id = 'bundle-voice'",
                          "DELETE FROM acquisition_records WHERE acquisition_id = 'acq-qwen36'",
                          "DELETE FROM acquisition_files WHERE acquisition_id = 'acq-qwen36'",
                          "DELETE FROM runs WHERE run_id = 'run-smoke-1'"):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
        self.assertEqual(connection.execute("SELECT count(*) FROM cards").fetchone()[0], 1)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM model_profiles").fetchone()[0], 3)

    def test_a_published_language_profile_is_refused_through_plain_sql(self):
        store = self.open_store()
        connection = store.connection()
        self.fill(connection)
        # A published profile nothing refers to, so the refusal cannot be a
        # foreign key doing the work.
        connection.execute("INSERT INTO model_profiles VALUES ('prof-published', 'language',"
                           " 'Published 96K', 'published-96k', 98304, 'off', 'card-r9', NULL, 1, '',"
                           " '2026-10-09T00:00:00+00:00')")

        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM model_profiles WHERE profile_id = 'prof-published'")

        self.assertEqual(connection.execute(
            "SELECT published FROM model_profiles WHERE profile_id = 'prof-published'").fetchone()[0], 1)
        connection.execute("UPDATE model_profiles SET published = 0 WHERE profile_id = 'prof-published'")
        connection.execute("DELETE FROM model_profiles WHERE profile_id = 'prof-published'")
        self.assertIsNone(connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-published'").fetchone())

    def test_an_unreferenced_record_can_be_removed(self):
        store = self.open_store()
        connection = store.connection()
        self.fill(connection)
        connection.execute("DELETE FROM bundle_members WHERE profile_id = 'prof-whisper'")

        connection.execute("DELETE FROM model_profiles WHERE profile_id = 'prof-whisper'")

        self.assertIsNone(connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-whisper'").fetchone())

    def test_removed_records_qualification_and_runs_stay_readable(self):
        store = self.open_store()
        connection = store.connection()
        self.fill(connection)
        connection.execute("INSERT INTO runs VALUES ('run-whisper', 'language', 'prof-whisper',"
                           " 'Whisper on the CPU', 'fp-whisper', NULL, 'smoke', '2026.10.1', NULL,"
                           " 'completed', 0, '', '', 0, '2026-10-06T00:00:00+00:00', NULL)")
        connection.execute("INSERT INTO qualification_records VALUES ('qual-whisper', 'language',"
                           " 'prof-whisper', 'Whisper on the CPU', 'fp-whisper', NULL,"
                           " '2026-10-06T00:00:00+00:00')")

        connection.execute("DELETE FROM acceptance_exceptions WHERE acceptance_id = 'acc-1'")
        connection.execute("DELETE FROM acceptance_runs WHERE acceptance_id = 'acc-1'")
        connection.execute("DELETE FROM acceptances WHERE acceptance_id = 'acc-1'")
        connection.execute("DELETE FROM qualification_runs WHERE qualification_id = 'qual-1'")
        connection.execute("DELETE FROM qualification_records WHERE qualification_id = 'qual-1'")
        connection.execute("DELETE FROM bundle_members WHERE profile_id = 'prof-whisper'")
        connection.execute("DELETE FROM model_profiles WHERE profile_id = 'prof-whisper'")

        self.assertIsNone(connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-whisper'").fetchone())
        run = connection.execute(
            "SELECT record_id, record_label, fingerprint FROM runs WHERE run_id = 'run-whisper'").fetchone()
        self.assertEqual(tuple(run), ("prof-whisper", "Whisper on the CPU", "fp-whisper"))
        qualification = connection.execute(
            "SELECT record_id, record_label, fingerprint FROM qualification_records"
            " WHERE qualification_id = 'qual-whisper'").fetchone()
        self.assertEqual(tuple(qualification), ("prof-whisper", "Whisper on the CPU", "fp-whisper"))


class RefusalTest(StoreTestCase):
    def test_a_newer_store_is_refused_naming_both_versions_and_unchanged(self):
        store = self.open_store()
        newer = str(store_module.SCHEMA_VERSION + 1)
        store.connection().execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
                                   (newer,))
        store.close()
        before = digest_of(self.path)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        message = str(refusal.exception)
        self.assertIn(f"schema version {newer}", message)
        self.assertIn(f"the {store_module.SCHEMA_VERSION} this Matuto reads", message)
        self.assertEqual(digest_of(self.path), before)

    def test_a_file_that_is_not_a_store_is_refused_and_never_re_initialised(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.path.write_bytes(b"SQLite format 3\x00 not really")
        os.chmod(self.path, 0o600)

        with self.assertRaises(store_module.StoreError):
            store_module.open_store(self.config)

        self.assertEqual(self.path.read_bytes(), b"SQLite format 3\x00 not really")

    def test_a_corrupt_store_is_refused_untouched(self):
        store = self.open_store()
        store.connection().execute("INSERT INTO cards VALUES ('c', 'n', 'g', 1, 1, 1, 1, 1, 1, '', '')")
        store.close()
        raw = bytearray(self.path.read_bytes())
        raw[4096:8192] = b"\xff" * 4096
        self.path.write_bytes(bytes(raw))
        before = digest_of(self.path)

        with self.assertRaises(store_module.StoreError):
            store_module.open_store(self.config)

        self.assertEqual(digest_of(self.path), before)

    def test_a_database_without_the_store_tables_is_refused(self):
        self.directory.mkdir(parents=True, mode=0o700)
        other = sqlite3.connect(str(self.path), isolation_level=None)
        other.execute("CREATE TABLE something_else (a)")
        other.close()
        os.chmod(self.path, 0o600)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("not a Matuto store", str(refusal.exception))
        self.assertNotIn("try again", str(refusal.exception))

    def test_a_linked_store_is_refused(self):
        self.open_store().close()
        real = self.path.read_bytes()
        self.path.unlink()
        os.symlink(self.home / "elsewhere.sqlite3", self.path)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("not a regular file", str(refusal.exception))
        self.assertFalse((self.home / "elsewhere.sqlite3").exists())

    def test_a_linked_state_directory_is_refused(self):
        self.open_store().close()
        shutil.move(str(self.directory), str(self.home / "real"))
        os.symlink(self.home / "real", self.directory)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("not a directory", str(refusal.exception))

    def test_a_link_to_a_missing_state_directory_is_refused(self):
        (self.config / store_module.CONFIG_ROOT_NAME).mkdir(parents=True)
        os.symlink(self.home / "never-created", self.directory)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        # The name is taken but nothing usable is there, which is the same race
        # another Matuto winning the first use looks like.
        self.assertIn("not a directory", str(refusal.exception))
        self.assertIn(str(self.directory), str(refusal.exception))

    def test_a_loose_or_linked_journal_beside_the_store_is_refused(self):
        self.open_store().close()
        journal = self.path.parent / (self.path.name + "-journal")
        os.symlink(self.home / "planted", journal)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("not a regular file", str(refusal.exception))
        self.assertFalse((self.home / "planted").exists())

        journal.unlink()
        journal.write_bytes(b"")
        os.chmod(journal, 0o666)
        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)
        self.assertIn("group or others", str(refusal.exception))

    def test_a_store_readable_or_writable_by_group_or_others_is_refused(self):
        for loose in (0o644, 0o640, 0o604, 0o666):
            with self.subTest(mode=loose):
                self.open_store().close()
                os.chmod(self.path, loose)

                with self.assertRaises(store_module.StoreError) as refusal:
                    store_module.open_store(self.config)

                self.assertIn("group or others", str(refusal.exception))
                self.assertEqual(mode_of(self.path), loose)
                os.chmod(self.path, 0o600)

    def test_a_state_directory_open_to_group_or_others_is_refused(self):
        for loose in (0o750, 0o705):
            with self.subTest(mode=loose):
                self.open_store().close()
                os.chmod(self.directory, loose)

                with self.assertRaises(store_module.StoreError) as refusal:
                    store_module.open_store(self.config)

                self.assertIn("group or others", str(refusal.exception))
                os.chmod(self.directory, 0o700)

    def test_a_store_or_directory_belonging_to_another_account_is_refused(self):
        self.open_store().close()
        foreign = os.getuid() + 1

        # The directory is judged first, so the file's own ownership rule needs a
        # second pass with the directory reading as this account's.
        with mock.patch.object(store_module.os, "getuid", return_value=foreign):
            with self.assertRaises(store_module.StoreError) as refusal:
                store_module.open_store(self.config)
        self.assertIn("Matuto state directory belongs to another account", str(refusal.exception))

        with mock.patch.object(store_module.os, "getuid", side_effect=[os.getuid(), foreign]):
            with self.assertRaises(store_module.StoreError) as refusal:
                store_module.open_store(self.config)
        self.assertIn("Matuto store belongs to another account", str(refusal.exception))
        self.assertEqual(mode_of(self.path), 0o600)

    def test_a_planted_journal_is_refused_before_the_first_open(self):
        self.directory.mkdir(parents=True, mode=0o700)
        journal = self.path.parent / (self.path.name + "-journal")
        os.symlink(self.home / "planted", journal)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("not a regular file", str(refusal.exception))
        self.assertFalse((self.home / "planted").exists())
        self.assertFalse(self.path.exists())

        journal.unlink()
        journal.write_bytes(b"")
        os.chmod(journal, 0o666)
        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)
        self.assertIn("group or others", str(refusal.exception))
        self.assertFalse(self.path.exists())

    def test_a_store_that_lost_its_tables_is_refused(self):
        self.directory.mkdir(parents=True, mode=0o700)
        other = sqlite3.connect(str(self.path), isolation_level=None)
        other.execute("CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        other.execute("INSERT INTO store_meta VALUES ('schema_version', ?)",
                      (str(store_module.SCHEMA_VERSION),))
        other.close()
        os.chmod(self.path, 0o600)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("missing its", str(refusal.exception))

    def test_corruption_outside_the_metadata_page_is_refused(self):
        store = self.open_store()
        connection = store.connection()
        connection.execute("INSERT INTO cards VALUES ('c', 'n', 'g', 1, 1, 1, 1, 1, 1, '', '')")
        root = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'cards'").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        store.close()
        raw = bytearray(self.path.read_bytes())
        raw[(root - 1) * page_size:root * page_size] = b"\xff" * page_size
        self.path.write_bytes(bytes(raw))
        before = digest_of(self.path)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("corrupt", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)

    def test_a_corrupt_store_with_an_interrupted_write_is_refused_untouched(self):
        store = self.open_store()
        connection = store.connection()
        connection.execute("INSERT INTO cards VALUES ('c', 'n', 'g', 1, 1, 1, 1, 1, 1, '', '')")
        root = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'cards'").fetchone()[0]
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        store.close()
        raw = bytearray(self.path.read_bytes())
        raw[(root - 1) * page_size:root * page_size] = b"\xff" * page_size
        self.path.write_bytes(bytes(raw))
        # An interrupted write of its own leaves a journal beside the corrupt
        # store, which is the case where the original must not be recovered
        # before the recovered view has been judged.
        killed = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
for index in range(50):
    connection.execute("INSERT INTO store_meta VALUES (?, ?)", ('key%d' % index, 'value' * 4000))
os._exit(9)
""", str(self.path)], capture_output=True, text=True)
        self.assertEqual(killed.returncode, 9, killed.stderr)
        journal = self.path.parent / (self.path.name + "-journal")
        self.assertTrue(journal.exists())
        before = digest_of(self.path)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        self.assertIn("corrupt", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)
        self.assertTrue(journal.exists())


class InterruptedCreationTest(StoreTestCase):
    def test_a_creation_interrupted_before_commit_is_finished_by_the_next_open(self):
        self.directory.mkdir(parents=True, mode=0o700)
        # A child that opens the file, starts the schema, and is killed before it
        # commits: SQLite's own rollback journal is what has to clean up.
        killed = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
sys.path.insert(0, sys.argv[1])
from matuto import store
path = store.store_path(sys.argv[2])
os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
connection = sqlite3.connect(str(path), isolation_level=None)
connection.execute('BEGIN IMMEDIATE')
connection.execute('CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
connection.execute("INSERT INTO store_meta VALUES ('schema_version', ?)",
                    (str(store.SCHEMA_VERSION),))
connection.execute('CREATE TABLE half_a_schema (a)')
os._exit(9)
""", str(AGENTS), str(self.config)], capture_output=True, text=True)
        self.assertEqual(killed.returncode, 9, killed.stderr)

        store = self.open_store()

        self.assertEqual(store.connection().execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0],
            str(store_module.SCHEMA_VERSION))
        self.assertIsNone(store.connection().execute(
            "SELECT name FROM sqlite_master WHERE name = 'half_a_schema'").fetchone())
        self.assertEqual(mode_of(self.path), 0o600)

    def test_an_empty_file_where_the_store_belongs_is_created_into(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.path.touch(mode=0o600)

        store = self.open_store()

        self.assertEqual(store.connection().execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0],
            str(store_module.SCHEMA_VERSION))
        self.assertIsNotNone(store.connection().execute(
            "SELECT name FROM sqlite_master WHERE name = 'runs'").fetchone())
        self.assertEqual(mode_of(self.path), 0o600)

    def test_an_interrupted_write_is_finished_before_anything_reads_the_store(self):
        store = self.open_store()
        self.fill(store.connection())
        store.close()
        # A write killed before its commit. SQLite leaves its journal beside the
        # file, and a read-only connection cannot replay it: a plain read of the
        # file raises "attempt to write a readonly database", and a read that
        # succeeds instead may see the interrupted transaction's own rows.
        killed = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
for index in range(50):
    connection.execute("INSERT INTO store_meta VALUES (?, ?)", ('key%d' % index, 'value' * 4000))
connection.execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
                    (sys.argv[2],))
connection.execute("INSERT INTO store_meta VALUES ('interrupted', 'yes')")
os._exit(9)
""", str(self.path), str(store_module.SCHEMA_VERSION)], capture_output=True, text=True)
        self.assertEqual(killed.returncode, 9, killed.stderr)

        recovered = self.open_store()

        self.assertEqual(recovered.connection().execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0],
            str(store_module.SCHEMA_VERSION))
        self.assertIsNone(recovered.connection().execute(
            "SELECT 1 FROM store_meta WHERE key = 'interrupted'").fetchone())
        self.assertEqual(recovered.connection().execute(
            "SELECT count(*) FROM cards").fetchone()[0], 1)
        recovered.close()
        self.assertEqual(self.open_store().connection().execute(
            "SELECT count(*) FROM cards").fetchone()[0], 1)

    def test_a_newer_store_with_an_interrupted_write_is_refused_unchanged(self):
        store = self.open_store()
        newer = str(store_module.SCHEMA_VERSION + 1)
        store.connection().execute("UPDATE store_meta SET value = ? WHERE key = 'schema_version'",
                                   (newer,))
        store.close()
        killed = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
for index in range(50):
    connection.execute("INSERT INTO store_meta VALUES (?, ?)", ('key%d' % index, 'value' * 4000))
connection.execute("INSERT INTO cards VALUES ('card-interrupted', 'n', 'g', 1, 1, 1, 1, 1, 1, '', '')")
os._exit(9)
""", str(self.path)], capture_output=True, text=True)
        self.assertEqual(killed.returncode, 9, killed.stderr)
        before = digest_of(self.path)

        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.open_store(self.config)

        message = str(refusal.exception)
        self.assertIn(f"schema version {newer}", message)
        self.assertIn("left unchanged", message)
        # The interrupted write was probed on a copy, so the operator's file is
        # byte-for-byte what it was, and its committed contents still say 2.
        self.assertEqual(digest_of(self.path), before)
        raw = sqlite3.connect(str(self.path))
        self.addCleanup(raw.close)
        self.assertIsNone(raw.execute(
            "SELECT 1 FROM cards WHERE card_id = 'card-interrupted'").fetchone())
        self.assertEqual(raw.execute(
            "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0], newer)


class ReadStoreTest(StoreTestCase):
    """`read_store` opens a store for reading alone: it never creates or recovers."""

    def test_it_creates_no_store_and_reports_nothing_to_read(self):
        self.assertIsNone(store_module.read_store(self.config))
        self.assertFalse(self.path.exists())

    def test_its_connection_cannot_write_so_a_writer_that_starts_is_left_alone(self):
        store = self.open_store()
        store.connection().execute(
            "INSERT INTO bundles (bundle_id, name, notes, load_tokens, load_prompt, created_at) "
            "VALUES ('bundle', 'Bundle', '', NULL, NULL, '')")
        store.connection().commit()
        opened = store_module.read_store(self.config)
        self.addCleanup(opened.close)
        with self.assertRaises(sqlite3.OperationalError):
            opened.connection().execute("DELETE FROM bundles")

    def test_an_interrupted_creation_and_another_writers_journal_are_left_alone(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.path.touch(mode=0o600)
        self.assertIsNone(store_module.read_store(self.config))
        store = self.open_store()
        store.connection().execute(
            "INSERT INTO bundles (bundle_id, name, notes, load_tokens, load_prompt, created_at) "
            "VALUES ('bundle', 'Bundle', '', NULL, NULL, '')")
        store.connection().commit()
        before = digest_of(self.path)
        journal = self.path.parent / (self.path.name + "-journal")
        journal.write_bytes(b"")
        os.chmod(journal, 0o600)
        self.assertIsNone(store_module.read_store(self.config))
        self.assertEqual(digest_of(self.path), before)
        self.assertTrue(journal.exists())
        journal.unlink()

    def test_a_file_this_release_refuses_is_reported_rather_than_opened(self):
        self.directory.mkdir(parents=True, mode=0o700)
        self.path.write_bytes(b"not a Matuto store")
        os.chmod(self.path, 0o600)
        self.assertIsNone(store_module.read_store(self.config))
        self.assertEqual(self.path.read_bytes(), b"not a Matuto store")

    def test_a_state_directory_open_to_group_or_others_is_reported(self):
        self.open_store()
        os.chmod(self.directory, 0o755)
        self.addCleanup(os.chmod, self.directory, 0o700)
        self.assertIsNone(store_module.read_store(self.config))

    def test_a_store_file_this_account_cannot_stat_is_reported_not_raised(self):
        self.open_store()
        exists = Path.exists

        def inaccessible(path):
            if path == self.path:
                raise PermissionError("store file is unreachable")
            return exists(path)

        with mock.patch.object(Path, "exists", inaccessible):
            self.assertIsNone(store_module.read_store(self.config))

class StoreUpdateLockTest(StoreTestCase):
    """The updater excludes new users and gates stores needing forward repair."""

    def test_an_exclusive_update_lock_refuses_new_store_users(self):
        exclusive = store_module.open_store(self.config, exclusive=True)
        try:
            with self.assertRaisesRegex(store_module.StoreError, "update is in progress"):
                store_module.open_store(self.config)
            self.assertIsNone(store_module.read_store(self.config))
        finally:
            exclusive.close()
        reopened = store_module.read_store(self.config)
        self.assertIsNotNone(reopened)
        reopened.close()

    def test_unfinished_update_phases_block_writers_until_verified_completion(self):
        initial = store_module.open_store(self.config)
        initial.connection().execute(
            "INSERT INTO sessions VALUES ('digest', 'before', 'generation', 'now', 'later')")
        initial.close()
        for phase in ("preparing", "prepared", "activating", "maintenance", "unknown"):
            store_module.write_update_state(self.path, "2026.10.1", phase)
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(store_module.StoreError, "Matuto update to 2026.10.1"):
                    store_module.open_store(self.config)
                self.assertIsNone(store_module.read_store(self.config))

        store_module.write_update_state(self.path, "2026.10.1", "verifying")
        readonly = store_module.open_store(self.config)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.connection().execute("UPDATE sessions SET username='changed'")
        readonly.close()
        store_module.write_update_state(self.path, "2026.10.1", "complete")
        writable = store_module.open_store(self.config)
        writable.connection().execute("UPDATE sessions SET username='verified'")
        writable.close()
        self.assertEqual(store_module.read_sessions_at(self.path)[0][1], "verified")


MODEL_SHA256 = "a" * 64
MMPROJ_SHA256 = "b" * 64
MTP_SHA256 = "c" * 64
WEIGHTS_SHA256 = "d" * 64
BREEZE_SHA256 = "e" * 64
WHISPER_SHA256 = "f" * 64


class ServedConfigurationTest(StoreTestCase):
    """The served configuration, its fingerprint and the computed qualification state.

    One fixture store holds a language profile with an offload override, sidecars
    and a bundle, a language profile that takes the card's default layers, a
    synthesis profile and a recognition profile, with fixed digests so the
    canonical fingerprint can be compared across processes.
    """

    def serve(self):
        store = self.open_store()
        connection = store.connection()
        connection.executescript(f"""
            INSERT INTO cards VALUES ('card-r9', 'Radeon R9', 'gfx1201', 34359738368, 99,
                16, 4096, 512, 5, 'loud', '2026-10-01T00:00:00+00:00');
            INSERT INTO cards VALUES ('card-twin', 'Identical Twin', 'gfx1201-twin', 34359738368,
                99, 16, 4096, 512, 5, 'quiet', '2026-10-01T00:00:00+00:00');
            INSERT INTO artifact_locations VALUES ('loc-models', '/models', '',
                '2026-10-01T00:00:00+00:00');
            INSERT INTO speech_executables VALUES ('exe-breeze', 'synthesis', 'loc-models',
                '/bin/breeze', '{BREEZE_SHA256}', '0.4.0', '', '2026-10-01T00:00:00+00:00');
            INSERT INTO speech_executables VALUES ('exe-whisper', 'recognition', 'loc-models',
                '/bin/whisper', '{WHISPER_SHA256}', '1.0', '', '2026-10-01T00:00:00+00:00');
            INSERT INTO acquisition_records VALUES ('acq-qwen', 'Qwen', 'download',
                'locally-hashed', NULL, NULL, NULL, 'loc-models', NULL, '',
                '2026-10-02T00:00:00+00:00');
            INSERT INTO acquisition_files VALUES ('acq-qwen', 'model.gguf', 100, NULL,
                '{MODEL_SHA256}', NULL);
            INSERT INTO acquisition_files VALUES ('acq-qwen', 'mmproj.gguf', 10, NULL,
                '{MMPROJ_SHA256}', NULL);
            INSERT INTO acquisition_files VALUES ('acq-qwen', 'mtp.gguf', 10, NULL,
                '{MTP_SHA256}', NULL);
            INSERT INTO acquisition_records VALUES ('acq-qwen-copy', 'Qwen copy', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc-models', NULL, '',
                '2026-10-02T00:00:00+00:00');
            INSERT INTO acquisition_files VALUES ('acq-qwen-copy', 'model.gguf', 100, NULL,
                '{MODEL_SHA256}', NULL);
            INSERT INTO acquisition_records VALUES ('acq-qwen-moved', 'Qwen moved', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc-models', NULL, '',
                '2026-10-02T00:00:00+00:00');
            INSERT INTO acquisition_files VALUES ('acq-qwen-moved', 'model.gguf', 100, NULL,
                '{MODEL_SHA256}', NULL);
            INSERT INTO acquisition_files VALUES ('acq-qwen-moved', 'mmproj.gguf', 10, NULL,
                '{MMPROJ_SHA256}', NULL);
            INSERT INTO acquisition_files VALUES ('acq-qwen-moved', 'mtp.gguf', 10, NULL,
                '{MTP_SHA256}', NULL);
            INSERT INTO acquisition_records VALUES ('acq-speech', 'Speech', 'download',
                'locally-hashed', NULL, NULL, NULL, 'loc-models', NULL, '',
                '2026-10-02T00:00:00+00:00');
            INSERT INTO acquisition_files VALUES ('acq-speech', 'weights.bin', 10, NULL,
                '{WEIGHTS_SHA256}', NULL);
            INSERT INTO model_profiles VALUES ('prof-lang', 'language', 'Qwen 192K', 'qwen',
                131072, 'high', 'card-r9', NULL, 1, 'notes', '2026-10-03T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-lang-default', 'language', 'Qwen default',
                'qwen-default', 131072, 'high', 'card-r9', NULL, 0, '',
                '2026-10-03T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-synth', 'synthesis', 'Breeze', '', NULL, NULL,
                'card-r9', 'exe-breeze', 0, 'n', '2026-10-03T00:00:00+00:00');
            INSERT INTO model_profiles VALUES ('prof-recog', 'recognition', 'Whisper', '', NULL, NULL,
                NULL, 'exe-whisper', 0, '', '2026-10-03T00:00:00+00:00');
            INSERT INTO profile_arguments VALUES ('prof-lang', 'server', 0, NULL, '--parallel', '1');
            INSERT INTO profile_arguments VALUES ('prof-lang', 'server', 1, NULL, '--n-gpu-layers', '60');
            INSERT INTO profile_arguments VALUES ('prof-lang', 'smoke', 0, NULL, '--single-turn', NULL);
            INSERT INTO profile_arguments VALUES ('prof-lang', 'bench', 0, NULL, '--repetitions', '5');
            INSERT INTO profile_artifacts VALUES ('prof-lang', 'model', 'acq-qwen', 'model.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-lang', 'mmproj', 'acq-qwen', 'mmproj.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-lang', 'mtp', 'acq-qwen', 'mtp.gguf');
            INSERT INTO profile_arguments VALUES ('prof-lang-default', 'server', 0, NULL,
                '--parallel', '1');
            INSERT INTO profile_artifacts VALUES ('prof-lang-default', 'model', 'acq-qwen',
                'model.gguf');
            INSERT INTO profile_arguments VALUES ('prof-synth', 'server', 0, NULL, '--threads', '4');
            INSERT INTO profile_artifacts VALUES ('prof-synth', 'weights', 'acq-speech', 'weights.bin');
            INSERT INTO profile_artifacts VALUES ('prof-recog', 'weights', 'acq-speech', 'weights.bin');
            INSERT INTO bundles VALUES ('bundle-1', 'Voice', '', 512, 'Describe the sea.',
                '2026-10-04T00:00:00+00:00');
            INSERT INTO bundle_members VALUES ('bundle-1', 'language', 'prof-lang');
            INSERT INTO bundle_members VALUES ('bundle-1', 'synthesis', 'prof-synth');
            INSERT INTO bundle_members VALUES ('bundle-1', 'recognition', 'prof-recog');
            INSERT INTO bundles VALUES ('bundle-2', 'Default voice', '', 256, 'Hello.',
                '2026-10-04T00:00:00+00:00');
            INSERT INTO bundle_members VALUES ('bundle-2', 'language', 'prof-lang-default');
            INSERT INTO bundle_members VALUES ('bundle-2', 'synthesis', 'prof-synth');
            INSERT INTO bundle_members VALUES ('bundle-2', 'recognition', 'prof-recog');
            INSERT INTO builds VALUES ('build-hip', 'ROCm', 'llama-hip', '/b/hip', 'hip', NULL, NULL,
                NULL, NULL, 'registered', '');
            INSERT INTO builds VALUES ('build-vk', 'Vulkan', 'llama-vulkan', '/b/vk', 'vulkan', NULL,
                NULL, NULL, NULL, 'registered', '');
            INSERT INTO runtime_selection VALUES (1, 'build-hip', 'card-r9', 'hip',
                '2026-10-05T00:00:00+00:00');
            """)
        return store, connection

    def accept(self, connection, qualification_id, record_kind, record_id, fingerprint,
               build_id=None):
        connection.execute(
            "INSERT INTO qualification_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (qualification_id, record_kind, record_id, record_id, fingerprint, build_id, NOW))
        connection.execute(
            "INSERT INTO acceptances VALUES (?, ?, ?, ?, ?, ?, ?)",
            (qualification_id + "-acceptance", qualification_id, "scope", "", "operator",
             NOW, None))

    def fingerprint(self, store, record_kind, record_id):
        return store_module.fingerprint(store, store_module.read_record(store, record_kind, record_id))

    def state(self, store, record_kind, record_id):
        return store_module.qualification_state(
            store, store_module.read_record(store, record_kind, record_id))

    def with_arguments(self, record, arguments):
        return dict(record, arguments=arguments)

    def without(self, record, *fields):
        return {field: value for field, value in record.items() if field not in fields}

    def test_equal_configurations_give_equal_fingerprints_in_every_process_and_reopening(self):
        store, _connection = self.serve()
        first = self.fingerprint(store, "language", "prof-lang")
        self.assertEqual(self.fingerprint(store, "language", "prof-lang"), first)
        store.close()

        reopened = self.open_store()
        self.assertEqual(self.fingerprint(reopened, "language", "prof-lang"), first)

        other_process = subprocess.run(
            [sys.executable, "-B", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from matuto import store; "
             "s = store.open_store(sys.argv[2]); "
             "print(store.fingerprint(s, store.read_record(s, 'language', 'prof-lang')))",
             str(AGENTS), str(self.config)], capture_output=True, text=True)
        self.assertEqual(other_process.returncode, 0, other_process.stderr)
        self.assertEqual(other_process.stdout.strip(), first)

    def test_row_order_does_not_change_a_fingerprint(self):
        store, connection = self.serve()
        language = self.fingerprint(store, "language", "prof-lang")
        bundle = self.fingerprint(store, "bundle", "bundle-1")
        connection.executescript("""
            INSERT INTO model_profiles VALUES ('prof-lang-b', 'language', 'Qwen 192K', 'qwen-b',
                131072, 'high', 'card-r9', NULL, 0, 'notes', '2026-10-03T00:00:00+00:00');
            INSERT INTO profile_arguments VALUES ('prof-lang-b', 'server', 1, NULL,
                '--n-gpu-layers', '60');
            INSERT INTO profile_arguments VALUES ('prof-lang-b', 'server', 0, NULL, '--parallel', '1');
            INSERT INTO profile_artifacts VALUES ('prof-lang-b', 'mtp', 'acq-qwen', 'mtp.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-lang-b', 'model', 'acq-qwen', 'model.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-lang-b', 'mmproj', 'acq-qwen', 'mmproj.gguf');
            INSERT INTO bundles VALUES ('bundle-3', 'Voice', '', 512, 'Describe the sea.',
                '2026-10-04T00:00:00+00:00');
            INSERT INTO bundle_members VALUES ('bundle-3', 'recognition', 'prof-recog');
            INSERT INTO bundle_members VALUES ('bundle-3', 'synthesis', 'prof-synth');
            INSERT INTO bundle_members VALUES ('bundle-3', 'language', 'prof-lang');
            """)

        self.assertEqual(self.fingerprint(store, "language", "prof-lang-b"), language)
        self.assertEqual(self.fingerprint(store, "bundle", "bundle-3"), bundle)

    def test_the_computation_takes_a_record_as_given(self):
        store, _connection = self.serve()
        given = {
            "kind": "language", "profile_id": "a-temporary-copy", "context_size": 131072,
            "thinking": "high", "card_id": "card-r9",
            "arguments": [{"tool": "server", "option": "--parallel", "value": "1"},
                          {"tool": "server", "option": "--n-gpu-layers", "value": "60"}],
            "model": {"acquisition_id": "acq-qwen", "file_name": "model.gguf"},
            "mmproj": {"acquisition_id": "acq-qwen", "file_name": "mmproj.gguf"},
            "mtp": {"acquisition_id": "acq-qwen", "file_name": "mtp.gguf"},
        }

        self.assertEqual(store_module.fingerprint(store, given),
                         self.fingerprint(store, "language", "prof-lang"))

    def test_a_stored_record_reads_back_in_the_form_validation_takes(self):
        store, connection = self.serve()
        before = self.fingerprint(store, "language", "prof-lang")
        connection.execute("INSERT INTO argument_catalogue"
                           " (argument_id, flag, takes_value, description, tools)"
                           " VALUES ('arg-parallel', '--parallel', 1, 'Parallel slots', 'server')")
        connection.execute("UPDATE profile_arguments SET argument_id = 'arg-parallel'"
                           " WHERE profile_id = 'prof-lang' AND flag = '--parallel'")
        connection.execute("UPDATE acquisition_records SET verified_at = ?", (NOW,))
        connection.execute("UPDATE acquisition_files SET verified_at = ?", (NOW,))

        # A catalogue row and a Custom row of one flag serve the same argument.
        self.assertEqual(self.fingerprint(store, "language", "prof-lang"), before)
        self.assertEqual(store_module.read_record(store, "language", "prof-lang")["arguments"], [
            {"tool": "bench", "option": "--repetitions", "value": "5"},
            {"tool": "server", "catalogue": "--parallel", "value": "1"},
            {"tool": "server", "option": "--n-gpu-layers", "value": "60"},
            {"tool": "smoke", "option": "--single-turn", "value": None}])
        for kind, record_id in (("language", "prof-lang"), ("synthesis", "prof-synth"),
                                ("recognition", "prof-recog"), ("bundle", "bundle-1")):
            with self.subTest(kind=kind):
                record = store_module.read_record(store, kind, record_id)
                store_module.validate_record(record, connection)
                self.assertTrue(store_module.fingerprint(store, record).startswith("sha256:"))

    def test_a_record_at_an_accepted_fingerprint_reads_qualified_and_an_edit_away_and_back(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted)

        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "qualified")

        record = store_module.read_record(store, "language", "prof-lang")
        away = dict(record, context_size=98304)
        edited = store_module.qualification_state(store, away)
        self.assertEqual(edited["state"], "candidate")
        self.assertTrue(edited["changed_since_qualification"])
        self.assertEqual(store_module.qualification_state(store, dict(away, context_size=131072))["state"],
                         "qualified")

    def test_a_record_with_no_acceptance_is_a_plain_candidate(self):
        store, _connection = self.serve()

        state = self.state(store, "language", "prof-lang")

        self.assertEqual(state["state"], "candidate")
        self.assertFalse(state["changed_since_qualification"])

    def test_a_withdrawn_acceptance_never_qualifies_again(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted)
        connection.execute("UPDATE acceptances SET withdrawn_at = ? WHERE acceptance_id = ?",
                           (NOW, "q-lang-acceptance"))

        state = self.state(store, "language", "prof-lang")

        self.assertEqual(state["state"], "candidate")
        self.assertFalse(state["changed_since_qualification"])

    def test_a_qualified_record_on_another_active_build_keeps_its_state_and_warns(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted, build_id="build-hip")

        self.assertNotIn("warning", self.state(store, "language", "prof-lang"))
        connection.execute("UPDATE runtime_selection SET build_id = 'build-vk' WHERE selection_id = 1")

        state = self.state(store, "language", "prof-lang")
        self.assertEqual(state["state"], "qualified")
        self.assertIn("ROCm", state["warning"])
        self.assertEqual(self.fingerprint(store, "language", "prof-lang"), accepted)

    def test_speech_profiles_have_fingerprints_but_no_qualification_state(self):
        store, _connection = self.serve()

        for kind, record_id in (("synthesis", "prof-synth"), ("recognition", "prof-recog")):
            with self.subTest(kind=kind):
                self.assertTrue(self.fingerprint(store, kind, record_id).startswith("sha256:"))
                with self.assertRaises(store_module.StoreError):
                    self.state(store, kind, record_id)
        # The speech fingerprints are what a bundle's own fingerprint is built from.
        self.assertTrue(self.fingerprint(store, "bundle", "bundle-1").startswith("sha256:"))

    def test_the_module_offers_no_way_to_set_or_store_qualification(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted)

        for table in ("model_profiles", "bundles"):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            self.assertFalse([name for name in columns if "qualif" in name])
        before = connection.execute("SELECT count(*) FROM qualification_records").fetchone()[0]
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "qualified")
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "qualified")
        self.assertEqual(
            connection.execute("SELECT count(*) FROM qualification_records").fetchone()[0], before)

    def test_identical_bytes_at_another_location_keep_the_fingerprint_and_state(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted)
        record = store_module.read_record(store, "language", "prof-lang")
        moved = dict(record,
                     model={"acquisition_id": "acq-qwen-moved", "file_name": "model.gguf"},
                     mmproj={"acquisition_id": "acq-qwen-moved", "file_name": "mmproj.gguf"},
                     mtp={"acquisition_id": "acq-qwen-moved", "file_name": "mtp.gguf"})

        self.assertEqual(store_module.fingerprint(store, moved), accepted)
        self.assertEqual(store_module.qualification_state(store, moved)["state"], "qualified")

    def test_replacement_bytes_change_the_fingerprint(self):
        store, connection = self.serve()
        before = self.fingerprint(store, "language", "prof-lang")

        connection.execute("UPDATE acquisition_files SET computed_sha256 = ?"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'",
                           ("9" * 64,))

        self.assertNotEqual(self.fingerprint(store, "language", "prof-lang"), before)

    def test_every_required_shard_contributes_to_the_model_digest(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files"
                           " SET file_name = 'model-00001-of-00002.gguf'"
                           " WHERE acquisition_id = 'acq-qwen-copy'")
        connection.execute("INSERT INTO acquisition_files VALUES ('acq-qwen-copy',"
                           " 'model-00002-of-00002.gguf', 100, NULL, ?, NULL)", ("1" * 64,))
        record = store_module.read_record(store, "language", "prof-lang")
        record = dict(record, model={"acquisition_id": "acq-qwen-copy",
                                     "file_name": "model-00001-of-00002.gguf"})
        accepted = store_module.fingerprint(store, record)
        self.accept(connection, "q-shards", "language", "prof-lang", accepted)

        connection.execute("UPDATE acquisition_files SET computed_sha256 = ?"
                           " WHERE acquisition_id = 'acq-qwen-copy'"
                           " AND file_name = 'model-00002-of-00002.gguf'", ("2" * 64,))
        self.assertNotEqual(store_module.fingerprint(store, record), accepted)
        self.assertEqual(store_module.qualification_state(store, record)["state"], "candidate")

        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen-copy'"
                           " AND file_name = 'model-00002-of-00002.gguf'")
        self.assertIsNone(store_module.fingerprint(store, record))
        self.assertEqual(store_module.qualification_state(store, record)["state"], "unavailable")

    def test_swapping_shard_digests_changes_the_fingerprint(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files"
                           " SET file_name = 'model-00001-of-00002.gguf'"
                           " WHERE acquisition_id = 'acq-qwen-copy'")
        connection.execute("INSERT INTO acquisition_files VALUES ('acq-qwen-copy',"
                           " 'model-00002-of-00002.gguf', 100, NULL, ?, NULL)", ("1" * 64,))
        record = store_module.read_record(store, "language", "prof-lang")
        record = dict(record, model={"acquisition_id": "acq-qwen-copy",
                                     "file_name": "model-00001-of-00002.gguf"})
        accepted = store_module.fingerprint(store, record)
        self.accept(connection, "q-swapped", "language", "prof-lang", accepted)

        connection.execute("UPDATE acquisition_files SET computed_sha256 = CASE file_name"
                           " WHEN 'model-00001-of-00002.gguf' THEN ? ELSE ? END"
                           " WHERE acquisition_id = 'acq-qwen-copy'", ("1" * 64, "a" * 64))

        self.assertNotEqual(store_module.fingerprint(store, record), accepted)
        self.assertEqual(store_module.qualification_state(store, record)["state"], "candidate")

    def test_an_exaggerated_shard_count_fails_closed_without_building_it(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files"
                           " SET file_name = 'model-00001-of-999999999999.gguf'"
                           " WHERE acquisition_id = 'acq-qwen-copy'")
        record = store_module.read_record(store, "language", "prof-lang")
        record = dict(record, model={"acquisition_id": "acq-qwen-copy",
                                     "file_name": "model-00001-of-999999999999.gguf"})

        self.assertIsNone(store_module.fingerprint(store, record))
        self.assertEqual(store_module.qualification_state(store, record)["state"], "unavailable")

    def test_two_files_at_one_shard_position_fail_closed(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files"
                           " SET file_name = 'model-00001-of-00002.gguf'"
                           " WHERE acquisition_id = 'acq-qwen-copy'")
        for name, digest in (("model-00002-of-00002.gguf", "1" * 64),
                             ("model-1-of-00002.gguf", "2" * 64)):
            connection.execute("INSERT INTO acquisition_files VALUES ('acq-qwen-copy',"
                               " ?, 100, NULL, ?, NULL)", (name, digest))
        record = store_module.read_record(store, "language", "prof-lang")
        record = dict(record, model={"acquisition_id": "acq-qwen-copy",
                                     "file_name": "model-00001-of-00002.gguf"})

        self.assertIsNone(store_module.fingerprint(store, record))
        self.assertEqual(store_module.qualification_state(store, record)["state"], "unavailable")

    def test_an_unselected_sidecar_in_the_same_acquisition_is_not_served(self):
        store, connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang")
        only_model = self.without(record, "mmproj", "mtp")
        accepted = store_module.fingerprint(store, only_model)
        self.accept(connection, "q-model-only", "language", "prof-lang", accepted)

        connection.execute("UPDATE acquisition_files SET computed_sha256 = ?"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'mmproj.gguf'",
                           ("9" * 64,))

        self.assertEqual(store_module.fingerprint(store, only_model), accepted)
        self.assertEqual(store_module.qualification_state(store, only_model)["state"], "qualified")

    def test_a_language_profiles_effective_gpu_layers_come_from_its_override_or_the_card(self):
        store, connection = self.serve()
        override = store_module.served_configuration(
            store, store_module.read_record(store, "language", "prof-lang"))
        default = store_module.served_configuration(
            store, store_module.read_record(store, "language", "prof-lang-default"))
        self.assertEqual((override["gpu_layers"], default["gpu_layers"]), (60, 99))
        inline = dict(store_module.read_record(store, "language", "prof-lang"), arguments=[
            {"tool": "server", "option": "--parallel", "value": "1"},
            {"tool": "server", "option": "--n-gpu-layers=60", "value": None}])
        self.assertEqual(store_module.served_configuration(store, inline)["gpu_layers"], 60)
        pinned_inline = store_module.fingerprint(store, inline)
        pinned = self.fingerprint(store, "language", "prof-lang")
        self.assertEqual(pinned_inline, pinned)
        taking_default = self.fingerprint(store, "language", "prof-lang-default")
        pinned_bundle = self.fingerprint(store, "bundle", "bundle-1")
        default_bundle = self.fingerprint(store, "bundle", "bundle-2")

        connection.execute("UPDATE cards SET default_gpu_layers = 42 WHERE card_id = 'card-r9'")

        self.assertEqual(self.fingerprint(store, "language", "prof-lang"), pinned)
        self.assertEqual(store_module.fingerprint(store, inline), pinned_inline)
        self.assertEqual(self.fingerprint(store, "bundle", "bundle-1"), pinned_bundle)
        self.assertNotEqual(self.fingerprint(store, "language", "prof-lang-default"), taking_default)
        self.assertNotEqual(self.fingerprint(store, "bundle", "bundle-2"), default_bundle)

    def test_a_profile_change_reaches_every_bundle_using_it(self):
        store, connection = self.serve()
        before = self.fingerprint(store, "bundle", "bundle-1")

        connection.execute("UPDATE model_profiles SET context_size = 98304 WHERE profile_id = 'prof-lang'")

        self.assertNotEqual(self.fingerprint(store, "bundle", "bundle-1"), before)

    def test_a_card_gpu_change_reaches_its_profiles_and_their_bundles(self):
        store, connection = self.serve()
        before = {kind: self.fingerprint(store, kind, record_id) for kind, record_id in (
            ("language", "prof-lang"), ("synthesis", "prof-synth"), ("bundle", "bundle-1"))}
        recognition = self.fingerprint(store, "recognition", "prof-recog")

        connection.execute("UPDATE cards SET gpu_identity = 'gfx1201-new' WHERE card_id = 'card-r9'")

        for kind, record_id in (("language", "prof-lang"), ("synthesis", "prof-synth"),
                                ("bundle", "bundle-1")):
            with self.subTest(kind=kind):
                self.assertNotEqual(self.fingerprint(store, kind, record_id), before[kind])
        self.assertEqual(self.fingerprint(store, "recognition", "prof-recog"), recognition)

    def test_pointing_a_profile_at_another_card_changes_the_fingerprint(self):
        store, _connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang")

        # The twin card holds identical values; the physical GPU is what differs,
        # and an edited pointer is resolved unless explicit card values override it.
        moved = dict(record, card_id="card-twin")

        self.assertNotEqual(store_module.fingerprint(store, moved),
                            store_module.fingerprint(store, record))
        self.assertIsNone(store_module.fingerprint(store, dict(record, card_id="card-gone")))

    def test_a_card_edit_can_be_previewed_as_given(self):
        store, _connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang-default")

        with_twins_values = dict(record, card={"gpu_identity": "gfx1201", "default_gpu_layers": 42})

        self.assertEqual(store_module.served_configuration(store, with_twins_values)["gpu_layers"], 42)
        self.assertNotEqual(store_module.fingerprint(store, with_twins_values),
                            store_module.fingerprint(store, record))

    def test_an_all_layers_card_preview_preserves_qualification(self):
        store, connection = self.serve()
        connection.execute("UPDATE cards SET default_gpu_layers = -1 WHERE card_id = 'card-r9'")
        record = store_module.read_record(store, "language", "prof-lang-default")
        before = store_module.fingerprint(store, record)
        self.accept(connection, "all-layers", "language", "prof-lang-default", before)
        self.assertEqual(self.state(store, "language", "prof-lang-default")["state"], "qualified")

        card = store_module.read_card(store, "card-r9")
        unchanged = dict(record, card=card)
        measurement_only = dict(record, card=dict(card, name="New label", vram_ceiling_bytes=1))

        self.assertEqual(card["default_gpu_layers"], "all")
        self.assertEqual(store_module.served_configuration(store, unchanged)["gpu_layers"], -1)
        self.assertEqual(store_module.fingerprint(store, unchanged), before)
        self.assertEqual(store_module.fingerprint(store, measurement_only), before)
        self.assertEqual(self.state(store, "language", "prof-lang-default")["state"], "qualified")

    def test_each_served_value_changes_the_fingerprint(self):
        store, connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang")
        before = store_module.fingerprint(store, record)

        def arguments(*pairs):
            return [{"tool": "server", "option": flag, "value": value} for flag, value in pairs]

        for label, changed in (
                ("context", dict(record, context_size=98304)),
                ("thinking", dict(record, thinking="off")),
                ("server arguments", self.with_arguments(record, arguments(("--parallel", "8"),
                                                                            ("--n-gpu-layers", "60")))),
                ("gpu layers", self.with_arguments(record, arguments(("--parallel", "1"),
                                                                     ("--n-gpu-layers", "20")))),
                ("mmproj", self.without(record, "mmproj")),
                ("mtp", self.without(record, "mtp"))):
            with self.subTest(value=label):
                self.assertNotEqual(store_module.fingerprint(store, changed), before)

        for label, statement in (
                ("model digest", "UPDATE acquisition_files SET computed_sha256 = '9' || substr(computed_sha256, 2)"
                 " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'"),):
            with self.subTest(value=label):
                connection.execute(statement)
                self.assertNotEqual(store_module.fingerprint(store, record), before)

    def test_only_the_matching_acceptances_build_drives_the_warning(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-old", "language", "prof-lang", "sha256:" + "0" * 64,
                    build_id="build-vk")
        self.accept(connection, "q-current", "language", "prof-lang", current, build_id="build-hip")

        state = self.state(store, "language", "prof-lang")

        self.assertEqual(state["state"], "qualified")
        self.assertNotIn("warning", state)

    def test_a_speech_profiles_served_values_change_its_fingerprint(self):
        store, connection = self.serve()
        before = self.fingerprint(store, "synthesis", "prof-synth")

        connection.execute("UPDATE speech_executables SET sha256 = '9' || substr(sha256, 2)"
                           " WHERE executable_id = 'exe-breeze'")
        after_executable = self.fingerprint(store, "synthesis", "prof-synth")
        self.assertNotEqual(after_executable, before)

        connection.execute("UPDATE acquisition_files SET computed_sha256 = '8' || substr(computed_sha256, 2)"
                           " WHERE acquisition_id = 'acq-speech' AND file_name = 'weights.bin'")
        self.assertNotEqual(self.fingerprint(store, "synthesis", "prof-synth"), after_executable)

    def test_a_bundle_is_qualified_only_by_its_own_acceptance(self):
        store, connection = self.serve()
        language = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", language)

        state = self.state(store, "bundle", "bundle-1")

        self.assertEqual(state["state"], "candidate")
        self.assertFalse(state["changed_since_qualification"])
        self.accept(connection, "q-bundle", "bundle", "bundle-1", state["fingerprint"])
        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "qualified")

    def test_a_bundle_member_edit_marks_the_bundle_changed_since_qualification(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "bundle", "bundle-1")
        self.accept(connection, "q-bundle", "bundle", "bundle-1", accepted)

        connection.execute("UPDATE model_profiles SET context_size = 98304 WHERE profile_id = 'prof-lang'")
        state = self.state(store, "bundle", "bundle-1")
        self.assertEqual(state["state"], "candidate")
        self.assertTrue(state["changed_since_qualification"])
        connection.execute("UPDATE model_profiles SET context_size = 131072 WHERE profile_id = 'prof-lang'")
        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "qualified")

    def test_a_bundles_member_and_stated_load_change_its_fingerprint(self):
        store, connection = self.serve()
        before = self.fingerprint(store, "bundle", "bundle-1")

        connection.execute("UPDATE bundles SET load_tokens = 1024 WHERE bundle_id = 'bundle-1'")
        self.assertNotEqual(self.fingerprint(store, "bundle", "bundle-1"), before)

        connection.execute("UPDATE bundles SET load_tokens = 512, load_prompt = 'Another.'"
                           " WHERE bundle_id = 'bundle-1'")
        self.assertNotEqual(self.fingerprint(store, "bundle", "bundle-1"), before)

        connection.execute("UPDATE model_profiles SET context_size = 98304 WHERE profile_id = 'prof-lang'")
        self.assertNotEqual(self.fingerprint(store, "bundle", "bundle-1"), before)

    def test_values_outside_the_served_configuration_leave_the_fingerprint_unchanged(self):
        store, connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang")
        before = store_module.fingerprint(store, record)

        for label, changed in (
                ("name", dict(record, name="Renamed")),
                ("notes", dict(record, notes="different")),
                ("alias", dict(record, alias="renamed")),
                ("smoke and bench arguments", self.with_arguments(record, [
                    {"tool": "server", "option": "--parallel", "value": "1"},
                    {"tool": "server", "option": "--n-gpu-layers", "value": "60"},
                    {"tool": "smoke", "option": "--different", "value": "1"},
                    {"tool": "bench", "option": "--different", "value": "2"}]))):
            with self.subTest(value=label):
                self.assertEqual(store_module.fingerprint(store, changed), before)

        connection.execute("UPDATE cards SET name = 'Renamed', vram_ceiling_bytes = 1,"
                           " smoke_output_tokens = 2, bench_prompt_tokens = 3,"
                           " bench_generation_tokens = 4, bench_repetitions = 5"
                           " WHERE card_id = 'card-r9'")
        connection.execute("UPDATE builds SET label = 'Other build' WHERE build_id = 'build-hip'")
        connection.execute("UPDATE runtime_selection SET build_id = 'build-vk' WHERE selection_id = 1")
        self.assertEqual(self.fingerprint(store, "language", "prof-lang"), before)

    def test_a_required_file_without_a_computed_sha256_gives_no_fingerprint_and_never_qualifies(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted)
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")

        self.assertIsNone(store_module.fingerprint(
            store, store_module.read_record(store, "language", "prof-lang")))
        with self.assertRaises(store_module.StoreError) as refusal:
            store_module.served_configuration(
                store, store_module.read_record(store, "language", "prof-lang"))
        self.assertIn("computed SHA-256", str(refusal.exception))

        state = self.state(store, "language", "prof-lang")
        self.assertEqual(state["state"], "unavailable")
        self.assertIn("computed SHA-256", state["reason"])

    def test_a_configuration_that_cannot_be_computed_in_full_fails_closed(self):
        store, connection = self.serve()
        record = store_module.read_record(store, "language", "prof-lang")

        for label, changed, expected in (
                ("no card", dict(record, card_id=None), "without its card"),
                ("an unrecorded card", dict(record, card_id="card-gone"), "not recorded"),
                ("an incomplete card", dict(record, card={"gpu_identity": "gfx1201"}),
                 "is incomplete"),
                ("no context", dict(record, context_size=None), "context size"),
                ("no model file", self.without(record, "model"), "no model file"),
                ("a negative GPU-layer count", self.with_arguments(record, [
                    {"tool": "server", "option": "--gpu-layers", "value": "0"}]),
                 "positive GPU-layer count"),
                ("a GPU-layer count int() refuses", self.with_arguments(record, [
                    {"tool": "server", "option": "--gpu-layers=\u00b2", "value": None}]),
                 "positive GPU-layer count"),
                ("more GPU-layer digits than int() converts", self.with_arguments(record, [
                    {"tool": "server", "option": "--gpu-layers", "value": "1" * 5000}]),
                 "positive GPU-layer count")):
            with self.subTest(case=label):
                self.assertIsNone(store_module.fingerprint(store, changed))
                with self.assertRaises(store_module.StoreError) as refusal:
                    store_module.served_configuration(store, changed)
                self.assertIn(expected, str(refusal.exception))
                state = store_module.qualification_state(store, changed)
                self.assertEqual(state["state"], "unavailable")

        connection.execute("DELETE FROM bundle_members"
                           " WHERE bundle_id = 'bundle-1' AND role = 'synthesis'")
        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "unavailable")
        connection.execute("UPDATE bundles SET load_tokens = NULL WHERE bundle_id = 'bundle-2'")
        self.assertEqual(self.state(store, "bundle", "bundle-2")["state"], "unavailable")
        connection.execute("UPDATE speech_executables SET sha256 = '' WHERE executable_id = 'exe-whisper'")
        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "unavailable")

    def test_a_bundle_can_be_computed_with_an_edited_member_as_given(self):
        store, _connection = self.serve()
        adapter = store_module.read_record(store, "language", "prof-lang")
        bundle = store_module.read_record(store, "bundle", "bundle-1")
        stored = store_module.fingerprint(store, bundle)

        preview = dict(bundle, members=dict(bundle["members"],
                                            language=dict(adapter, context_size=98304)))

        self.assertTrue(store_module.fingerprint(store, preview).startswith("sha256:"))
        self.assertNotEqual(store_module.fingerprint(store, preview), stored)
        # The stored member is untouched by the temporary copy.
        self.assertEqual(store_module.fingerprint(store, bundle), stored)

    def insert_run(self, connection, run_id, kind, record_id, fingerprint, procedure,
                   checks, *, outcome="completed", dirty=0, build_id="build-hip"):
        connection.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, kind, record_id, record_id, fingerprint, build_id, procedure,
             "revision", None, outcome, dirty, "", "", 0, NOW, NOW))
        connection.executemany(
            "INSERT INTO checks (run_id, sequence, name, status, detail) VALUES (?, ?, ?, ?, '')",
            ((run_id, index, name, status) for index, (name, status) in enumerate(checks)))

    def test_language_acceptance_validates_and_records_citations_atomically(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.insert_run(connection, "smoke", "language", "prof-lang", current,
                        "smoke", [("exact reply", "PASS")])
        self.insert_run(connection, "bench", "language", "prof-lang", current,
                        "bench", [("measurement", "FAIL")], build_id="build-vk")

        accepted = store_module.accept(store, "language", "prof-lang", ["smoke", "bench"],
                                        "single-consumer lookup", {"measurement": "thermal margin"},
                                        "middle-context retrieval remains imperfect", "operator", now=NOW)

        self.assertEqual(accepted["record_id"], "prof-lang")
        self.assertEqual(accepted["fingerprint"], current)
        self.assertEqual(accepted["accepted_by"], "operator")
        self.assertEqual(accepted["accepted_at"], NOW)
        self.assertEqual(accepted["run_ids"], ["smoke", "bench"])
        self.assertEqual(accepted["exceptions"], {"measurement": "thermal margin"})
        self.assertIn("Vulkan", accepted["warning"])
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "qualified")
        self.assertIn("Vulkan", self.state(store, "language", "prof-lang")["warning"])
        run_builds = {row["run_id"]: row["build_id"] for row in connection.execute(
            "SELECT run_id, build_id FROM acceptance_runs WHERE acceptance_id = ?",
            (accepted["acceptance_id"],))}
        self.assertEqual(run_builds, {"smoke": "build-hip", "bench": "build-vk"})
        self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM checks WHERE status = 'FAIL'").fetchone()[0], 1)

    def test_bundle_acceptance_needs_its_own_pass_and_not_qualified_members(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "bundle", "bundle-1")
        self.insert_run(connection, "bundle-pass", "bundle", "bundle-1", current,
                        "bundle", [("fit", "PASS"), ("real-time", "PASS"),
                                   ("recognition", "PASS")])
        self.insert_run(connection, "bundle-fail", "bundle", "bundle-1", current,
                        "bundle", [("fit", "PASS"), ("real-time", "PASS"),
                                   ("recognition", "FAIL")])
        with self.assertRaisesRegex(store_module.StoreError, "required bundle checks"):
            store_module.accept(store, "bundle", "bundle-1", ["bundle-pass", "bundle-fail"],
                                "voice bundle", {"recognition": "not enough"}, "", "operator")

        store_module.accept(store, "bundle", "bundle-1", ["bundle-pass"],
                            "voice bundle", {}, "", "operator", now=NOW)

        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "qualified")
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "candidate")

    def test_acceptance_refuses_bad_outcomes_dirty_or_uncitable_runs_and_rolls_back(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.insert_run(connection, "smoke", "language", "prof-lang", current,
                        "smoke", [("exact reply", "PASS")])
        self.insert_run(connection, "bench", "language", "prof-lang", current,
                        "bench", [("measurement", "PASS")])
        cases = (
            ("legacy", None, "smoke", "completed", 0, "fingerprint"),
            ("stale", "sha256:" + "0" * 64, "smoke", "completed", 0, "fingerprint"),
            ("wrong-record", current, "smoke", "completed", 0, "another record"),
            ("errored", current, "smoke", "errored", 0, "outcome"),
            ("cancelled", current, "smoke", "cancelled", 0, "outcome"),
            ("interrupted", current, "smoke", "interrupted", 0, "outcome"),
            ("dirty", current, "smoke", "completed", 1, "dirty checkout"),
        )
        for run_id, fingerprint, procedure, outcome, dirty, reason in cases:
            record_id = "prof-whisper" if run_id == "wrong-record" else "prof-lang"
            self.insert_run(connection, run_id, "language", record_id, fingerprint, procedure,
                            [("exact reply", "PASS")], outcome=outcome, dirty=dirty)
            with self.subTest(run=run_id):
                with self.assertRaises(store_module.StoreError) as refusal:
                    store_module.accept(store, "language", "prof-lang", ["smoke", "bench", run_id],
                                        "scope", {}, "", "operator", now=NOW)
                self.assertIn(reason, str(refusal.exception))
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM qualification_records").fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM acceptances").fetchone()[0], 0)

        with self.assertRaises(store_module.StoreError):
            store_module.accept(store, "synthesis", "prof-synth", ["smoke", "bench"],
                                "scope", {}, "", "operator")
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        with self.assertRaisesRegex(store_module.StoreError, "computed SHA-256"):
            store_module.accept(store, "language", "prof-lang", ["smoke", "bench"],
                                "scope", {}, "", "operator")
        self.assertEqual(connection.execute(
            "SELECT count(*) FROM qualification_records").fetchone()[0], 0)

    def test_required_checks_cannot_be_overridden_and_failed_optional_checks_need_reasons(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.insert_run(connection, "smoke-fail", "language", "prof-lang", current,
                        "smoke", [("exact reply", "FAIL")])
        self.insert_run(connection, "smoke-pass", "language", "prof-lang", current,
                        "smoke", [("exact reply", "PASS")])
        self.insert_run(connection, "bench", "language", "prof-lang", current,
                        "bench", [("measurement", "FAIL")])
        with self.assertRaisesRegex(store_module.StoreError, "required smoke checks"):
            store_module.accept(store, "language", "prof-lang",
                                ["smoke-pass", "smoke-fail", "bench"],
                                "scope", {"exact reply": "not enough"}, "", "operator")
        with self.assertRaisesRegex(store_module.StoreError, "required smoke checks"):
            store_module.accept(store, "language", "prof-lang", ["smoke-fail", "bench"],
                                "scope", {"exact reply": "not enough"}, "", "operator")
        with self.assertRaisesRegex(store_module.StoreError, "failed checks need named exceptions"):
            store_module.accept(store, "language", "prof-lang",
                                ["smoke-pass", "bench"], "scope", {}, "", "operator")
        with self.assertRaisesRegex(store_module.StoreError, "completed bench run"):
            store_module.accept(store, "language", "prof-lang", ["smoke-pass"],
                                "scope", {}, "", "operator")

    def test_acceptance_serializes_against_concurrent_record_edits(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.insert_run(connection, "smoke", "language", "prof-lang", current,
                        "smoke", [("exact reply", "PASS")])
        self.insert_run(connection, "bench", "language", "prof-lang", current,
                        "bench", [("measurement", "PASS")])
        competitor = sqlite3.connect(str(store.path), isolation_level=None, timeout=0)
        self.addCleanup(competitor.close)
        compute, blocked = store_module.fingerprint, []

        def try_edit(opened, record):
            try:
                competitor.execute("UPDATE model_profiles SET context_size = 98304"
                                   " WHERE profile_id = 'prof-lang'")
            except sqlite3.OperationalError as error:
                blocked.append(str(error))
            return compute(opened, record)

        with mock.patch.object(store_module, "fingerprint", side_effect=try_edit):
            store_module.accept(store, "language", "prof-lang", ["smoke", "bench"],
                                "scope", {}, "", "operator", now=NOW)

        self.assertTrue(blocked and "locked" in blocked[0])
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "qualified")

    def test_withdrawal_preserves_history_and_prevents_run_deletion(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "language", "prof-lang")
        self.insert_run(connection, "smoke", "language", "prof-lang", current,
                        "smoke", [("exact reply", "PASS")])
        self.insert_run(connection, "bench", "language", "prof-lang", current,
                        "bench", [("measurement", "PASS")])
        accepted = store_module.accept(store, "language", "prof-lang", ["smoke", "bench"],
                                       "scope", {}, "risk", "operator", now=NOW)
        retried = store_module.accept(store, "language", "prof-lang", ["bench", "smoke"],
                                      "scope", {}, "risk", "operator", now="retry-time")
        self.assertEqual(retried["acceptance_id"], accepted["acceptance_id"])
        self.assertEqual(retried["accepted_at"], NOW)
        with self.assertRaisesRegex(store_module.StoreError, "already has active acceptance"):
            store_module.accept(store, "language", "prof-lang", ["smoke", "bench"],
                                "different scope", {}, "risk", "operator", now="later")
        self.assertEqual(connection.execute("SELECT count(*) FROM acceptances").fetchone()[0], 1)

        withdrawn = store_module.withdraw(store, retried["acceptance_id"], now="withdrawn")

        self.assertEqual(withdrawn["withdrawn_at"], "withdrawn")
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "candidate")
        connection.execute("UPDATE model_profiles SET context_size = 98304 WHERE profile_id = 'prof-lang'")
        connection.execute("UPDATE model_profiles SET context_size = 131072 WHERE profile_id = 'prof-lang'")
        self.assertEqual(self.state(store, "language", "prof-lang")["state"], "candidate")
        self.assertEqual(connection.execute("SELECT count(*) FROM acceptances").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT count(*) FROM acceptance_runs").fetchone()[0], 2)
        with self.assertRaisesRegex(store_module.StoreError, accepted["acceptance_id"]):
            store_module.refuse_accepted_run_deletion(store, "smoke")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "cited by acceptance"):
            connection.execute("DELETE FROM runs WHERE run_id = 'smoke'")
        self.assertEqual(store_module.withdraw(store, accepted["acceptance_id"], now="again")
                         ["withdrawn_at"], "withdrawn")

    def test_withdrawing_a_bundle_acceptance_does_not_stop_voice_serving(self):
        store, connection = self.serve()
        current = self.fingerprint(store, "bundle", "bundle-1")
        self.insert_run(connection, "bundle-pass", "bundle", "bundle-1", current,
                        "bundle", [("fit", "PASS"), ("real-time", "PASS"),
                                   ("recognition", "PASS")])
        connection.execute("INSERT INTO voice_serving VALUES (1, 'bundle-1')")
        accepted = store_module.accept(store, "bundle", "bundle-1", ["bundle-pass"],
                                       "voice bundle", {}, "", "operator", now=NOW)

        store_module.withdraw(store, accepted["acceptance_id"], now="withdrawn")

        self.assertEqual(connection.execute(
            "SELECT bundle_id FROM voice_serving WHERE voice_serving_id = 1").fetchone()[0],
            "bundle-1")
        self.assertEqual(self.state(store, "bundle", "bundle-1")["state"], "candidate")

    def test_withdrawal_refuses_unknown_acceptance(self):
        store, connection = self.serve()
        with self.assertRaisesRegex(store_module.StoreError, "is not recorded"):
            store_module.withdraw(store, "missing")
        self.assertEqual(connection.execute("SELECT count(*) FROM acceptances").fetchone()[0], 0)

    def test_preview_lists_served_values_state_affected_bundles_and_restoration(self):
        store, connection = self.serve()
        before = store_module.read_record(store, "language", "prof-lang")
        accepted = store_module.fingerprint(store, before)
        self.accept(connection, "q-preview", "language", "prof-lang", accepted)
        edited = dict(before, context_size=98304)
        writes = connection.total_changes

        preview = store_module.preview_edit(store, before, edited)

        self.assertEqual(connection.total_changes, writes)
        self.assertEqual(preview["kind"], "served-change")
        self.assertEqual(preview["changed_values"], [{
            "field": "context", "label": "Context size", "old": 131072, "new": 98304}])
        self.assertEqual(preview["state"]["state"], "candidate")
        self.assertTrue(preview["state"]["changed_since_qualification"])
        self.assertEqual([(item["kind"], item["id"], item["state"]["state"])
                          for item in preview["affected"]],
                         [("bundle", "bundle-1", "candidate")])
        restored = store_module.preview_edit(store, before, dict(edited, context_size=131072))
        self.assertEqual(restored["state"]["state"], "qualified")

    def test_bundle_previews_name_changed_member_selections(self):
        store, _connection = self.serve()
        before = store_module.read_record(store, "bundle", "bundle-1")
        members = dict(before["members"], language="prof-lang-default")

        preview = store_module.preview_edit(store, before, dict(before, members=members))

        self.assertEqual(preview["changed_values"], [{
            "field": "members.language", "label": "Language profile",
            "old": "prof-lang", "new": "prof-lang-default"}])

    def test_bundle_preview_shows_changed_values_in_an_as_given_member(self):
        store, connection = self.serve()
        before = store_module.read_record(store, "bundle", "bundle-1")
        self.accept(connection, "q-bundle-preview", "bundle", "bundle-1",
                    store_module.fingerprint(store, before))
        language = store_module.read_record(store, "language", "prof-lang")
        edited_language = dict(language, context_size=98304)
        after = dict(before, members=dict(before["members"], language=edited_language))

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual(preview["kind"], "served-change")
        self.assertEqual(preview["changed_values"], [{
            "field": "members.language.context", "label": "Language profile: Context size",
            "old": 131072, "new": 98304}])
        self.assertEqual(preview["state"]["state"], "candidate")
        self.assertTrue(preview["state"]["changed_since_qualification"])

    def test_bundle_impacts_compare_the_supplied_before_and_after_drafts(self):
        store, _connection = self.serve()
        stored = store_module.read_record(store, "language", "prof-lang")
        draft = dict(stored, context_size=98304)

        no_op = store_module.preview_edit(store, draft, dict(draft))
        cosmetic = store_module.preview_edit(store, draft, dict(draft, name="Draft name"))
        changed = store_module.preview_edit(store, draft, dict(draft, context_size=65536))

        self.assertEqual(no_op["affected"], [])
        self.assertEqual(cosmetic["affected"], [])
        self.assertEqual([(item["kind"], item["id"]) for item in changed["affected"]],
                         [("bundle", "bundle-1")])

    def test_arguments_are_compared_by_flag_when_a_row_is_removed(self):
        store, _connection = self.serve()
        before = store_module.read_record(store, "language", "prof-lang")
        before = dict(before, arguments=[
            {"tool": "server", "option": "--parallel", "value": "1"},
            {"tool": "server", "option": "--threads", "value": "4"}])
        after = dict(before, arguments=before["arguments"][1:])

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual(preview["changed_values"], [{
            "field": "server_arguments.--parallel", "label": "--parallel",
            "old": ["--parallel", "1"], "new": None}])

    def test_cosmetic_and_unchanged_edits_have_no_affected_records(self):
        store, _connection = self.serve()
        before = store_module.read_record(store, "language", "prof-lang")

        cosmetic = store_module.preview_edit(store, before, dict(before, name="Renamed"))
        unchanged = store_module.preview_edit(store, before, dict(before))

        self.assertEqual(cosmetic["kind"], "cosmetic-only")
        self.assertEqual(cosmetic["changed_values"], [])
        self.assertEqual(cosmetic["affected"], [])
        self.assertEqual(unchanged["kind"], "unchanged")
        self.assertEqual(unchanged["affected"], [])

    def test_unavailable_preview_retains_server_argument_order_and_affected_bundles(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        before = store_module.read_record(store, "language", "prof-lang")
        server = [{"tool": "server", "option": "--parallel", "value": "1"},
                  {"tool": "server", "option": "--threads", "value": "4"}]
        other = [row for row in before["arguments"] if row["tool"] != "server"]
        before = dict(before, arguments=server + other)
        after = dict(before, arguments=list(reversed(server)) + other)

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual(preview["kind"], "unavailable")
        self.assertEqual(preview["changed_values"], [{
            "field": "server_argument_order", "label": "Argument order",
            "old": ["--parallel", "--threads"],
            "new": ["--threads", "--parallel"]}])
        self.assertEqual([(item["kind"], item["id"], item["state"]["state"])
                          for item in preview["affected"]],
                         [("bundle", "bundle-1", "unavailable")])
        self.assertIn("computed SHA-256", preview["affected"][0]["state"]["reason"])

    def test_unavailable_preview_ignores_resolved_gpu_directive_order(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        before = store_module.read_record(store, "language", "prof-lang")
        server = [row for row in before["arguments"] if row["tool"] == "server"]
        before = dict(before, arguments=server)
        after = dict(before, arguments=list(reversed(server)))

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual(preview["kind"], "unavailable")
        self.assertIn("computed SHA-256", preview["reason"])
        self.assertEqual(preview["changed_values"], [])
        self.assertEqual(preview["affected"], [])

    def test_card_preview_reaches_all_profiles_and_bundles(self):
        store, _connection = self.serve()
        before = store_module.read_card(store, "card-r9")
        after = dict(before, gpu_identity="gfx1201-new")

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual({(item["kind"], item["id"]) for item in preview["affected"]}, {
            ("language", "prof-lang"), ("language", "prof-lang-default"),
            ("synthesis", "prof-synth"), ("bundle", "bundle-1"),
            ("bundle", "bundle-2")})
        self.assertEqual(preview["changed_values"][0]["label"], "Physical GPU")

    def test_card_default_preview_compares_supplied_snapshots_and_excludes_overrides(self):
        store, _connection = self.serve()
        before = dict(store_module.read_card(store, "card-r9"), default_gpu_layers=42)
        after = store_module.read_card(store, "card-r9")

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual({(item["kind"], item["id"]) for item in preview["affected"]}, {
            ("language", "prof-lang-default"), ("bundle", "bundle-2")})

    def test_speech_preview_has_no_own_state_and_lists_its_bundle(self):
        store, _connection = self.serve()
        before = store_module.read_record(store, "synthesis", "prof-synth")
        after = dict(before, speech_executable_id="exe-alternate")
        store.connection().execute(
            "INSERT INTO speech_executables VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("exe-alternate", "synthesis", "loc-models", "/bin/other", "1" * 64,
             "1.0", "", NOW))

        preview = store_module.preview_edit(store, before, after)

        self.assertIsNone(preview["state"])
        self.assertEqual(preview["changed_values"], [{
            "field": "speech_executable_id", "label": "Speech executable",
            "old": "exe-breeze", "new": "exe-alternate"}])
        self.assertEqual([(item["kind"], item["id"]) for item in preview["affected"]],
                         [("bundle", "bundle-1"), ("bundle", "bundle-2")])

    def test_preview_from_an_uncomputable_record_to_a_valid_one_is_a_served_change(self):
        store, _connection = self.serve()
        stored = store_module.read_record(store, "language", "prof-lang")
        before = self.without(stored, "model")

        preview = store_module.preview_edit(store, before, stored)

        self.assertEqual(preview["kind"], "served-change")
        self.assertIn("no model file", preview["reason"])
        self.assertEqual(preview["state"]["state"], "candidate")
        self.assertEqual(preview["changed_values"][0]["field"], "served_configuration")

    def test_uncomputable_dependencies_are_only_affected_by_known_served_card_changes(self):
        store, connection = self.serve()
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        before = store_module.read_card(store, "card-r9")

        cosmetic = store_module.preview_edit(store, before, dict(before, name="New card name"))
        unchanged = store_module.preview_edit(store, before, dict(before))
        changed_default = store_module.preview_edit(
            store, before, dict(before, default_gpu_layers=42))

        self.assertEqual(cosmetic["kind"], "cosmetic-only")
        self.assertEqual(cosmetic["affected"], [])
        self.assertEqual(unchanged["kind"], "unchanged")
        self.assertEqual(unchanged["affected"], [])
        self.assertEqual({(item["kind"], item["id"], item["state"]["state"])
                          for item in changed_default["affected"]}, {
            ("language", "prof-lang-default", "unavailable"),
            ("bundle", "bundle-2", "unavailable")})

    def test_bundle_edits_do_not_traverse_a_profile_with_the_same_id(self):
        store, connection = self.serve()
        connection.execute(
            "INSERT INTO model_profiles SELECT 'bundle-1', 'language', 'Collision', 'collision',"
            " context_size, thinking, card_id, NULL, 0, '', created_at"
            " FROM model_profiles WHERE profile_id = 'prof-lang-default'")
        connection.execute(
            "INSERT INTO profile_arguments SELECT 'bundle-1', tool, position, argument_id, flag, value"
            " FROM profile_arguments WHERE profile_id = 'prof-lang-default'")
        connection.execute(
            "INSERT INTO profile_artifacts SELECT 'bundle-1', role, acquisition_id, file_name"
            " FROM profile_artifacts WHERE profile_id = 'prof-lang-default'")
        connection.execute("UPDATE bundle_members SET profile_id = 'bundle-1'"
                           " WHERE bundle_id = 'bundle-2' AND role = 'language'")
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        before = store_module.read_record(store, "bundle", "bundle-1")

        preview = store_module.preview_edit(store, before,
                                            dict(before, load_tokens=before["load_tokens"] + 1))

        self.assertEqual(preview["kind"], "unavailable")
        self.assertEqual(preview["affected"], [])

    def test_default_layer_preview_fails_closed_for_malformed_profile_arguments(self):
        store, connection = self.serve()
        connection.execute("UPDATE profile_arguments SET value = '0'"
                           " WHERE profile_id = 'prof-lang' AND flag = '--n-gpu-layers'")
        before = store_module.read_card(store, "card-r9")

        preview = store_module.preview_edit(store, before,
                                            dict(before, default_gpu_layers=42))

        affected = {(item["kind"], item["id"]): item for item in preview["affected"]}
        self.assertEqual(affected[("language", "prof-lang")]["state"]["state"], "unavailable")
        self.assertIn("positive GPU-layer count",
                      affected[("language", "prof-lang")]["state"]["reason"])
        self.assertEqual(affected[("bundle", "bundle-1")]["state"]["state"], "unavailable")

    def test_preview_fails_closed_when_a_required_digest_is_missing(self):
        store, connection = self.serve()
        before = store_module.read_record(store, "language", "prof-lang")
        after = dict(before, context_size=98304)
        self.accept(connection, "q-failing-preview", "language", "prof-lang",
                    store_module.fingerprint(store, before))
        connection.execute("UPDATE acquisition_files SET computed_sha256 = ''"
                           " WHERE acquisition_id = 'acq-qwen' AND file_name = 'model.gguf'")
        writes = connection.total_changes

        preview = store_module.preview_edit(store, before, after)

        self.assertEqual(connection.total_changes, writes)
        self.assertEqual(preview["kind"], "unavailable")
        self.assertEqual(preview["state"]["state"], "unavailable")
        self.assertIn("computed SHA-256", preview["reason"])
        self.assertNotEqual(preview["state"]["state"], "qualified")
        self.assertEqual([(item["kind"], item["id"], item["state"]["state"])
                          for item in preview["affected"]],
                         [("bundle", "bundle-1", "unavailable")])
        self.assertIn("computed SHA-256", preview["affected"][0]["state"]["reason"])

    def test_forgetting_an_inactive_build_keeps_the_qualification_history(self):
        store, connection = self.serve()
        accepted = self.fingerprint(store, "language", "prof-lang")
        self.accept(connection, "q-lang", "language", "prof-lang", accepted, build_id="build-hip")
        connection.execute("UPDATE runtime_selection SET build_id = 'build-vk' WHERE selection_id = 1")

        connection.execute("DELETE FROM builds WHERE build_id = 'build-hip'")

        self.assertIsNone(connection.execute(
            "SELECT 1 FROM builds WHERE build_id = 'build-hip'").fetchone())
        state = self.state(store, "language", "prof-lang")
        self.assertEqual(state["state"], "qualified")
        self.assertIn("build-hip", state["warning"])


class MigrationStagingTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.version_patch = mock.patch.object(store_module, "SCHEMA_VERSION", 1)
        self.version_patch.start()
        self.addCleanup(self.version_patch.stop)

    def test_v1_migration_adds_build_provenance_and_preserves_acceptance_citations(self):
        store = self.open_store()
        self.fill(store.connection())
        store.close()
        connection = store_module._connect(self.path)
        connection.execute("DROP TRIGGER accepted_run_not_removable")
        connection.execute("ALTER TABLE runs DROP COLUMN build_id")
        connection.execute("ALTER TABLE acceptance_runs DROP COLUMN build_id")
        connection.execute(
            "UPDATE store_meta SET value = '1' WHERE key = 'schema_version'")
        connection.close()
        original = digest_of(self.path)

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2):
            staged = store_module.stage_migrations(self.config)
        self.addCleanup(store_module._discard_migration_temp, staged)
        migrated = store_module._readonly_connect(staged)
        try:
            self.assertEqual(migrated.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0], "2")
            self.assertEqual(migrated.execute(
                "SELECT build_id FROM runs WHERE run_id = 'run-smoke-1'").fetchone()[0], None)
            self.assertEqual(migrated.execute(
                "SELECT build_id FROM acceptance_runs WHERE acceptance_id = 'acc-1'").fetchone()[0], None)
            self.assertEqual(migrated.execute(
                "SELECT run_fingerprint FROM acceptance_runs WHERE acceptance_id = 'acc-1'").fetchone()[0],
                "fp-9c1")
            self.assertEqual(migrated.execute(
                "SELECT reason FROM acceptance_exceptions WHERE acceptance_id = 'acc-1'").fetchone()[0],
                "retrieval is good enough")
            self.assertIsNotNone(migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                " AND name = 'accepted_run_not_removable'").fetchone())
        finally:
            migrated.close()

        self.assertEqual(digest_of(self.path), original)

    def test_v2_migration_retains_origins_as_enabled_rows(self):
        with mock.patch.object(store_module, "SCHEMA_VERSION", 3):
            opened = store_module.open_store(self.config)
            connection = opened.connection()
            connection.execute(
                "INSERT INTO download_origins"
                " (origin_id, kind, name, notes, created_at)"
                " VALUES ('origin-owner', 'organisation', 'owner', '', 'now')")
            opened.close()
        connection = store_module._connect(self.path)
        connection.execute("ALTER TABLE download_origins DROP COLUMN enabled")
        connection.execute(
            "UPDATE store_meta SET value = '2' WHERE key = 'schema_version'")
        connection.close()

        with mock.patch.object(store_module, "SCHEMA_VERSION", 3):
            staged = store_module.stage_migrations(self.config)
        self.addCleanup(store_module._discard_migration_temp, staged)
        migrated = store_module._readonly_connect(staged)
        try:
            row = migrated.execute(
                "SELECT origin_id, enabled FROM download_origins").fetchone()
            self.assertEqual(tuple(row), ("origin-owner", 1))
            self.assertEqual(migrated.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0], "3")
            self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            migrated.close()

    def test_v3_migration_adds_verification_reason_without_changing_acquisition_history(self):
        opened = store_module.open_store(self.config)
        connection = opened.connection()
        connection.execute(
            "INSERT INTO artifact_locations (location_id, path, created_at)"
            " VALUES ('loc-models', '/models', 'before')")
        connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, location_id, created_at)"
            " VALUES ('acq-model', 'Model', 'adoption', 'loc-models', 'before')")
        connection.execute(
            "INSERT INTO acquisition_files"
            " (acquisition_id, file_name, size_bytes, computed_sha256)"
            " VALUES ('acq-model', 'model.gguf', 4, 'digest')")
        opened.close()
        connection = store_module._connect(self.path)
        connection.execute("DROP TABLE acquisition_verification_failures")
        connection.execute("UPDATE store_meta SET value = '3' WHERE key = 'schema_version'")
        connection.close()
        original = digest_of(self.path)

        with mock.patch.object(store_module, "SCHEMA_VERSION", 4):
            staged = store_module.stage_migrations(self.config)
        self.addCleanup(store_module._discard_migration_temp, staged)
        migrated = store_module._readonly_connect(staged)
        try:
            self.assertEqual(migrated.execute(
                "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0], "4")
            self.assertEqual(tuple(migrated.execute(
                "SELECT acquisition_id, name, verified_at FROM acquisition_records").fetchone()),
                ("acq-model", "Model", None))
            self.assertIsNotNone(migrated.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table'"
                " AND name = 'acquisition_verification_failures'").fetchone())
            self.assertEqual(migrated.execute(
                "SELECT count(*) FROM acquisition_verification_failures").fetchone()[0], 0)
            self.assertEqual(tuple(migrated.execute(
                "SELECT file_name, size_bytes, computed_sha256 FROM acquisition_files").fetchone()),
                ("model.gguf", 4, "digest"))
        finally:
            migrated.close()
        self.assertEqual(digest_of(self.path), original)

    def test_ordered_migrations_stage_a_private_copy_and_leave_v1_untouched(self):
        store = self.open_store()
        self.fill(store.connection())
        store.close()
        original = digest_of(self.path)

        def migrate_one(connection):
            connection.execute("CREATE TABLE migration_order (position INTEGER)")
            connection.execute("INSERT INTO migration_order VALUES (1)")

        def migrate_two(connection):
            connection.execute("INSERT INTO migration_order VALUES (2)")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 3), mock.patch.object(
                store_module, "MIGRATIONS", {1: migrate_one, 2: migrate_two}):
            staged = store_module.stage_migrations(self.config)
            self.assertEqual(mode_of(staged), 0o600)
            result = store_module._readonly_connect(staged)
            try:
                self.assertEqual([tuple(row) for row in result.execute(
                    "SELECT position FROM migration_order ORDER BY position").fetchall()],
                    [(1,), (2,)])
                self.assertEqual(result.execute(
                    "SELECT value FROM store_meta WHERE key = 'schema_version'").fetchone()[0], "3")
                self.assertEqual(result.execute("SELECT count(*) FROM cards").fetchone()[0], 1)
                self.assertEqual(result.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(result.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                result.close()
            with mock.patch.object(store_module, "SCHEMA_VERSION", 1):
                with self.assertRaises(store_module.StoreError) as refusal:
                    store_module._refuse_version(staged, True, 3)
                self.assertIn("newer", str(refusal.exception))
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(self.open_store().connection().execute(
            "SELECT count(*) FROM cards").fetchone()[0], 1)

    def test_missing_step_and_downgrade_refuse_before_creating_a_copy(self):
        store = self.open_store()
        store.close()
        original = digest_of(self.path)
        with mock.patch.object(store_module, "SCHEMA_VERSION", 2), mock.patch.object(
                store_module, "MIGRATIONS", {}), mock.patch.object(
                    store_module.tempfile, "mkstemp", wraps=store_module.tempfile.mkstemp) as create:
            with self.assertRaisesRegex(store_module.StoreError, "no schema migration"):
                store_module.stage_migrations(self.config)
            create.assert_not_called()
        with mock.patch.object(store_module, "SCHEMA_VERSION", 0):
            with self.assertRaisesRegex(store_module.StoreError, "downgrades are not supported"):
                store_module.stage_migrations(self.config)
        self.assertEqual(digest_of(self.path), original)

    def test_failed_step_rolls_back_and_removes_its_staged_copy(self):
        store = self.open_store()
        store.close()
        original = digest_of(self.path)

        def fail(connection):
            connection.execute("CREATE TABLE partial_change (value TEXT)")
            raise RuntimeError("injected failure")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2), mock.patch.object(
                store_module, "MIGRATIONS", {1: fail}):
            with self.assertRaisesRegex(store_module.StoreError, "migration 1 to 2 failed.*injected failure"):
                store_module.stage_migrations(self.config)
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [])

    def test_failure_opening_a_later_step_names_that_step(self):
        store = self.open_store()
        store.close()
        original = digest_of(self.path)
        connect = store_module._connect
        stage_connections = 0

        def fail_second_migration_open(path):
            nonlocal stage_connections
            if Path(path).name.startswith(store_module.MIGRATION_TEMP_PREFIX):
                stage_connections += 1
                if stage_connections == 3:  # backup target, step 1, then step 2
                    raise sqlite3.OperationalError("injected open failure")
            return connect(path)

        def noop(connection):
            pass

        with mock.patch.object(store_module, "SCHEMA_VERSION", 3), mock.patch.object(
                store_module, "MIGRATIONS", {1: noop, 2: noop}), mock.patch.object(
                    store_module, "_connect", side_effect=fail_second_migration_open):
            with self.assertRaisesRegex(store_module.StoreError,
                                        "migration 2 to 3 failed.*injected open failure"):
                store_module.stage_migrations(self.config)
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [])

    def test_a_step_cannot_disable_check_validation_and_stage_invalid_records(self):
        store = self.open_store()
        self.fill(store.connection())
        store.close()
        original = digest_of(self.path)

        def break_check(connection):
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE cards SET vram_ceiling_bytes = -1")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2), mock.patch.object(
                store_module, "MIGRATIONS", {1: break_check}):
            with self.assertRaisesRegex(store_module.StoreError,
                                        "migration 1 to 2 failed.*integrity_check failed"):
                store_module.stage_migrations(self.config)
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [])
        self.assertFalse(list(self.directory.glob(f"{self.path.name}-*")))

    def test_a_step_with_broken_foreign_keys_fails_and_does_not_escape_the_copy(self):
        store = self.open_store()
        store.close()
        original = digest_of(self.path)

        def break_reference(connection):
            connection.execute(
                "CREATE TABLE migrated_child (card_id TEXT REFERENCES cards(card_id)"
                " DEFERRABLE INITIALLY DEFERRED)")
            connection.execute("INSERT INTO migrated_child VALUES ('missing-card')")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2), mock.patch.object(
                store_module, "MIGRATIONS", {1: break_reference}):
            with self.assertRaisesRegex(store_module.StoreError, "migration 1 to 2 failed.*foreign_key_check"):
                store_module.stage_migrations(self.config)
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [])

    def test_interrupted_step_is_discarded_and_rebuilt_from_the_original(self):
        store = self.open_store()
        self.fill(store.connection())
        store.close()
        original = digest_of(self.path)
        child = subprocess.run([sys.executable, "-c", """
import os, sys
sys.path.insert(0, sys.argv[1])
from matuto import store

def interrupted(connection):
    connection.execute('CREATE TABLE partial_change (value TEXT)')
    os._exit(23)

store.SCHEMA_VERSION = 2
store.MIGRATIONS = {1: interrupted}
store.stage_migrations(sys.argv[2])
""", str(AGENTS), str(self.config)], capture_output=True, text=True)
        self.assertEqual(child.returncode, 23, child.stderr)
        self.assertTrue(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")))

        def migrate(connection):
            connection.execute("CREATE TABLE completed_change (value TEXT)")

        with mock.patch.object(store_module, "SCHEMA_VERSION", 2), mock.patch.object(
                store_module, "MIGRATIONS", {1: migrate}):
            staged = store_module.stage_migrations(self.config)
            result = store_module._readonly_connect(staged)
            try:
                self.assertIsNotNone(result.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'completed_change'").fetchone())
                self.assertIsNone(result.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'partial_change'").fetchone())
            finally:
                result.close()
        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [staged])

    def test_unsafe_store_directory_is_refused_without_staging(self):
        self.open_store().close()
        os.chmod(self.directory, 0o755)
        original = digest_of(self.path)

        with self.assertRaisesRegex(store_module.StoreError, "group or others"):
            store_module.stage_migrations(self.config)

        self.assertEqual(digest_of(self.path), original)
        self.assertEqual(list(self.directory.glob(f"{store_module.MIGRATION_TEMP_PREFIX}*")), [])


if __name__ == "__main__":
    unittest.main()
