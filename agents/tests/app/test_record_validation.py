"""One record validation and one availability check, through the store module.

Every record a save or an import may write is refused here before anything is
stored, and every pick resolves against the store's own acquisition records,
registry and installed speech executables. The availability check reports an
outside change without touching the record. Everything below goes through
`matuto.store`'s Python API against a temporary store and fixture files, which is
the seam the spec's Testing Decisions name.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import admission  # noqa: E402  the package is not on the path yet
from matuto import live_records, serving_state  # noqa: E402
from matuto import store as store_module  # noqa: E402

GPU = "0000:03:00.0"
NOW = "2026-10-09T16:00:00+00:00"


def digest_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecordTestCase(unittest.TestCase):
    """A temporary store, a fixture model root and detected GPUs to hand it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.config = self.home / "config"
        self.models = self.home / "models"
        self.models.mkdir()
        self.build = self.home / "builds" / "hip-b10918"
        self.build.mkdir(parents=True)
        self.store = store_module.open_store(self.config)
        self.addCleanup(self.store.close)
        self.connection = self.store.connection()
        self.detected = [GPU]
        # What is serving is settled by test_live_records, over a fake systemctl
        # and a fake router. Here it is stopped, so every save below is a save
        # Matuto would allow on an idle machine.
        stopped = mock.patch.object(live_records, "read_state",
                                    return_value=serving_state.State(admission.STOPPED))
        stopped.start()
        self.addCleanup(stopped.stop)
        self._fill()

    def file(self, name, body=b"weights"):
        path = self.models / name
        path.write_bytes(body)
        return path

    def executable(self, name):
        path = self.models / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    def build_server(self, root):
        path = root / "bin" / "llama-server"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o700)
        return path

    def _fill(self):
        self.file("Qwen3.6-35B-A3B-Q4_K_M.gguf")
        self.file("mmproj-f16.gguf")
        self.file("mtp-head.gguf")
        self.file("breeze-tts-2-q8_0.gguf")
        self.file("whisper-large-v3-turbo.bin")
        breeze = self.executable("breeze-server")
        whisper = self.executable("whisper-server")
        self.build_server(self.build)
        self.connection.executescript(
            f"""
            INSERT INTO cards VALUES ('card-r9', 'Radeon AI PRO R9700', '{GPU}',
                34359738368, 99, 16, 4096, 512, 5, 'the loud one', '{NOW}');
            INSERT INTO artifact_locations VALUES ('loc-models', '{self.models}', '',
                '{NOW}');
            INSERT INTO argument_catalogue VALUES ('arg-single-turn', '--single-turn', 0,
                'one exchange per prompt', 'smoke,bench', '');
            INSERT INTO argument_catalogue VALUES ('arg-parallel', '--parallel', 1,
                'parallel slots', 'server', '');
            INSERT INTO speech_executables VALUES ('exe-breeze', 'synthesis', 'loc-models',
                '{breeze}', '{digest_of(breeze)}', '0.4.0', '', '{NOW}');
            INSERT INTO speech_executables VALUES ('exe-whisper', 'recognition', 'loc-models',
                '{whisper}', '{digest_of(whisper)}', '1.0', '', '{NOW}');
            INSERT INTO acquisition_records VALUES ('acq-qwen36', 'Qwen3.6 35B-A3B',
                'download', 'published-checksum', NULL, 'unsloth/Qwen3.6-35B-GGUF',
                'a483e9e6', 'loc-models', '{NOW}', '', '{NOW}');
            INSERT INTO acquisition_files VALUES ('acq-qwen36',
                'Qwen3.6-35B-A3B-Q4_K_M.gguf', 8, NULL, '{'c' * 64}', '{NOW}');
            INSERT INTO acquisition_files VALUES ('acq-qwen36', 'mmproj-f16.gguf', 8,
                NULL, '{'d' * 64}', '{NOW}');
            INSERT INTO acquisition_files VALUES ('acq-qwen36', 'mtp-head.gguf', 8,
                NULL, '{'e' * 64}', '{NOW}');
            INSERT INTO acquisition_records VALUES ('acq-breeze', 'Breeze TTS 2 weights',
                'download', 'locally-hashed', NULL, 'HoppouAI/Breeze-TTS-2.cpp',
                '81b22bad', 'loc-models', '{NOW}', '', '{NOW}');
            INSERT INTO acquisition_files VALUES ('acq-breeze',
                'breeze-tts-2-q8_0.gguf', 8, NULL, '{'f' * 64}', '{NOW}');
            INSERT INTO acquisition_records VALUES ('acq-whisper', 'Whisper turbo',
                'download', 'locally-hashed', NULL, 'openai/whisper-large-v3-turbo',
                'abc12345', 'loc-models', '{NOW}', '', '{NOW}');
            INSERT INTO acquisition_files VALUES ('acq-whisper',
                'whisper-large-v3-turbo.bin', 8, NULL, '{'1' * 64}', '{NOW}');
            INSERT INTO builds VALUES ('build-hip', 'ROCm 7.0', 'llama-hip',
                '{self.build}', 'hip', 'b10918', 'gfx1201', 'llama-hip', 'rocm', 'registered', '');
            INSERT INTO runtime_selection VALUES (1, 'build-hip', 'card-r9', 'hip', '{NOW}');
            """
        )

    def insert_language_profile(self):
        """The stored profile a bundle document resolves its language member to."""
        self.connection.executescript(
            f"""
            INSERT INTO model_profiles VALUES ('prof-coder', 'language', 'Qwen3.6 Coder 192K',
                'qwen3-coder', 131072, 'high', 'card-r9', NULL, 0, '', '{NOW}');
            INSERT INTO profile_artifacts VALUES ('prof-coder', 'model', 'acq-qwen36',
                'Qwen3.6-35B-A3B-Q4_K_M.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-coder', 'mmproj', 'acq-qwen36',
                'mmproj-f16.gguf');
            INSERT INTO profile_artifacts VALUES ('prof-coder', 'mtp', 'acq-qwen36',
                'mtp-head.gguf');
            INSERT INTO model_profiles VALUES ('prof-breeze', 'synthesis', 'Breeze on the R9',
                '', NULL, NULL, 'card-r9', 'exe-breeze', 0, 'voice', '{NOW}');
            INSERT INTO profile_artifacts VALUES ('prof-breeze', 'weights', 'acq-breeze',
                'breeze-tts-2-q8_0.gguf');
            INSERT INTO model_profiles VALUES ('prof-whisper', 'recognition', 'Whisper CPU',
                '', NULL, NULL, NULL, 'exe-whisper', 0, '', '{NOW}');
            INSERT INTO profile_artifacts VALUES ('prof-whisper', 'weights', 'acq-whisper',
                'whisper-large-v3-turbo.bin');
            """
        )

    def card(self):
        return {"kind": "card", "card_id": "card-r9", "name": "Radeon AI PRO R9700",
                "notes": "the loud one", "gpu_identity": GPU, "vram_ceiling_bytes": 34359738368,
                "default_gpu_layers": 99, "smoke_output_tokens": 16, "bench_prompt_tokens": 4096,
                "bench_generation_tokens": 512, "bench_repetitions": 5}

    def language(self):
        return {"kind": "language", "profile_id": "prof-coder", "name": "Qwen3.6 Coder 192K",
                "notes": "tuned for the router", "alias": "qwen3-coder", "context_size": 131072,
                "thinking": "high", "card_id": "card-r9",
                "arguments": [{"tool": "server", "catalogue": "--parallel", "value": "1"},
                              {"tool": "bench", "catalogue": "--single-turn", "value": None}],
                "model": {"acquisition_id": "acq-qwen36",
                          "file_name": "Qwen3.6-35B-A3B-Q4_K_M.gguf"},
                "mmproj": {"acquisition_id": "acq-qwen36", "file_name": "mmproj-f16.gguf"},
                "mtp": {"acquisition_id": "acq-qwen36", "file_name": "mtp-head.gguf"}}

    def synthesis(self):
        return {"kind": "synthesis", "profile_id": "prof-breeze", "name": "Breeze on the R9",
                "notes": "voice", "card_id": "card-r9", "speech_executable_id": "exe-breeze",
                "weights": {"acquisition_id": "acq-breeze",
                            "file_name": "breeze-tts-2-q8_0.gguf"}}

    def recognition(self):
        return {"kind": "recognition", "profile_id": "prof-whisper", "name": "Whisper CPU",
                "notes": "", "speech_executable_id": "exe-whisper",
                "weights": {"acquisition_id": "acq-whisper",
                            "file_name": "whisper-large-v3-turbo.bin"}}

    def bundle(self):
        return {"kind": "bundle", "bundle_id": "bundle-voice", "name": "Voice 192K",
                "notes": "the voice bundle",
                "members": {"language": "prof-coder", "synthesis": "prof-breeze",
                            "recognition": "prof-whisper"}}

    def help_text(self):
        return {"server": "--parallel N\n", "smoke": "--single-turn\n", "bench": "--single-turn\n"}

    def validate(self, record, **kwargs):
        kwargs.setdefault("detected_gpus", self.detected)
        return store_module.validate_record(record, self.connection, **kwargs)

    def assertRefused(self, record, *needles, **kwargs):
        with self.assertRaises(store_module.RecordRefusal) as raised:
            self.validate(record, **kwargs)
        message = str(raised.exception)
        for needle in needles:
            self.assertIn(needle, message, message)
        return message

    def assertAvailable(self, record):
        available, reason = store_module.record_availability(
            record, self.connection, detected_gpus=self.detected)
        self.assertTrue(available, reason)
        self.assertIsNone(reason)

    def assertUnavailable(self, record, *needles):
        available, reason = store_module.record_availability(
            record, self.connection, detected_gpus=self.detected)
        self.assertFalse(available)
        for needle in needles:
            self.assertIn(needle, reason, reason)
        return reason


