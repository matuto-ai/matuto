"""Model storage is saved by the operator only, and never falls back.

Everything here goes through the store module's Python API against a
temporary home, which is the seam the spec's Testing Decisions name.
"""

import hashlib
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission  # noqa: E402  the package is not on the path yet
from matuto import discovery  # noqa: E402  the package is not on the path yet
from matuto import store as store_module  # noqa: E402
from matuto import storage  # noqa: E402


class StorageTestCase(unittest.TestCase):
    """A temporary config root standing in for the operator's home directory."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.store = store_module.open_store(self.config)
        self.addCleanup(self.store.close)
        self.connection = self.store.connection()

    def usable(self, name="models"):
        path = self.home / name
        path.mkdir(mode=0o700)
        return path

    def provider(self):
        """The status provider found by name, the way the shell finds it."""
        names = discovery.item_names("status")
        self.assertIn("storage", names)
        return discovery.load_item("status", "storage")


class ModelStorageTest(StorageTestCase):
    def test_no_saved_storage_refuses_the_destination_naming_the_setting(self):
        # The checkout install's own model-root setting and its default data
        # directory exist, and neither is read: the old root never becomes a
        # confirmed selection (CUT-4).
        legacy = store_module.store_directory(self.config) / "model-root"
        legacy.write_text("/somewhere/else\n")
        default = self.home / ".local" / "share" / "ai-agents-sandbox" / "models"
        default.mkdir(parents=True, mode=0o700)

        for asking in (storage.download_destination, storage.discovery_directory):
            with self.assertRaises(store_module.StoreError) as refused:
                asking(self.connection)
            self.assertIn("model storage", str(refused.exception))
            self.assertIn(storage.SETTINGS, str(refused.exception))
            self.assertNotIn("/somewhere/else", str(refused.exception))
            self.assertNotIn(str(default), str(refused.exception))
        self.assertIsNone(storage.model_storage(self.connection))
        self.assertIsNone(self.provider().report(self.connection))

    def test_saved_root_is_the_destination_and_its_linked_location(self):
        root = self.usable()
        saved = storage.save_model_storage(self.connection, root)

        self.assertEqual(saved["path"], str(root))
        self.assertTrue(saved["available"])
        self.assertIsNone(saved["reason"])
        self.assertEqual(saved["router"]["model_directory"], str(root))
        self.assertTrue(Path(saved["router"]["presets_path"]).is_file())
        self.assertEqual(storage.download_destination(self.connection), str(root))
        self.assertEqual(storage.discovery_directory(self.connection), str(root))
        locations = self.connection.execute(
            "SELECT path FROM artifact_locations").fetchall()
        self.assertEqual([row["path"] for row in locations], [str(root)])
        linked = self.connection.execute(
            "SELECT location_id FROM model_storage WHERE setting_id = 1").fetchone()
        self.assertEqual(linked["location_id"], saved["location_id"])
        # The root is a configured artifact location too, and adoption takes
        # files in any of them, so it is never narrowed away.
        self.assertEqual(storage.artifact_locations(self.connection), [str(root)])

    def test_missing_root_is_saved_reported_and_never_replaced(self):
        missing = self.home / "absent"
        saved = storage.save_model_storage(self.connection, missing)

        self.assertFalse(saved["available"])
        self.assertEqual(saved["reason"], "does not exist")
        self.assertEqual(storage.model_storage(self.connection), str(missing))
        with self.assertRaises(store_module.StoreError) as refused:
            storage.download_destination(self.connection)
        self.assertIn(str(missing), str(refused.exception))
        self.assertIn("does not exist", str(refused.exception))
        self.assertEqual(self.provider().report(self.connection),
                         {"title": "Storage unavailable", "path": str(missing),
                          "reason": "does not exist",
                          "link": self.provider().LINK})

    def test_group_writable_root_is_saved_and_reported_unavailable(self):
        root = self.usable("shared")
        root.chmod(0o770)
        self.addCleanup(root.chmod, 0o700)
        saved = storage.save_model_storage(self.connection, root)

        self.assertFalse(saved["available"])
        self.assertEqual(saved["reason"], "is writable by another account")
        with self.assertRaises(store_module.StoreError) as refused:
            storage.download_destination(self.connection)
        self.assertIn(str(root), str(refused.exception))
        self.assertIn("writable by another account", str(refused.exception))
        self.assertEqual(self.provider().report(self.connection)["title"],
                         "Storage unavailable")

    def test_a_file_is_saved_and_reported_not_a_directory(self):
        a_file = self.home / "weights"
        a_file.write_text("")
        saved = storage.save_model_storage(self.connection, a_file)

        self.assertFalse(saved["available"])
        self.assertEqual(saved["reason"], "is not a directory")
        with self.assertRaises(store_module.StoreError):
            storage.download_destination(self.connection)

    def test_a_path_with_a_non_control_unicode_character_is_saved(self):
        # Private use is not a control character, so a well-formed path holding
        # one is saved like any other.
        root = self.home / "models\ue000a"
        root.mkdir(mode=0o700)
        saved = storage.save_model_storage(self.connection, root)

        self.assertTrue(saved["available"])
        self.assertEqual(storage.download_destination(self.connection), str(root))

    def test_saving_twice_is_refused(self):
        storage.save_model_storage(self.connection, self.usable("first"))
        with self.assertRaises(store_module.StoreError) as refused:
            storage.save_model_storage(self.connection, self.usable("second"))
        self.assertIn("already saved", str(refused.exception))
        self.assertEqual(storage.model_storage(self.connection), str(self.home / "first"))

    def test_an_artifact_location_at_the_root_becomes_the_linked_one(self):
        root = self.usable()
        storage.add_artifact_location(self.connection, root)
        saved = storage.save_model_storage(self.connection, root)

        self.assertEqual(saved["location_id"], "loc-1")
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM artifact_locations").fetchone()[0], 1)
        self.assertEqual(storage.artifact_locations(self.connection), [str(root)])


class ArtifactLocationTest(StorageTestCase):
    def test_every_location_is_offered_for_adoption(self):
        root = self.usable()
        storage.save_model_storage(self.connection, root)
        extra = self.usable("adopted")
        added = storage.add_artifact_location(self.connection, extra)

        self.assertEqual(added["path"], str(extra))
        self.assertTrue(added["available"])
        # Adoption takes files in any configured location, the linked model
        # storage among them; only the download destination and native discovery
        # stay the root alone.
        self.assertEqual(storage.artifact_locations(self.connection),
                         [str(extra), str(root)])
        self.assertEqual(storage.download_destination(self.connection), str(root))
        self.assertEqual(storage.discovery_directory(self.connection), str(root))
        self.assertEqual(storage.model_storage(self.connection), str(root))
        self.assertIsNone(self.provider().report(self.connection))

    def test_an_unavailable_artifact_location_is_saved_and_reported(self):
        missing = self.home / "elsewhere"
        added = storage.add_artifact_location(self.connection, missing)

        self.assertFalse(added["available"])
        self.assertEqual(added["reason"], "does not exist")
        self.assertIn(str(missing), storage.artifact_locations(self.connection))

    def test_a_duplicate_location_is_refused(self):
        root = self.usable()
        storage.add_artifact_location(self.connection, root)
        with self.assertRaises(store_module.StoreError) as refused:
            storage.add_artifact_location(self.connection, root)
        self.assertIn("already configured", str(refused.exception))


class LocationChangeTest(StorageTestCase):
    def relocation_record(self, *, extra=False):
        old = self.usable("old")
        new = self.usable("new")
        old_file, new_file = old / "model.gguf", new / "model.gguf"
        old_file.write_bytes(b"model bytes")
        new_file.write_bytes(b"model bytes")
        saved = (storage.add_artifact_location(self.connection, old) if extra else
                 storage.save_model_storage(self.connection, old))
        digest = hashlib.sha256(old_file.read_bytes()).hexdigest()
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, verified_at, created_at) VALUES ('acq', 'Model', 'download',"
            " 'published-checksum', ?, 'before', 'created')", (saved["location_id"],))
        self.connection.execute(
            "INSERT INTO acquisition_files VALUES ('acq', 'model.gguf', ?, ?, ?, 'before')",
            (old_file.stat().st_size, digest, digest))
        self.connection.execute(
            "INSERT INTO cards VALUES ('card', 'Card', 'gpu', 1024, -1, 1, 1, 1, 1, '', 'created')")
        self.connection.execute(
            "INSERT INTO model_profiles VALUES ('profile', 'language', 'Profile', 'profile',"
            " 2048, 'high', 'card', NULL, 1, '', 'created')")
        self.connection.execute(
            "INSERT INTO profile_artifacts VALUES ('profile', 'model', 'acq', 'model.gguf')")
        for kind, profile_id, executable_id, role in (
                ("synthesis", "synth", "exec-synth", "synthesis"),
                ("recognition", "recognition", "exec-recognition", "recognition")):
            file_name = f"{profile_id}.bin"
            for root in (old, new):
                (root / file_name).write_bytes(file_name.encode())
            body = (old / file_name).read_bytes()
            file_digest = hashlib.sha256(body).hexdigest()
            acquisition_id = f"acq-{profile_id}"
            self.connection.execute(
                "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
                " location_id, verified_at, created_at) VALUES (?, ?, 'adoption',"
                " 'published-checksum', ?, 'before', 'created')",
                (acquisition_id, profile_id, saved["location_id"]))
            self.connection.execute(
                "INSERT INTO acquisition_files VALUES (?, ?, ?, ?, ?, 'before')",
                (acquisition_id, file_name, len(body), file_digest, file_digest))
            self.connection.execute(
                "INSERT INTO speech_executables VALUES (?, ?, ?, ?, ?, NULL, '', 'created')",
                (executable_id, kind, saved["location_id"], f"/{executable_id}", file_digest))
            self.connection.execute(
                "INSERT INTO model_profiles VALUES (?, ?, ?, '', NULL, NULL, ?, ?, 0, '', 'created')",
                (profile_id, kind, profile_id, "card" if kind == "synthesis" else None,
                 executable_id))
            self.connection.execute(
                "INSERT INTO profile_artifacts VALUES (?, 'weights', ?, ?)",
                (profile_id, acquisition_id, file_name))
        self.connection.execute(
            "INSERT INTO bundles VALUES ('bundle', 'Bundle', '', 10, 'prompt', 'created')")
        for role, profile_id in (("language", "profile"), ("synthesis", "synth"),
                                 ("recognition", "recognition")):
            self.connection.execute("INSERT INTO bundle_members VALUES ('bundle', ?, ?)",
                                    (role, profile_id))
        return saved, old, new, old_file, new_file

    def test_preview_and_change_reverify_identical_bytes_without_moving_them(self):
        saved, old, new, old_file, new_file = self.relocation_record()
        before = store_module.fingerprint(
            self.store, store_module.read_record(self.store, "language", "profile"))
        before_bundle = store_module.read_record(self.store, "bundle", "bundle")
        before_bundle_fingerprint = store_module.fingerprint(self.store, before_bundle)
        saved_at = self.connection.execute(
            "SELECT saved_at FROM model_storage WHERE setting_id = 1").fetchone()[0]
        preview = storage.preview_location_change(self.connection, saved["location_id"], new)
        self.assertTrue(preview["model_storage"])
        self.assertEqual(preview["changes"], {
            "model_storage": {"from": str(old), "to": str(new)},
            "artifact_location": {"from": str(old), "to": str(new)}})
        self.assertEqual({row["acquisition_id"] for row in preview["records"]},
                         {"acq", "acq-synth", "acq-recognition"})
        self.assertEqual({row["profile_id"] for row in preview["profiles"]},
                         {"profile", "synth", "recognition"})
        published_preview = next(row for row in preview["profiles"] if row["profile_id"] == "profile")
        self.assertEqual(published_preview["qualification"]["state"], "candidate")
        self.assertEqual([row["bundle_id"] for row in preview["bundles"]], ["bundle"])

        result = storage.change_location(self.connection, saved["location_id"], new,
                                         serving_state=admission.STOPPED)

        self.assertTrue(result["available"])
        self.assertTrue(result["records"][0]["available"])
        published_result = next(row for row in result["profiles"] if row["profile_id"] == "profile")
        self.assertEqual(published_result["qualification"]["state"], "candidate")
        self.assertEqual(storage.model_storage(self.connection), str(new))
        self.assertEqual(self.connection.execute(
            "SELECT saved_at FROM model_storage WHERE setting_id = 1").fetchone()[0], saved_at)
        self.assertEqual(self.connection.execute(
            "SELECT path FROM artifact_locations WHERE location_id = ?",
            (saved["location_id"],)).fetchone()["path"], str(new))
        self.assertTrue(old_file.exists())
        self.assertTrue(new_file.exists())
        self.assertEqual(old_file.read_bytes(), new_file.read_bytes())
        after_record = store_module.read_record(self.store, "language", "profile")
        self.assertEqual(store_module.fingerprint(self.store, after_record), before)
        self.assertEqual(store_module.qualification_state(self.store, after_record)["state"],
                         "candidate")
        after_bundle = store_module.read_record(self.store, "bundle", "bundle")
        self.assertEqual(store_module.fingerprint(self.store, after_bundle), before_bundle_fingerprint)
        self.assertEqual(store_module.qualification_state(self.store, after_bundle)["state"],
                         "candidate")

    def test_records_without_files_are_included_and_stay_unavailable(self):
        old, new = self.usable("old"), self.usable("new")
        saved = storage.save_model_storage(self.connection, old)
        self.connection.execute(
            "INSERT INTO acquisition_records (acquisition_id, name, source, checksum_status,"
            " location_id, verified_at, created_at) VALUES ('empty', 'Incomplete', 'adoption',"
            " 'locally-hashed', ?, NULL, 'created')", (saved["location_id"],))

        preview = storage.preview_location_change(self.connection, saved["location_id"], new)
        self.assertEqual([row["acquisition_id"] for row in preview["records"]], ["empty"])
        result = storage.change_location(self.connection, saved["location_id"], new,
                                         serving_state=admission.STOPPED)
        self.assertFalse(result["available"])
        self.assertFalse(result["records"][0]["available"])
        self.assertEqual(result["records"][0]["files"], [])
        self.assertIn("names no required file", result["records"][0]["reason"])

    def test_changed_bytes_leave_the_record_unavailable_and_identity_unchanged(self):
        saved, _old, new, _old_file, new_file = self.relocation_record()
        new_file.write_bytes(b"other bytes")
        digest = self.connection.execute(
            "SELECT computed_sha256 FROM acquisition_files WHERE acquisition_id = 'acq'").fetchone()[0]

        result = storage.change_location(self.connection, saved["location_id"], new,
                                         serving_state=admission.STOPPED)

        self.assertFalse(result["available"])
        self.assertFalse(result["records"][0]["available"])
        self.assertIn("model.gguf", result["records"][0]["reason"])
        self.assertEqual(self.connection.execute(
            "SELECT computed_sha256 FROM acquisition_files WHERE acquisition_id = 'acq'").fetchone()[0],
            digest)
        self.assertIsNone(self.connection.execute(
            "SELECT verified_at FROM acquisition_records WHERE acquisition_id = 'acq'").fetchone()[0])

    def test_serving_and_live_work_refuse_the_location_change(self):
        saved, _old, new, _old_file, _new_file = self.relocation_record()
        with self.assertRaisesRegex(store_module.StoreError, "Stop serving"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state=admission.AGENTIC)

        self.connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES ('download-1', 'download', 'unit', 'destination', 'now')")
        with self.assertRaisesRegex(store_module.StoreError, "download job.*Runs & jobs"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state=admission.STOPPED)
        self.connection.execute("DELETE FROM jobs")
        self.connection.execute(
            "INSERT INTO runs (run_id, record_kind, record_id, record_label, procedure,"
            " procedure_revision, started_at) VALUES ('run-1', 'language', 'profile',"
            " 'Profile', 'smoke', 'rev', 'now')")
        with self.assertRaisesRegex(store_module.StoreError, "smoke run.*Runs & jobs"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state=admission.STOPPED)

    def test_published_profile_using_an_additional_location_blocks_agentic_serving(self):
        saved, _old, new, _old_file, _new_file = self.relocation_record(extra=True)
        with self.assertRaisesRegex(store_module.StoreError, "Stop serving.*published profile"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state=admission.AGENTIC)

    def test_voice_bundle_using_an_additional_location_blocks_change(self):
        saved, _old, new, _old_file, _new_file = self.relocation_record(extra=True)
        self.connection.execute("INSERT INTO voice_serving VALUES (1, 'bundle')")
        with self.assertRaisesRegex(store_module.StoreError, "Stop serving.*voice bundle"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state=admission.VOICE)

    def test_unavailable_well_formed_path_is_saved_and_reported(self):
        saved, _old, _new, _old_file, _new_file = self.relocation_record()
        missing = self.home / "not-created"
        result = storage.change_location(self.connection, saved["location_id"], missing,
                                         serving_state=admission.STOPPED)
        self.assertFalse(result["available"])
        self.assertIn("does not exist", result["reason"])
        self.assertEqual(storage.model_storage(self.connection), str(missing))
        self.assertIsNone(self.connection.execute(
            "SELECT verified_at FROM acquisition_records WHERE acquisition_id = 'acq'").fetchone()[0])

    def test_unknown_serving_state_fails_closed(self):
        saved, _old, new, _old_file, _new_file = self.relocation_record()
        with self.assertRaisesRegex(store_module.StoreError, "serving state is unknown"):
            storage.change_location(self.connection, saved["location_id"], new,
                                    serving_state="unknown")
        self.assertEqual(storage.model_storage(self.connection), str(self.home / "old"))

    def test_location_change_waits_for_the_runner_admission_transaction(self):
        saved, _old, new, _old_file, _new_file = self.relocation_record()
        admitted = store_module.open_store(self.config)
        connection = admitted.connection()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO jobs (job_id, kind, unit_name, pinned_request, started_at)"
            " VALUES ('download-race', 'download', 'unit', 'destination', 'now')")
        started, finished = threading.Event(), threading.Event()
        result = []

        def change():
            other = sqlite3.connect(store_module.store_path(self.config), isolation_level=None)
            other.row_factory = sqlite3.Row
            other.execute("PRAGMA foreign_keys = ON")
            try:
                started.set()
                try:
                    storage.change_location(other, saved["location_id"], new,
                                            serving_state=admission.STOPPED)
                except store_module.StoreError as error:
                    result.append(str(error))
                finally:
                    finished.set()
            finally:
                other.close()

        worker = threading.Thread(target=change)
        worker.start()
        self.assertTrue(started.wait(2))
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        connection.execute("COMMIT")
        worker.join(3)
        admitted.close()
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertIn("download-race", result[0])
        self.assertEqual(storage.model_storage(self.connection), str(self.home / "old"))


class MalformedPathTest(StorageTestCase):
    def test_malformed_paths_are_refused_and_nothing_is_saved(self):
        for path in ("models", "", "~/models", "/models/\x00weights",
                     "/models/\nweights", "/models/\x85weights", "/models/\u202eweights",
                     "/var/../models", "/models/../..",
                     Path("relative/models"), None):
            with self.subTest(path=path):
                with self.assertRaises(store_module.StoreError) as refused:
                    storage.save_model_storage(self.connection, path)
                self.assertIn("must be", str(refused.exception))
                with self.assertRaises(store_module.StoreError):
                    storage.add_artifact_location(self.connection, path)

        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM artifact_locations").fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM model_storage").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()