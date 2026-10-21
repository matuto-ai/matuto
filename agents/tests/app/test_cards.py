"""Card detection and creation through Matuto's store API and fixture sysfs."""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import cards, store  # noqa: E402

GPU = "0000:03:00.0"
ATTRS = {"vendor": "0x1002", "device": "0x747e", "class": "0x030000",
         "subsystem_vendor": "0x1002", "subsystem_device": "0x0e1b"}
NVIDIA = {"vendor": "0x10de", "device": "0x1eb8", "class": "0x030000",
          "subsystem_vendor": "0x10de", "subsystem_device": "0x12d2"}


class CardsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pci = self.root / "pci"
        self.drm = self.root / "drm"
        self.ids = self.root / "pci.ids"
        self.ids.write_text("1002  AMD\n\t747e  Display accelerator\n"
                            "10de  NVIDIA\n\t1eb8  GeForce accelerator\n")
        self.store = store.open_store(self.root / "config")
        self.addCleanup(self.store.close)
        self.add_gpu()

    def add_gpu(self, identity=GPU, attrs=None, *, vram="32000000000"):
        device = self.pci / identity
        device.mkdir(parents=True)
        for name, value in (attrs or ATTRS).items():
            (device / name).write_text(value)
        if vram is not None:
            drm_card = self.drm / f"card{len(list(self.drm.glob('card*')))}"
            drm_card.mkdir(parents=True, exist_ok=True)
            (drm_card / "device").symlink_to(device)
            (drm_card / "device" / "mem_info_vram_total").write_text(vram)
        return {"pciAddress": identity, **(attrs or ATTRS)}

    def detect(self, **kwargs):
        return cards.detect(self.store, pci_root=self.pci, drm_root=self.drm,
                            pci_ids=(self.ids,), **kwargs)

    def card_values(self, card_id="card-r9", **changes):
        return {"card_id": card_id, "smoke_output_tokens": 16,
                "bench_prompt_tokens": 4096, "bench_generation_tokens": 512,
                "bench_repetitions": 5, **changes}

    def create_card(self, card, selected):
        detect = cards.detect
        with patch.object(cards, "detect", side_effect=lambda store, **_kwargs: detect(
                store, pci_root=self.pci, drm_root=self.drm, pci_ids=(self.ids,))):
            return cards.add(self.store, card, selected, pci_root=self.pci,
                             drm_root=self.drm, pci_ids=(self.ids,))

    def test_store_creation_uses_its_own_current_detection(self):
        record = {**self.card_values("card-api"), "kind": "card", "name": "API card",
                  "gpu_identity": GPU, "vram_ceiling_bytes": 32000000001,
                  "default_gpu_layers": "all", "notes": ""}
        missing = {"state": "completed", "devices": [], "cards": []}
        with patch.object(cards, "detect", return_value=missing):
            with self.assertRaisesRegex(store.RecordRefusal, "detection does not report"):
                store.create_card(self.store, record)

        detected = {"state": "completed", "devices": [
            {"pciAddress": GPU, "vramBytes": 32000000000}]}
        with patch.object(cards, "detect", return_value=detected):
            with self.assertRaisesRegex(store.RecordRefusal, "must not exceed"):
                store.create_card(self.store, record)
        self.assertEqual(self.store.connection().execute("SELECT count(*) FROM cards").fetchone()[0], 0)

        self.add_gpu("0000:04:00.0", vram="16000000000")
        inventory = self.detect()
        records = (
            dict(record, card_id="card-api-all", vram_ceiling_bytes=32000000000,
                 default_gpu_layers="all"),
            dict(record, card_id="card-api-numeric", gpu_identity="0000:04:00.0",
                 vram_ceiling_bytes=16000000000, default_gpu_layers=42))
        with patch.object(cards, "detect", return_value=inventory):
            all_layers, numeric = (store.create_card(self.store, item) for item in records)

        for saved, expected in (
                (all_layers, (GPU, "all", 32000000000)),
                (numeric, ("0000:04:00.0", 42, 16000000000))):
            with self.subTest(card=saved["card_id"]):
                self.assertEqual((saved["gpu_identity"], saved["default_gpu_layers"],
                                  saved["vram_ceiling_bytes"]), expected)
                self.assertEqual((saved["smoke_output_tokens"], saved["bench_prompt_tokens"],
                                  saved["bench_generation_tokens"], saved["bench_repetitions"],
                                  saved["notes"]), (16, 4096, 512, 5, ""))
        self.assertEqual(self.store.connection().execute("SELECT count(*) FROM cards").fetchone()[0], 2)

    def test_detection_lists_identity_vram_and_optional_name(self):
        before = hashlib.sha256(self.store.path.read_bytes()).digest()
        found = self.detect()
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).digest(), before)
        self.assertEqual(found["state"], "completed")
        self.assertEqual(found["devices"], [{**ATTRS, "pciAddress": GPU,
                                               "displayName": "Display accelerator",
                                               "vramBytes": 32000000000, "offered": True}])
        self.assertNotEqual(found["devices"][0].get("displayName"), found["devices"][0]["pciAddress"])

    def test_vram_requires_the_drm_link_to_match_the_pci_tree_device(self):
        drm_device = self.drm / "card0" / "device"
        drm_device.unlink()
        redirected = self.root / "redirected" / GPU
        redirected.mkdir(parents=True)
        (redirected / "mem_info_vram_total").write_text("64000000000")
        drm_device.symlink_to(redirected)

        self.assertIsNone(self.detect()["devices"][0]["vramBytes"])

    def test_pci_names_ignore_symlinked_files(self):
        target = self.root / "other-pci.ids"
        target.write_text("1002  AMD\n\t747e  Redirected display name\n")
        link = self.root / "pci.ids-link"
        link.symlink_to(target)

        found = cards.detect(self.store, pci_root=self.pci, drm_root=self.drm, pci_ids=(link,))

        self.assertNotIn("displayName", found["devices"][0])

    def test_detection_includes_physical_gpus_from_other_vendors(self):
        self.add_gpu("0000:05:00.0", NVIDIA, vram="16000000000")

        found = self.detect()

        self.assertEqual([(device["vendor"], device["displayName"], device["vramBytes"])
                          for device in found["devices"]], [
                              ("0x1002", "Display accelerator", 32000000000),
                              ("0x10de", "GeForce accelerator", 16000000000)])
        saved = self.create_card(self.card_values("card-nvidia"), found["devices"][1])
        self.assertEqual(saved["gpu_identity"], "0000:05:00.0")

    def test_card_creation_uses_detection_and_defaults_to_full_layers(self):
        detected = self.detect()["devices"][0]
        stale_capacity = dict(detected, vramBytes=64000000000)
        saved = self.create_card(self.card_values(), stale_capacity)
        self.assertEqual(saved["gpu_identity"], GPU)
        self.assertEqual(saved["name"], "Display accelerator")
        self.assertEqual(saved["vram_ceiling_bytes"], 32000000000)
        self.assertEqual(saved["default_gpu_layers"], "all")
        self.assertEqual(self.store.connection().execute(
            "SELECT default_gpu_layers FROM cards WHERE card_id = 'card-r9'").fetchone()[0], -1)
        self.assertEqual(cards.detect(self.store, pci_root=self.pci, drm_root=self.drm,
                                      pci_ids=(self.ids,))["devices"][0]["offered"], False)

        second = self.add_gpu("0000:04:00.0")
        selected = next(device for device in self.detect()["devices"]
                        if device["pciAddress"] == second["pciAddress"])
        with self.assertRaisesRegex(store.RecordRefusal, "no greater than detected GPU VRAM"):
            self.create_card(self.card_values("card-too-large", vram_ceiling_bytes=32000000001),
                             selected)
        custom = self.create_card(self.card_values("card-custom", default_gpu_layers=42,
                                                   vram_ceiling_bytes=16000000000), selected)
        self.assertEqual(custom["default_gpu_layers"], 42)
        self.assertEqual(custom["vram_ceiling_bytes"], 16000000000)

    def test_typed_or_stale_gpu_and_duplicate_card_are_refused(self):
        with self.assertRaisesRegex(store.RecordRefusal, "choose a currently detected GPU"):
            self.create_card(self.card_values(), GPU)
        detected = self.detect()["devices"][0]
        with self.assertRaisesRegex(store.RecordRefusal, "typed identity"):
            self.create_card(self.card_values(), dict(detected, device="0x0001"))
        shutil.rmtree(self.pci)
        self.pci.mkdir()
        shutil.rmtree(self.drm)
        with self.assertRaisesRegex(store.RecordRefusal, "currently detected GPU"):
            self.create_card(self.card_values(), detected)
        self.add_gpu()
        detected = self.detect()["devices"][0]
        self.create_card(self.card_values(), detected)
        with self.assertRaisesRegex(store.RecordRefusal, "already has a card"):
            self.create_card(self.card_values("duplicate"), detected)

    def test_stored_card_reports_when_its_gpu_disappears_without_changing(self):
        detected = self.detect()["devices"][0]
        self.create_card(self.card_values(), detected)
        before = store.read_card(self.store, "card-r9")
        shutil.rmtree(self.pci)
        self.pci.mkdir()
        found = self.detect()
        self.assertEqual(found["state"], "completed")
        self.assertEqual(found["cards"][0]["detected"], False)
        self.assertEqual(store.read_card(self.store, "card-r9"), before)

    def test_unreadable_tree_is_unavailable_and_bad_attributes_are_warned_and_omitted(self):
        detected = self.detect()["devices"][0]
        self.create_card(self.card_values(), detected)
        saved = store.read_card(self.store, "card-r9")
        missing = cards.detect(self.store, pci_root=self.root / "missing", drm_root=self.drm,
                               pci_ids=(self.ids,))
        self.assertEqual(missing["state"], "unavailable")
        self.assertEqual(missing["devices"], [])
        self.assertIsNone(missing["cards"][0]["detected"])
        self.assertEqual(store.read_card(self.store, "card-r9"), saved)

        bad = self.pci / "0000:04:00.0"
        bad.mkdir()
        (bad / "vendor").write_text("0x1002")
        self.add_gpu("0000:05:00.0", dict(ATTRS, vendor="0x10021"), vram=None)
        partial = self.detect()
        self.assertEqual(len(partial["devices"]), 1)
        self.assertIn("warning", partial)

    def test_unreadable_or_invalid_vram_stays_unknown(self):
        shutil.rmtree(self.drm)
        unknown = self.detect()["devices"][0]
        self.assertIsNone(unknown["vramBytes"])
        with self.assertRaisesRegex(store.RecordRefusal, "VRAM is unknown"):
            self.create_card(self.card_values(), unknown)

        (self.pci / GPU / "mem_info_vram_total").write_text("0")
        self.assertIsNone(self.detect()["devices"][0]["vramBytes"])


if __name__ == "__main__":
    unittest.main()