class AcceptanceTest(RecordTestCase):
    def test_a_valid_record_of_every_kind_is_accepted(self):
        self.insert_language_profile()

        self.assertEqual(self.validate(self.card()), [])
        self.assertEqual(self.validate(self.synthesis()), [])
        self.assertEqual(self.validate(self.recognition()), [])
        self.assertEqual(self.validate(self.bundle()), [])
        self.assertEqual(self.validate(self.language(), help_text=self.help_text()), [])


class FieldTest(RecordTestCase):
    def test_a_missing_required_field_is_refused_naming_the_record_and_field(self):
        for record, label, field in (
                (self.card(), "card-r9", "gpu_identity"),
                (self.card(), "card-r9", "vram_ceiling_bytes"),
                (self.language(), "prof-coder", "alias"),
                (self.language(), "prof-coder", "context_size"),
                (self.language(), "prof-coder", "thinking"),
                (self.language(), "prof-coder", "card_id"),
                (self.language(), "prof-coder", "model"),
                (self.synthesis(), "prof-breeze", "speech_executable_id"),
                (self.synthesis(), "prof-breeze", "card_id"),
                (self.synthesis(), "prof-breeze", "weights"),
                (self.recognition(), "prof-whisper", "speech_executable_id"),
                (self.bundle(), "bundle-voice", "members.synthesis")):
            with self.subTest(kind=record["kind"], field=field):
                if field.startswith("members."):
                    record["members"].pop(field.split(".")[1])
                else:
                    record.pop(field)
                self.assertRefused(record, label, field)

    def test_a_field_another_kind_carries_is_refused_naming_it(self):
        recognition = self.recognition()
        recognition["card_id"] = "card-r9"
        self.assertRefused(recognition, "prof-whisper", "card_id")

        synthesis = self.synthesis()
        synthesis["mmproj"] = {"acquisition_id": "acq-qwen36", "file_name": "mmproj-f16.gguf"}
        self.assertRefused(synthesis, "prof-breeze", "mmproj")

        language = self.language()
        language["weights"] = {"acquisition_id": "acq-breeze",
                               "file_name": "breeze-tts-2-q8_0.gguf"}
        self.assertRefused(language, "prof-coder", "weights")

        card = self.card()
        card["alias"] = "not-a-card-field"
        self.assertRefused(card, "card-r9", "alias")

    def test_a_value_of_the_wrong_type_is_refused_naming_the_record_and_field(self):
        language = self.language()
        language["context_size"] = "131072"
        self.assertRefused(language, "prof-coder", "context_size")

        language = self.language()
        language["notes"] = 7
        self.assertRefused(language, "prof-coder", "notes")

        synthesis = self.synthesis()
        synthesis["published"] = "yes"
        self.assertRefused(synthesis, "prof-breeze", "published")

        card = self.card()
        card["gpu_identity"] = 3
        self.assertRefused(card, "card-r9", "gpu_identity")

        bundle = self.bundle()
        bundle["members"] = ["prof-coder"]
        self.assertRefused(bundle, "bundle-voice", "members")

    def test_only_a_language_profile_may_be_published(self):
        for record in (self.synthesis(), self.recognition()):
            record["published"] = True
            self.assertRefused(record, record["profile_id"], "published")

        language = self.language()
        language["published"] = True
        self.assertEqual(self.validate(language, help_text=self.help_text()), [])

    def test_a_gpu_detection_does_not_report_is_refused(self):
        self.assertRefused(self.card(), "card-r9", "gpu_identity", detected_gpus=[])

    def test_every_record_requires_a_notes_field(self):
        for record in (self.card(), self.language(), self.synthesis(), self.recognition(),
                       self.bundle()):
            label = record[store_module.ID_FIELDS[record["kind"]]]
            with self.subTest(kind=record["kind"]):
                missing = {key: value for key, value in record.items() if key != "notes"}
                self.assertRefused(missing, label, "notes")
                self.assertRefused(dict(record, notes=None), label, "notes")

    def test_a_language_profile_requires_its_argument_rows(self):
        missing = self.language()
        missing.pop("arguments")
        self.assertRefused(missing, "prof-coder", "arguments")
        self.assertRefused(dict(self.language(), arguments=None), "prof-coder", "arguments")
        self.assertEqual(self.validate(dict(self.language(), arguments=[]),
                                       help_text=self.help_text()), [])

    def test_a_bundle_carries_its_stated_load(self):
        self.insert_language_profile()
        loaded = dict(self.bundle(), load_tokens=4096, load_prompt="Summarise the call.")
        self.assertEqual(self.validate(loaded), [])
        self.assertRefused(dict(loaded, load_tokens=0), "bundle-voice", "load_tokens")
        self.assertRefused(dict(loaded, load_prompt=""), "bundle-voice", "load_prompt")

    def test_a_non_string_creation_time_is_refused(self):
        record = self.language()
        record["created_at"] = {"invalid": "timestamp"}
        self.assertRefused(record, "prof-coder", "created_at")
        self.assertRefused(dict(self.language(), created_at=None), "prof-coder", "created_at")
        self.assertEqual(self.validate(dict(self.language(), created_at=NOW),
                                       help_text=self.help_text()), [])


