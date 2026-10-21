"""One acquisition record's identity: hash the whole set, judge it, verify at launch.

Every rule is exercised through `matuto.verification`'s Python API against the
store module's own connection, with fixture shards in a temporary directory. The
records here exist before any profile does, because a full-set Verify is
available on every acquisition record.
"""

import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import store as store_module  # noqa: E402  the package is not on the path yet
from matuto import verification  # noqa: E402

NOW = "2026-10-09T16:00:00+00:00"
MOMENT = datetime.datetime.fromisoformat(NOW)
SHARDS = ("model-00001-of-00003.gguf", "model-00002-of-00003.gguf", "model-00003-of-00003.gguf")
SIDECAR = "mmproj-f16.gguf"
# One record the publisher shipped a digest for, one it shipped none for.
PUBLISHED_RECORD = "acq-shards"
ADOPTED_RECORD = "acq-adopted"


class FailingConnection:
    """The store's connection with one statement's write refused, to interrupt a verification."""

    def __init__(self, connection, failing_sql):
        self._connection = connection
        self._failing_sql = failing_sql

    def execute(self, sql, *arguments):
        if self._failing_sql in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._connection.execute(sql, *arguments)

    def executemany(self, sql, arguments):
        if self._failing_sql in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._connection.executemany(sql, arguments)


class HookedConnection:
    """The store's connection with a hook run just after the file rows are read."""

    def __init__(self, connection, after_rows):
        self._connection = connection
        self._after_rows = after_rows

    def execute(self, sql, *arguments):
        # Read to the end before the hook runs, so this verification holds no
        # open statement while the other Matuto writes.
        rows = self._connection.execute(sql, *arguments).fetchall()
        if sql.lstrip().startswith("SELECT file_name"):
            self._after_rows()
        return Rows(rows)

    def executemany(self, sql, arguments):
        return self._connection.executemany(sql, arguments)


