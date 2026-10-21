"""Local adoption through the store's Python API."""

import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import adoption, storage, store  # noqa: E402


class AdoptionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.connection_store = store.open_store(self.home / "config")
        self.addCleanup(self.connection_store.close)
        self.connection = self.connection_store.connection()
        self.models = self.home / "artifacts"
        self.models.mkdir(mode=0o700)
        storage.add_artifact_location(self.connection, self.models)

    def write(self, name, body):
        path = self.models / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def row(self, acquisition_id):
        return dict(self.connection.execute(
            "SELECT * FROM acquisition_records WHERE acquisition_id = ?",
            (acquisition_id,)).fetchone())

    def adopted_files(self, acquisition_id):
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM acquisition_files WHERE acquisition_id = ? ORDER BY file_name",
            (acquisition_id,))]

    def adopt(self, acquisition_id, path, **kwargs):
        return adoption.adopt(self.connection, acquisition_id, "Local model", path,
                              **kwargs)

    def test_single_file_is_hashed_in_place_with_unknown_provenance(self):
        path = self.write("models/local.gguf", b"weights")
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)

        result = self.adopt("acq-local", path, consent=True)

        self.assertTrue(result.complete, result.reason)
        self.assertEqual(result.label, "locally hashed; publisher unverified")
        record = self.row("acq-local")
        self.assertEqual(record["source"], "adoption")
        self.assertEqual(record["checksum_status"], "locally-hashed")
        self.assertIsNone(record["origin_id"])
        self.assertIsNone(record["repository"])
        self.assertIsNone(record["revision"])
        self.assertEqual(record["location_id"], self.connection.execute(
            "SELECT location_id FROM artifact_locations").fetchone()[0])
        file = self.adopted_files("acq-local")[0]
        self.assertEqual(file["file_name"], "models/local.gguf")
        self.assertEqual(file["size_bytes"], len(before[0]))
        self.assertIsNone(file["published_sha256"])
        self.assertEqual(file["computed_sha256"], hashlib.sha256(before[0]).hexdigest())
        self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), before)
        for table in ("model_profiles", "bundles", "jobs", "runs"):
            with self.subTest(table=table):
                self.assertEqual(self.connection.execute(
                    f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_selecting_one_shard_adopts_and_hashes_the_complete_set(self):
        paths = [self.write(f"set/model-{index:05d}-of-00003.gguf", f"shard {index}".encode())
                 for index in range(1, 4)]
        before = {path.name: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                  for path in paths}

        result = self.adopt("acq-shards", paths[1], consent=True)

        self.assertTrue(result.complete, result.reason)
        files = self.adopted_files("acq-shards")
        self.assertEqual([row["file_name"] for row in files], [
            "set/model-00001-of-00003.gguf", "set/model-00002-of-00003.gguf",
            "set/model-00003-of-00003.gguf"])
        self.assertEqual([row["computed_sha256"] for row in files], [
            hashlib.sha256(before[path.name][0]).hexdigest() for path in paths])
        for path in paths:
            self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns),
                             before[path.name])

    def test_missing_shard_refuses_without_leaving_a_record(self):
        path = self.write("split/model-1-of-2.gguf", b"one")

        with self.assertRaisesRegex(adoption.AdoptionError, "incomplete"):
            self.adopt("acq-incomplete", path, consent=True)

        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-incomplete'").fetchone())

    def test_consent_is_required_before_any_record_is_written(self):
        path = self.write("local.gguf", b"weights")

        with self.assertRaisesRegex(adoption.AdoptionError, "explicit consent"):
            self.adopt("acq-no-consent", path)

        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-no-consent'").fetchone())

    def test_unconfigured_model_storage_and_origins_are_not_required(self):
        path = self.write("local.gguf", b"weights")

        result = self.adopt("acq-no-root", path, consent=True)

        self.assertTrue(result.complete, result.reason)
        self.assertIsNone(storage.model_storage(self.connection))
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM download_origins").fetchone()[0], 0)

    def test_outside_files_and_symlinks_resolving_outside_are_refused(self):
        outside = self.home / "outside.gguf"
        outside.write_bytes(b"weights")
        link = self.models / "escape.gguf"
        link.symlink_to(outside)

        for path in (outside, link):
            with self.subTest(path=path), self.assertRaises(adoption.AdoptionError):
                self.adopt("acq-outside-" + path.name, path, consent=True)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM acquisition_records").fetchone()[0], 0)

    def test_retargeted_configured_root_is_refused(self):
        first = self.home / "first"
        second = self.home / "second"
        first.mkdir()
        second.mkdir()
        (first / "local.gguf").write_bytes(b"first!")
        (second / "local.gguf").write_bytes(b"second")
        configured = self.home / "models-link"
        configured.symlink_to(first, target_is_directory=True)
        storage.add_artifact_location(self.connection, configured)
        swapped = False

        def retarget_after_insert(statement):
            nonlocal swapped
            if statement == "COMMIT" and not swapped:
                swapped = True
                configured.unlink()
                configured.symlink_to(second, target_is_directory=True)

        self.connection.set_trace_callback(retarget_after_insert)
        try:
            with self.assertRaisesRegex(adoption.AdoptionError, "configured artifact location changed"):
                self.adopt("acq-root-retarget", configured / "local.gguf", consent=True)
        finally:
            self.connection.set_trace_callback(None)

        self.assertTrue(swapped)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-root-retarget'"
        ).fetchone())

    def test_custom_store_private_work_staging_is_not_adoptable(self):
        custom_store = store.open_store(self.home / "custom-config")
        self.addCleanup(custom_store.close)
        connection = custom_store.connection()
        private = store.store_directory(self.home / "custom-config")
        staging = private / "work" / "download-1" / "partial.gguf"
        staging.parent.mkdir(parents=True)
        staging.write_bytes(b"partial")
        storage.add_artifact_location(connection, private)

        with self.assertRaisesRegex(adoption.AdoptionError, "staging"):
            adoption.adopt(connection, "acq-staging", "Staged", staging, consent=True)

        self.assertEqual(connection.execute(
            "SELECT count(*) FROM acquisition_records").fetchone()[0], 0)

    def test_symlink_retargeted_during_identity_preflight_is_refused(self):
        path = self.write("replaceable.gguf", b"inside")
        outside = self.home / "outside.gguf"
        outside.write_bytes(b"outside")
        original_stat = Path.stat
        swapped = False

        def retarget_before_stat(current, *args, **kwargs):
            nonlocal swapped
            if current == path and not swapped:
                swapped = True
                path.unlink()
                path.symlink_to(outside)
            return original_stat(current, *args, **kwargs)

        with mock.patch.object(Path, "stat", retarget_before_stat):
            with self.assertRaises(adoption.AdoptionError):
                self.adopt("acq-preflight-retarget", path, consent=True)

        self.assertTrue(swapped)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-preflight-retarget'"
        ).fetchone())

    def test_symlink_retargeted_after_preflight_is_not_hashed(self):
        path = self.write("replaceable.gguf", b"inside")
        outside = self.home / "outside.gguf"
        outside.write_bytes(b"outside")
        swapped = False

        def retarget_after_insert(statement):
            nonlocal swapped
            if statement == "COMMIT" and not swapped:
                swapped = True
                path.unlink()
                path.symlink_to(outside)

        self.connection.set_trace_callback(retarget_after_insert)
        try:
            with self.assertRaises(adoption.AdoptionError):
                self.adopt("acq-retargeted", path, consent=True)
        finally:
            self.connection.set_trace_callback(None)

        self.assertTrue(swapped)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-retargeted'").fetchone())

    def test_huge_incomplete_shard_count_is_refused_with_bounded_memory(self):
        script = '''
import resource, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from matuto import adoption, storage, store
home = Path(sys.argv[2])
opened = store.open_store(home / "config")
connection = opened.connection()
models = home / "models"
models.mkdir()
path = models / "model-1-of-999999999.gguf"
path.write_bytes(b"partial")
storage.add_artifact_location(connection, models)
limit = 128 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
try:
    adoption.adopt(connection, "acq-huge-count", "Huge count", path, consent=True)
except adoption.AdoptionError:
    print("refused")
except MemoryError:
    print("memory-error")
else:
    print("accepted")
opened.close()
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(AGENTS), str(self.home / "bounded")],
            text=True, capture_output=True, check=True)

        self.assertEqual(result.stdout.strip(), "refused", result.stderr)

    def test_failed_hash_does_not_leave_an_acquisition_record(self):
        path = self.write("local.gguf", b"weights")
        with mock.patch("matuto.verification._read_digest", return_value=None):
            with self.assertRaisesRegex(adoption.AdoptionError, "could not be read"):
                self.adopt("acq-unreadable", path, consent=True)

        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM acquisition_records WHERE acquisition_id = 'acq-unreadable'").fetchone())


if __name__ == "__main__":
    unittest.main()