class PickTest(RecordTestCase):
    def test_a_typed_path_or_digest_in_place_of_a_pick_is_refused(self):
        typed_path = self.language()
        typed_path["model"] = "/var/lib/matuto/models/Qwen3.6-Q4_K_M.gguf"
        self.assertRefused(typed_path, "prof-coder", "model")

        typed_digest = self.language()
        typed_digest["model"] = {"acquisition_id": "acq-qwen36",
                                 "file_name": "Qwen3.6-35B-A3B-Q4_K_M.gguf",
                                 "sha256": "c" * 64}
        self.assertRefused(typed_digest, "prof-coder", "model")

        wrong_key = self.recognition()
        wrong_key["weights"] = {"acquisition_id": "acq-whisper",
                                "file_name": "whisper-large-v3-turbo.bin",
                                "path": "/somewhere/else"}
        self.assertRefused(wrong_key, "prof-whisper", "weights")

    def test_an_incomplete_acquisition_record_is_refused(self):
        self.connection.execute(
            "UPDATE acquisition_records SET verified_at = NULL WHERE acquisition_id = 'acq-qwen36'")
        self.assertRefused(self.language(), "prof-coder", "model")

        self.connection.execute(
            "UPDATE acquisition_records SET verified_at = ? WHERE acquisition_id = 'acq-qwen36'",
            (NOW,))
        self.connection.execute(
            "UPDATE acquisition_files SET verified_at = NULL WHERE file_name = 'mmproj-f16.gguf'")
        self.assertRefused(self.language(), "prof-coder", "mmproj")

    def test_a_pick_that_names_nothing_in_the_store_is_refused(self):
        unknown_record = self.language()
        unknown_record["model"] = {"acquisition_id": "acq-nobody", "file_name": "model.gguf"}
        self.assertRefused(unknown_record, "prof-coder", "model")

        unknown_file = self.language()
        unknown_file["model"] = {"acquisition_id": "acq-qwen36", "file_name": "other.gguf"}
        self.assertRefused(unknown_file, "prof-coder", "model")

        unknown_card = self.language()
        unknown_card["card_id"] = "card-nobody"
        self.assertRefused(unknown_card, "prof-coder", "card_id")

        unknown_executable = self.synthesis()
        unknown_executable["speech_executable_id"] = "exe-nobody"
        self.assertRefused(unknown_executable, "prof-breeze", "speech_executable_id")

        wrong_executable_kind = self.recognition()
        wrong_executable_kind["speech_executable_id"] = "exe-breeze"
        self.assertRefused(wrong_executable_kind, "prof-whisper", "speech_executable_id")

        self.insert_language_profile()
        unknown_member = self.bundle()
        unknown_member["members"]["recognition"] = "prof-nobody"
        self.assertRefused(unknown_member, "bundle-voice", "members.recognition")

        wrong_member_kind = self.bundle()
        wrong_member_kind["members"]["language"] = "prof-breeze"
        self.assertRefused(wrong_member_kind, "bundle-voice", "members.language")