class Rows:
    """Rows already read, so a caller can fetch them again after a hook ran."""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class VerificationTestCase(unittest.TestCase):
    """A temporary store, one sharded model root and two records over it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.models = self.home / "models"
        self.models.mkdir()
        self.store = store_module.open_store(self.home / "config")
        self.addCleanup(self.store.close)
        self.connection = self.store.connection()
        self._fill()

    def file(self, name, body):
        path = self.models / name
        path.write_bytes(body)
        return path

    def alter(self, name):
        """Other bytes of exactly the same size, so a wrong size is not what is caught."""
        path = self.models / name
        path.write_bytes(b"x" * path.stat().st_size)

    def digest(self, name):
        return hashlib.sha256((self.models / name).read_bytes()).hexdigest()

    def rows(self, acquisition_id=PUBLISHED_RECORD):
        return {row["file_name"]: dict(row) for row in self.connection.execute(
            "SELECT * FROM acquisition_files WHERE acquisition_id = ?", (acquisition_id,))}

    def record(self, acquisition_id=PUBLISHED_RECORD):
        return dict(self.connection.execute(
            "SELECT * FROM acquisition_records WHERE acquisition_id = ?",
            (acquisition_id,)).fetchone())

    def failure_reason(self, acquisition_id=PUBLISHED_RECORD):
        row = self.connection.execute(
            "SELECT reason FROM acquisition_verification_failures WHERE acquisition_id = ?",
            (acquisition_id,)).fetchone()
        return row["reason"] if row else ""

    def verify(self, acquisition_id=PUBLISHED_RECORD, **kwargs):
        kwargs.setdefault("now", MOMENT)
        return verification.verify_record(self.connection, acquisition_id, **kwargs)

    def insert_profile(self, profile_id="prof-coder", acquisition_id=PUBLISHED_RECORD):
        """The profile the launch check reads its first model file and sidecars from."""
        self.connection.execute(
            "INSERT INTO model_profiles VALUES (?, 'language', 'Coder', 'coder', 131072,"
            " 'high', NULL, NULL, 0, '', ?)", (profile_id, NOW))
        for role, name in (("model", SHARDS[0]), ("mmproj", SIDECAR)):
            self.connection.execute(
                "INSERT INTO profile_artifacts VALUES (?, ?, ?, ?)",
                (profile_id, role, acquisition_id, name))

    def card_and_catalogue(self):
        """The card and argument row a language profile document needs to reach its pick."""
        self.connection.execute(
            "INSERT INTO cards VALUES ('card-r9', 'Radeon', '0000:03:00.0', 34359738368,"
            f" 99, 16, 4096, 512, 5, '', '{NOW}')")
        self.connection.execute(
            "INSERT INTO argument_catalogue VALUES ('arg-parallel', '--parallel', 1,"
            " 'parallel slots', 'server', '')")

    def language_document(self, acquisition_id):
        return {"kind": "language", "profile_id": "prof-x", "name": "x", "alias": "x",
                "context_size": 131072, "thinking": "high", "card_id": "card-r9", "notes": "",
                "arguments": [{"tool": "server", "catalogue": "--parallel", "value": "1"}],
                "model": {"acquisition_id": acquisition_id, "file_name": SHARDS[0]}}

    def _fill(self):
        for name in SHARDS:
            self.file(name, name.encode())
        self.file(SIDECAR, b"projector")
        # What a download leaves behind: every shard hashed on arrival, the
        # sidecar's published checksum absent, no verification time yet.
        rows = ""
        for acquisition_id, published in ((PUBLISHED_RECORD, True), (ADOPTED_RECORD, False)):
            files = ""
            for name in SHARDS + (SIDECAR,):
                size = (self.models / name).stat().st_size
                expected = repr(self.digest(name)) if published else "NULL"
                files += (f"INSERT INTO acquisition_files VALUES"
                          f" ('{acquisition_id}', '{name}', {size}, {expected}, '', NULL);\n")
            status = "'published-checksum'" if published else "'locally-hashed'"
            rows += (f"INSERT INTO acquisition_records VALUES ('{acquisition_id}',"
                     f" '{acquisition_id}', 'download', {status}, NULL,"
                     f" 'unsloth/Sharded-GGUF', 'a483e9e6', 'loc-models', NULL, '',"
                     f" '{NOW}');\n{files}")
        self.connection.executescript(
            f"""
            INSERT INTO artifact_locations VALUES ('loc-models', '{self.models}', '', '{NOW}');
            {rows}""")


class CompleteSetTest(VerificationTestCase):
    def test_a_complete_set_records_every_field_of_every_required_file(self):
        result = self.verify()

        self.assertTrue(result.complete, result.reason)
        self.assertEqual(result.checksum_status, verification.PUBLISHED)
        self.assertEqual(result.label, verification.LABEL_PUBLISHED)
        self.assertEqual(result.verified_at, NOW)
        self.assertEqual(len(result.files), 4)
        for name in SHARDS + (SIDECAR,):
            with self.subTest(file=name):
                row = self.rows()[name]
                self.assertEqual(row["size_bytes"], (self.models / name).stat().st_size)
                self.assertEqual(row["published_sha256"], self.digest(name))
                self.assertEqual(row["computed_sha256"], self.digest(name))
                self.assertEqual(row["verified_at"], NOW)
        record = self.record()
        self.assertEqual(record["verified_at"], NOW)
        self.assertEqual(record["checksum_status"], verification.PUBLISHED)

    def test_every_shard_and_sidecar_is_hashed_not_only_the_first_file(self):
        result = self.verify()

        self.assertTrue(result.complete, result.reason)
        self.assertEqual({check.file_name: check.computed_sha256 for check in result.files},
                         {name: self.digest(name) for name in SHARDS + (SIDECAR,)})

    def test_a_file_larger_than_one_block_is_hashed_whole(self):
        body = b"shard" * (verification.BLOCK // 5 + 10)
        self.file("big.gguf", body)
        self.connection.execute(
            "INSERT INTO acquisition_files VALUES (?, 'big.gguf', ?, ?, '', NULL)",
            (ADOPTED_RECORD, len(body), hashlib.sha256(body).hexdigest()))

        result = self.verify(ADOPTED_RECORD, consent=True)

        self.assertTrue(result.complete, result.reason)
        self.assertEqual(self.rows(ADOPTED_RECORD)["big.gguf"]["computed_sha256"],
                         hashlib.sha256(body).hexdigest())

    def test_a_set_without_published_checksums_is_complete_only_after_consent(self):
        without = self.verify(ADOPTED_RECORD)
        self.assertFalse(without.complete)
        self.assertIn(SIDECAR, without.reason)
        self.assertIn(verification.LABEL_NO_PUBLISHED, without.reason)
        self.assertIsNone(self.record(ADOPTED_RECORD)["verified_at"])

        consented = self.verify(ADOPTED_RECORD, consent=True)
        self.assertTrue(consented.complete, consented.reason)
        self.assertEqual(consented.label, verification.LABEL_NO_PUBLISHED)
        self.assertEqual(consented.checksum_status, verification.LOCALLY_HASHED)
        record = self.record(ADOPTED_RECORD)
        self.assertEqual(record["checksum_status"], verification.LOCALLY_HASHED)
        self.assertEqual(record["verified_at"], NOW)
        self.assertIsNone(self.rows(ADOPTED_RECORD)[SIDECAR]["published_sha256"])
        self.assertEqual(self.rows(ADOPTED_RECORD)[SIDECAR]["computed_sha256"],
                         self.digest(SIDECAR))

    def test_a_record_the_operator_already_consented_to_stays_complete(self):
        self.verify(ADOPTED_RECORD, consent=True)

        again = self.verify(ADOPTED_RECORD)

        self.assertTrue(again.complete, again.reason)
        self.assertEqual(again.label, verification.LABEL_NO_PUBLISHED)

    def test_verify_runs_on_a_record_no_profile_uses(self):
        result = self.verify()

        self.assertTrue(result.complete, result.reason)
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM model_profiles").fetchone()[0], 0)

    def test_an_unknown_record_is_refused(self):
        with self.assertRaises(verification.VerificationError) as raised:
            self.verify("acq-missing")
        self.assertIn("acq-missing", str(raised.exception))


class UnusableSetTest(VerificationTestCase):
    def assertUnusable(self, result, name, state):
        self.assertFalse(result.complete)
        self.assertIn(name, result.reason)
        self.assertEqual(next(check.state for check in result.files
                              if check.file_name == name), state)
        return result

    def test_a_missing_shard_leaves_the_record_unusable_naming_the_file(self):
        (self.models / SHARDS[2]).unlink()

        self.assertUnusable(self.verify(), SHARDS[2], verification.MISSING)
        self.assertIsNone(self.record()["verified_at"])
        self.assertEqual(self.rows()[SHARDS[2]]["computed_sha256"], "")
        self.assertIsNone(self.rows()[SHARDS[2]]["verified_at"])

    def test_a_wrong_size_leaves_the_record_unusable_naming_the_file(self):
        self.connection.execute(
            "UPDATE acquisition_files SET size_bytes = 3 WHERE file_name = ?", (SHARDS[1],))

        self.assertUnusable(self.verify(), SHARDS[1], verification.WRONG_SIZE)
        self.assertIsNone(self.record()["verified_at"])

    def test_a_mismatched_shard_leaves_the_record_unusable_naming_the_file(self):
        self.alter(SHARDS[1])

        self.assertUnusable(self.verify(), SHARDS[1], verification.MISMATCHED)
        self.assertIsNone(self.record()["verified_at"])

    def test_a_discovered_mismatch_blocks_selection_and_launch_while_keeping_the_record(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.insert_profile(acquisition_id=ADOPTED_RECORD)
        recorded = self.rows(ADOPTED_RECORD)[SHARDS[2]]["computed_sha256"]
        self.alter(SHARDS[2])

        self.assertUnusable(self.verify(ADOPTED_RECORD, consent=True), SHARDS[2],
                            verification.CHANGED)

        # The record keeps what it recorded, and stops being selectable and
        # launchable, because a discovered mismatch blocks use.
        self.assertEqual(self.rows(ADOPTED_RECORD)[SHARDS[2]]["computed_sha256"], recorded)
        self.assertEqual(self.rows(ADOPTED_RECORD)[SHARDS[2]]["verified_at"], NOW)
        self.assertIsNone(self.record(ADOPTED_RECORD)["verified_at"])
        self.card_and_catalogue()
        with self.assertRaises(store_module.RecordRefusal) as raised:
            store_module.validate_record(self.language_document(ADOPTED_RECORD), self.connection)
        self.assertIn("incomplete acquisition record", str(raised.exception))
        ok, reason = verification.launch_check(self.connection, "prof-coder")
        self.assertFalse(ok)
        self.assertIn("no verification has completed", reason)

    def test_consent_never_applies_to_a_file_whose_checksum_is_known_and_mismatched(self):
        self.alter(SHARDS[0])
        published = self.rows()[SHARDS[0]]["published_sha256"]

        result = self.assertUnusable(self.verify(consent=True), SHARDS[0],
                                     verification.MISMATCHED)

        self.assertEqual(self.rows()[SHARDS[0]]["published_sha256"], published)
        self.assertEqual(self.rows()[SHARDS[0]]["computed_sha256"], "")
        self.assertIsNone(self.record()["verified_at"])

    def test_a_record_with_no_required_file_is_unusable(self):
        for name in SHARDS + (SIDECAR,):
            self.connection.execute(
                "DELETE FROM acquisition_files WHERE file_name = ?", (name,))

        result = self.verify()

        self.assertFalse(result.complete)
        self.assertIn("no required file", result.reason)


class UnreadableFileTest(VerificationTestCase):
    def test_interrupted_reverify_preserves_failure_and_recovery_clears_it(self):
        self.assertTrue(self.verify().complete)
        last_success = self.record()["verified_at"]
        recorded_files = self.rows()
        changed = (self.models / SHARDS[1]).read_bytes()
        self.alter(SHARDS[1])

        mismatch = self.verify()

        self.assertFalse(mismatch.complete)
        self.assertIn(SHARDS[1], mismatch.reason)
        self.assertIsNone(self.record()["verified_at"])
        self.assertEqual(self.failure_reason(), mismatch.reason)
        self.assertEqual(self.rows()[SHARDS[0]]["verified_at"], last_success)
        self.assertEqual({name: (row["computed_sha256"], row["verified_at"])
                          for name, row in self.rows().items()},
                         {name: (row["computed_sha256"], row["verified_at"])
                          for name, row in recorded_files.items()})

        read_digest = verification._read_digest

        def interrupted(path, **kwargs):
            if path.name == SHARDS[0]:
                raise KeyboardInterrupt("the operator stopped Verify")
            return read_digest(path, **kwargs)

        with mock.patch.object(verification, "_read_digest", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.verify()
        self.assertIsNone(self.record()["verified_at"])
        self.assertEqual(self.failure_reason(), mismatch.reason)
        self.assertEqual(self.rows()[SHARDS[1]]["computed_sha256"], recorded_files[SHARDS[1]]["computed_sha256"])

        self.file(SHARDS[1], changed)
        self.assertTrue(self.verify().complete)
        self.assertEqual(self.failure_reason(), "")

    def test_a_file_changed_while_it_is_hashed_is_refused(self):
        inode = (self.models / SHARDS[1]).stat().st_ino
        real_fstat = os.fstat

        def changed_while_read(descriptor):
            status = real_fstat(descriptor)
            if status.st_ino != inode:
                return status
            # The stat after the read reports a file that grew under it.
            return os.stat_result(tuple(status)[:6] + (status.st_size + 1,)
                                  + tuple(status)[7:])

        with mock.patch.object(verification.os, "fstat", changed_while_read):
            result = self.verify()

        self.assertFalse(result.complete)
        self.assertIn(SHARDS[1], result.reason)
        self.assertIn("read whole", result.reason)
        self.assertIsNone(self.record()["verified_at"])
        self.assertEqual(self.rows()[SHARDS[1]]["computed_sha256"], "")

    def test_an_unreadable_file_is_refused(self):
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            result = self.verify()

        self.assertFalse(result.complete)
        self.assertIn("read whole", result.reason)
        self.assertIsNone(self.record()["verified_at"])

    def test_an_interrupted_verification_records_nothing(self):
        read_digest = verification._read_digest

        def interrupted(path, expected_fd=None, on_progress=None):
            if path.name == SHARDS[2]:
                raise KeyboardInterrupt("the operator stopped the Verify")
            return read_digest(path, expected_fd=expected_fd, on_progress=on_progress)

        with mock.patch.object(verification, "_read_digest", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.verify()

        self.assertIsNone(self.record()["verified_at"])
        for name, row in self.rows().items():
            self.assertEqual(row["computed_sha256"], "", name)
            self.assertIsNone(row["verified_at"], name)

    def test_a_write_that_fails_records_nothing(self):
        failing = FailingConnection(self.connection, "UPDATE acquisition_records")

        with self.assertRaises(sqlite3.OperationalError):
            verification.verify_record(failing, PUBLISHED_RECORD, now=MOMENT)

        self.assertIsNone(self.record()["verified_at"])
        self.assertEqual(self.record()["checksum_status"], "published-checksum")
        for name, row in self.rows().items():
            self.assertEqual(row["computed_sha256"], "", name)
            self.assertIsNone(row["verified_at"], name)

    def test_an_overlapping_verify_cannot_overwrite_a_newer_recorded_digest(self):
        second = store_module.open_store(self.home / "config")
        self.addCleanup(second.close)
        original = verification._record_results
        raced = []

        def another_matuto_first(connection, *arguments):
            # A second Matuto verifies and records the same record while this one
            # is between its hashes and its write, over different bytes.
            if not raced:
                raced.append(True)
                self.alter(SHARDS[0])
                verification.verify_record(second.connection(), ADOPTED_RECORD,
                                           consent=True, now=MOMENT)
            return original(connection, *arguments)

        with mock.patch.object(verification, "_record_results", another_matuto_first):
            result = self.verify(ADOPTED_RECORD, consent=True)

        self.assertFalse(result.complete)
        self.assertIn("changed while it was being verified", result.reason)
        self.assertEqual(self.rows(ADOPTED_RECORD)[SHARDS[0]]["computed_sha256"],
                         self.digest(SHARDS[0]))
        self.assertEqual(self.record(ADOPTED_RECORD)["verified_at"], NOW)

    def test_a_verify_cannot_adopt_a_newer_identity_as_its_own_baseline(self):
        second = store_module.open_store(self.home / "config")
        self.addCleanup(second.close)
        original = (self.models / SHARDS[0]).read_bytes()
        raced = []

        def another_matuto_verifies_first():
            # Another Matuto verifies different bytes and records them between
            # this verification's read of the rows it judges and its hashing,
            # and the bytes on disk return to what they were.
            if not raced:
                raced.append(True)
                self.file(SHARDS[0], b"y" * len(original))
                verification.verify_record(second.connection(), ADOPTED_RECORD,
                                           consent=True, now=MOMENT)
                self.file(SHARDS[0], original)

        hooked = HookedConnection(self.connection, another_matuto_verifies_first)
        result = verification.verify_record(hooked, ADOPTED_RECORD, consent=True, now=MOMENT)

        self.assertFalse(result.complete)
        self.assertIn("changed while it was being verified", result.reason)
        # The newer verification's digest stands: this one judged an older set
        # and refuses rather than recording over an established identity.
        self.assertEqual(self.rows(ADOPTED_RECORD)[SHARDS[0]]["computed_sha256"],
                         hashlib.sha256(b"y" * len(original)).hexdigest())

    def test_a_verify_that_hashed_before_a_replacement_cannot_recomplete_the_old_identity(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.insert_profile(acquisition_id=ADOPTED_RECORD)
        recorded = self.rows(ADOPTED_RECORD)[SHARDS[2]]["computed_sha256"]
        second = store_module.open_store(self.home / "config")
        self.addCleanup(second.close)
        original = verification._record_results
        raced = []

        def another_matuto_replaces_first(connection, *arguments):
            # Another Matuto ends this identity while this verification is
            # between its hashes and its write.
            if not raced:
                raced.append(True)
                self.alter(SHARDS[2])
                verification.accept_replacement(
                    second.connection(), ADOPTED_RECORD, SHARDS[2], "acq-adopted-r1", now=MOMENT)
            return original(connection, *arguments)

        with mock.patch.object(verification, "_record_results", another_matuto_replaces_first):
            result = self.verify(ADOPTED_RECORD, consent=True)

        self.assertFalse(result.complete)
        self.assertIn("changed while it was being verified", result.reason)
        self.assertEqual(self.rows(ADOPTED_RECORD)[SHARDS[2]]["computed_sha256"], recorded)
        self.assertIsNone(self.record(ADOPTED_RECORD)["verified_at"])
        ok, reason = verification.launch_check(self.connection, "prof-coder")
        self.assertFalse(ok, reason)


class ReplacementTest(VerificationTestCase):
    def test_accepting_replacement_bytes_creates_a_new_identity_beside_the_old_one(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.insert_profile(acquisition_id=ADOPTED_RECORD)
        self.alter(SHARDS[2])
        replacement_body = (self.models / SHARDS[2]).read_bytes()

        replacement = verification.accept_replacement(
            self.connection, ADOPTED_RECORD, SHARDS[2], "acq-adopted-r1", now=MOMENT)

        self.assertEqual(replacement, "acq-adopted-r1")
        new = self.rows("acq-adopted-r1")
        self.assertEqual(new[SHARDS[2]]["computed_sha256"], self.digest(SHARDS[2]))
        self.assertEqual(new[SHARDS[2]]["size_bytes"], len(replacement_body))
        self.assertEqual(new[SHARDS[0]]["computed_sha256"], self.digest(SHARDS[0]))
        self.assertIsNone(new[SIDECAR]["published_sha256"])
        self.assertEqual(self.record("acq-adopted-r1")["revision"], "a483e9e6")
        # The new identity records no verification of its own until one is run.
        self.assertIsNone(self.record("acq-adopted-r1")["verified_at"])
        self.assertTrue(self.verify("acq-adopted-r1", consent=True).complete)
        # Profiles keep the old identity, whose bytes no longer match what it
        # recorded, so it is no longer selectable.
        self.assertEqual(self.connection.execute(
            "SELECT acquisition_id FROM profile_artifacts WHERE role = 'model'"
        ).fetchone()[0], ADOPTED_RECORD)
        old = self.verify(ADOPTED_RECORD, consent=True)
        self.assertFalse(old.complete)
        self.assertIn(SHARDS[2], old.reason)
        self.assertIsNone(self.record(ADOPTED_RECORD)["verified_at"])

    def test_replacement_bytes_contradicting_a_published_checksum_are_refused(self):
        self.verify()
        self.alter(SHARDS[0])

        with self.assertRaises(verification.VerificationError) as raised:
            verification.accept_replacement(
                self.connection, PUBLISHED_RECORD, SHARDS[0], "acq-shards-r1", now=MOMENT)

        self.assertIn("contradicts the published checksum", str(raised.exception))
        self.assertIsNone(self.connection.execute(
            "SELECT acquisition_id FROM acquisition_records WHERE acquisition_id ="
            " 'acq-shards-r1'").fetchone())
        self.assertEqual(self.record()["verified_at"], NOW)

    def test_the_old_identity_is_unselectable_to_a_new_profile_pick(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.insert_profile(acquisition_id=ADOPTED_RECORD)
        self.alter(SHARDS[0])
        verification.accept_replacement(
            self.connection, ADOPTED_RECORD, SHARDS[0], "acq-adopted-r1", now=MOMENT)
        self.verify("acq-adopted-r1", consent=True)
        self.card_and_catalogue()

        with self.assertRaises(store_module.RecordRefusal) as raised:
            store_module.validate_record(
                self.language_document(ADOPTED_RECORD), self.connection)
        self.assertIn("incomplete acquisition record", str(raised.exception))
        store_module.validate_record(self.language_document("acq-adopted-r1"), self.connection)

    def test_an_old_profile_cannot_launch_after_a_later_shard_is_replaced(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.insert_profile(acquisition_id=ADOPTED_RECORD)
        self.alter(SHARDS[2])
        verification.accept_replacement(
            self.connection, ADOPTED_RECORD, SHARDS[2], "acq-adopted-r1", now=MOMENT)

        # The files this launch reads are intact; the old identity is not.
        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertFalse(ok)
        self.assertIn("no verification has completed", reason)

    def test_replacement_bytes_the_publisher_also_published_are_accepted(self):
        self.verify()
        # The stored digest is stale, but these bytes are what the publisher
        # published, so they are a replacement rather than a contradiction.
        self.connection.execute(
            "UPDATE acquisition_files SET computed_sha256 = ? WHERE file_name = ?",
            ("f" * 64, SHARDS[0]))

        verification.accept_replacement(
            self.connection, PUBLISHED_RECORD, SHARDS[0], "acq-shards-r1", now=MOMENT)

        row = self.rows("acq-shards-r1")[SHARDS[0]]
        self.assertEqual(row["computed_sha256"], self.digest(SHARDS[0]))
        self.assertEqual(row["published_sha256"], self.digest(SHARDS[0]))
        self.assertTrue(self.verify("acq-shards-r1").complete)

    def test_bytes_that_still_match_their_recorded_digest_are_no_replacement(self):
        self.verify(ADOPTED_RECORD, consent=True)

        with self.assertRaises(verification.VerificationError) as raised:
            verification.accept_replacement(
                self.connection, ADOPTED_RECORD, SHARDS[0], "acq-adopted-r1", now=MOMENT)

        self.assertIn("nothing to replace", str(raised.exception))

    def test_a_replacement_id_already_in_use_is_refused(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.alter(SHARDS[0])

        with self.assertRaises(verification.VerificationError) as raised:
            verification.accept_replacement(
                self.connection, ADOPTED_RECORD, SHARDS[0], ADOPTED_RECORD, now=MOMENT)

        self.assertIn("already names", str(raised.exception))

    def test_an_unreadable_replacement_file_is_refused(self):
        self.verify(ADOPTED_RECORD, consent=True)
        self.alter(SHARDS[0])
        (self.models / SHARDS[0]).unlink()

        with self.assertRaises(verification.VerificationError) as raised:
            verification.accept_replacement(
                self.connection, ADOPTED_RECORD, SHARDS[0], "acq-adopted-r1", now=MOMENT)

        self.assertIn("read whole", str(raised.exception))


class LaunchCheckTest(VerificationTestCase):
    def test_the_launch_check_passes_an_intact_first_file_and_sidecars(self):
        self.verify()
        self.insert_profile()

        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_the_launch_check_refuses_an_altered_first_file(self):
        self.verify()
        self.insert_profile()
        self.alter(SHARDS[0])

        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertFalse(ok)
        self.assertIn(SHARDS[0], reason)

    def test_the_launch_check_refuses_an_altered_sidecar(self):
        self.verify()
        self.insert_profile()
        self.alter(SIDECAR)

        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertFalse(ok)
        self.assertIn(SIDECAR, reason)

    def test_the_launch_check_refuses_a_missing_file(self):
        self.verify()
        self.insert_profile()
        (self.models / SIDECAR).unlink()

        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertFalse(ok)
        self.assertIn(SIDECAR, reason)

    def test_the_launch_check_reads_no_other_shard(self):
        self.verify()
        self.insert_profile()
        # Only the picked files are hashed, so altered later shards do not block it.
        for name in SHARDS[1:]:
            self.alter(name)

        ok, reason = verification.launch_check(self.connection, "prof-coder")

        self.assertTrue(ok, reason)

    def test_the_launch_check_of_an_unknown_profile_is_refused(self):
        ok, reason = verification.launch_check(self.connection, "prof-missing")

        self.assertFalse(ok)
        self.assertIn("prof-missing", reason)


if __name__ == "__main__":
    unittest.main()
