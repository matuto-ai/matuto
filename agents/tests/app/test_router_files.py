"""Router presets are generated from the store at its Python API seam."""

import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission  # noqa: E402
from matuto import live_records  # noqa: E402
from matuto import serving_state  # noqa: E402
from matuto import store  # noqa: E402
from matuto import storage  # noqa: E402


class RouterFilesTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.db = store.open_store(self.config)
        self.addCleanup(self.db.close)
        self.connection = self.db.connection()
        self.models = self.home / "models"
        self.outside = self.home / "adopted"
        self.models.mkdir(mode=0o700)
        self.outside.mkdir(mode=0o700)
        self.key = self.db.path.parent.parent / "pi-local" / "api-key"
        self.key.parent.mkdir(mode=0o700)
        self.key.write_bytes(b"A" * 32 + b"\n")
        self.key.chmod(0o600)
        self.old_preset = self.db.path.parent / "presets.ini"
        self.old_preset.write_text("checkout-manager preset\\n")
        # These saves regenerate files for a store Matuto is not serving from:
        # what is serving is settled by test_live_records over a fake systemctl
        # and a fake router, and here it is stopped.
        stopped = mock.patch.object(live_records, "read_state",
                                    return_value=serving_state.State(admission.STOPPED))
        stopped.start()
        self.addCleanup(stopped.stop)
        self._seed()

    def _seed(self):
        c = self.connection
        c.execute("INSERT INTO cards VALUES ('card', 'Card', 'gfx1201', 1000, 99, 8, 16, 4, 1, '', 'now')")
        for acquisition, root, name, body in (
                ("inside", "loc-models", "model.gguf", b"model"),
                ("outside", "loc-extra", "adopted.gguf", b"adopted"),):
            directory = self.models if acquisition == "inside" else self.outside
            (directory / name).write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            c.execute("INSERT INTO artifact_locations VALUES (?, ?, '', 'now')",
                      (root, str(directory)))
            c.execute("INSERT INTO acquisition_records VALUES (?, ?, 'adoption', 'locally-hashed', NULL, NULL, NULL, ?, 'now', '', 'now')",
                      (acquisition, acquisition, root))
            c.execute("INSERT INTO acquisition_files VALUES (?, ?, ?, NULL, ?, 'now')",
                      (acquisition, name, len(body), digest))
        for name, body in (("vision.gguf", b"vision"), ("mtp.gguf", b"mtp"),
                           ("model-00001-of-00002.gguf", b"shard-1"),
                           ("model-00002-of-00002.gguf", b"shard-2")):
            (self.models / name).write_bytes(body)
            c.execute("INSERT INTO acquisition_files VALUES ('inside', ?, ?, NULL, ?, 'now')",
                      (name, len(body), hashlib.sha256(body).hexdigest()))
        c.execute("INSERT INTO model_storage VALUES (1, 'loc-models', 'now')")
        for profile, alias, acquisition, name, published in (
                ("published", "alpha", "inside", "model.gguf", 1),
                ("adopted", "beta", "outside", "adopted.gguf", 1),
                ("sharded", "gamma", "inside", "model-00001-of-00002.gguf", 1),
                ("candidate", "hidden", "inside", "model.gguf", 0)):
            c.execute("INSERT INTO model_profiles VALUES (?, 'language', ?, ?, 8192, 'high', 'card', NULL, ?, '', 'now')",
                      (profile, profile, alias, published))
            c.execute("INSERT INTO profile_arguments VALUES (?, 'server', 0, NULL, '--temp', '0.7')",
                      (profile,))
            c.execute("INSERT INTO profile_arguments VALUES (?, 'server', 1, NULL, '--parallel', '1')",
                      (profile,))
            if profile == "published":
                c.execute("INSERT INTO profile_arguments VALUES (?, 'server', 2, NULL, '--n-predict', '256')",
                          (profile,))
            c.execute("INSERT INTO profile_artifacts VALUES (?, 'model', ?, ?)",
                      (profile, acquisition, name))
        c.execute("INSERT INTO profile_artifacts VALUES ('published', 'mmproj', 'inside', 'vision.gguf')")
        c.execute("INSERT INTO profile_artifacts VALUES ('published', 'mtp', 'inside', 'mtp.gguf')")

    def test_generates_only_published_profiles_with_absolute_artifact_paths(self):
        result = store.regenerate_router_files(self.db)
        path = self.db.path.parent / "router-presets.ini"

        self.assertEqual(Path(result["presets_path"]), path)
        self.assertEqual(result["model_directory"], str(self.models))
        self.assertEqual(result["model_directory_reason"], None)
        self.assertEqual(Path(result["key_path"]), self.key)
        self.assertIsNone(result["key_reason"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = path.read_text()
        self.assertIn(f"[alpha]\nmodel = {self.models / 'model.gguf'}", text)
        self.assertIn(f"[beta]\nmodel = {self.outside / 'adopted.gguf'}", text)
        self.assertIn("ctx-size = 8192\nn-gpu-layers = 99\nn-predict = 256", text)
        self.assertIn("[beta]\nmodel = " + str(self.outside / "adopted.gguf")
                      + "\nctx-size = 8192\nn-gpu-layers = 99\nn-predict = 8192", text)
        self.assertIn(f"mmproj = {self.models / 'vision.gguf'}", text)
        self.assertIn("spec-type = draft-mtp", text)
        self.assertIn(f"spec-draft-model = {self.models / 'mtp.gguf'}", text)
        self.assertIn(f"[gamma]\nmodel = {self.models / 'model-00001-of-00002.gguf'}", text)
        self.assertIn("temp = 0.7", text)
        self.assertNotIn("[hidden]", text)
        for forbidden in ("host", "port", "api-key", "api-key-file", "models-dir",
                          "models-max", "models-preset", "alias"):
            self.assertNotIn(f"{forbidden} =", text)
        self.assertFalse(path.with_name("models.json").exists())
        self.assertEqual(self.key.read_bytes(), b"A" * 32 + b"\n")
        self.assertNotIn("A" * 32, repr(result))
        self.assertEqual([item["reason"] for item in result["profiles"]], [None, None, None])
        self.assertEqual(self.old_preset.read_text(), "checkout-manager preset\\n")

    def test_published_profile_save_regenerates_and_cosmetic_save_keeps_bytes(self):
        store.regenerate_router_files(self.db)
        path = self.db.path.parent / "router-presets.ini"
        original = path.read_bytes()
        record = store.read_record(self.db, "language", "published")
        saved, _warnings = store.save_record(self.db, record, record_id="published")
        self.assertEqual(saved["qualification"]["state"], "candidate")
        store.save_record(self.db, dict(record, notes="updated note"), record_id="published")
        self.assertEqual(path.read_bytes(), original)

        store.save_record(self.db, dict(record, context_size=16384), record_id="published")
        self.assertIn(b"ctx-size = 16384", path.read_bytes())

    def test_concurrent_saves_cannot_replace_a_newer_router_snapshot_with_an_older_one(self):
        store.regenerate_router_files(self.db)
        second_started = threading.Event()
        first_replace_started = threading.Event()
        release_first_replace = threading.Event()
        second_finished = threading.Event()
        errors = []
        replace = store._replace_router_preset

        def delayed_replace(path, text):
            if "ctx-size = 16384" in text:
                first_replace_started.set()
                if not release_first_replace.wait(5):
                    raise AssertionError("timed out waiting to release the first preset write")
            return replace(path, text)

        def save(context_size, *, second=False):
            target = store.open_store(self.config)
            try:
                if second:
                    second_started.set()
                record = store.read_record(target, "language", "published")
                store.save_record(target, dict(record, context_size=context_size),
                                  record_id="published")
            except BaseException as error:
                errors.append(error)
            finally:
                target.close()
                if second:
                    second_finished.set()

        with mock.patch.object(store, "_replace_router_preset", side_effect=delayed_replace):
            first = threading.Thread(target=save, args=(16384,))
            second = threading.Thread(target=save, args=(32768,), kwargs={"second": True})
            first.start()
            try:
                self.assertTrue(first_replace_started.wait(3))
                second.start()
                self.assertTrue(second_started.wait(3))
                self.assertFalse(second_finished.wait(0.1))
            finally:
                release_first_replace.set()
                first.join(5)
                second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(b"ctx-size = 32768", (self.db.path.parent / "router-presets.ini").read_bytes())

    def test_empty_operand_free_switch_is_preserved_in_preset(self):
        record = store.read_record(self.db, "language", "published")
        record["arguments"].append({"tool": "server", "option": "--no-warmup", "value": ""})

        saved, _warnings = store.save_record(
            self.db, record, record_id="published", help_text={"server": "--no-warmup\n"})

        self.assertIn("--no-warmup", [item.get("option") for item in saved["arguments"]])
        text = (self.db.path.parent / "router-presets.ini").read_text()
        self.assertIn("no-warmup = true", text)
        self.assertIn("[alpha]", text)

    def test_short_server_alias_preserves_its_actual_setting_and_distinct_options(self):
        record = store.read_record(self.db, "language", "published")
        record["arguments"].extend((
            {"tool": "server", "option": "-r", "value": "STOP"},
            {"tool": "server", "option": "--repeat-penalty", "value": "1.1"},
        ))

        saved, _warnings = store.save_record(
            self.db, record, record_id="published",
            help_text={"server": "-r, --reverse-prompt PROMPT\n--repeat-penalty N\n"})

        self.assertIn("--reverse-prompt", [item.get("option") for item in saved["arguments"]])
        self.assertIn("reverse-prompt = STOP", (self.db.path.parent / "router-presets.ini").read_text())
        self.assertIn("repeat-penalty = 1.1", (self.db.path.parent / "router-presets.ini").read_text())
        self.assertIn("[alpha]", (self.db.path.parent / "router-presets.ini").read_text())

    def test_positive_and_negative_short_switches_keep_their_separate_meaning(self):
        record = store.read_record(self.db, "language", "published")
        record["arguments"].append({"tool": "server", "option": "-kvo", "value": ""})

        saved, _warnings = store.save_record(
            self.db, record, record_id="published",
            help_text={"server": "-kvo, --kv-offload, -nkvo, --no-kv-offload\n"})

        self.assertIn("--kv-offload", [item.get("option") for item in saved["arguments"]])
        text = (self.db.path.parent / "router-presets.ini").read_text()
        self.assertIn("kv-offload = true", text)
        self.assertNotIn("no-kv-offload = true", text)
        self.assertIn("[alpha]", text)

    def test_missing_file_is_omitted_with_a_reason_and_discovery_never_falls_back(self):
        (self.outside / "adopted.gguf").unlink()
        self.connection.execute("DELETE FROM model_storage")
        result = store.regenerate_router_files(self.db)

        text = (self.db.path.parent / "router-presets.ini").read_text()
        self.assertIn("[alpha]", text)
        self.assertNotIn("[beta]", text)
        self.assertIn("unavailable", result["profiles"][1]["reason"])
        self.assertIsNone(result["model_directory"])
        self.assertIn("no model storage is saved", result["model_directory_reason"])

    def test_missing_required_shard_omits_section(self):
        (self.models / "model-00002-of-00002.gguf").unlink()

        result = store.router_files(self.db)

        self.assertNotIn("[gamma]", result["presets"])
        report = next(item for item in result["profiles"] if item["profile_id"] == "sharded")
        self.assertIn("model-00002-of-00002.gguf", report["reason"])

    def test_profile_save_reports_omitted_section_reason(self):
        (self.outside / "adopted.gguf").unlink()
        record = store.read_record(self.db, "language", "adopted")

        _saved, warnings = store.save_record(
            self.db, dict(record, notes="save even if file is unavailable"), record_id="adopted")

        self.assertTrue(any("adopted" in warning and "unavailable" in warning
                            for warning in warnings))
        self.assertNotIn("[beta]", (self.db.path.parent / "router-presets.ini").read_text())

    def test_storage_move_regenerates_preset_paths(self):
        store.regenerate_router_files(self.db)
        relocated = self.home / "relocated"
        shutil.copytree(self.models, relocated)

        storage.change_location(self.connection, "loc-models", relocated,
                                serving_state=admission.STOPPED)

        text = (self.db.path.parent / "router-presets.ini").read_text()
        self.assertIn(f"model = {relocated / 'model.gguf'}", text)
        self.assertIn(f"mmproj = {relocated / 'vision.gguf'}", text)
        self.assertNotIn(str(self.models / "model.gguf"), text)

    def test_unsafe_key_is_reported_without_being_rewritten_or_exposed(self):
        self.key.chmod(0o640)

        result = store.router_files(self.db)

        self.assertIn("unsafe", result["key_reason"])
        self.assertNotIn("A" * 32, repr(result))
        self.assertEqual(self.key.read_bytes(), b"A" * 32 + b"\n")

    def test_linked_key_is_refused_without_touching_its_target(self):
        self.key.unlink()
        target = self.home / "elsewhere"
        target.write_bytes(b"A" * 32 + b"\n")
        target.chmod(0o600)
        self.key.symlink_to(target)

        result = store.router_files(self.db)

        self.assertIn("link", result["key_reason"])
        self.assertEqual(target.read_bytes(), b"A" * 32 + b"\n")
        self.assertTrue(self.key.is_symlink())

    def test_duplicate_published_aliases_are_both_omitted_with_named_reasons(self):
        self.connection.execute("DROP INDEX published_alias")
        self.connection.execute("UPDATE model_profiles SET alias = 'alpha', published = 1"
                                " WHERE profile_id = 'candidate'")

        result = store.router_files(self.db)

        self.assertNotIn("[alpha]", result["presets"])
        reports = {item["profile_id"]: item["reason"] for item in result["profiles"]}
        self.assertIn("published", reports)
        self.assertIn("candidate", reports)
        self.assertIn("published", reports["published"])
        self.assertIn("candidate", reports["candidate"])

    def qualify(self, profile_id):
        profile = store.read_record(self.db, "language", profile_id)
        fingerprint = store.fingerprint(self.db, profile)
        self.connection.execute(
            "INSERT INTO qualification_records VALUES (?, 'language', ?, ?, ?, NULL, 'now')",
            (f"qualification-{profile_id}", profile_id, profile["name"], fingerprint))
        self.connection.execute(
            "INSERT INTO acceptances VALUES (?, ?, 'scope', '', 'operator', 'now', NULL)",
            (f"acceptance-{profile_id}", f"qualification-{profile_id}"))
        self.connection.commit()

    def test_published_profile_reads_always_include_computed_qualification(self):
        candidate = store.read_record(self.db, "language", "candidate")
        self.assertNotIn("qualification", candidate)
        published = store.read_record(self.db, "language", "published")
        self.assertEqual(published["qualification"]["state"], "candidate")
        reports = store.router_files(self.db)["profiles"]
        self.assertTrue(all("qualification" in item for item in reports))

        self.qualify("published")
        published = store.read_record(self.db, "language", "published")
        self.assertEqual(published["qualification"]["state"], "qualified")

    def test_publish_is_allowed_while_serving_and_already_published_is_unchanged(self):
        serving = mock.patch.object(live_records, "read_state",
                                    return_value=serving_state.State(admission.AGENTIC))
        serving.start()
        try:
            profile, _warnings = store.publish_profile(
                self.db, "candidate", confirmation="Publish candidate as not qualified")
        finally:
            serving.stop()
        self.assertTrue(profile["published"])
        before = (self.db.path.parent / "router-presets.ini").read_bytes()
        with self.assertRaisesRegex(store.StoreError, "already published"):
            store.publish_profile(self.db, "candidate")
        self.assertEqual((self.db.path.parent / "router-presets.ini").read_bytes(), before)

    def test_publish_refuses_unknown_and_non_language_profiles(self):
        with self.assertRaisesRegex(store.StoreError, "no model profile is stored"):
            store.publish_profile(self.db, "missing")
        self.connection.execute(
            "INSERT INTO artifact_locations VALUES ('speech-location', ?, '', 'now')",
            (str(self.home),))
        self.connection.execute(
            "INSERT INTO speech_executables VALUES ('speech-exe', 'synthesis', 'speech-location',"
            " '/usr/bin/synth', 'digest', '1', '', 'now')")
        self.connection.execute(
            "INSERT INTO model_profiles VALUES ('speech-profile', 'synthesis', 'Speech profile',"
            " '', NULL, NULL, 'card', 'speech-exe', 0, '', 'now')")
        self.connection.commit()
        with self.assertRaisesRegex(store.StoreError, "is a synthesis profile, not language"):
            store.publish_profile(self.db, "speech-profile")
        self.assertEqual(self.connection.execute(
            "SELECT published FROM model_profiles WHERE profile_id = 'speech-profile'").fetchone()[0], 0)

    def test_publish_requires_candidate_confirmation_and_regenerates_as_one_change(self):
        with self.assertRaisesRegex(store.StoreError, "not qualified"):
            store.publish_profile(self.db, "candidate")
        self.assertFalse(store.read_record(self.db, "language", "candidate")["published"])

        profile, warnings = store.publish_profile(
            self.db, "candidate", confirmation="Publish candidate as not qualified")

        self.assertTrue(profile["published"])
        self.assertEqual(profile["qualification"]["state"], "candidate")
        self.assertFalse(warnings)
        self.assertIn("[hidden]", (self.db.path.parent / "router-presets.ini").read_text())
        self.assertTrue(store.read_record(self.db, "language", "published")["published"])

    def test_publish_uses_current_qualification_and_qualified_profiles_need_no_confirmation(self):
        self.qualify("candidate")
        profile = store.read_record(self.db, "language", "candidate")
        self.assertEqual(store.qualification_state(self.db, profile)["state"], "qualified")

        # The served fingerprint changes after the caller's read, so qualification
        # must be recomputed under the publication write lock.
        edited = dict(profile, context_size=16384)
        store.save_record(self.db, edited, record_id="candidate")
        with self.assertRaisesRegex(store.StoreError, "not qualified"):
            store.publish_profile(self.db, "candidate")

        source = store.read_record(self.db, "language", "candidate")
        store.save_record(self.db, dict(source, profile_id="qualified", name="Qualified",
                                         alias="qualified"))
        self.qualify("qualified")
        qualified, _warnings = store.publish_profile(self.db, "qualified")
        self.assertTrue(qualified["published"])
        self.assertEqual(qualified["qualification"]["state"], "qualified")

    def test_publish_alias_collision_names_holder_and_changes_nothing(self):
        candidate = store.read_record(self.db, "language", "candidate")
        store.save_record(self.db, dict(candidate, alias="alpha"), record_id="candidate")
        with self.assertRaises(store.RecordRefusal) as raised:
            store.publish_profile(
                self.db, "candidate", confirmation="Publish candidate as not qualified")
        self.assertIn("published", str(raised.exception))
        self.assertFalse(store.read_record(self.db, "language", "candidate")["published"])
        self.assertTrue(store.read_record(self.db, "language", "published")["published"])

    def test_failed_publish_regeneration_rolls_back_publication_and_preset(self):
        path = self.db.path.parent / "router-presets.ini"
        store.regenerate_router_files(self.db)
        previous = path.read_bytes()

        with mock.patch.object(store, "_replace_router_preset", side_effect=store.StoreError("disk error")):
            with self.assertRaisesRegex(store.StoreError, "disk error"):
                store.publish_profile(
                    self.db, "candidate", confirmation="Publish candidate as not qualified")

        self.assertFalse(store.read_record(self.db, "language", "candidate")["published"])
        self.assertEqual(path.read_bytes(), previous)

    def test_unpublish_preserves_qualification_and_refuses_live_or_repeated_requests(self):
        self.qualify("published")
        store.regenerate_router_files(self.db)
        before = (self.db.path.parent / "router-presets.ini").read_bytes()
        live = mock.patch.object(live_records, "read_state",
                                 return_value=serving_state.State(admission.AGENTIC))
        live.start()
        try:
            with self.assertRaisesRegex(store.RecordRefusal, "Stop serving"):
                store.unpublish_profile(self.db, "published")
        finally:
            live.stop()
        self.assertTrue(store.read_record(self.db, "language", "published")["published"])
        self.assertEqual((self.db.path.parent / "router-presets.ini").read_bytes(), before)

        profile, warnings = store.unpublish_profile(self.db, "published")
        self.assertFalse(profile["published"])
        self.assertEqual(store.qualification_state(self.db, profile)["state"], "qualified")
        self.assertFalse(warnings)
        self.assertNotIn("[alpha]", (self.db.path.parent / "router-presets.ini").read_text())
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM qualification_records WHERE record_id = 'published'").fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM acceptances WHERE acceptance_id = 'acceptance-published'").fetchone()[0], 1)
        with self.assertRaisesRegex(store.StoreError, "nothing to unpublish"):
            store.unpublish_profile(self.db, "published")

    def test_publish_failure_after_rename_restores_the_previous_preset(self):
        path = self.db.path.parent / "router-presets.ini"
        store.regenerate_router_files(self.db)
        previous = path.read_bytes()
        real_fsync = os.fsync
        directory_failures = 0

        def fail_first_directory_fsync(descriptor):
            nonlocal directory_failures
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not directory_failures:
                directory_failures += 1
                raise OSError("injected directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(store.os, "fsync", side_effect=fail_first_directory_fsync):
            with self.assertRaisesRegex(store.StoreError, "cannot atomically write"):
                store.publish_profile(
                    self.db, "candidate", confirmation="Publish candidate as not qualified")

        self.assertEqual(directory_failures, 1)
        self.assertFalse(store.read_record(self.db, "language", "candidate")["published"])
        self.assertEqual(path.read_bytes(), previous)

    def test_publish_commit_failure_restores_previous_preset(self):
        path = self.db.path.parent / "router-presets.ini"
        store.regenerate_router_files(self.db)
        previous = path.read_bytes()
        reader = sqlite3.connect(self.db.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM model_profiles").fetchall()
        self.connection.execute("PRAGMA busy_timeout = 20")

        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            store.publish_profile(
                self.db, "candidate", confirmation="Publish candidate as not qualified")

        self.assertFalse(self.connection.in_transaction)
        self.assertFalse(store.read_record(self.db, "language", "candidate")["published"])
        self.assertEqual(path.read_bytes(), previous)
        reader.rollback()

    def test_unpublish_commit_failure_restores_previous_preset(self):
        path = self.db.path.parent / "router-presets.ini"
        store.regenerate_router_files(self.db)
        previous = path.read_bytes()
        reader = sqlite3.connect(self.db.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM model_profiles").fetchall()
        self.connection.execute("PRAGMA busy_timeout = 20")

        with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
            store.unpublish_profile(self.db, "published")

        self.assertFalse(self.connection.in_transaction)
        self.assertTrue(store.read_record(self.db, "language", "published")["published"])
        self.assertEqual(path.read_bytes(), previous)
        reader.rollback()

    def test_failed_unpublish_regeneration_rolls_back_publication_and_preset(self):
        path = self.db.path.parent / "router-presets.ini"
        store.regenerate_router_files(self.db)
        previous = path.read_bytes()
        with mock.patch.object(store, "_replace_router_preset", side_effect=store.StoreError("disk error")):
            with self.assertRaisesRegex(store.StoreError, "disk error"):
                store.unpublish_profile(self.db, "published")
        self.assertTrue(store.read_record(self.db, "language", "published")["published"])
        self.assertEqual(path.read_bytes(), previous)

    def test_failed_atomic_replace_preserves_the_previous_complete_file(self):
        result = store.regenerate_router_files(self.db)
        path = Path(result["presets_path"])
        previous = path.read_bytes()
        record = store.read_record(self.db, "language", "published")
        record["context_size"] = 16384
        # Build the changed output via the supported save path, but fail its replacement.
        with mock.patch.object(store.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(store.StoreError, "cannot atomically write"):
                store.save_record(self.db, record, record_id="published")
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
