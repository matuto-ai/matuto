"""Per-card runtime targets and prerequisite checks use fixture host inputs."""

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import cards, recipes, runtime_builds, store  # noqa: E402


class RuntimeBuildsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.pci = self.root / "pci"
        self.drm = self.root / "drm"
        self.ids = self.root / "pci.ids"
        self.ids.write_text("1002  AMD\n\t747e  Integrated\n\t744c  Discrete\n")
        self.store = store.open_store(self.root / "config")
        self.addCleanup(self.store.close)
        self.add_gpu("0000:00:01.0", "0x747e", "32000000000")
        self.add_gpu("0000:03:00.0", "0x744c", "64000000000")

    def add_gpu(self, address, device_id, vram):
        device = self.pci / address
        device.mkdir(parents=True)
        for name, value in {"vendor": "0x1002", "device": device_id,
                            "class": "0x030000", "subsystem_vendor": "0x1002",
                            "subsystem_device": "0x0e1b"}.items():
            (device / name).write_text(value)
        drm = self.drm / f"card{len(list(self.drm.glob('card*')))}"
        drm.mkdir(parents=True)
        (drm / "device").symlink_to(device)
        (drm / "device" / "mem_info_vram_total").write_text(vram)

    @staticmethod
    def rocminfo():
        return """Agent 1\n  Name: gfx1036\n  Device Type: GPU\n  BDFID: 8\n  ISA Info:\n    ISA 1\n      Name: amdgcn-amd-amdhsa--gfx1036\nAgent 2\n  Name: gfx1201\n  Device Type: GPU\n  BDFID: 768\n  ISA Info:\n    ISA 1\n      Name: amdgcn-amd-amdhsa--gfx1201\nAgent 3\n  Name: gfx9999\n  Device Type: CPU\n  BDFID: 768\n"""

    def choices(self, output=None):
        if output is None:
            output = self.rocminfo()
        with patch.object(runtime_builds, "_host_targets",
                          return_value=("completed", runtime_builds._parse_rocminfo(output), None)), \
                patch.object(runtime_builds, "prerequisites", return_value=[]):
            return runtime_builds.choices(self.store, pci_root=self.pci, drm_root=self.drm,
                                          pci_ids=(self.ids,))

    def add_card(self, card_id, address):
        detected = cards.detect(self.store, pci_root=self.pci, drm_root=self.drm,
                                pci_ids=(self.ids,))
        device = next(item for item in detected["devices"] if item["pciAddress"] == address)
        detect = cards.detect
        with patch.object(cards, "detect", side_effect=lambda store, **_kwargs: detect(
                store, pci_root=self.pci, drm_root=self.drm, pci_ids=(self.ids,))):
            cards.add(self.store, {"card_id": card_id, "smoke_output_tokens": 16,
                                  "bench_prompt_tokens": 4096, "bench_generation_tokens": 512,
                                  "bench_repetitions": 5, "vram_ceiling_bytes": 64000000000},
                       device, pci_root=self.pci, drm_root=self.drm, pci_ids=(self.ids,))

    def test_each_detected_card_gets_only_its_own_host_target_and_no_first_use_default(self):
        before = hashlib.sha256(self.store.path.read_bytes()).digest()

        result = self.choices()

        by_address = {item["pciAddress"]: item for item in result["cards"]}
        self.assertEqual(by_address["0000:00:01.0"]["gfxTarget"], "gfx1036")
        self.assertEqual(by_address["0000:03:00.0"]["gfxTarget"], "gfx1201")
        self.assertFalse(any(item["default"] for item in result["cards"]))
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).digest(), before)

    def test_targetless_card_stays_visible_but_is_not_choosable_for_hip(self):
        output = self.rocminfo().replace("  BDFID: 768", "  BDFID: 999")
        result = self.choices(output)
        card = next(item for item in result["cards"] if item["pciAddress"] == "0000:03:00.0")
        self.assertIsNone(card["gfxTarget"])
        self.assertFalse(card["canBuildHIP"])
        self.assertIn("target", card["targetReason"])
        self.assertEqual([item["pciAddress"] for item in result["cards"]],
                         ["0000:00:01.0", "0000:03:00.0"])

    def test_rocminfo_failure_is_honestly_unavailable(self):
        with patch.object(runtime_builds, "_host_targets",
                          return_value=("unavailable", {}, "rocminfo could not run")):
            result = runtime_builds.choices(self.store, pci_root=self.pci, drm_root=self.drm,
                                            pci_ids=(self.ids,))
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["reason"], "rocminfo could not run")
        self.assertTrue(all(item["gfxTarget"] is None for item in result["cards"]))

    def test_partial_pci_read_reports_omitted_devices(self):
        (self.pci / "0000:03:00.0" / "vendor").unlink()

        result = self.choices()

        self.assertEqual(result["state"], "completed")
        self.assertIn("attributes were unavailable or invalid", result["warning"])
        self.assertEqual([device["pciAddress"] for device in result["cards"]],
                         ["0000:00:01.0"])

    def test_active_card_is_default_when_its_runtime_selection_exists(self):
        self.add_card("card-discrete", "0000:03:00.0")
        connection = self.store.connection()
        connection.execute(
            "INSERT INTO builds VALUES ('build', 'Build', 'llama-hip', ?, 'hip', ?, ?, ?, ?, 'registered', '')",
            (str(self.root / "build"), "a" * 40, "gfx1201", recipes.LLAMA_HIP, "ROCm"))
        connection.execute(
            "INSERT INTO runtime_selection VALUES (1, 'build', 'card-discrete', 'hip', 'now')")

        result = self.choices()

        self.assertEqual([item["pciAddress"] for item in result["cards"] if item["default"]],
                         ["0000:03:00.0"])

    def test_target_reader_uses_rocminfo_as_a_read_only_host_query(self):
        class Result:
            returncode = 0
            stdout = self.rocminfo()
            stderr = ""

        with patch.object(runtime_builds.subprocess, "run", return_value=Result()) as run:
            state, targets, reason = runtime_builds._host_targets()

        self.assertEqual(state, "completed")
        self.assertIsNone(reason)
        self.assertEqual(targets["03:00.0"], "gfx1201")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["rocminfo"])
        self.assertEqual(kwargs["cwd"], "/")
        self.assertEqual(kwargs["check"], False)

    def test_target_reader_reports_tool_failure_instead_of_empty_host(self):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "permission denied"

        with patch.object(runtime_builds.subprocess, "run", return_value=Result()):
            state, targets, reason = runtime_builds._host_targets()

        self.assertEqual(state, "unavailable")
        self.assertEqual(targets, {})
        self.assertIn("permission denied", reason)

    def test_rocminfo_bdf_mapping_requires_unambiguous_gpu_agent(self):
        output = self.rocminfo() + "Agent 4\n  Name: gfx1100\n  Device Type: GPU\n  BDFID: 768\n"
        targets = runtime_builds._parse_rocminfo(output)
        self.assertEqual(targets["00:01.0"], "gfx1036")
        self.assertNotIn("03:00.0", targets)
        self.assertEqual(runtime_builds._bdf_from_id("0000:03:00.0"), "0000:03:00.0")
        self.assertIsNone(runtime_builds._bdf_from_id("65536"))
        target = {"03:00.0": "gfx1201"}
        self.assertEqual(runtime_builds._targets_for_cards(
            [{"pciAddress": "0001:03:00.0"}], target), {"0001:03:00.0": "gfx1201"})
        self.assertEqual(runtime_builds._targets_for_cards(
            [{"pciAddress": "0000:03:00.0"}, {"pciAddress": "0001:03:00.0"}], target), {})

    def test_hip_prerequisites_require_development_headers_configs_and_linker_names(self):
        root = self.root / "rocm"
        libs = root / "lib"
        libs.mkdir(parents=True)
        for name in ("libamdhip64.so.1", "libhipblas.so.1", "librocblas.so.1"):
            (libs / name).touch()

        missing = runtime_builds._hip_development(root, Path.exists)

        self.assertEqual(missing, [
            "HIP development headers", "HIP CMake package", "HIP linker library",
            "hipBLAS development headers", "hipBLAS CMake package", "hipBLAS linker library",
            "rocBLAS development headers", "rocBLAS CMake package", "rocBLAS linker library",
        ])
        files = (
            "include/hip/hip_runtime.h", "lib/cmake/hip/hip-config.cmake", "lib/libamdhip64.so",
            "include/hipblas/hipblas.h", "lib/cmake/hipblas/hipblas-config.cmake",
            "lib/libhipblas.so", "include/rocblas/rocblas.h",
            "lib/cmake/rocblas/rocblas-config.cmake", "lib/librocblas.so",
        )
        for path in files:
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.touch()
        self.assertEqual(runtime_builds._hip_development(root, Path.exists), [])

    def test_vulkan_loader_requires_the_development_linker_name(self):
        self.assertFalse(runtime_builds._vulkan_loader({}, lambda _path: False))
        self.assertTrue(runtime_builds._vulkan_loader(
            {}, lambda path: path == Path("/usr/lib/x86_64-linux-gnu/libvulkan.so")))

    def test_recipe_prerequisites_name_missing_tools_and_versions_without_installing(self):
        def find(name, path=None):
            return None if name in {"ninja", "glslc"} else f"/fake/{name}"

        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            class Result:
                returncode = int(argv[0] == "c++" and kwargs.get("input") != "int main() {}\n")
                stderr = ""
                stdout = "cmake version 3.18.0\n" if argv == ["cmake", "--version"] else "/fake/llvm/bin"
            return Result()

        with patch.object(runtime_builds, "_hip_development", return_value=[]), \
                patch.object(runtime_builds, "_executable", return_value=True), \
                patch.object(runtime_builds, "_vulkan_loader", return_value=True):
            hip = runtime_builds.prerequisites(recipes.LLAMA_HIP, find=find, run=run,
                                               exists=lambda _path: True, env={})
            breeze = runtime_builds.prerequisites(recipes.BREEZE, find=find, run=run,
                                                  exists=lambda _path: True, env={})
            vulkan = runtime_builds.prerequisites(recipes.LLAMA_VULKAN, find=find, run=run,
                                                 exists=lambda _path: True, env={})
            whisper = runtime_builds.prerequisites(recipes.WHISPER, find=find, run=run,
                                                  exists=lambda _path: True, env={})
        self.assertEqual(hip, ["ninja"])
        self.assertEqual(breeze, ["ninja", "CMake 3.19 or later", "glslc"])
        self.assertEqual(vulkan, ["ninja", "glslc"])
        self.assertEqual(whisper, ["ninja"])
        self.assertEqual({call[0] for call in calls}, {"cmake", "hipconfig", "c++"})
        with patch.object(runtime_builds, "prerequisites", return_value=["ninja"]):
            with self.assertRaisesRegex(runtime_builds.PrerequisiteError, "ninja"):
                runtime_builds.require_prerequisites(recipes.BREEZE)
        with patch.object(runtime_builds, "prerequisites", return_value=["ninja"]), \
                patch.object(runtime_builds.recipes, "plan") as make_plan:
            with self.assertRaisesRegex(runtime_builds.PrerequisiteError, "ninja"):
                runtime_builds.build_plan(
                    self.store, recipes.BREEZE, "a" * 40, source="src", staging="build")
        make_plan.assert_not_called()

    def test_build_plan_uses_only_the_target_for_the_selected_detected_pci_card(self):
        with patch.object(runtime_builds, "prerequisites", return_value=[]), \
                patch.object(runtime_builds, "_host_targets",
                             return_value=("completed", runtime_builds._parse_rocminfo(
                                 self.rocminfo()), None)), \
                patch.object(runtime_builds.recipes, "plan", return_value="plan") as make_plan:
            result = runtime_builds.build_plan(
                self.store, recipes.LLAMA_HIP, "a" * 40, card_pci_address="0000:03:00.0",
                source="src", staging="build", pci_root=self.pci, drm_root=self.drm,
                pci_ids=(self.ids,))

        self.assertEqual(result, "plan")
        self.assertEqual(make_plan.call_args.args[:3],
                         (recipes.LLAMA_HIP, "a" * 40, "gfx1201"))

    def test_build_plan_refuses_typed_or_unreadable_targets_before_planning(self):
        with patch.object(runtime_builds, "prerequisites", return_value=[]), \
                patch.object(runtime_builds, "_host_targets",
                             return_value=("completed", runtime_builds._parse_rocminfo(
                                 self.rocminfo()), None)), \
                patch.object(runtime_builds.recipes, "plan") as make_plan:
            with self.assertRaisesRegex(runtime_builds.RuntimeTargetError, "currently detected"):
                runtime_builds.build_plan(
                    self.store, recipes.LLAMA_HIP, "a" * 40, card_pci_address="0000:09:00.0",
                    source="src", staging="build", pci_root=self.pci, drm_root=self.drm,
                    pci_ids=(self.ids,))
            targetless_report = {"00:01.0": "gfx1036"}
            with patch.object(runtime_builds, "_host_targets",
                              return_value=("completed", targetless_report, None)):
                with self.assertRaisesRegex(runtime_builds.RuntimeTargetError, "no HIP target"):
                    runtime_builds.build_plan(
                        self.store, recipes.LLAMA_HIP, "a" * 40,
                        card_pci_address="0000:03:00.0", source="src", staging="build",
                        pci_root=self.pci, drm_root=self.drm, pci_ids=(self.ids,))
        make_plan.assert_not_called()

    def test_build_plan_refuses_numeric_bdfid_ambiguous_across_pci_domains(self):
        self.add_gpu("0001:03:00.0", "0x744c", "64000000000")
        report = {"03:00.0": "gfx1201"}
        with patch.object(runtime_builds, "prerequisites", return_value=[]), \
                patch.object(runtime_builds, "_host_targets",
                             return_value=("completed", report, None)), \
                patch.object(runtime_builds.recipes, "plan") as make_plan:
            with self.assertRaisesRegex(runtime_builds.RuntimeTargetError, "no HIP target"):
                runtime_builds.build_plan(
                    self.store, recipes.LLAMA_HIP, "a" * 40,
                    card_pci_address="0001:03:00.0", source="src", staging="build",
                    pci_root=self.pci, drm_root=self.drm, pci_ids=(self.ids,))
        make_plan.assert_not_called()

    def test_non_hip_build_plan_rejects_a_selected_card_target(self):
        with patch.object(runtime_builds, "prerequisites", return_value=[]):
            with self.assertRaisesRegex(ValueError, "takes no GPU target"):
                runtime_builds.build_plan(
                    self.store, recipes.LLAMA_VULKAN, "a" * 40,
                    card_pci_address="0000:03:00.0", source="src", staging="build")

    def test_breeze_requires_a_compiler_that_accepts_cxx17(self):
        def run(argv, **_kwargs):
            class Result:
                returncode = 1 if argv[0] == "c++" else 0
                stderr = "unsupported standard" if argv[0] == "c++" else ""
                stdout = "cmake version 3.20.0"
            return Result()

        with patch.object(runtime_builds, "_vulkan_loader", return_value=True):
            missing = runtime_builds.prerequisites(
                recipes.BREEZE, find=lambda _name, path=None: "/fake/tool",
                run=run, exists=lambda _path: True, env={})
        self.assertEqual(missing, ["a C++17 compiler"])

    def test_all_recipe_choices_report_no_gfx_target_except_hip(self):
        result = self.choices()
        choices = {item["name"]: item for item in result["recipes"]}
        self.assertTrue(choices[recipes.LLAMA_HIP]["takesGfxTarget"])
        self.assertTrue(all(not item["takesGfxTarget"] for name, item in choices.items()
                            if name != recipes.LLAMA_HIP))
        self.assertTrue(all("missingPrerequisites" in item and "buildable" in item
                            for item in choices.values()))


if __name__ == "__main__":
    unittest.main()
