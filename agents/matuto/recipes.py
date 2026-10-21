"""The four fixed runtime build recipes. See README.md for the conventions.

A recipe names one canonical official origin and the exact CMake Release
configure options and server target that origin is built with, so a build never
invents an option. The request inputs are the recipe, the full commit and — for
llama.cpp HIP alone — the gfx target; the clean source and staging directories
are the caller's build job. There is no parameter for a fork URL, a patch, a
command or a free-form CMake option, and no configured remote or operator
checkout is consulted: the clone always names the canonical origin.

The RPATH options are the install probe's: every recipe builds with
`-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON`; the installable llama.cpp and whisper
recipes also freeze `-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON
-DCMAKE_INSTALL_RPATH=$ORIGIN`, whose `$ORIGIN` reaches CMake literally because
the argv is never given to a shell. Breeze has no install rules and its in-tree
RUNPATH breaks under those two, so it takes neither.
"""

import re
from dataclasses import dataclass

# The four fixed recipes, named as the spec's Runtime builds list them.
LLAMA_HIP = "llama.cpp HIP"
LLAMA_VULKAN = "llama.cpp Vulkan"
BREEZE = "Breeze synthesis (Vulkan)"
WHISPER = "whisper recognition (CPU)"

LLAMA_ORIGIN = "https://github.com/ggml-org/llama.cpp"
BREEZE_ORIGIN = "https://github.com/HoppouAI/Breeze-TTS-2.cpp"
WHISPER_ORIGIN = "https://github.com/ggml-org/whisper.cpp"

COMMIT = re.compile(r"[0-9a-f]{40}")
GFX_TARGET = re.compile(r"gfx[0-9a-f]{3,4}")

BUILD_RPATH = "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON"
INSTALL_RPATH = ("-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON", "-DCMAKE_INSTALL_RPATH=$ORIGIN")


@dataclass(frozen=True)
class Recipe:
    """One fixed recipe: where it comes from, how it is configured, what it builds."""

    origin: str
    recursive: bool
    takes_gfx: bool
    server: str
    options: tuple


# The configure options are the recipe research's, one entry per CMake argument.
RECIPES = {
    LLAMA_HIP: Recipe(LLAMA_ORIGIN, False, True, "llama-server",
                      ("-DGGML_HIP=ON", "-DGPU_TARGETS={target}") + (BUILD_RPATH,) + INSTALL_RPATH),
    LLAMA_VULKAN: Recipe(LLAMA_ORIGIN, False, False, "llama-server",
                         ("-DGGML_VULKAN=ON",) + (BUILD_RPATH,) + INSTALL_RPATH),
    BREEZE: Recipe(BREEZE_ORIGIN, True, False, "breeze-server",
                   ("-DBREEZE_VULKAN=ON", "-DBREEZE_BUILD_SERVER=ON", BUILD_RPATH)),
    WHISPER: Recipe(WHISPER_ORIGIN, False, False, "whisper-server",
                    ("-DWHISPER_BUILD_EXAMPLES=ON", "-DWHISPER_BUILD_TESTS=OFF",
                     "-DGGML_VULKAN=OFF", "-DGGML_HIP=OFF", "-DGGML_CUDA=OFF",
                     BUILD_RPATH) + INSTALL_RPATH),
}


@dataclass(frozen=True)
class Plan:
    """The argv a build job runs, in order, plus the identity it was made for."""

    recipe: str
    origin: str
    commit: str
    target: object
    clone: list
    configure: list
    build: list


def plan(recipe, commit, target=None, *, source, staging):
    """The clone, configure and build argv for one recipe at a full commit.

    `source` is the clean checkout directory and `staging` the build directory;
    both belong to the build job. A bad recipe, commit or target is refused
    rather than repaired, and a gfx target is refused for every non-HIP recipe.
    """
    chosen = RECIPES.get(recipe)
    if chosen is None:
        raise ValueError(f"{recipe!r} is not one of the four fixed recipes")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError(f"{commit!r} is not a full 40-character commit")
    if chosen.takes_gfx:
        if not isinstance(target, str) or not GFX_TARGET.fullmatch(target):
            raise ValueError(f"{target!r} is not a gfx target")
    elif target is not None:
        raise ValueError(f"the {recipe} recipe takes no gfx target")

    clone = ["git", "clone"]
    if chosen.recursive:
        clone.append("--recursive")
    clone += [chosen.origin, str(source)]
    options = [option.format(target=target) if "{target}" in option else option
               for option in chosen.options]
    configure = ["cmake", "-S", str(source), "-B", str(staging), "-G", "Ninja",
                 "-DCMAKE_BUILD_TYPE=Release"] + options
    build = ["cmake", "--build", str(staging), "--target", chosen.server]
    return Plan(recipe, chosen.origin, commit, target, clone, configure, build)
