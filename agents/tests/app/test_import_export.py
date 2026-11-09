"""Portable record documents use the validated store API and stay atomic."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission, cli, discovery, store  # noqa: E402
from matuto import live_records, serving_state  # noqa: E402


class ImportExportTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.models = self.home / "models"
        self.models.mkdir()
        for name in ("model.gguf", "weights.bin"):
            (self.models / name).write_bytes(name.encode())
        exe = self.models / "speech"
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(exe, 0o700)
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        # What is serving is settled by test_live_records, over a fake systemctl
        # and a fake router. Here it is stopped, so every document below imports
        # on a machine Matuto is not serving from.
        stopped = mock.patch.object(live_records, "read_state",
                                    return_value=serving_state.State(admission.STOPPED))
        stopped.start()
        self.addCleanup(stopped.stop)
        now = "2026-10-11T00:00:00+00:00"
        db = self.opened.connection()
        db.executescript(f"""
            INSERT INTO artifact_locations VALUES ('loc', '{self.models}', '', '{now}');
            INSERT INTO argument_catalogue VALUES ('arg-catalogued', '--catalogued', 0,
                'fixture option', 'server,smoke,bench', '');
            INSERT INTO acquisition_records VALUES ('acq-model', 'Model', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc', '{now}', '', '{now}');
            INSERT INTO acquisition_files VALUES ('acq-model', 'model.gguf', 9, NULL,
                '{'a' * 64}', '{now}');
            INSERT INTO acquisition_records VALUES ('acq-weights', 'Weights', 'adoption',
                'locally-hashed', NULL, NULL, NULL, 'loc', '{now}', '', '{now}');
            INSERT INTO acquisition_files VALUES ('acq-weights', 'weights.bin', 11, NULL,
                '{'b' * 64}', '{now}');
            INSERT INTO speech_executables VALUES ('exe', 'synthesis', 'loc', '{exe}',
                '{'c' * 64}', NULL, '', '{now}');
            INSERT INTO speech_executables VALUES ('exe-rec', 'recognition', 'loc', '{exe}',
                '{'c' * 64}', NULL, '', '{now}');
        """)
        self.card = {"kind": "card", "card_id": "card-new", "name": "New card",
                     "notes": "card note", "gpu_identity": "0000:04:00.0",
                     "vram_ceiling_bytes": 100, "default_gpu_layers": "all",
                     "smoke_output_tokens": 1, "bench_prompt_tokens": 1,
                     "bench_generation_tokens": 1, "bench_repetitions": 1}
        self.language = {"kind": "language", "profile_id": "language-new", "name": "Language",
                        "notes": "language note", "alias": "language", "context_size": 16,
                        "thinking": {"mode": "none"}, "card_id": "card-new",
                        "arguments": [{"tool": "server", "catalogue": None,
                                       "option": "--safe"},
                                      {"tool": "smoke", "option": "--safe-smoke"},
                                      {"tool": "bench", "catalogue": "--catalogued",
                                       "option": None}], "mmproj": None,
                        "model": {"acquisition_id": "acq-model", "file_name": "model.gguf"}}
        self.synthesis = {"kind": "synthesis", "profile_id": "synthesis-new", "name": "Synth",
                          "notes": "synth note", "card_id": "card-new", "speech_executable_id": "exe",
                          "arguments": None,
                          "weights": {"acquisition_id": "acq-weights", "file_name": "weights.bin"}}
        self.recognition = {"kind": "recognition", "profile_id": "recognition-new", "name": "Recognize",
                            "notes": "recognition note", "speech_executable_id": "exe-rec",
                            "weights": {"acquisition_id": "acq-weights", "file_name": "weights.bin"}}
        self.bundle = {"kind": "bundle", "bundle_id": "bundle-new", "name": "Bundle",
                       "notes": "bundle note", "members": {"language": "language-new",
                       "synthesis": "synthesis-new", "recognition": "recognition-new"}}
        self.document = {"version": 1, "records": [self.bundle, self.recognition, self.synthesis,
                                                       self.language, self.card]}
        self.inventory = {"state": "completed", "devices": [{"pciAddress": "0000:04:00.0",
                                                                  "vramBytes": 100}]}

    def import_document(self, document):
        with mock.patch("matuto.cards.detect", return_value=self.inventory):
            return store.import_records(self.opened, document, help_text={
                "server": "", "smoke": "", "bench": ""})

    def test_import_resolves_in_document_and_exports_without_store_state(self):
        results = self.import_document(self.document)
        self.assertEqual([result["action"] for result in results], ["created"] * 5)
        changes = self.opened.connection().total_changes
        self.assertEqual([result["action"] for result in self.import_document(self.document)],
                         ["unchanged"] * 5)
        self.assertEqual(self.opened.connection().total_changes, changes)
        self.opened.connection().executescript("""
            INSERT INTO voice_serving VALUES (1, 'bundle-new');
            INSERT INTO voice_serving_parts VALUES ('synthesis', 'bundle-new', 10000);
            INSERT INTO voice_serving_parts VALUES ('recognition', 'bundle-new', 10001);
        """)
        changes = self.opened.connection().total_changes
        self.assertEqual([result["action"] for result in self.import_document(self.document)],
                         ["unchanged"] * 5)
        self.assertEqual(self.opened.connection().total_changes, changes)
        self.assertFalse(store.read_record(self.opened, "language", "language-new")["published"])
        self.assertEqual(store.read_record(self.opened, "bundle", "bundle-new")["members"],
                         self.bundle["members"])
        self.opened.connection().execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'language-new'")
        exported = store.export_records(self.opened)
        self.assertEqual(exported["version"], 1)
        self.assertEqual(len(exported["records"]), 5)
        self.assertTrue(all("published" not in record and "created_at" not in record
                            for record in exported["records"]))

        def contains_path(value):
            if isinstance(value, dict):
                return any("path" in key.lower() or contains_path(item)
                           for key, item in value.items())
            if isinstance(value, list):
                return any(contains_path(item) for item in value)
            return False

        self.assertFalse(contains_path(exported))
        self.assertFalse(any("qualification" in key or "acceptance" in key
                             for record in exported["records"] for key in record))
        exported["records"][1]["notes"] = "edited while published"
        edits = self.import_document(exported)
        self.assertEqual(edits[1]["action"], "edited")
        self.assertEqual(store.read_record(self.opened, "language", "language-new")["notes"],
                         "edited while published")
        self.assertTrue(store.read_record(self.opened, "language", "language-new")["published"])
        changes = self.opened.connection().total_changes
        self.assertEqual(self.import_document(exported), [
            {"kind": "card", "id": "card-new", "action": "unchanged", "warnings": []},
            {"kind": "language", "id": "language-new", "action": "unchanged", "warnings": []},
            {"kind": "synthesis", "id": "synthesis-new", "action": "unchanged", "warnings": []},
            {"kind": "recognition", "id": "recognition-new", "action": "unchanged", "warnings": []},
            {"kind": "bundle", "id": "bundle-new", "action": "unchanged", "warnings": []},
        ])
        self.assertEqual(self.opened.connection().total_changes, changes)
        self.assertTrue(store.read_record(self.opened, "language", "language-new")["published"])

    def test_any_refusal_rolls_back_earlier_records_in_the_document(self):
        broken = json.loads(json.dumps(self.document))
        broken["records"][0]["members"]["recognition"] = "missing"
        before = self.opened.connection().total_changes
        with self.assertRaises(store.RecordRefusal):
            self.import_document(broken)
        self.assertIsNone(self.opened.connection().execute(
            "SELECT 1 FROM cards WHERE card_id = 'card-new'").fetchone())
        self.assertGreater(self.opened.connection().total_changes, before)
        self.assertFalse(self.opened.connection().in_transaction)

    def test_imported_published_profile_regenerates_after_the_batch_commit(self):
        self.import_document(self.document)
        connection = self.opened.connection()
        connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'language-new'")
        store.regenerate_router_files(self.opened)
        path = self.opened.path.parent / "router-presets.ini"
        self.assertIn("ctx-size = 16", path.read_text(encoding="utf-8"))
        document = store.export_records(self.opened, [("language", "language-new")])
        document["records"][0]["context_size"] = 32

        result = self.import_document(document)

        self.assertEqual(result[0]["action"], "edited")
        self.assertEqual(store.read_record(self.opened, "language", "language-new")["context_size"], 32)
        self.assertIn("ctx-size = 32", path.read_text(encoding="utf-8"))
        self.assertFalse(connection.in_transaction)

    def test_failed_import_keeps_router_preset_at_last_committed_profile(self):
        self.import_document(self.document)
        connection = self.opened.connection()
        connection.execute(
            "UPDATE model_profiles SET published = 1 WHERE profile_id = 'language-new'")
        store.regenerate_router_files(self.opened)
        path = self.opened.path.parent / "router-presets.ini"
        previous = path.read_bytes()
        broken = json.loads(json.dumps(self.document))
        next(record for record in broken["records"] if record["kind"] == "language")["context_size"] = 32
        next(record for record in broken["records"] if record["kind"] == "bundle")[
            "members"]["recognition"] = "missing"

        with self.assertRaises(store.RecordRefusal):
            self.import_document(broken)

        self.assertEqual(store.read_record(self.opened, "language", "language-new")["context_size"], 16)
        self.assertEqual(path.read_bytes(), previous)
        self.assertFalse(connection.in_transaction)

    def test_existing_malformed_values_are_refused_before_noop_detection(self):
        self.import_document(self.document)
        for position, field, value, pattern in (
                (4, "bench_repetitions", True, "bench_repetitions"),
                (3, "context_size", 16.0, "context_size")):
            invalid = json.loads(json.dumps(self.document))
            invalid["records"][position][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(store.RecordRefusal, pattern):
                self.import_document(invalid)
        self.assertEqual(store.read_record(self.opened, "language", "language-new")["context_size"], 16)
        self.assertEqual(self.opened.connection().execute(
            "SELECT bench_repetitions FROM cards WHERE card_id = 'card-new'").fetchone()[0], 1)
        self.assertFalse(self.opened.connection().in_transaction)

    def test_format_refuses_publication_and_unknown_fields(self):
        for record, key in ((dict(self.language, published=False), "published"),
                            (dict(self.language, surprise=True), "surprise")):
            with self.subTest(key=key), self.assertRaisesRegex(store.RecordRefusal, key):
                self.import_document({"version": 1, "records": [record]})
        nested = json.loads(json.dumps(self.document))
        nested["records"][3]["arguments"] = [{"tool": "server", "option": "--safe",
                                                "value": "x", "surprise": True}]
        with self.assertRaisesRegex(store.RecordRefusal, "arguments row 1 surprise"):
            self.import_document(nested)

    def test_cli_commands_use_the_same_store_api_and_refuse_bad_documents(self):
        import_command = discovery.load_item("commands", "import")
        export_command = discovery.load_item("commands", "export")
        source = self.home / "records.json"
        target = self.home / "export.json"
        source.write_text(json.dumps(self.document), encoding="utf-8")
        with mock.patch.object(self.opened, "close"):
            with mock.patch("matuto.store.open_store", return_value=self.opened):
                with mock.patch("matuto.cards.detect", return_value=self.inventory):
                    self.assertEqual(import_command.run([str(source)]), 0)
                    self.assertEqual(export_command.run([str(target)]), 0)
                    self.assertEqual(cli.main(["matuto", "export", str(target),
                                               "language:language-new"]), 0)
        selected = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual([(record["kind"], record.get("profile_id"))
                          for record in selected["records"]], [("language", "language-new")])
        source.write_text(json.dumps({"version": 1, "records": [
            dict(self.language, unexpected="refuse")]}), encoding="utf-8")
        with mock.patch.object(self.opened, "close"):
            with mock.patch("matuto.store.open_store", return_value=self.opened):
                self.assertEqual(import_command.run([str(source)]), 1)
        source.write_text('{"version":1,"version":1,"records":[]}', encoding="utf-8")
        with mock.patch.object(self.opened, "close"):
            with mock.patch("matuto.store.open_store", return_value=self.opened):
                self.assertEqual(import_command.run([str(source)]), 1)


class ModelListTest(unittest.TestCase):
    """A schema-v4 store migrates, then the CLI exports only its published language models."""

    LEVELS = {"mode": "levels", "levels": ["low", "high"],
              "request": {"field": "reasoning_effort", "values": {"low": "low", "high": "high"}}}

    def setUp(self):
        ImportExportTest.setUp(self)
        (self.models / "mmproj.gguf").write_bytes(b"projector")
        self.opened.connection().execute(
            "INSERT INTO acquisition_records VALUES ('acq-mmproj', 'Projector', 'adoption',"
            " 'locally-hashed', NULL, NULL, NULL, 'loc', 'now', '', 'now')")
        self.opened.connection().execute(
            "INSERT INTO acquisition_files VALUES ('acq-mmproj', 'mmproj.gguf', 9, NULL, ?, 'now')",
            ("d" * 64,))

    def profile(self, identifier, alias, **fields):
        return {**self.language, "profile_id": identifier, "name": identifier, "alias": alias,
                "notes": "", "arguments": [], **fields}

    def test_migrated_store_exports_published_language_facts_and_keeps_a_failed_write_atomic(self):
        ImportExportTest.import_document(self, {"version": 1, "records": [
            self.card,
            self.profile("p-levels", "levels-model", context_size=8192, thinking=self.LEVELS,
                         arguments=[{"tool": "server", "option": "--n-predict", "value": "2048"}]),
            self.profile("p-vision", "vision-model", context_size=4096,
                         mmproj={"acquisition_id": "acq-mmproj", "file_name": "mmproj.gguf"}),
            self.profile("p-free", "free-model", context_size=1024),
            self.profile("p-hidden", "hidden-model", context_size=512)]})
        connection = self.opened.connection()
        connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id <> 'p-hidden'")
        # Rewind to schema 4, whose thinking was free text.
        connection.execute("UPDATE model_profiles SET thinking = ' None ' WHERE profile_id = 'p-vision'")
        connection.execute("UPDATE model_profiles SET thinking = 'think hard', notes = 'old note'"
                           " WHERE profile_id = 'p-free'")
        # The old install's capability object for one chat-template switch.
        connection.execute("UPDATE model_profiles SET thinking = ? WHERE profile_id = 'p-hidden'", (json.dumps(
            {"reasoning": True, "thinkingLevelMap": {"off": "off", "high": "high"},
             "compat": {"thinkingFormat": "chat-template",
                        "chatTemplateKwargs": {"enable_thinking": {"$var": "thinking.enabled"}}}}),))
        connection.execute("UPDATE model_profiles SET thinking = 'high' WHERE profile_id = 'p-levels'")
        connection.execute("UPDATE store_meta SET value = '4' WHERE key = 'schema_version'")
        self.opened.close()

        staged = store.stage_migrations(self.config)
        os.replace(staged, store.store_path(self.config))
        self.opened = store.open_store(self.config)
        self.addCleanup(self.opened.close)
        free = store.read_record(self.opened, "language", "p-free")
        self.assertIsNone(free["thinking"])
        self.assertEqual(free["notes"], "old note\nThinking before the fixed format: think hard")
        self.assertEqual(store.read_record(self.opened, "language", "p-vision")["thinking"], {"mode": "none"})
        self.assertEqual(store.read_record(self.opened, "language", "p-hidden")["thinking"], {
            "mode": "toggle", "request": {"field": "chat_template_kwargs.enable_thinking",
                                          "on": True, "off": False}})
        # A profile needing a choice cannot be saved again until thinking is chosen.
        with self.assertRaisesRegex(store.RecordRefusal, "thinking"):
            store.save_record(self.opened, dict(free, notes="edited"), record_id="p-free")
        levels = store.read_record(self.opened, "language", "p-levels")
        self.assertIsNone(levels["thinking"])
        levels.pop("qualification")
        store.save_record(self.opened, dict(levels, thinking=self.LEVELS), record_id="p-levels")
        self.assertEqual(store.read_record(self.opened, "language", "p-levels")["thinking"], self.LEVELS)
        accepted = store.fingerprint(self.opened, store.read_record(self.opened, "language", "p-vision"))
        self.opened.connection().executescript(f"""
            INSERT INTO qualification_records VALUES ('q-vision', 'language', 'p-vision', 'p-vision',
                '{accepted}', NULL, 'now');
            INSERT INTO acceptances VALUES ('a-vision', 'q-vision', 'serving', '', 'operator', 'now', NULL);
        """)

        target = self.home / "consumer" / "models.json"
        target.parent.mkdir()
        output = io.StringIO()
        with (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}),
              contextlib.redirect_stdout(output)):
            self.assertEqual(cli.main(["matuto-manager", "model-list", str(target)]), 0)
        text = target.read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(set(document), {"format", "version", "exported_at", "models"})
        self.assertEqual((document["format"], document["version"]), ("matuto-model-list", 1))
        self.assertEqual(document["models"], [
            {"id": "free-model", "qualified": False, "context_tokens": 1024, "max_output_tokens": 1024,
             "input": ["text"], "thinking": {"mode": "unknown"}},
            {"id": "levels-model", "qualified": False, "context_tokens": 8192, "max_output_tokens": 2048,
             "input": ["text"], "thinking": self.LEVELS},
            {"id": "vision-model", "qualified": True, "context_tokens": 4096, "max_output_tokens": 4096,
             "input": ["text", "image"], "thinking": {"mode": "none"}}])
        self.assertIn("free-model", output.getvalue())
        self.assertNotIn("levels-model", output.getvalue().split("\n", 1)[1])
        # The exact documents above carry no key, port or consumer term; no host path or address either.
        for private in (str(self.home), "127.0.0.1", "localhost"):
            self.assertNotIn(private, text)

        errors = io.StringIO()
        with (mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}),
              mock.patch("os.replace", side_effect=OSError("disk full")),
              contextlib.redirect_stderr(errors)):
            self.assertEqual(cli.main(["matuto-manager", "model-list", str(target)]), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["models.json"])
        self.assertIn("disk full", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
