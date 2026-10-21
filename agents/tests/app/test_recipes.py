"""The four fixed build recipes, through their own Python API."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from matuto import recipes


COMMIT = "b29c606e28a01b1bc8c1351026a0fa6e616bf6c4"
BREEZE_COMMIT = "a5436642d4c64304b398ceeda9b8fce4577bfdb1"
WHISPER_COMMIT = "927cfce34f31707e17f2bff35c349632fb9e2c3a"
SOURCE = "/srv/matuto/source"
STAGING = "/srv/matuto/staging"
GFX = "gfx1201"

# Written out from the spec and the recipe research, not read from the
# implementation, so an added, dropped or altered option fails these tests.
FIXED = (recipes.LLAMA_HIP, recipes.LLAMA_VULKAN, recipes.BREEZE, recipes.WHISPER)
ORIGINS = {
    recipes.LLAMA_HIP: "https://github.com/ggml-org/llama.cpp",
    recipes.LLAMA_VULKAN: "https://github.com/ggml-org/llama.cpp",
    recipes.BREEZE: "https://github.com/HoppouAI/Breeze-TTS-2.cpp",
    recipes.WHISPER: "https://github.com/ggml-org/whisper.cpp",
}
OPTIONS = {
    recipes.LLAMA_HIP: ("-DGGML_HIP=ON", f"-DGPU_TARGETS={GFX}"),
    recipes.LLAMA_VULKAN: ("-DGGML_VULKAN=ON",),
    recipes.BREEZE: ("-DBREEZE_VULKAN=ON", "-DBREEZE_BUILD_SERVER=ON"),
    recipes.WHISPER: ("-DWHISPER_BUILD_EXAMPLES=ON", "-DWHISPER_BUILD_TESTS=OFF",
                      "-DGGML_VULKAN=OFF", "-DGGML_HIP=OFF", "-DGGML_CUDA=OFF"),
}
SERVERS = {
    recipes.LLAMA_HIP: "llama-server",
    recipes.LLAMA_VULKAN: "llama-server",
    recipes.BREEZE: "breeze-server",
    recipes.WHISPER: "whisper-server",
}
RELEASE = "-DCMAKE_BUILD_TYPE=Release"
BUILD_RPATH = "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON"
INSTALL_RPATH = ("-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN")
NO_INSTALL_RPATH = (recipes.BREEZE,)
COMMITS = {
    recipes.LLAMA_HIP: COMMIT,
    recipes.LLAMA_VULKAN: COMMIT,
    recipes.BREEZE: BREEZE_COMMIT,
    recipes.WHISPER: WHISPER_COMMIT,
}


def plan(recipe, commit=None, target=None):
    """A positive-case plan; HIP gets the only target a positive call needs."""
    if commit is None:
        commit = COMMITS[recipe]
    if target is None and recipe == recipes.LLAMA_HIP:
        target = GFX
    return recipes.plan(recipe, commit, target, source=SOURCE, staging=STAGING)


def options_of(configure):
    """The recipe's own CMake -D arguments, without the shared Release setting."""
    return [argument for argument in configure
            if argument.startswith("-D") and argument != RELEASE]


