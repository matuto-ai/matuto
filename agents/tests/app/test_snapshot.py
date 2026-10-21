"""Matuto's one pre-update snapshot: what it copies, refuses and keeps.

Everything here goes through the module's own Python API against a temporary
home directory, which is the seam the spec's Testing Decisions name.
"""

import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import snapshot as snapshot_module  # noqa: E402  the package is not on the path yet
from matuto import store as store_module  # noqa: E402


def mode_of(path):
    return stat.S_IMODE(path.lstat().st_mode)


def digest_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(connection, statement):
    return [tuple(row) for row in connection.execute(statement)]


class SnapshotTestCase(unittest.TestCase):
    """A temporary config root standing in for the operator's home directory."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.store = store_module.open_store(self.config)
        self.addCleanup(self.store.close)
        self.path = snapshot_module.snapshot_path(self.store.path)

    def fill(self, connection):
        connection.execute("INSERT INTO cards VALUES ('card-r9', 'Radeon AI PRO R9700',"
                           " 'gfx1201', 34359738368, 99, 16, 4096, 512, 5, 'the loud one',"
                           " '2026-10-01T00:00:00+00:00')")
        connection.execute("INSERT INTO artifact_locations VALUES ('loc-models',"
                           " '/var/lib/matuto/models', 'the download root',"
                           " '2026-10-01T00:00:00+00:00')")
        connection.execute("INSERT INTO model_storage VALUES (1, 'loc-models',"
                           " '2026-10-01T00:00:00+00:00')")

    def temporary_files(self):
        return list(self.path.parent.glob(".pre-update-snapshot-*"))


class SnapshotTest(SnapshotTestCase):
    def test_a_populated_store_snapshots_and_the_copy_opens_on_its_own(self):
        connection = self.store.connection()
        self.fill(connection)

        snapshot = snapshot_module.take(self.store, "2026.10.0")

        self.assertEqual(snapshot.path, self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(snapshot.release_version, "2026.10.0")
        self.assertEqual(snapshot.schema_version, store_module.SCHEMA_VERSION)
        self.assertEqual(snapshot.size_bytes, self.path.stat().st_size)
        self.assertEqual(mode_of(self.path), 0o600)
        self.assertEqual(mode_of(self.path.parent), 0o700)
        self.assertRegex(snapshot.taken_at, r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\Z")

        copy = store_module._readonly_connect(self.path)
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchall()[0][0], "ok")
            for table in ("cards", "artifact_locations", "model_storage"):
                self.assertEqual(rows(copy, f"SELECT * FROM {table}"),
                                 rows(connection, f"SELECT * FROM {table}"))
            self.assertEqual(
                copy.execute("SELECT value FROM store_meta WHERE key = ?",
                             (snapshot_module.LABEL_RELEASE_KEY,)).fetchone()[0],
                "2026.10.0")
        finally:
            copy.close()
        # The label lives in the copy, not in the live store.
        self.assertIsNone(connection.execute(
            "SELECT value FROM store_meta WHERE key = ?",
            (snapshot_module.LABEL_RELEASE_KEY,)).fetchone())

    def test_the_label_is_recorded_in_the_update_state_block(self):
        self.fill(self.store.connection())
        snapshot = snapshot_module.take(self.store, "2026.10.0")

        row = self.store.connection().execute(
            "SELECT release_version, snapshot_path, snapshot_taken_at FROM update_state"
            " WHERE state_id = 1").fetchone()

        self.assertEqual(row["release_version"], "2026.10.0")
        self.assertEqual(row["snapshot_path"], str(self.path))
        self.assertEqual(row["snapshot_taken_at"], snapshot.taken_at)

    def test_taking_a_snapshot_preserves_the_updaters_phase(self):
        self.store.connection().execute(
            "INSERT INTO update_state VALUES (1, '2026.9.0', 'staging', NULL, NULL,"
            " '2026-10-09T00:00:00+00:00')")

        snapshot_module.take(self.store, "2026.10.0")

        row = self.store.connection().execute(
            "SELECT phase, release_version FROM update_state WHERE state_id = 1").fetchone()
        self.assertEqual(row["phase"], "staging")
        self.assertEqual(row["release_version"], "2026.10.0")

    def test_asking_again_for_the_same_update_keeps_the_original(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        before = (self.path.stat().st_ino, self.path.stat().st_mtime_ns, digest_of(self.path))

        second = snapshot_module.take(self.store, "2026.10.0")

        self.assertEqual(first, second)
        self.assertEqual((self.path.stat().st_ino, self.path.stat().st_mtime_ns,
                          digest_of(self.path)), before)

    def test_a_newers_snapshot_replaces_the_previous_only_after_verifying(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        self.store.connection().execute(
            "INSERT INTO artifact_locations VALUES ('loc-more', '/var/lib/matuto/more', '',"
            " '2026-10-10T00:00:00+00:00')")

        second = snapshot_module.take(self.store, "2026.11.0")

        self.assertEqual(second.release_version, "2026.11.0")
        self.assertNotEqual(second, first)
        self.assertEqual([path.name for path in self.path.parent.glob("pre-update-snapshot*")],
                         [snapshot_module.SNAPSHOT_FILENAME])
        self.assertEqual(self.temporary_files(), [])
        copy = store_module._readonly_connect(self.path)
        try:
            self.assertEqual(rows(copy, "SELECT location_id FROM artifact_locations"
                                       " ORDER BY location_id"),
                             [("loc-models",), ("loc-more",)])
        finally:
            copy.close()

    def test_a_copy_that_fails_verification_is_refused_and_the_previous_kept(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        before = digest_of(self.path)

        def other_database(opened, temporary, release_version, taken_at):
            connection = sqlite3.connect(str(temporary))
            try:
                connection.execute("CREATE TABLE unrelated (x)")
                connection.commit()
            finally:
                connection.close()

        with mock.patch.object(snapshot_module, "_copy_store", other_database):
            with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                snapshot_module.take(self.store, "2026.11.0")

        self.assertIn("not a Matuto store", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)
        self.assertEqual(self.temporary_files(), [])
        self.assertEqual(snapshot_module.take(self.store, "2026.10.0"), first)

    def test_a_copy_that_is_not_a_database_is_refused_with_nothing_left(self):
        def garbage(opened, temporary, release_version, taken_at):
            temporary.write_bytes(b"this is not a database")

        with mock.patch.object(snapshot_module, "_copy_store", garbage):
            with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("not a Matuto store", str(refusal.exception))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.temporary_files(), [])

    def test_a_kept_snapshot_that_no_longer_verifies_is_refused_not_replaced(self):
        self.fill(self.store.connection())
        snapshot_module.take(self.store, "2026.10.0")
        self.path.write_bytes(b"damaged beyond recovery")
        before = digest_of(self.path)

        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("not a Matuto store", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)

    def test_a_destination_that_cannot_be_written_leaves_everything_as_it_was(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        before = digest_of(self.path)

        # The destination refusing a new file, whatever account runs it.
        with mock.patch.object(snapshot_module.tempfile, "mkstemp",
                               side_effect=OSError("read-only file system")):
            with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                snapshot_module.take(self.store, "2026.11.0")

        self.assertIn("cannot create the pre-update snapshot", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)
        self.assertEqual(self.temporary_files(), [])
        self.assertEqual(snapshot_module.take(self.store, "2026.10.0"), first)

    def test_an_interruption_before_the_replacement_keeps_the_previous_snapshot(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        before = digest_of(self.path)

        with mock.patch.object(snapshot_module.os, "replace",
                               side_effect=OSError("interrupted")):
            with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                snapshot_module.take(self.store, "2026.11.0")

        self.assertIn("cannot replace the pre-update snapshot", str(refusal.exception))
        self.assertEqual(digest_of(self.path), before)
        self.assertEqual(self.temporary_files(), [])
        self.assertEqual(snapshot_module.take(self.store, "2026.10.0"), first)

    def test_a_snapshot_left_under_another_updates_label_is_retaken(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        # Reproduce the state an interruption between the rename and the record
        # leaves: the file holds the newer update's copy while the record still
        # names the older one.
        snapshot_module.take(self.store, "2026.11.0")
        self.store.connection().execute(
            "UPDATE update_state SET release_version = '2026.10.0', snapshot_taken_at = ?"
            " WHERE state_id = 1", (first.taken_at,))

        retaken = snapshot_module.take(self.store, "2026.10.0")

        self.assertEqual(retaken.release_version, "2026.10.0")
        copy = store_module._readonly_connect(self.path)
        try:
            stamped = dict(rows(copy, "SELECT key, value FROM store_meta"
                                     " WHERE key LIKE 'snapshot_%'"))
        finally:
            copy.close()
        self.assertEqual(stamped[snapshot_module.LABEL_RELEASE_KEY], "2026.10.0")

    def test_a_partial_copy_left_by_an_interruption_is_swept(self):
        leftover = self.path.parent / f"{snapshot_module.TEMPORARY_PREFIX}deadbeef"
        leftover.write_bytes(b"half a snapshot")
        os.chmod(leftover, 0o600)

        snapshot_module.take(self.store, "2026.10.0")

        self.assertFalse(leftover.exists())
        self.assertEqual(self.temporary_files(), [])

    def test_a_store_restored_from_a_snapshot_can_be_snapshotted_again(self):
        # A snapshot put back by hand leaves its label keys in the live store.
        self.store.connection().executemany(
            "INSERT INTO store_meta (key, value) VALUES (?, ?)",
            ((snapshot_module.LABEL_RELEASE_KEY, "2026.9.0"),
             (snapshot_module.LABEL_TAKEN_KEY, "2026-10-09T00:00:00+00:00")))

        snapshot = snapshot_module.take(self.store, "2026.10.0")

        copy = store_module._readonly_connect(self.path)
        try:
            stamped = dict(rows(copy, "SELECT key, value FROM store_meta"
                                     " WHERE key LIKE 'snapshot_%'"))
        finally:
            copy.close()
        self.assertEqual(stamped[snapshot_module.LABEL_RELEASE_KEY], "2026.10.0")
        self.assertEqual(stamped[snapshot_module.LABEL_TAKEN_KEY], snapshot.taken_at)

    def test_a_linked_snapshot_is_refused_and_never_followed(self):
        target = self.home / "elsewhere.sqlite3"
        os.symlink(target, self.path)

        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("not a regular file", str(refusal.exception))
        self.assertFalse(target.exists())
        self.assertTrue(self.path.is_symlink())

    def test_a_snapshot_loose_to_group_or_others_is_refused_untouched(self):
        snapshot_module.take(self.store, "2026.10.0")
        for loose in (0o640, 0o604, 0o666):
            with self.subTest(mode=loose):
                os.chmod(self.path, loose)

                with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                    snapshot_module.take(self.store, "2026.10.0")

                self.assertIn("group or others", str(refusal.exception))
                self.assertEqual(mode_of(self.path), loose)
                os.chmod(self.path, 0o600)

    def test_a_snapshot_belonging_to_another_account_is_refused(self):
        snapshot_module.take(self.store, "2026.10.0")
        foreign = os.getuid() + 1

        with mock.patch.object(store_module.os, "getuid",
                               side_effect=[os.getuid(), foreign]):
            with self.assertRaises(snapshot_module.SnapshotError) as refusal:
                snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("belongs to another account", str(refusal.exception))

    def test_a_snapshot_directory_open_to_group_or_others_is_refused(self):
        os.chmod(self.path.parent, 0o750)
        self.addCleanup(os.chmod, self.path.parent, 0o700)

        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("group or others", str(refusal.exception))
        self.assertFalse(self.path.exists())

    def test_a_snapshot_needs_the_release_version_it_is_taken_for(self):
        for bad in ("", None, 7):
            with self.subTest(release_version=bad):
                with self.assertRaises(snapshot_module.SnapshotError):
                    snapshot_module.take(self.store, bad)

        self.assertFalse(self.path.exists())

    def test_a_verified_copy_whose_record_was_missed_is_adopted_untouched(self):
        self.fill(self.store.connection())
        first = snapshot_module.take(self.store, "2026.10.0")
        before = (self.path.stat().st_ino, self.path.stat().st_mtime_ns, digest_of(self.path))
        # Reproduce a kill between the rename and the record: the verified copy
        # is there with its own label, but no update_state row reached it.
        self.store.connection().execute("DELETE FROM update_state")

        adopted = snapshot_module.take(self.store, "2026.10.0")

        self.assertEqual(adopted, first)
        self.assertEqual((self.path.stat().st_ino, self.path.stat().st_mtime_ns,
                          digest_of(self.path)), before)
        row = self.store.connection().execute(
            "SELECT release_version, snapshot_taken_at FROM update_state"
            " WHERE state_id = 1").fetchone()
        self.assertEqual(row["release_version"], "2026.10.0")
        self.assertEqual(row["snapshot_taken_at"], first.taken_at)

    def test_take_refuses_while_the_store_connection_holds_a_transaction(self):
        connection = self.store.connection()
        connection.execute("BEGIN IMMEDIATE")
        self.addCleanup(connection.execute, "ROLLBACK")

        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("holds a transaction", str(refusal.exception))
        self.assertFalse(self.path.exists())

    def test_a_sidecar_beside_the_snapshot_is_held_to_the_store_rule(self):
        snapshot_module.take(self.store, "2026.10.0")
        before = digest_of(self.path)
        sidecar = self.path.parent / (self.path.name + "-wal")
        sidecar.write_bytes(b"")
        os.chmod(sidecar, 0o666)

        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")

        self.assertIn("group or others", str(refusal.exception))
        self.assertEqual(mode_of(sidecar), 0o666)
        self.assertEqual(digest_of(self.path), before)

        sidecar.unlink()
        planted = self.home / "planted"
        os.symlink(planted, sidecar)
        with self.assertRaises(snapshot_module.SnapshotError) as refusal:
            snapshot_module.take(self.store, "2026.10.0")
        self.assertIn("not a regular file", str(refusal.exception))
        self.assertFalse(planted.exists())

    def test_the_module_offers_only_taking_and_never_restore(self):
        public = {name for name, value in vars(snapshot_module).items()
                  if not name.startswith("_")
                  and getattr(value, "__module__", None) == snapshot_module.__name__}

        self.assertEqual(public, {"Snapshot", "SnapshotError", "snapshot_path", "take"})

    def test_a_write_landing_while_it_runs_still_yields_a_consistent_copy(self):
        batch = 5
        batches = 40
        first_commit = threading.Event()

        def writer():
            connection = store_module._connect(self.store.path)
            try:
                connection.execute("PRAGMA busy_timeout = 30000")
                for index in range(batches):
                    connection.execute("BEGIN IMMEDIATE")
                    for offset in range(batch):
                        number = index * batch + offset
                        connection.execute(
                            "INSERT INTO download_origins"
                            " (origin_id, kind, name, notes, created_at)"
                            " VALUES (?, 'repository', ?, ?, ?)",
                            (f"origin-{number}", f"origin-{number}", "x" * 2000,
                             "2026-10-10T00:00:00+00:00"))
                    connection.execute("COMMIT")
                    first_commit.set()
                    time.sleep(0.002)
            except sqlite3.Error:
                pass
            finally:
                connection.close()

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(first_commit.wait(10))
        snapshot = snapshot_module.take(self.store, "2026.10.0")
        thread.join(30)
        self.assertFalse(thread.is_alive())

        copy = store_module._readonly_connect(snapshot.path)
        try:
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchall()[0][0], "ok")
            count = copy.execute("SELECT count(*) FROM download_origins").fetchone()[0]
        finally:
            copy.close()
        # Every committed transaction is whole in the copy: a torn read would
        # leave a count that is not a multiple of the batch. The first batch was
        # committed before the snapshot started, so it must be in the copy.
        self.assertEqual(count % batch, 0)
        self.assertGreaterEqual(count, batch)
        self.assertLessEqual(count, batch * batches)


if __name__ == "__main__":
    unittest.main()