class ArgumentTest(RecordTestCase):
    def test_the_argument_validator_refuses_and_warns(self):
        refused = self.language()
        refused["arguments"] = [{"tool": "server", "option": "--rpc", "value": "127.0.0.1"}]
        message = self.assertRefused(refused, "prof-coder")
        self.assertIn("argument row 1", message)
        self.assertIn("--rpc", message)

        warning = self.language()
        warning["arguments"] = [{"tool": "server", "option": "--verbose", "value": None}]
        warnings = self.validate(warning, help_text=self.help_text())
        self.assertEqual(len(warnings), 1)
        self.assertIn("--verbose", warnings[0])
        self.assertIn("argument row 1", warnings[0])

    def test_a_malformed_argument_row_is_refused_naming_the_row(self):
        bad_tool = self.language()
        bad_tool["arguments"] = [{"tool": "vision", "option": "--verbose"}]
        self.assertRefused(bad_tool, "prof-coder", "arguments row 1")

        bad_value = self.language()
        bad_value["arguments"] = [{"tool": "server", "option": "--temp", "value": 1}]
        self.assertRefused(bad_value, "prof-coder", "arguments row 1")

        not_a_list = self.language()
        not_a_list["arguments"] = "--parallel 1"
        self.assertRefused(not_a_list, "prof-coder", "arguments")

    def test_a_speech_profiles_rows_are_checked_structurally(self):
        # Launch validation governs the llama.cpp tools a language profile launches; a speech
        # profile's rows are its own launcher's, so they are held to the row shape.
        synthesis = self.synthesis()
        synthesis["arguments"] = [{"tool": "server", "option": "--threads", "value": "4"}]
        self.assertEqual(self.validate(synthesis), [])

        bad_value = self.synthesis()
        bad_value["arguments"] = [{"tool": "server", "option": "--threads", "value": 4}]
        self.assertRefused(bad_value, "prof-breeze", "arguments row 1")

    def test_a_row_must_name_a_catalogue_entry_or_a_custom_flag(self):
        for kind_record in (self.synthesis(), self.language()):
            label = kind_record[store_module.ID_FIELDS[kind_record["kind"]]]
            unnamed = dict(kind_record, arguments=[{"tool": "server"}])
            self.assertRefused(unnamed, label, "arguments row 1")
            unknown = dict(kind_record,
                           arguments=[{"tool": "server", "catalogue": "--nonexistent"}])
            self.assertRefused(unknown, label, "arguments row 1")
            blank = dict(kind_record, arguments=[{"tool": "server", "option": "   "}])
            self.assertRefused(blank, label, "arguments row 1")