class RecipeTest(unittest.TestCase):
    def test_exactly_the_four_fixed_recipes_exist(self):
        self.assertEqual(sorted(recipes.RECIPES), sorted(FIXED))

    def test_each_recipe_clones_only_its_canonical_official_origin(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                built = plan(recipe)
                recursive = ["--recursive"] if recipe == recipes.BREEZE else []
                self.assertEqual(built.clone,
                                 ["git", "clone"] + recursive + [ORIGINS[recipe], SOURCE])

    def test_only_breeze_clones_recursively(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                self.assertEqual("--recursive" in plan(recipe).clone, recipe == recipes.BREEZE)

    def test_a_configured_source_environment_never_reaches_the_clone(self):
        with mock.patch.dict(os.environ, {"MATUTO_LLAMA_SOURCE": "/home/operator/llama.cpp"}):
            for recipe in FIXED:
                with self.subTest(recipe=recipe):
                    built = plan(recipe)
                    self.assertEqual(built.origin, ORIGINS[recipe])
                    self.assertNotIn("/home/operator/llama.cpp", built.clone)

    def test_each_recipe_configures_source_and_staging_as_a_release_build(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                self.assertEqual(plan(recipe).configure[:8],
                                 ["cmake", "-S", SOURCE, "-B", STAGING, "-G", "Ninja", RELEASE])

    def test_each_recipe_passes_the_research_options_and_never_another(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                fixed = options_of(plan(recipe).configure)
                expected = set(OPTIONS[recipe]) | {BUILD_RPATH}
                if recipe not in NO_INSTALL_RPATH:
                    expected |= set(INSTALL_RPATH)
                self.assertEqual(set(fixed), expected)

    def test_origin_reaches_cmake_literally(self):
        for recipe in FIXED:
            if recipe in NO_INSTALL_RPATH:
                continue
            with self.subTest(recipe=recipe):
                self.assertIn("-DCMAKE_INSTALL_RPATH=$ORIGIN", plan(recipe).configure)

    def test_the_hip_recipe_names_the_chosen_gfx_target(self):
        built = plan(recipes.LLAMA_HIP, COMMIT, "gfx1036")
        self.assertEqual(built.target, "gfx1036")
        self.assertIn("-DGPU_TARGETS=gfx1036", built.configure)

    def test_each_recipe_builds_its_server_target(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                self.assertEqual(plan(recipe).build,
                                 ["cmake", "--build", STAGING, "--target", SERVERS[recipe]])

    def test_every_emitted_command_is_an_argv_list_of_strings(self):
        for recipe in FIXED:
            with self.subTest(recipe=recipe):
                built = plan(recipe)
                for argv in (built.clone, built.configure, built.build):
                    self.assertIsInstance(argv, list)
                    for argument in argv:
                        self.assertIsInstance(argument, str)

    def test_no_fork_url_patch_command_or_option_can_be_supplied(self):
        for extra in ({"url": "https://evil.example/llama.cpp"}, {"patch": "p.diff"},
                      {"command": "make"}, {"options": "-DGGML_CUDA=ON"},
                      {"origin": "https://evil.example/llama.cpp"}):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                recipes.plan(recipes.WHISPER, WHISPER_COMMIT, source=SOURCE,
                             staging=STAGING, **extra)


class RefusalTest(unittest.TestCase):
    def test_an_unknown_recipe_is_refused(self):
        with self.assertRaises(ValueError):
            recipes.plan("llama.cpp OpenCL", COMMIT, source=SOURCE, staging=STAGING)

    def test_an_abbreviated_or_malformed_commit_is_refused(self):
        for commit in ("b29c606e", COMMIT[:12], COMMIT.upper(), COMMIT + "0", COMMIT[:-1],
                       "z" * 40, "", "HEAD", "main", 40, None):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                recipes.plan(recipes.LLAMA_HIP, commit, GFX, source=SOURCE, staging=STAGING)

    def test_a_gfx_target_is_refused_for_every_non_hip_recipe(self):
        for recipe in (recipes.LLAMA_VULKAN, recipes.BREEZE, recipes.WHISPER):
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                recipes.plan(recipe, COMMITS[recipe], GFX, source=SOURCE, staging=STAGING)

    def test_the_hip_recipe_requires_a_gfx_target(self):
        with self.assertRaises(ValueError):
            recipes.plan(recipes.LLAMA_HIP, COMMIT, source=SOURCE, staging=STAGING)

    def test_a_malformed_gfx_target_is_refused(self):
        for target in ("gfx12", "gfx12011", "GFX1201", "gfx1201z", "1201", "gfx", "", 1201, None):
            with self.subTest(target=target), self.assertRaises(ValueError):
                recipes.plan(recipes.LLAMA_HIP, COMMIT, target, source=SOURCE, staging=STAGING)
