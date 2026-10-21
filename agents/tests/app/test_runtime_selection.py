"""Runtime selection validates all inputs before its one SQLite replacement."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parents[2]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))
if str(AGENTS / "tests") not in sys.path:
    sys.path.insert(0, str(AGENTS / "tests"))

import matuto_runtime  # noqa: E402
import runtime_fixture  # noqa: E402
from matuto import admission, recipes, registry, runner, runtime_selection, store  # noqa: E402

PCI = "0000:03:00.0"
NOW = "2026-10-11T00:00:00+00:00"


class RuntimeSelectionTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="runtime-selection-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.opened = store.open_store(self.home / "config")
        self.addCleanup(self.opened.close)
        self.connection = self.opened.connection()
        self.connection.execute(
            "INSERT INTO cards (card_id, name, gpu_identity, vram_ceiling_bytes,"
            " default_gpu_layers, smoke_output_tokens, bench_prompt_tokens,"
            " bench_generation_tokens, bench_repetitions, notes, created_at)"
            " VALUES ('card-a', 'GPU', ?, 34359738368, -1, 256, 512, 128, 3, '', ?)",
            (PCI, NOW))
        self.hip = self.build("hip", "hip", "gfx1201")
        self.hip_old = self.build("hip-old", "hip", "gfx1201")
        self.vulkan = self.build("vulkan", "vulkan", None)

    def build(self, build_id, backend, target):
        path = self.home / build_id
        server = path / "bin" / "llama-server"
        server.parent.mkdir(parents=True)
        server.write_text("#!/bin/sh\n")
        server.chmod(0o755)
        recipe = recipes.LLAMA_HIP if backend == "hip" else recipes.LLAMA_VULKAN
        kind = "llama-hip" if backend == "hip" else "llama-vulkan"
        self.connection.execute(
            "INSERT INTO installations (location, recipe, source_revision, target, toolchain,"
            " check_state, check_reason, check_digests, recorded_at)"
            " VALUES (?, ?, 'revision', ?, 'toolchain', 'checked', '', '{}', ?)",
            (str(path), recipe, target, NOW))
        self.connection.execute(
            "INSERT INTO builds (build_id, label, kind, location, backend, source_revision,"
            " target, recipe, toolchain, state, notes)"
            " VALUES (?, ?, ?, ?, ?, 'revision', ?, ?, 'toolchain', 'registered', '')",
            (build_id, build_id, kind, str(path), backend, target, recipe))
        return path

    def choose(self, build="hip", backend="hip", card="card-a", **overrides):
        options = dict(serving="stopped", runtime_run=False, detected_cards={PCI},
                       validate_device=lambda selected, identity: identity == PCI,
                       hip_targets={PCI: "gfx1201"})
        options.update(overrides)
        return runtime_selection.select(self.connection, build, backend, card, **options)

    def record_server_digest(self, path):
        descriptor = os.open(path / "bin" / "llama-server", os.O_RDONLY)
        try:
            digest = matuto_runtime.fingerprint(descriptor)
        finally:
            os.close(descriptor)
        self.connection.execute(
            "UPDATE installations SET check_digests = ? WHERE location = ?",
            (json.dumps({"bin/llama-server": digest}), str(path)))

    def selection(self):
        row = self.connection.execute(
            "SELECT * FROM runtime_selection WHERE selection_id = 1").fetchone()
        return None if row is None else dict(row)

    def test_first_selection_saves_only_after_all_checks_pass(self):
        saved = self.choose()
        self.assertEqual((saved["build_id"], saved["backend"], saved["card_id"]),
                         ("hip", "hip", "card-a"))
        self.assertEqual(self.selection(), saved)

    def test_missing_card_record_points_to_settings_and_saves_nothing(self):
        with self.assertRaisesRegex(store.StoreError, "Settings › Cards"):
            self.choose(card="missing")
        self.assertIsNone(self.selection())

    def test_missing_card_detection_refuses_selection(self):
        with self.assertRaisesRegex(store.StoreError, "not currently detected"):
            self.choose(detected_cards=set())
        self.assertIsNone(self.selection())

    def test_use_preserves_backend_and_card_and_switches_only_build(self):
        self.choose()
        saved = runtime_selection.use(
            self.connection, "hip-old", serving="stopped", runtime_run=False,
            detected_cards={PCI}, validate_device=lambda _build, _pci: True,
            hip_targets={PCI: "gfx1201"})
        self.assertEqual((saved["build_id"], saved["backend"], saved["card_id"]),
                         ("hip-old", "hip", "card-a"))

    def test_use_without_selection_and_wrong_backend_refuse_without_substitution(self):
        with self.assertRaisesRegex(store.StoreError, "Select a runtime"):
            runtime_selection.use(self.connection, "hip", serving="stopped", runtime_run=False,
                                  detected_cards={PCI}, validate_device=lambda *_: True,
                                  hip_targets={PCI: "gfx1201"})
        self.choose()
        before = self.selection()
        with self.assertRaisesRegex(store.StoreError, "not hip"):
            runtime_selection.use(self.connection, "vulkan", serving="stopped", runtime_run=False,
                                  detected_cards={PCI}, validate_device=lambda *_: True,
                                  hip_targets={PCI: "gfx1201"})
        self.assertEqual(self.selection(), before)

    def test_selection_refuses_wrong_backend_and_hip_target(self):
        with self.assertRaisesRegex(store.StoreError, "uses the vulkan backend, not hip"):
            self.choose(build="vulkan", backend="hip")
        with self.assertRaisesRegex(store.StoreError, "uses gfx1036"):
            self.choose(hip_targets={PCI: "gfx1036"})
        self.assertIsNone(self.selection())

    def test_use_refuses_a_mismatched_hip_target_without_replacing_selection(self):
        self.choose()
        before = self.selection()
        with self.assertRaisesRegex(store.StoreError, "uses gfx1036"):
            runtime_selection.use(self.connection, "hip-old", serving="stopped", runtime_run=False,
                                  detected_cards={PCI}, validate_device=lambda *_: True,
                                  hip_targets={PCI: "gfx1036"})
        self.assertEqual(self.selection(), before)

    def test_running_serving_and_runtime_run_refuse_explicit_selection(self):
        for serving, runtime_run, expected in (("agentic", False, "Stop serving"),
                                               ("stopped", True, "run using the runtime")):
            with self.subTest(serving=serving, runtime_run=runtime_run):
                with self.assertRaisesRegex(store.StoreError, expected):
                    self.choose(serving=serving, runtime_run=runtime_run)
                self.assertIsNone(self.selection())

    def test_running_serving_runtime_run_and_unknown_state_refuse_both_paths(self):
        self.choose()
        before = self.selection()
        for state, run, expected in (("agentic", False, "Stop serving"),
                                     ("voice", False, "Stop serving"),
                                     ("stopped", True, "run using the runtime"),
                                     ("unknown", False, "unknown"),
                                     ("stopped", None, "unknown")):
            with self.subTest(state=state, run=run):
                with self.assertRaisesRegex(store.StoreError, expected):
                    runtime_selection.use(
                        self.connection, "vulkan", serving=state, runtime_run=run,
                        detected_cards={PCI}, validate_device=lambda *_: True,
                        hip_targets={PCI: "gfx1201"})
                self.assertEqual(self.selection(), before)

    def test_use_rechecks_runner_admission_under_the_selection_write_lock(self):
        self.choose()
        before = self.selection()
        active_runner = runner.Runner(
            self.home / "config", serving_state=lambda: admission.STOPPED,
            card_holder=lambda: admission.Card(admission.CARD_FREE))
        self.addCleanup(active_runner.close)
        active_runner._answers = lambda: True
        active_runner.launch = lambda *_args: SimpleNamespace(returncode=0)

        def start_run(_build, _pci):
            start = active_runner.start(runner.Request.run(
                "smoke", "race probe", ("true",),
                runner.Target("language", "profile", "Profile", "sha256:fixture"), "fixture"))
            self.assertEqual(start.decision, admission.ADMIT)
            return True

        with self.assertRaisesRegex(store.StoreError, "run using the runtime"):
            runtime_selection.use(
                self.connection, "hip-old", serving="stopped", runtime_run=False,
                detected_cards={PCI}, validate_device=start_run,
                hip_targets={PCI: "gfx1201"})
        self.assertEqual(self.selection(), before)

    def test_registered_fixture_executable_corroborates_the_exact_selected_card(self):
        fixture_root = self.home / "runtime-fixture"
        _agents, fixture_bin = runtime_fixture.create(fixture_root)
        shutil.copy2(fixture_bin / "llama-server", self.hip / "bin" / "llama-server")
        self.record_server_digest(self.hip)
        self.connection.execute("DELETE FROM builds WHERE build_id = 'hip'")
        registry.register(self.connection, "hip", str(self.hip))
        identities = lambda _backend, env: [
            {"pciAddress": PCI, "uuid": runtime_fixture.HIP_UUID}
        ] if env.get("ROCR_VISIBLE_DEVICES") in (None, runtime_fixture.HIP_UUID) else []
        with (
            mock.patch.object(matuto_runtime, "inspect_installation", return_value={
                "tools": {"llama-server": {"state": "completed",
                                            "capabilities": ["--device", "--list-devices"]}}}),
            mock.patch.object(matuto_runtime, "pci_devices", return_value={
                "state": "completed", "devices": [runtime_fixture.HOST]}),
            mock.patch.object(matuto_runtime, "bounded_identity_probe", side_effect=identities),
        ):
            selected = runtime_selection.select(
                self.connection, "hip", "hip", "card-a", serving="stopped", runtime_run=False,
                detected_cards={PCI}, hip_targets={PCI: "gfx1201"})
        self.assertEqual((selected["build_id"], selected["card_id"]), ("hip", "card-a"))

    def test_real_fixture_corroboration_failure_preserves_the_existing_selection(self):
        self.choose()
        before = self.selection()
        fixture_root = self.home / "runtime-fixture"
        _agents, fixture_bin = runtime_fixture.create(fixture_root)
        shutil.copy2(fixture_bin / "llama-server", self.hip_old / "bin" / "llama-server")
        self.record_server_digest(self.hip_old)
        with (
            mock.patch.object(matuto_runtime, "inspect_installation", return_value={
                "tools": {"llama-server": {"state": "completed",
                                            "capabilities": ["--device", "--list-devices"]}}}),
            mock.patch.object(matuto_runtime, "pci_devices", return_value={
                "state": "completed", "devices": [runtime_fixture.HOST]}),
            mock.patch.object(matuto_runtime, "bounded_identity_probe", return_value=[
                {"pciAddress": "0000:04:00.0", "uuid": "GPU-other"}]),
        ):
            with self.assertRaisesRegex(store.StoreError, "uniquely corroborate"):
                runtime_selection.use(
                    self.connection, "hip-old", serving="stopped", runtime_run=False,
                    detected_cards={PCI}, hip_targets={PCI: "gfx1201"})
        self.assertEqual(self.selection(), before)

    def test_failed_database_replacement_keeps_the_existing_selection(self):
        self.choose()
        before = self.selection()
        self.connection.execute(
            "CREATE TRIGGER refuse_runtime_selection_update BEFORE UPDATE ON runtime_selection"
            " BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected write failure"):
            self.choose(build="hip-old")
        self.assertEqual(self.selection(), before)

    def test_unavailable_build_and_failed_device_check_preserve_old_selection(self):
        self.choose()
        before = self.selection()
        self.connection.execute("UPDATE builds SET state = 'unavailable' WHERE build_id = 'hip-old'")
        with self.assertRaisesRegex(store.StoreError, "unavailable"):
            runtime_selection.use(self.connection, "hip-old", serving="stopped", runtime_run=False,
                                  detected_cards={PCI}, validate_device=lambda *_: True,
                                  hip_targets={PCI: "gfx1201"})
        self.connection.execute("UPDATE builds SET state = 'registered' WHERE build_id = 'hip-old'")
        with self.assertRaisesRegex(store.StoreError, "does not corroborate"):
            runtime_selection.use(self.connection, "hip-old", serving="stopped", runtime_run=False,
                                  detected_cards={PCI}, validate_device=lambda *_: False,
                                  hip_targets={PCI: "gfx1201"})
        self.assertEqual(self.selection(), before)


if __name__ == "__main__":
    unittest.main()