class NoWriteTest(RecordTestCase):
    def test_a_refusal_leaves_the_store_unchanged(self):
        before = digest_of(self.store.path)
        counts = self.counts()

        foreign = self.recognition()
        foreign["card_id"] = "card-r9"
        self.assertRefused(foreign, "prof-whisper", "card_id")
        incomplete = self.language()
        incomplete.pop("alias")
        self.assertRefused(incomplete, "prof-coder", "alias")

        self.assertEqual(digest_of(self.store.path), before)
        self.assertEqual(self.counts(), counts)

    def counts(self):
        return {table: self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("model_profiles", "profile_artifacts", "cards", "bundles")}


class AvailabilityTest(RecordTestCase):
    def test_an_available_record_of_every_kind_reports_no_reason(self):
        self.insert_language_profile()
        self.assertAvailable(self.card())
        self.assertAvailable(self.language())
        self.assertAvailable(self.synthesis())
        self.assertAvailable(self.recognition())
        self.assertAvailable(self.bundle())

    def test_a_missing_file_makes_the_record_unavailable_naming_it(self):
        self.insert_language_profile()
        (self.models / "Qwen3.6-35B-A3B-Q4_K_M.gguf").unlink()

        self.assertUnavailable(self.language(), "model", "Qwen3.6-35B-A3B-Q4_K_M.gguf")
        self.assertUnavailable(self.bundle(), "language", "prof-coder")

    def test_an_undetected_card_makes_the_record_unavailable(self):
        for record in (self.card(), self.synthesis()):
            available, reason = store_module.record_availability(
                record, self.connection, detected_gpus=[])
            self.assertFalse(available)
            self.assertIn("not detected", reason)

    def test_a_profile_on_another_card_is_unavailable(self):
        self.connection.executescript(
            f"""
            INSERT INTO cards VALUES ('card-other', 'Other GPU', '0000:04:00.0',
                8, 1, 1, 1, 1, 1, '', '{NOW}');
            UPDATE runtime_selection SET card_id = 'card-other' WHERE selection_id = 1;
            """
        )
        self.assertUnavailable(self.synthesis(), "card-r9", "not the active card", "card-other")

    def test_a_removed_or_changed_speech_executable_is_unavailable(self):
        self.insert_language_profile()
        (self.models / "breeze-server").unlink()
        self.assertUnavailable(self.synthesis(), "exe-breeze", "missing")
        self.assertUnavailable(self.bundle(), "synthesis", "prof-breeze")

        (self.models / "breeze-server").write_text("#!/bin/sh\necho other\n", encoding="utf-8")
        os.chmod(self.models / "breeze-server", 0o700)
        self.assertUnavailable(self.synthesis(), "exe-breeze", "changed")

    def test_a_removed_build_makes_a_language_profile_unavailable(self):
        self.connection.execute("UPDATE builds SET state = 'unavailable' WHERE build_id = 'build-hip'")
        self.assertUnavailable(self.language(), "build-hip", "unavailable")

        self.connection.execute("UPDATE builds SET state = 'registered' WHERE build_id = 'build-hip'")
        (self.build / "bin" / "llama-server").unlink()
        self.assertTrue(self.build.is_dir())
        self.assertUnavailable(self.language(), "build-hip", "missing its server")

        shutil.rmtree(self.build)
        self.assertUnavailable(self.language(), "build-hip", "missing its server")

    def test_a_speech_executable_without_execute_permission_is_unavailable(self):
        os.chmod(self.models / "breeze-server", 0o600)
        self.assertUnavailable(self.synthesis(), "exe-breeze", "not executable")

    def test_another_registered_build_keeps_the_record_available(self):
        other = self.home / "builds" / "vulkan"
        self.build_server(other)
        self.connection.executescript(
            f"""
            INSERT INTO builds VALUES ('build-vulkan', 'Vulkan', 'llama-vulkan', '{other}',
                'vulkan', 'b20000', NULL, 'llama-vulkan', '', 'registered', '');
            """
        )
        self.assertAvailable(self.language())

        self.connection.execute("UPDATE runtime_selection SET build_id = 'build-vulkan'")
        self.assertAvailable(self.language())

    def test_the_availability_check_reads_only(self):
        self.insert_language_profile()
        row = self.connection.execute(
            "SELECT * FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone()
        before = digest_of(self.store.path)

        self.assertAvailable(self.language())

        self.assertEqual(digest_of(self.store.path), before)
        after = self.connection.execute(
            "SELECT * FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone()
        self.assertEqual(tuple(row), tuple(after))


class SaveRemoveTest(RecordTestCase):
    def save(self, record, **kwargs):
        return store_module.save_record(self.store, record, **kwargs)

    def test_creates_and_edits_every_record_kind_through_the_validated_store_api(self):
        self.insert_language_profile()
        self.save(self.bundle())
        card_record = {**self.card(), "card_id": "card-new", "gpu_identity": "0000:04:00.0",
                       "name": "New card", "vram_ceiling_bytes": 16000000000}
        inventory = {"state": "completed", "devices": [{"pciAddress": "0000:04:00.0",
                                                              "vramBytes": 16000000000}]}
        with mock.patch("matuto.cards.detect", return_value=inventory):
            card, warnings = self.save(card_record)
        self.assertEqual((card["card_id"], card["notes"], warnings), ("card-new", "the loud one", []))

        records = [
            dict(self.language(), profile_id="prof-new", name="New language", notes="language note"),
            dict(self.synthesis(), profile_id="prof-new-synthesis", notes="synthesis note"),
            dict(self.recognition(), profile_id="prof-new-recognition", notes="recognition note"),
            dict(self.bundle(), bundle_id="bundle-new", notes="bundle note"),
        ]
        for candidate in records:
            with self.subTest(kind=candidate["kind"]):
                created, warnings = self.save(candidate, help_text=self.help_text())
                identifier = candidate[store_module.ID_FIELDS[candidate["kind"]]]
                edited = dict(created, name=f"Edited {candidate['kind']}", notes="edited note")
                saved, _ = self.save(edited, record_id=identifier, help_text=self.help_text())
                self.assertEqual(saved[store_module.ID_FIELDS[candidate["kind"]]], identifier)
                self.assertEqual((saved["name"], saved["notes"]),
                                 (f"Edited {candidate['kind']}", "edited note"))
                self.assertEqual(self.save(store_module.read_record(
                    self.store, candidate["kind"], identifier), record_id=identifier,
                    help_text=self.help_text())[0]["notes"], "edited note")

    def test_language_save_returns_warnings_and_keeps_publication_and_stable_id(self):
        self.insert_language_profile()
        self.connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        record = store_module.read_record(self.store, "language", "prof-coder")
        record["notes"] = "updated notes"
        record["arguments"] = [{"tool": "server", "option": "--unadvertised", "value": None}]

        saved, warnings = self.save(record, record_id="prof-coder", help_text=self.help_text())

        self.assertEqual(saved["profile_id"], "prof-coder")
        self.assertEqual(saved["notes"], "updated notes")
        self.assertTrue(saved["published"])
        self.assertGreater(len(warnings), 0)
        with self.assertRaisesRegex(store_module.RecordRefusal, "published"):
            self.save(dict(saved, published=False), record_id="prof-coder")
        with self.assertRaisesRegex(store_module.RecordRefusal, "stable ID"):
            self.save(dict(saved, profile_id="prof-renamed"), record_id="prof-coder")

    def test_duplicate_and_malformed_ids_are_refused_without_partial_saves(self):
        self.insert_language_profile()
        before = digest_of(self.store.path)
        with self.assertRaisesRegex(store_module.RecordRefusal, "already exists"):
            self.save(self.language())
        cross_kind = dict(self.synthesis(), profile_id="prof-coder")
        with self.assertRaisesRegex(store_module.RecordRefusal, "profile_id.*language"):
            self.save(cross_kind, record_id="prof-coder")
        missing_edit = dict(self.language(), profile_id="prof-missing")
        with self.assertRaisesRegex(store_module.RecordRefusal, "profile_id.*prof-missing"):
            self.save(missing_edit, record_id="prof-missing")
        malformed = dict(self.language(), profile_id="../outside")
        with self.assertRaisesRegex(store_module.RecordRefusal, "profile_id.*malformed"):
            self.save(malformed)
        for invalid_id in ([], {}):
            with self.subTest(invalid_id=type(invalid_id).__name__):
                with self.assertRaisesRegex(store_module.RecordRefusal, "profile_id"):
                    self.save(dict(self.language(), profile_id=invalid_id))
        invalid = dict(self.language(), profile_id="prof-invalid")
        invalid.pop("alias")
        with self.assertRaisesRegex(store_module.RecordRefusal, "alias"):
            self.save(invalid)
        self.assertEqual(digest_of(self.store.path), before)
        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-invalid'").fetchone())

    def test_malformed_edit_metadata_is_refused_before_preserving_managed_values(self):
        self.insert_language_profile()
        before = digest_of(self.store.path)
        record = store_module.read_record(self.store, "language", "prof-coder")
        for field, value in (("created_at", []), ("published", 0)):
            with self.subTest(field=field):
                with self.assertRaisesRegex(store_module.RecordRefusal, field):
                    self.save(dict(record, notes="must not persist", **{field: value}),
                              record_id="prof-coder", help_text=self.help_text())
        self.assertEqual(digest_of(self.store.path), before)
        self.assertNotEqual(store_module.read_record(
            self.store, "language", "prof-coder")["notes"], "must not persist")

    def test_validated_custom_speech_row_persists_as_custom_when_catalogue_is_blank(self):
        self.insert_language_profile()
        record = dict(self.synthesis(), arguments=[{"tool": "server", "catalogue": " ",
                                                     "option": "--custom", "value": "x"}])
        self.assertEqual(self.validate(record), [])

        self.save(record, record_id="prof-breeze")

        self.assertEqual(store_module.read_record(
            self.store, "synthesis", "prof-breeze")["arguments"],
            [{"tool": "server", "option": "--custom", "value": "x"}])

    def test_card_save_refusals_name_the_record_and_field(self):
        card = dict(self.card(), card_id="card-new")
        too_large_inventory = {"state": "completed", "devices": [{"pciAddress": GPU, "vramBytes": 1}]}
        with mock.patch("matuto.cards.detect", return_value=too_large_inventory):
            with self.assertRaisesRegex(store_module.RecordRefusal,
                                        "card record 'card-new' field 'vram_ceiling_bytes'"):
                self.save(card)
        unavailable = {"state": "unavailable", "devices": []}
        with mock.patch("matuto.cards.detect", return_value=unavailable):
            with self.assertRaisesRegex(store_module.RecordRefusal,
                                        "card record 'card-new' field 'gpu_identity'"):
                self.save(card)
        duplicate_inventory = {"state": "completed", "devices": [{"pciAddress": GPU,
                                                                         "vramBytes": 34359738368}]}
        with mock.patch("matuto.cards.detect", return_value=duplicate_inventory):
            with self.assertRaisesRegex(store_module.RecordRefusal,
                                        "card record 'card-new' field 'gpu_identity'"):
                self.save(card)

    def test_published_alias_collision_is_refused_with_alias_field(self):
        self.insert_language_profile()
        other = dict(self.language(), profile_id="prof-other", alias="other")
        self.save(other, help_text=self.help_text())
        self.connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        self.connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-other'")
        record = store_module.read_record(self.store, "language", "prof-other")
        record["alias"] = "qwen3-coder"

        with self.assertRaisesRegex(store_module.RecordRefusal, "prof-other.*alias"):
            self.save(record, record_id="prof-other", help_text=self.help_text())

    def test_removing_a_voice_served_bundle_names_the_holder_and_changes_nothing(self):
        self.insert_language_profile()
        self.save(self.bundle())
        self.connection.executescript("""
            INSERT INTO voice_serving VALUES (1, 'bundle-voice');
            INSERT INTO voice_serving_parts VALUES ('synthesis', 'bundle-voice', 8810);
            INSERT INTO voice_serving_parts VALUES ('recognition', 'bundle-voice', 8811);
        """)
        before = digest_of(self.store.path)

        with self.assertRaisesRegex(store_module.RecordRefusal, "voice serving"):
            store_module.remove_record(self.store, "bundle", "bundle-voice")

        self.assertEqual(digest_of(self.store.path), before)

    def test_a_refused_save_of_each_kind_leaves_the_store_unchanged(self):
        self.insert_language_profile()
        self.save(self.bundle())
        invalid = [
            dict(self.language(), profile_id="prof-bad-language", alias=""),
            dict(self.synthesis(), profile_id="prof-bad-synthesis", weights=None),
            dict(self.recognition(), profile_id="prof-bad-recognition", weights=None),
            dict(self.bundle(), bundle_id="bundle-bad", members={"language": "missing",
                                                                  "synthesis": "prof-breeze",
                                                                  "recognition": "prof-whisper"}),
        ]
        card = self.card()
        card["card_id"] = "card-bad"
        card.pop("notes")
        inventory = {"state": "completed", "devices": [{"pciAddress": GPU,
                                                              "vramBytes": 34359738368}]}
        before = digest_of(self.store.path)
        with mock.patch("matuto.cards.detect", return_value=inventory):
            with self.assertRaises(store_module.RecordRefusal):
                self.save(card)
        for record in invalid:
            with self.subTest(kind=record["kind"]):
                with self.assertRaises(store_module.RecordRefusal):
                    self.save(record, help_text=self.help_text())
        self.assertEqual(digest_of(self.store.path), before)

    def test_removal_names_publication_and_each_referring_record_without_changes(self):
        self.insert_language_profile()
        self.save(self.bundle())
        self.connection.execute("UPDATE model_profiles SET published = 1 WHERE profile_id = 'prof-coder'")
        with self.assertRaisesRegex(store_module.RecordRefusal, "publication"):
            store_module.remove_record(self.store, "language", "prof-coder")
        self.connection.execute("UPDATE model_profiles SET published = 0 WHERE profile_id = 'prof-coder'")
        with self.assertRaisesRegex(store_module.RecordRefusal, "Voice 192K.*bundle-voice"):
            store_module.remove_record(self.store, "language", "prof-coder")
        with self.assertRaisesRegex(store_module.RecordRefusal, "runtime selection"):
            store_module.remove_record(self.store, "card", "card-r9")
        with self.assertRaisesRegex(store_module.RecordRefusal, "profile .*prof-coder"):
            store_module.remove_record(self.store, "card", "card-r9")
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone())

    def test_allowed_removal_keeps_runs_qualification_acceptance_and_other_records(self):
        self.insert_language_profile()
        store_module.save_record(self.store, self.bundle())
        self.connection.executescript(f"""
            INSERT INTO runs (run_id, record_kind, record_id, record_label, fingerprint,
                procedure, procedure_revision, unit_name, outcome, dirty_checkout, summary, log,
                log_truncated, started_at, ended_at)
            VALUES ('run-retained', 'language', 'prof-coder', 'Coder', 'fp-coder',
                'smoke', '2026.10', NULL, 'completed', 0, '', '', 0, '{NOW}', NULL);
            INSERT INTO qualification_records VALUES ('qual-retained', 'language', 'prof-coder',
                'Coder', 'fp-coder', NULL, '{NOW}');
            INSERT INTO acceptances VALUES ('accept-retained', 'qual-retained', 'agentic serving',
                '', 'operator', '{NOW}', NULL);
        """)
        store_module.remove_record(self.store, "bundle", "bundle-voice")
        self.connection.execute("UPDATE model_profiles SET published = 0 WHERE profile_id = 'prof-coder'")

        store_module.remove_record(self.store, "language", "prof-coder")

        self.assertIsNone(self.connection.execute(
            "SELECT 1 FROM model_profiles WHERE profile_id = 'prof-coder'").fetchone())
        self.assertIsNotNone(self.connection.execute(
            "SELECT 1 FROM cards WHERE card_id = 'card-r9'").fetchone())
        self.assertEqual(self.connection.execute(
            "SELECT record_id FROM runs WHERE run_id = 'run-retained'").fetchone()[0], "prof-coder")
        self.assertEqual(self.connection.execute(
            "SELECT record_id FROM qualification_records WHERE qualification_id = 'qual-retained'").fetchone()[0],
            "prof-coder")
        self.assertEqual(self.connection.execute(
            "SELECT qualification_id FROM acceptances WHERE acceptance_id = 'accept-retained'").fetchone()[0],
            "qual-retained")


if __name__ == "__main__":
    unittest.main()
