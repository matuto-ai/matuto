"""Read build targets and detectable prerequisites without starting a build."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from matuto import cards, recipes

TIMEOUT_SECONDS = 15
BDF = re.compile(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])")
GFX = re.compile(r"gfx[0-9a-f]{3,4}")


class PrerequisiteError(ValueError):
    """A fixed recipe cannot build until its named host requirements exist."""


class RuntimeTargetError(ValueError):
    """A HIP build target is not available for the selected physical GPU."""


def choices(store, *, pci_root=Path("/sys/bus/pci/devices"),
            drm_root=Path("/sys/class/drm"), pci_ids=cards.PCI_IDS):
    """Offer recipe inputs and per-card HIP targets; never infer a target.

    HIP cards without a host-reported target remain visible but cannot be chosen.
    Only a runtime selection's stored card is the default; an empty store has no
    default and still offers detected cards for the first build.
    """
    inventory = cards.detect(store, pci_root=pci_root, drm_root=drm_root, pci_ids=pci_ids)
    selected = store.connection().execute(
        "SELECT card_id FROM runtime_selection WHERE selection_id = 1").fetchone()
    active_card = selected["card_id"] if selected else None
    target_state, targets, reason = _host_targets()
    devices = []
    targets_by_card = _targets_for_cards(inventory["devices"], targets)
    for device in inventory["devices"]:
        target = targets_by_card.get(device["pciAddress"])
        devices.append({
            **device,
            "gfxTarget": target,
            "canBuildHIP": target is not None,
            "targetReason": None if target else reason or "rocminfo reported no target for this card.",
            "default": any(card["gpu_identity"] == device["pciAddress"] and
                           card["card_id"] == active_card for card in inventory["cards"]),
        })
    return {
        "state": inventory["state"] if inventory["state"] != "completed" else target_state,
        "reason": inventory.get("reason") or (reason if target_state == "unavailable" else None),
        "warning": inventory.get("warning"),
        "recipes": [
            {"name": name, "takesGfxTarget": recipe.takes_gfx,
             "missingPrerequisites": missing,
             "buildable": not missing}
            for name, recipe in recipes.RECIPES.items()
            for missing in (prerequisites(name),)
        ],
        "cards": devices,
    }


def prerequisites(recipe, *, path=None, exists=Path.exists, find=shutil.which,
                   run=subprocess.run, env=None):
    """Name detectable missing prerequisites for one fixed recipe.

    `path`, `exists`, `find`, and `run` are injectable for fixture tests. No
    package manager, privilege escalation, build command or GPU workload runs.
    """
    if recipe not in recipes.RECIPES:
        raise ValueError(f"{recipe!r} is not one of the four fixed recipes")
    env = os.environ if env is None else env
    path = env.get("PATH", os.defpath) if path is None else path
    missing = []

    def tool(name, label=None):
        if find(name, path=path) is None:
            missing.append(label or name)

    def one_of(names, label):
        if not any(find(name, path=path) is not None for name in names):
            missing.append(label)

    for name in ("git", "cmake", "ninja"):
        tool(name)
    if recipe == recipes.WHISPER:
        one_of(("cc", "gcc", "clang"), "a C compiler (cc, gcc, or clang)")
        one_of(("c++", "g++", "clang++"), "a C++ compiler (c++, g++, or clang++)")
        return missing

    cxx = next((name for name in ("c++", "g++", "clang++")
                if find(name, path=path) is not None), None)
    if cxx is None:
        missing.append("a host C++ compiler (c++, g++, or clang++)")
    if recipe == recipes.BREEZE and cxx and not _supports_cxx17(cxx, run, env):
        missing.append("a C++17 compiler")
    if recipe == recipes.LLAMA_HIP:
        tool("hipconfig")
        if find("hipconfig", path=path) is None:
            return missing
        location, error = _command(["hipconfig", "-l"], run, env)
        if error:
            missing.append(f"HIP compiler location ({error})")
        else:
            clang = Path(location) / "clang"
            if not _executable(clang, exists):
                missing.append("HIP compiler clang (from hipconfig -l)")
        root, error = _command(["hipconfig", "-R"], run, env)
        if error:
            missing.append(f"ROCm HIP, hipBLAS and rocBLAS ({error})")
        else:
            missing.extend(_hip_development(Path(root), exists))
        return missing

    if recipe in (recipes.LLAMA_VULKAN, recipes.BREEZE):
        if recipe == recipes.BREEZE:
            _cmake_minimum(missing, run, env, path, find)
        if not _glslc_available(env, path, find, exists):
            missing.append("glslc")
        sdk = env.get("VULKAN_SDK")
        # shortcut: checks VULKAN_SDK and standard Linux prefixes; use CMake discovery for other prefixes.
        headers = [Path(sdk) / "include/vulkan/vulkan.h"] if sdk else []
        headers += [Path("/usr/include/vulkan/vulkan.h"),
                    Path("/usr/local/include/vulkan/vulkan.h")]
        spirv = [Path(sdk) / "include/spirv/unified1/spirv.hpp"] if sdk else []
        spirv += [Path("/usr/include/spirv/unified1/spirv.hpp"),
                  Path("/usr/local/include/spirv/unified1/spirv.hpp")]
        if not any(exists(candidate) for candidate in headers):
            missing.append("Vulkan development headers")
        if not any(exists(candidate) for candidate in spirv):
            missing.append("SPIRV-Headers")
        if not _vulkan_loader(env, exists):
            missing.append("Vulkan loader")
    return missing


def require_prerequisites(recipe, **kwargs):
    """Refuse a recipe when its detectable prerequisites are missing."""
    missing = prerequisites(recipe, **kwargs)
    if missing:
        raise PrerequisiteError(
            f"Missing prerequisites for {recipe}: {', '.join(missing)}")


def build_plan(store, recipe, commit, *, source, staging, card_pci_address=None,
               pci_root=Path("/sys/bus/pci/devices"), drm_root=Path("/sys/class/drm"),
               pci_ids=cards.PCI_IDS, **host):
    """Return a fixed plan only after its prerequisites and physical target pass."""
    if recipe not in recipes.RECIPES:
        raise ValueError(f"{recipe!r} is not one of the four fixed recipes")
    require_prerequisites(recipe, **host)
    target = None
    if recipes.RECIPES[recipe].takes_gfx:
        inventory = cards.detect(store, pci_root=pci_root, drm_root=drm_root, pci_ids=pci_ids)
        if inventory["state"] != "completed":
            raise RuntimeTargetError(inventory.get("reason") or "GPU detection is unavailable")
        device = next((item for item in inventory["devices"]
                       if item["pciAddress"] == card_pci_address), None)
        if device is None:
            raise RuntimeTargetError("choose a currently detected physical GPU")
        _state, reports, reason = _host_targets()
        targets_by_card = _targets_for_cards(inventory["devices"], reports)
        target = targets_by_card.get(card_pci_address)
        if target is None:
            raise RuntimeTargetError(reason or
                                     f"no HIP target was read for GPU {card_pci_address}")
    elif card_pci_address is not None:
        raise ValueError(f"the {recipe} recipe takes no GPU target")
    return recipes.plan(recipe, commit, target, source=source, staging=staging)


def _host_targets():
    try:
        result = subprocess.run(["rocminfo"], cwd="/", capture_output=True, text=True,
                                timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "unavailable", {}, f"Could not read per-card HIP targets: {error}."
    if result.returncode != 0:
        detail = result.stderr.strip() or f"rocminfo exited {result.returncode}"
        return "unavailable", {}, f"Could not read per-card HIP targets: {detail}."
    targets = _parse_rocminfo(result.stdout)
    if not targets:
        return "completed", {}, "rocminfo reported no per-card HIP target."
    return "completed", targets, None


def _parse_rocminfo(output):
    """Map rocminfo GPU agents to PCI BDFs; ambiguous identities are omitted."""
    agents = []
    current = None
    for line in output.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"Agent\s+\d+", stripped):
            current = {}
            agents.append(current)
        elif current is not None and len(line) - len(line.lstrip()) == 2 and ":" in stripped:
            key, value = (part.strip() for part in stripped.split(":", 1))
            if key in ("Name", "Device Type", "BDFID"):
                current[key] = value
    matches = {}
    ambiguous = set()
    for agent in agents:
        if agent.get("Device Type", "").lower() != "gpu":
            continue
        target = agent.get("Name", "")
        address = _bdf_from_id(agent.get("BDFID", ""))
        if not address or not GFX.fullmatch(target):
            continue
        if address in matches:
            ambiguous.add(address)
        matches[address] = target
    return {address: target for address, target in matches.items() if address not in ambiguous}


def _bdf_from_id(value):
    match = BDF.fullmatch(value)
    if match:
        return (match[1].lower() + ":" + match[2].lower() + ":" +
                match[3].lower() + "." + match[4])
    # Numeric ROCm BDFID packs bus, device and function but omits the PCI domain.
    if not re.fullmatch(r"\d+", value):
        return None
    packed = int(value)
    bus, device, function = packed >> 8, (packed >> 3) & 0x1f, packed & 7
    if bus > 0xff:
        return None
    return f"{bus:02x}:{device:02x}.{function}"


def _targets_for_cards(devices, reports):
    """Join numeric BDFIDs only when one detected PCI domain can match."""
    addresses = [device["pciAddress"].lower() for device in devices]
    result = {}
    for reported, target in reports.items():
        if reported.count(":") == 2:
            if reported in addresses:
                result[reported] = target
            continue
        matches = [address for address in addresses if address.endswith(":" + reported)]
        if len(matches) == 1:
            result[matches[0]] = target
    return result


def _command(argv, run, env):
    try:
        result = run(argv, cwd="/", capture_output=True, text=True,
                      timeout=TIMEOUT_SECONDS, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "", str(error)
    output = result.stdout.strip()
    if result.returncode or not output:
        return "", result.stderr.strip() or f"{argv[0]} returned no answer"
    return output, None


def _supports_cxx17(compiler, run, env):
    try:
        result = run([compiler, "-std=c++17", "-x", "c++", "-fsyntax-only", "-"],
                     cwd="/", capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                     check=False, env=env, input="int main() {}\n")
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _cmake_minimum(missing, run, env, path, find):
    if find("cmake", path=path) is None:
        return
    output, error = _command(["cmake", "--version"], run, env)
    match = re.search(r"cmake version (\d+)\.(\d+)", output)
    if error or not match or tuple(map(int, match.groups())) < (3, 19):
        missing.append("CMake 3.19 or later")


def _hip_development(root, exists):
    if not root.is_absolute():
        return ["ROCm HIP development prefix"]
    prefixes = tuple(dict.fromkeys((root, Path("/usr"), Path("/usr/local"))))
    libraries = tuple(prefix / directory for prefix in prefixes for directory in
                      ("lib", "lib64", "lib/x86_64-linux-gnu"))
    missing = []
    packages = (
        ("HIP", "hip", "hip/hip_runtime.h", "libamdhip64.so"),
        ("hipBLAS", "hipblas", "hipblas/hipblas.h", "libhipblas.so"),
        ("rocBLAS", "rocblas", "rocblas/rocblas.h", "librocblas.so"),
    )
    for name, package, header, library in packages:
        if not any(exists(prefix / "include" / header) for prefix in prefixes):
            missing.append(f"{name} development headers")
        config = f"{package}-config.cmake"
        config_dirs = (prefix / directory / "cmake" / package for prefix in prefixes
                       for directory in ("lib", "lib64", "lib/x86_64-linux-gnu"))
        if not any(exists(directory / config) for directory in config_dirs):
            missing.append(f"{name} CMake package")
        if not any(exists(directory / library) for directory in libraries):
            missing.append(f"{name} linker library")
    return missing


def _glslc_available(env, path, find, exists):
    if find("glslc", path=path) is not None:
        return True
    sdk = env.get("VULKAN_SDK")
    return bool(sdk and any(exists(Path(sdk) / relative)
                            for relative in ("bin/glslc", "Bin/glslc")))


def _vulkan_loader(env, exists):
    sdk = env.get("VULKAN_SDK")
    libs = ([Path(sdk) / relative for relative in
             ("lib/libvulkan.so", "lib64/libvulkan.so")] if sdk else [])
    libs += [Path("/usr/lib/libvulkan.so"), Path("/usr/lib64/libvulkan.so"),
             Path("/usr/lib/x86_64-linux-gnu/libvulkan.so")]
    return any(exists(path) for path in libs)


def _executable(path, exists):
    return exists(path) and os.access(path, os.X_OK)
