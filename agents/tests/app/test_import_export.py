"""Portable record documents use the validated store API and stay atomic."""

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
                        "thinking": "off", "card_id": "card-new",
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


if __name__ == "__main__":
    unittest.main()
