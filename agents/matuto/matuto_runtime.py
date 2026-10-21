"""Read-only inspection of explicitly supplied, trusted llama.cpp installations."""

import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import tempfile

TOOLS = ("llama-server", "llama-cli", "llama-bench", "llama-perplexity")
VISIBILITY = ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES", "GGML_VK_VISIBLE_DEVICES")
MAX_OUTPUT = 262_144
PROBE_SECONDS = 5
class InspectionError(Exception):
    """Only fixed, public diagnostics belong in this exception."""


# CLI versions use "10218 (de699957b)" or "0.4.0-dev (build 10917,
# commit 8ea290247)"; current /props build_info uses "b10917-8ea290247".
BUILD_SPELLINGS = (re.compile(r"\(build ([0-9]{1,10}), commit ([0-9a-f]{7,40})\)"),
                   re.compile(r"(?:^|\s)([0-9]{1,10}) \(([0-9a-f]{7,40})\)"),
                   re.compile(r"\Ab([0-9]{1,10})-([0-9a-f]{7,40})\Z"))


def build_identity(text):
    """The build number and commit from a llama.cpp CLI or server report."""
    for spelling in BUILD_SPELLINGS:
        match = spelling.search(text) if type(text) is str else None
        if match:
            return {"number": int(match[1]), "revision": match[2]}
    return None


def build_revision(text):
    """The commit a llama.cpp tool reports, from --version output or build_info."""
    identity = build_identity(text)
    return identity["revision"] if identity else None


def directory_is_safe(status):
    """Whether a configuration directory may be used, from its stat result.

    One rule, shared with the manager: it is a directory. Ownership and mode are
    not judged here. Every file these directories hold carries its own 0600
    protection and is revalidated on each read, so judging the directory bought
    nothing and refused host actions until someone changed it back by hand.
    """
    return stat.S_ISDIR(status.st_mode)


def read_private_text(path, maximum):
    """Read one of this account's own protected settings files, or say why not.

    The manager, the lab and the qualification runner read the same settings, so
    the rule for what counts as a readable setting lives here once.
    """
    # A link this account placed is its own deployment, so it is followed and its
    # target judged below. A link somebody else placed would let them choose the
    # file this account reads, so it is refused.
    try:
        link = os.lstat(path)
    except OSError:
        link = None
    target = path
    if link is not None and stat.S_ISLNK(link.st_mode):
        if link.st_uid != os.getuid():
            raise InspectionError(f"protected file is a link belonging to another account: {path}")
        # Messages keep naming the configured path; the operator has to find the
        # setting, not the anonymous thing at the end of their own link.
        target = os.path.realpath(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError:
        raise InspectionError(f"cannot open protected file: {path}") from None
    try:
        status = os.fstat(descriptor)
        # None of these settings is a secret; the router key and the browser
        # capability are read by read_private_token, which keeps its own rule.
        # So this asks who can *change* what it is about to parse and act on,
        # not who can read it, and it names which condition refused.
        if not stat.S_ISREG(status.st_mode):
            raise InspectionError(f"protected file is not a regular file: {path}")
        # A zero link count means a concurrent atomic replacement took the file
        # after this descriptor opened it, not that the file gained a name.
        if status.st_nlink > 1:
            raise InspectionError(f"protected file has more than one name: {path}")
        if status.st_size > maximum:
            raise InspectionError(f"protected file is too large: {path}")
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise InspectionError(f"protected file is too large: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise InspectionError(f"protected file is not UTF-8: {path}") from None


def protected_open(path, directory=False, regular_data=False, follow_links=False):
    """Walk from / through pinned directories, checking each one in place.

    A backend installation is supplied rather than owned and names a binary to
    execute, so its walk follows nothing. A configuration directory is this
    account's own, reached through whatever links its deployment uses, and what
    it resolves to is checked all the same. Only directories are ever followed;
    the file at the end of a walk keeps its no-follow open either way.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise InspectionError("Use an absolute installation path without parent traversal.")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            is_dir = directory or index < len(path.parts) - 2
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
            if is_dir:
                flags |= os.O_DIRECTORY
            if not (is_dir and follow_links):
                flags |= os.O_NOFOLLOW
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if not is_dir and (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o6000 or (not regular_data and not info.st_mode & 0o111)):
                raise InspectionError("Expected a regular executable without setuid or setgid permissions.")
            # A walk to a data file or a configuration directory is not graded on
            # any mode. A walk that ends at an executable is: write permission on
            # the binary, or on a directory holding it, is permission to replace
            # the code about to run. A root-owned sticky ancestor such as /tmp
            # cannot replace another owner's child, so it is exempt.
            sticky_ancestor = is_dir and index < len(path.parts) - 2 and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if not directory and not regular_data and info.st_mode & 0o022 and not sticky_ancestor:
                raise InspectionError("The executable, or a directory holding it, is writable by another account; Matuto will not launch code anyone can rewrite.")
        result, descriptor = descriptor, None
        return result
    except FileNotFoundError:
        raise
    except OSError as error:
        if error.errno == errno.ENOTDIR:
            raise InspectionError("A path component is not a directory, or is a symbolic link that may not be followed here.") from None
        raise InspectionError("Cannot safely open the path; check that every component exists, is readable by this account, and is not a symbolic link where one may not be followed.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def fingerprint(descriptor):
    before = os.fstat(descriptor)
    if before.st_size > 1_073_741_824:
        raise InspectionError("Executable exceeds the inspection size limit.")
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1_048_576):
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise InspectionError("Executable changed during inspection; retry after the update completes.")
    return {"sha256": digest.hexdigest(), "bytes": after.st_size, "owner": after.st_uid, "mode": oct(stat.S_IMODE(after.st_mode))}


def host_observation():
    """Observed host/kernel identity. Unreadable fields stay explicit, never inferred."""
    uname = os.uname()
    observation = {"system": uname.sysname, "kernel": uname.release, "machine": uname.machine}
    observation["os_id"] = "unavailable"
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                observation["os_id"] = line.partition("=")[2].strip().strip('"') or "unavailable"
                break
    except OSError:
        pass
    return observation


def driver_observation():
    """Best-effort kernel-driver and GPU-runtime versions; absent values stay unavailable."""
    observation = {}
    for name, path in (("amdgpu_driver", "/sys/module/amdgpu/version"), ("rocm_runtime", "/opt/rocm/.info/version")):
        try:
            text = Path(path).read_text().strip()
        except OSError:
            text = ""
        observation[name] = text or "unavailable"
    return observation


# Installation and host identity that every retained run captures.
RUNTIME_INSTALLATION_FIELDS = ("backend", "pci", "uuid", "executable.sha256", "host.kernel", "host.os_id", "driver.amdgpu_driver", "driver.rocm_runtime")
# Material workload request captured by checks that load one model. A router serves
# many presets and has no single requested model, context, or offload to compare.
# The verified digest is carried next to the path: a same-path replacement keeps
# request.model equal while the weights those results came from are gone.
RUNTIME_WORKLOAD_FIELDS = ("request.model", "request.model_sha256", "request.context", "offload.requested_gpu_layers")
RUNTIME_CONFIGURATION_FIELDS = RUNTIME_INSTALLATION_FIELDS + RUNTIME_WORKLOAD_FIELDS


def _nested(record, path):
    value = record
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def requested_argument(parsed, *canonicals):
    """The operand a launch actually passed for an option, or unavailable.

    The caller passes the operand-aware parsed view, so an option's operand is
    never mistaken for another option: a prompt that happens to look like
    "--ctx-size" stays a prompt value. The last occurrence wins, matching how
    llama.cpp applies a repeated scalar option in the order it reads them.
    """
    value = "unavailable"
    for _option, canonical, operand in parsed:
        if canonical in canonicals and operand:
            value = operand
    return value


def configuration_comparison(retained, current, fields=RUNTIME_CONFIGURATION_FIELDS):
    """Compare a retained run identity with a current configuration without rewriting either.

    A field absent from either side, or recorded as the explicit "unavailable"
    sentinel, is unknown, not a match, so incomplete historical identity yields
    applicability unknown instead of a guessed result.
    """
    changed, unknown, matched = [], [], []
    for field in fields:
        before, after = _nested(retained, field), _nested(current, field)
        if before in (None, "", "unavailable") or after in (None, "", "unavailable"):
            unknown.append(field)
        elif before != after:
            changed.append(field)
        else:
            matched.append(field)
    return {"state": "changed" if changed else ("unknown" if unknown else "current"),
            "changed": changed, "unknown": unknown, "matched": matched}


def executable_observation(selection, tool):
    """Fingerprint the executable as installed now, never the selection-time snapshot."""
    try:
        directory, descriptor = _tool_descriptor(selection["installation"], tool)
    except (InspectionError, OSError, KeyError):
        return "unavailable"
    try:
        return fingerprint(descriptor)
    except (InspectionError, OSError):
        return "unavailable"
    finally:
        os.close(descriptor)
        os.close(directory)


def current_configuration(selection, tool):
    """The configuration as installed right now, for comparison with a retained run identity."""
    return {"backend": selection["backend"], "pci": selection["pci"], "uuid": selection["corroborator"]["uuid"],
            "executable": executable_observation(selection, tool),
            "host": host_observation(), "driver": driver_observation()}


def probe(descriptor, directory, option, environment=None):
    """Execute the checked inode, bounding both output and the whole process group."""
    if environment is None:
        environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
        environment.update({name: os.environ[name] for name in VISIBILITY if name in os.environ})
    else:
        environment = dict(environment)
    process = None
    try:
        process = subprocess.Popen(
            [f"/proc/self/fd/{descriptor}", option], pass_fds=(descriptor, directory),
            cwd=f"/proc/self/fd/{directory}", env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + PROBE_SECONDS
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise InspectionError("Probe timed out; check the installed runtime and driver.")
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise InspectionError("Probe output exceeded the inspection limit.")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())):
            raise InspectionError("Probe failed; check this tool's shared libraries, runtime and driver prerequisites locally.")
        return output.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        raise InspectionError("Probe could not complete; check executable access and runtime prerequisites.") from None
    finally:
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()


def pci_devices(root=Path("/sys/bus/pci/devices")):
    """Read only kernel PCI attributes, never infer a backend association."""
    devices = []
    incomplete = False
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {"state": "unavailable", "devices": [], "reason": "Linux PCI identity attributes are unavailable."}
    for entry in entries:
        if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", entry.name):
            continue
        try:
            values = {name: (entry / name).read_text().strip() for name in ("vendor", "device", "class", "subsystem_vendor", "subsystem_device")}
        except OSError:
            incomplete = True
            continue
        if not all(re.fullmatch(r"0x[0-9a-f]{4,6}", value) for value in values.values()):
            incomplete = True
            continue
        if values["vendor"] == "0x1002" and values["class"].startswith("0x03"):
            devices.append({"pciAddress": entry.name, **values})
    result = {"state": "completed", "devices": devices}
    if incomplete:
        result["warning"] = "Some PCI identity attributes were unavailable or invalid; incomplete entries were omitted."
    return result


def named_pci_devices(inventory, paths=(Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"), Path("/usr/share/pci.ids"))):
    """Optional local PCI names for display only; never part of the saved identity."""
    if inventory["state"] != "completed" or not inventory["devices"]:
        return inventory
    for path in paths:
        try:
            if path.stat().st_size > 5_000_000:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            break
        except OSError:
            continue
    else:
        return inventory
    models, subsystems, vendor, device = {}, {}, False, None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith("\t"):
            vendor = line.startswith("1002  ")
            device = None
        elif vendor and line.startswith("\t\t") and device:
            parts = line[2:].split(None, 2)
            if len(parts) == 3 and all(re.fullmatch(r"[0-9a-fA-F]{4}", part) for part in parts[:2]):
                subsystems[device, parts[0].lower(), parts[1].lower()] = parts[2]
        elif vendor:
            parts = line[1:].split(None, 1)
            device = parts[0].lower() if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{4}", parts[0]) else None
            if device:
                models[device] = parts[1]
    result = dict(inventory)
    result["devices"] = []
    for item in inventory["devices"]:
        named = dict(item)
        key = (item["device"][2:], item["subsystem_vendor"][2:], item["subsystem_device"][2:])
        name = subsystems.get(key) or models.get(key[0])
        if name and not re.fullmatch(r"(?:unknown )?device(?: [0-9a-fA-F]+)?", name, re.I):
            named["displayName"] = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name)[:160]
        result["devices"].append(named)
    return result


def _device_counter(pci, name, root=Path("/sys/class/drm")):
    """One byte-valued counter of the single AMD device at the selected PCI address."""
    candidates = []
    for card in sorted(root.glob("card[0-9]*")):
        device = card / "device"
        try:
            if device.resolve().name == pci and (device / "vendor").read_text().strip() == "0x1002":
                candidates.append(device)
        except OSError:
            continue
    if len(candidates) != 1:
        raise InspectionError("Byte-valued VRAM telemetry is unavailable for the selected physical GPU; no zero measurement is assumed.")
    return candidates[0] / name


def vram_telemetry(pci, root=Path("/sys/class/drm")):
    """Byte-valued used-VRAM counter for the one AMD device at the selected PCI address.

    The counter belongs to the device, not to a process: a reading attributes
    memory to the selected GPU, never to the workload that happens to own it.
    Ambiguous or absent telemetry stays unavailable; no zero is assumed.
    """
    return _device_counter(pci, "mem_info_vram_used", root)


def vram_capacity(pci, root=Path("/sys/class/drm")):
    """Byte-valued total-VRAM counter for the one AMD device at the selected PCI address.

    The card states its own capacity, so a bundle refusal compares the measured
    occupancy with this reading rather than with a ceiling a profile declares.
    """
    return _device_counter(pci, "mem_info_vram_total", root)


def reported_description(text, redactions):
    """Retain bounded reported text, not credentials or an inferred identity."""
    for value in redactions:
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://\S+|matuto-[0-9a-f]{32}\.localhost", "[redacted]", text, flags=re.I)
    text = re.sub(r"\b(?:api[-_ ]?key|authorization|bearer|token|secret|password)\b.*", "[redacted]", text, flags=re.I)
    text = re.sub(r"[A-Za-z0-9_-]{32,}", "[redacted]", text)
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", text)


def inspect_installation(location, redactions=()):
    if not isinstance(location, str) or not location or len(location) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in location):
        raise InspectionError("Supply one absolute installation directory, not a command or options.")
    installation = Path(location)
    opened = {}
    tools = {}
    directory = None
    try:
        try:
            directory = protected_open(installation, directory=True)
        except FileNotFoundError:
            return {"state": "unavailable", "reason": "Installation directory is absent. Supply its existing bin directory.", "tools": {}}
        # Accept either a prefix containing bin/ or the executable directory itself.
        try:
            nested = protected_open(installation / "bin", directory=True)
        except FileNotFoundError:
            pass
        else:
            os.close(directory)
            directory = nested
            installation /= "bin"
        # Validate each candidate before any probes; rejected siblings never execute.
        for name in TOOLS:
            try:
                descriptor = protected_open(installation / name)
                opened[name] = descriptor
                tools[name] = {"state": "completed", "executable": fingerprint(descriptor)}
            except FileNotFoundError:
                tools[name] = {"state": "unavailable", "reason": "Expected executable is absent; only actions requiring this tool are unavailable."}
            except InspectionError as error:
                if name in opened:
                    os.close(opened.pop(name))
                tools[name] = {"state": "failure", "reason": str(error)}
        for name, descriptor in opened.items():
            tool = tools[name]
            try:
                help_text = probe(descriptor, directory, "--help")
                # Publish typed observations, not backend logs or arbitrary labels.
                flags = set(re.findall(r"(?<![\w-])--[a-z][a-z-]+\b", help_text))
                tool["capabilities"] = sorted(flags & {"--version", "--list-devices", "--device", "--models-dir", "--models-preset", "--api-key-file"})
                tool["build"] = None
                tool["buildState"] = "unavailable"
                if "--version" in flags:
                    try:
                        version = probe(descriptor, directory, "--version")
                        identity = build_identity(version)
                        if identity:
                            tool["build"] = identity
                            tool["buildState"] = "completed"
                        else:
                            tool["buildReason"] = "Reported version format is unrecognized; inspect this tool's version locally."
                    except InspectionError as error:
                        tool["buildState"] = "failure"
                        tool["buildReason"] = str(error)
                else:
                    tool["buildReason"] = "This executable does not advertise --version; reported build information is unavailable."
                tool["devices"] = []
                tool["backends"] = []
                if "--list-devices" in flags:
                    try:
                        listing = probe(descriptor, directory, "--list-devices")
                        if "Available devices:" not in listing:
                            raise InspectionError("Device listing format is unrecognized; inspect this tool locally.")
                        seen = set()
                        for line in listing.split("Available devices:", 1)[1].splitlines():
                            if not line.strip() or line.strip() == "(none)":
                                continue
                            match = re.fullmatch(r"\s*((?:ROCm|HIP|Vulkan)[0-9]{1,4}): (.{1,200}) \(([0-9]{1,10}) MiB, ([0-9]{1,10}) MiB free\)", line)
                            if not match:
                                tool["deviceWarning"] = "Some listed devices are unsupported or unrecognized; no identity was inferred."
                                continue
                            selector = match[1]
                            backend = "Vulkan" if selector.startswith("Vulkan") else "HIP/ROCm"
                            tool["devices"].append({"selector": selector, "backend": backend, "reportedDescription": reported_description(match[2], redactions), "memoryMiB": int(match[3]), "identity": None,
                                                    "association": "unresolved: listing does not establish a physical PCI identity"})
                            seen.add(backend)
                        tool["backends"] = sorted(seen)
                        tool["deviceState"] = "completed"
                    except InspectionError as error:
                        tool["deviceState"] = "failure"
                        tool["deviceWarning"] = str(error)
                else:
                    tool["deviceState"] = "unavailable"
                    tool["deviceWarning"] = "This executable does not advertise --list-devices. Use an installed tool that does."
                if fingerprint(descriptor) != tool["executable"]:
                    raise InspectionError("Executable changed during inspection; retry after the update completes.")
            except InspectionError as error:
                tools[name] = {"state": "failure", "executable": tool["executable"], "reason": str(error)}
        completed = any(tool["state"] == "completed" for tool in tools.values())
        failed = any(tool["state"] == "failure" for tool in tools.values())
        result = {"state": "completed" if completed else "failure" if failed else "unavailable", "tools": tools, "hostDevices": named_pci_devices(pci_devices()),
                  "visibilityVariables": [name for name in VISIBILITY if name in os.environ],
                  "note": "Inspection only, not selection or qualification. Backend selectors are not physical identities; PCI devices are not matched by name or order. No detected backend means unavailable evidence, not proof that the build lacks it."}
        if not opened and not failed:
            result["reason"] = "No expected llama.cpp executables found. Supply the installation prefix or its bin directory."
        elif not completed:
            result["reason"] = "No tool completed inspection. Check each tool's prerequisite failure and retry."
        # Defence in depth for browser credentials, including hex-looking version fields.
        encoded = json.dumps(result)
        for value in redactions:
            if value:
                encoded = encoded.replace(value, "[redacted]")
        return json.loads(encoded)
    except OSError:
        raise InspectionError("Inspection could not read the installation; check access and retry after any update completes.") from None
    finally:
        for descriptor in opened.values():
            os.close(descriptor)
        if directory is not None:
            os.close(directory)



# Backend identity probes use only the published ROCr and Vulkan Loader ABIs. They
# run in a disposable interpreter (identity_probe) because a driver may hang.
# Sources: ROCR hsa_ext_amd.h ba56a24c6132c5d195686ae4adf969ca1222fbba
# (BDFID/DOMAIN/UUID), Vulkan-Headers ee2ec5fd83dafce291024683b50dc89219333076
# (VkPhysicalDeviceIDProperties/VkPhysicalDevicePCIBusInfoPropertiesEXT).
def _load_system_library(names):
    """Load only a pinned protected system-library inode, never a loader search path."""
    import ctypes
    prefixes = (Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64"), Path("/usr/lib"), Path("/opt/rocm/lib"))
    for prefix in prefixes:
        for name in names:
            candidate = prefix / name
            try:
                # Distribution sonames commonly link to an ABI-pinned inode. This
                # follows only the fixed system link, then reopens every resolved
                # ancestor without links; user-supplied installation paths never use it.
                resolved = candidate.resolve(strict=True)
                if not resolved.is_relative_to(prefix.resolve(strict=True)):
                    raise InspectionError("Protected backend runtime path escapes its system directory.")
                descriptor = protected_open(resolved, regular_data=True)
            except FileNotFoundError:
                continue
            try:
                return ctypes.CDLL(f"/proc/self/fd/{descriptor}"), descriptor
            except OSError:
                os.close(descriptor)
                raise InspectionError("Protected backend runtime could not load.") from None
    raise InspectionError("Protected backend runtime is unavailable; selected device cannot be identified.")


def _hsa_identities():
    import ctypes
    library, descriptor = _load_system_library(("libhsa-runtime64.so.1",))
    class Agent(ctypes.Structure):
        _fields_ = [("handle", ctypes.c_uint64)]
    callback_type = ctypes.CFUNCTYPE(ctypes.c_uint32, Agent, ctypes.c_void_p)
    library.hsa_init.argtypes = (); library.hsa_init.restype = ctypes.c_uint32
    library.hsa_shut_down.argtypes = (); library.hsa_shut_down.restype = ctypes.c_uint32
    library.hsa_iterate_agents.argtypes = (callback_type, ctypes.c_void_p); library.hsa_iterate_agents.restype = ctypes.c_uint32
    library.hsa_agent_get_info.argtypes = (Agent, ctypes.c_uint32, ctypes.c_void_p); library.hsa_agent_get_info.restype = ctypes.c_uint32
    if library.hsa_init() != 0:
        raise InspectionError("ROCr initialization failed; selected HIP device cannot be identified.")
    found = []
    try:
        @callback_type
        def visit(agent, _data):
            device = ctypes.c_uint32()
            # HSA_AGENT_INFO_DEVICE is 0; HSA_DEVICE_TYPE_GPU is 1.
            if library.hsa_agent_get_info(agent, 17, ctypes.byref(device)) != 0 or device.value != 1:
                return 0
            bdf, domain, uuid = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.create_string_buffer(21)
            if (library.hsa_agent_get_info(agent, 0xA006, ctypes.byref(bdf)) or
                    library.hsa_agent_get_info(agent, 0xA00F, ctypes.byref(domain)) or
                    library.hsa_agent_get_info(agent, 0xA011, uuid)):
                return 0
            value = uuid.value.decode("ascii", "ignore")
            if not re.fullmatch(r"GPU-[0-9a-fA-F]{16}", value):
                return 0
            # ROCr BDFID is bus:device:function encoded in its low 16 bits.
            raw = bdf.value
            address = f"{domain.value:04x}:{(raw >> 8) & 0xff:02x}:{(raw >> 3) & 0x1f:02x}.{raw & 7}"
            found.append({"pciAddress": address, "uuid": value})
            return 0
        if library.hsa_iterate_agents(visit, None) != 0:
            raise InspectionError("ROCr agent enumeration failed; selected HIP device cannot be identified.")
    finally:
        library.hsa_shut_down()
        os.close(descriptor)
    return found


def _vulkan_identities():
    import ctypes
    class ApplicationInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("pApplicationName", ctypes.c_char_p), ("applicationVersion", ctypes.c_uint32), ("pEngineName", ctypes.c_char_p), ("engineVersion", ctypes.c_uint32), ("apiVersion", ctypes.c_uint32)]
    class InstanceInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("flags", ctypes.c_uint32), ("pApplicationInfo", ctypes.c_void_p), ("enabledLayerCount", ctypes.c_uint32), ("ppEnabledLayerNames", ctypes.c_void_p), ("enabledExtensionCount", ctypes.c_uint32), ("ppEnabledExtensionNames", ctypes.c_void_p)]
    class IDProperties(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("deviceUUID", ctypes.c_ubyte * 16), ("driverUUID", ctypes.c_ubyte * 16), ("deviceLUID", ctypes.c_ubyte * 8), ("deviceNodeMask", ctypes.c_uint32), ("deviceLUIDValid", ctypes.c_uint32)]
    class PCIProperties(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("domain", ctypes.c_uint32), ("bus", ctypes.c_uint32), ("device", ctypes.c_uint32), ("function", ctypes.c_uint32)]
    class Properties2(ctypes.Structure):
        # VkPhysicalDeviceProperties is ABI-opaque here. The fixed 4096-byte
        # tail exceeds the Vulkan-Headers ee2ec5 field and preserves pNext ABI.
        _fields_ = [("sType", ctypes.c_uint32), ("pNext", ctypes.c_void_p), ("properties", ctypes.c_ubyte * 4096)]
    class Extension(ctypes.Structure):
        _fields_ = [("extensionName", ctypes.c_char * 256), ("specVersion", ctypes.c_uint32)]
    library, descriptor = _load_system_library(("libvulkan.so.1",))
    library.vkCreateInstance.argtypes = (ctypes.POINTER(InstanceInfo), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)); library.vkCreateInstance.restype = ctypes.c_int32
    library.vkDestroyInstance.argtypes = (ctypes.c_void_p, ctypes.c_void_p); library.vkDestroyInstance.restype = None
    library.vkEnumeratePhysicalDevices.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p)); library.vkEnumeratePhysicalDevices.restype = ctypes.c_int32
    library.vkEnumerateDeviceExtensionProperties.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(Extension)); library.vkEnumerateDeviceExtensionProperties.restype = ctypes.c_int32
    library.vkGetPhysicalDeviceProperties2.argtypes = (ctypes.c_void_p, ctypes.POINTER(Properties2)); library.vkGetPhysicalDeviceProperties2.restype = None
    app = ApplicationInfo(0, None, b"matuto-runtime", 0, b"matuto-runtime", 0, 4198400)
    create = InstanceInfo(1, None, 0, ctypes.cast(ctypes.pointer(app), ctypes.c_void_p), 0, None, 0, None)
    instance = ctypes.c_void_p()
    if library.vkCreateInstance(ctypes.byref(create), None, ctypes.byref(instance)) != 0:
        raise InspectionError("Vulkan Loader initialization failed; selected Vulkan device cannot be identified.")
    try:
        count = ctypes.c_uint32()
        if library.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None) != 0:
            raise InspectionError("Vulkan physical-device enumeration failed.")
        handles = (ctypes.c_void_p * count.value)()
        if library.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), handles) != 0:
            raise InspectionError("Vulkan physical-device enumeration changed; retry selection.")
        found = []
        for index, handle in enumerate(handles[:count.value]):
            physical = ctypes.c_void_p(handle)
            size = ctypes.c_uint32()
            if library.vkEnumerateDeviceExtensionProperties(physical, None, ctypes.byref(size), None) != 0: continue
            extensions = (Extension * size.value)()
            if library.vkEnumerateDeviceExtensionProperties(physical, None, ctypes.byref(size), extensions) != 0: continue
            if b"VK_EXT_pci_bus_info" not in {item.extensionName.split(b"\\0", 1)[0] for item in extensions[:size.value]}: continue
            pci = PCIProperties(1000212000, None, 0, 0, 0, 0)
            identity = IDProperties(1000071004, ctypes.cast(ctypes.pointer(pci), ctypes.c_void_p))
            props = Properties2(1000059001, ctypes.cast(ctypes.pointer(identity), ctypes.c_void_p))
            # vkGetPhysicalDeviceProperties2 is core Vulkan 1.1. The opaque tail
            # is intentionally over-sized, never read, and correctly aligns pNext.
            library.vkGetPhysicalDeviceProperties2(physical, ctypes.byref(props))
            uuid = bytes(identity.deviceUUID).hex()
            if uuid == "0" * 32: continue
            found.append({"pciAddress": f"{pci.domain:04x}:{pci.bus:02x}:{pci.device:02x}.{pci.function}", "uuid": uuid, "index": index})
        return found
    finally:
        library.vkDestroyInstance(instance, None)
        os.close(descriptor)


def identity_probe(backend):
    """Output only typed identities; never driver logs or descriptions."""
    try:
        if backend == "HIP/ROCm":
            devices = _hsa_identities()
        elif backend == "Vulkan":
            devices = _vulkan_identities()
        else:
            raise InspectionError("Unsupported backend identity probe.")
    except (OSError, AttributeError):
        raise InspectionError("Backend identity runtime is unavailable; selected device cannot be identified.") from None
    return {"backend": backend, "devices": devices}


def bounded_identity_probe(backend, environment):
    """Isolate native drivers with the same streamed size/deadline/group limits as probes."""
    if any(name.startswith("LD_") or name in {"VK_ICD_FILENAMES", "VK_LAYER_PATH", "VK_DRIVER_FILES"} for name in environment):
        raise InspectionError("Loader override environment is not permitted for selected runtime identity.")
    process = None
    try:
        process = subprocess.Popen([sys.executable, "-B", __file__, "--identity-probe", backend], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline, output = time.monotonic() + PROBE_SECONDS, bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise InspectionError("Backend identity probe timed out; selected device is unavailable.")
                chunk = os.read(process.stdout.fileno(), 65_536)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    raise InspectionError("Backend identity probe output exceeded the inspection limit.")
        if process.wait(timeout=max(.01, deadline - time.monotonic())):
            raise InspectionError("Backend identity probe failed; selected device is unavailable.")
    except (OSError, subprocess.TimeoutExpired):
        raise InspectionError("Backend identity probe could not complete; selected device is unavailable.") from None
    finally:
        # CLI interruption must reap the owned child before returning.
        if process is not None:
            with contextlib.suppress(ProcessLookupError): os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception): process.wait()
            if process.stdout is not None: process.stdout.close()
    try:
        value = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InspectionError("Backend identity probe returned invalid data.") from None
    if not isinstance(value, dict) or value.get("backend") != backend or not isinstance(value.get("devices"), list) or any(not isinstance(item, dict) for item in value["devices"]):
        raise InspectionError("Backend identity probe returned invalid data.")
    return value["devices"]

def inspection_cli(location):
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        try:
            result = inspect_installation(location)
        except InspectionError as error:
            result = {"state": "failure", "reason": str(error), "tools": {}}
        except KeyboardInterrupt:
            result = {"state": "failure", "reason": "Inspection interrupted; no selection was changed. Retry to inspect again.", "tools": {}}
        print(json.dumps(result, indent=2))
        return 0 if result["state"] == "completed" else 1
    finally:
        signal.signal(signal.SIGTERM, previous)



# The state root deliberately does not use XDG_CONFIG_HOME: WQ profiles use a
# temporary XDG root but must share the account's selected runtime.
RUNTIME_ROOT = Path.home() / ".config" / "ai-agents-sandbox" / "matuto-runtime"
SELECTION_PATH = RUNTIME_ROOT / "selection.json"
LOCK_PATH = RUNTIME_ROOT / "run.lock"

# Where the weights and the backends live is a fact about this host, so the
# manager, the lab and the qualification runner read one setting each rather
# than carrying a copy per profile. The build registry sits beside the runtime
# selection because the selection is what names the active build, and because
# that directory deliberately ignores XDG_CONFIG_HOME: a workload running under
# a temporary configuration root still shares the account's builds.
DATA_ROOT = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "ai-agents-sandbox"
MODEL_ROOT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ai-agents-sandbox" / "matuto-manager" / "model-root"
DEFAULT_MODEL_ROOT = DATA_ROOT / "models"
BUILD_ROOT = DATA_ROOT / "builds"
BUILD_REGISTRY = RUNTIME_ROOT / "builds.json"
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def model_root():
    """The model root: the configured absolute path, else the data-root default.

    A configured root is the operator's own directory on whatever filesystem
    holds the weights, so it is checked but never created; only the default is
    ours to make. Failing by name beats silently serving an empty default.
    """
    try:
        # Path.exists() answers False for a setting this account cannot reach,
        # which would silently fall back; only a real absence may do that.
        MODEL_ROOT_CONFIG.lstat()
        configured = True
    except FileNotFoundError:
        configured = False
    except OSError:
        raise InspectionError(f"cannot reach the model root setting: {MODEL_ROOT_CONFIG}") from None
    if configured:
        value = read_private_text(MODEL_ROOT_CONFIG, 4_096).removesuffix("\n")
        if not value.startswith("/") or any(character in value for character in "\r\n\0"):
            raise InspectionError(f"model root must be one absolute path in {MODEL_ROOT_CONFIG}: {value!r}")
        path = Path(value)
    else:
        path = DEFAULT_MODEL_ROOT
    try:
        status = path.stat()
    except OSError:
        raise InspectionError(f"model root is missing or unreadable: {path}") from None
    # A supplied data root is not one of ours: another account able to write here
    # can substitute a model file, so this keeps the group/other-write rule that
    # the directories Matuto creates for itself no longer carry.
    if not directory_is_safe(status):
        raise InspectionError(f"unsafe model root: {path}")
    if status.st_mode & 0o500 != 0o500:
        raise InspectionError(f"model root must be readable and searchable: {path}")
    return path


def model_location(source, file_name):
    """Where a profile's weights are, from what the profile says they are.

    A profile names the repository it came from and the file inside it. The
    directory those live in belongs to the host, so moving the collection is one
    setting rather than one edit per profile.
    """
    for value, field in ((source, "MODEL_SOURCE"), (file_name, "MODEL_FILE")):
        # Whatever the publisher called it is allowed; only what would leave the
        # root, or break the line a checksum is read from, is not.
        if not isinstance(value, str) or not value or value.startswith("/") or any(character in value for character in "\r\n\0"):
            raise InspectionError(f"{field} must name a location inside the model root, without a leading slash or a control character.")
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise InspectionError(f"{field} must not contain an empty or parent path component.")
    if "/" in file_name:
        raise InspectionError("MODEL_FILE must be one file name, not a path.")
    return model_root() / source / file_name


def _build_installation(location):
    if not isinstance(location, str) or not location.startswith("/"):
        raise InspectionError("A build is registered by absolute path.")
    return str(installation_bin(location))


def _build_is_usable(installation):
    try:
        directory, descriptor = _tool_descriptor(installation, "llama-server")
    except (OSError, InspectionError):
        return False
    os.close(descriptor)
    os.close(directory)
    return True


def read_builds():
    """The registered builds. Data on disk, so adding one needs no new release."""
    try:
        BUILD_REGISTRY.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        raise InspectionError(f"cannot reach the build registry: {BUILD_REGISTRY}") from None
    try:
        value = json.loads(read_private_text(BUILD_REGISTRY, 65_536))
    except json.JSONDecodeError:
        raise InspectionError(f"build registry is not valid JSON: {BUILD_REGISTRY}") from None
    if not isinstance(value, list):
        raise InspectionError(f"build registry must hold a list of builds: {BUILD_REGISTRY}")
    builds = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "path", "label"} or not all(isinstance(item[name], str) for name in item):
            raise InspectionError(f"build registry holds an invalid entry: {BUILD_REGISTRY}")
        if not BUILD_ID.fullmatch(item["id"]) or not item["path"].startswith("/") or ".." in Path(item["path"]).parts:
            raise InspectionError(f"build registry holds an invalid entry: {BUILD_REGISTRY}")
        builds.append(dict(item))
    if len({item["id"] for item in builds}) != len(builds):
        raise InspectionError(f"build registry names one id twice: {BUILD_REGISTRY}")
    return builds


def _write_builds(builds):
    directory = _runtime_directory()
    try:
        _atomic_json(directory, "builds.json", builds)
    finally:
        os.close(directory)


def register_build(identifier, location, label=""):
    """Approve one build directory. Discovery never reaches this by itself."""
    if not isinstance(identifier, str) or not BUILD_ID.fullmatch(identifier):
        raise InspectionError("A build id is letters, digits, dot, dash or underscore, up to 64 characters.")
    installation = _build_installation(location)
    if not _build_is_usable(installation):
        raise InspectionError(f"No usable llama-server was found to register: {installation}")
    if not isinstance(label, str) or any(character in label for character in "\r\n\0") or len(label) > 200:
        raise InspectionError("A build label is one line of at most 200 characters.")
    builds = read_builds()
    # Re-registering the same directory is how a label is corrected. Pointing a
    # name that already means one build at a different one is not: two builds of
    # one revision for two GPU targets would otherwise replace each other in
    # silence, and the qualification would follow whichever registered last.
    existing = next((item for item in builds if item["id"] == identifier), None)
    if existing is not None and existing["path"] != installation:
        raise InspectionError(f"{identifier} already names {existing['path']}; choose another id or forget it first.")
    builds = [item for item in builds if item["id"] != identifier]
    builds.append({"id": identifier, "path": installation, "label": label or identifier})
    _write_builds(builds)
    return builds[-1]


def _unregistered(identifier):
    """Why an id was not found: an empty registry is not a missing entry."""
    builds = read_builds()
    if not builds:
        return f"No builds are registered at all; the registry is empty or absent: {BUILD_REGISTRY}"
    names = ", ".join(sorted(item["id"] for item in builds))
    return f"No build is registered as {identifier}; the registry holds {names}."


def forget_build(identifier):
    """Remove a build from the registry. The directory itself is left alone."""
    builds = read_builds()
    remaining = [item for item in builds if item["id"] != identifier]
    if len(remaining) == len(builds):
        raise InspectionError(_unregistered(identifier))
    _write_builds(remaining)


def _candidate_builds(root, depth=2):
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        # A link is an alias for a directory that discovery already reports on
        # its own, so following one would offer the same build twice.
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        if (path / "bin/llama-server").is_file() or (path / "llama-server").is_file():
            yield path
        elif depth > 1:
            yield from _candidate_builds(path, depth - 1)


def discovered_builds():
    """Build directories under the build root that nobody has approved yet.

    Discovery lists; it never registers. A directory found here stays here, and
    stays unusable, until the owner registers it by name.
    """
    registered = {item["path"] for item in read_builds()}
    found = []
    for path in _candidate_builds(BUILD_ROOT):
        installation = str(installation_bin(str(path)))
        if installation not in registered:
            # The suggested id carries the GPU target the build sits under, because
            # the same revision built for two targets otherwise offers two rows
            # under one name and approving either would look the same.
            found.append({"path": installation, "name": "-".join(path.relative_to(BUILD_ROOT).parts)})
    return found


def active_installation():
    """The backend directory every workload runs from: the one selection names."""
    selection = read_selection()
    if selection is None:
        raise InspectionError("No runtime selection is configured; explicitly select an installation, backend, and GPU first.")
    return Path(selection["installation"])


def build_listing():
    """Registered builds with the active one marked, plus what discovery found."""
    try:
        selection = read_selection()
    except InspectionError:
        selection = None
    builds = read_builds()
    active = selection["installation"] if selection is not None else None
    return {
        "builds": [{**item, "active": item["path"] == active, "installed": _build_is_usable(item["path"])} for item in builds],
        "discovered": discovered_builds(),
        "active": active,
    }


def activate_build(identifier):
    """Switch to a registered build, or back to one. No profile is touched.

    Only the installation changes; the backend and physical GPU already chosen
    are kept, so switching and rolling back each name a build and nothing else.
    """
    chosen = next((item for item in read_builds() if item["id"] == identifier), None)
    if chosen is None:
        raise InspectionError(_unregistered(identifier))
    selection = read_selection()
    if selection is None:
        raise InspectionError("Select an installation, backend, and GPU once before switching builds.")
    select_runtime(chosen["path"], selection["backend"], selection["pci"])
    return chosen


def _runtime_directory():
    """Create/pin private state one protected ancestor at a time, links resolved."""
    path = Path.home()
    try:
        descriptor = protected_open(path, directory=True, follow_links=True)
    except OSError:
        raise InspectionError("Runtime selection home directory is unsafe.") from None
    try:
        for part in (".config", "ai-agents-sandbox", "matuto-runtime"):
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            # Reached through whatever links the deployment uses; what the
            # component resolves to is what has to be ours.
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=descriptor)
            info = os.fstat(child)
            if not directory_is_safe(info):
                os.close(child)
                raise InspectionError(f"Runtime selection directory is unsafe: {part} is not a directory.")
            os.close(descriptor)
            descriptor = child
        result, descriptor = descriptor, None
        return result
    except OSError:
        raise InspectionError(f"Runtime selection directory is unsafe: {part} could not be opened.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def runtime_lease(exclusive=False):
    import fcntl
    directory = _runtime_directory()
    try:
        descriptor = os.open("run.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise InspectionError("Runtime coordination lock is unsafe.")
    try:
        fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise InspectionError("A Matuto runtime selection or workload is active; stop it before changing selection.") from None
    return descriptor


def installation_bin(installation):
    root = Path(installation)
    nested = root / "bin"
    return nested if nested.is_dir() else root


def _tool_descriptor(installation, tool):
    if tool not in TOOLS:
        raise InspectionError("Unsupported llama.cpp tool.")
    directory = installation_bin(installation)
    directory_fd = protected_open(directory, directory=True)
    try:
        descriptor = protected_open(directory / tool)
    except Exception:
        os.close(directory_fd)
        raise
    return directory_fd, descriptor


def _device_listing(text):
    if "Available devices:" not in text:
        raise InspectionError("Selected runtime returned an unrecognized device listing.")
    devices = []
    for line in text.split("Available devices:", 1)[1].splitlines():
        match = re.fullmatch(r"\s*((?:ROCm|HIP|Vulkan)[0-9]{1,4}): (.{1,200}) \(([0-9]{1,10}) MiB, ([0-9]{1,10}) MiB free\)", line)
        if match:
            devices.append((match[1], "Vulkan" if match[1].startswith("Vulkan") else "HIP/ROCm"))
    return devices


def execution_environment(source=None):
    source = os.environ if source is None else source
    for name in sorted(source):
        if name in VISIBILITY or name.startswith(("LLAMA_ARG_", "LD_", "VK_", "GGML_BACKEND_")) or name in {"HSA_TOOLS_LIB", "HSA_ENABLE_SDMA", "HSA_OVERRIDE_GFX_VERSION", "HIP_VISIBLE_DEVICES", "HIP_PATH", "ROCM_PATH"}:
            raise InspectionError(f"Runtime environment constraint {name} must be removed or expressed in the selected profile.")
    # Native configuration files are another source of implicit option overrides.
    for path in (Path("/etc/llama.cpp/config.ini"), Path.home() / ".config/llama.cpp/config.ini"):
        if path.exists() or path.is_symlink():
            raise InspectionError("An implicit llama.cpp config.ini conflicts with Matuto-owned execution options.")
    return {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C", "HOME": str(Path.home())}


def resolve_device(selection, environment, backend=None):
    hosts = [item for item in pci_devices()["devices"] if item["pciAddress"] == selection["pci"]]
    if hosts != [selection["corroborator"]["host"]]:
        raise InspectionError("Selected physical GPU is missing or its hardware identity changed; select it explicitly again.")
    backend = selection["backend"] if backend is None else backend
    identities = bounded_identity_probe(backend, environment)
    if backend == selection["backend"]:
        # The selection's own API names the card by UUID, so the match stays the
        # strongest evidence available.
        matches = [item for item in identities if item.get("uuid") == selection["corroborator"]["uuid"]]
        if len(matches) != 1 or matches[0].get("pciAddress") != selection["pci"]:
            raise InspectionError("Selected GPU UUID is missing, changed, or ambiguous; select the physical device explicitly again.")
    else:
        # A second API on the same card has its own device UUID and no UUID the
        # selection recorded can name it. The card's PCI address is what the two
        # APIs share, so that is what identifies it here, and two devices at one
        # address are ambiguous rather than a pick.
        matches = [item for item in identities if item.get("pciAddress") == selection["pci"]]
        if len(matches) != 1:
            raise InspectionError(f"Selected physical GPU is not exactly one {backend} device; select it explicitly again.")
    identity = matches[0]
    environment = dict(environment)
    if backend == "Vulkan":
        if type(identity.get("index")) is not int or identity["index"] < 0:
            raise InspectionError("Vulkan physical-device selector is unavailable.")
        environment["GGML_VK_VISIBLE_DEVICES"] = str(identity["index"])
    else:
        environment["ROCR_VISIBLE_DEVICES"] = identity["uuid"]
        filtered = bounded_identity_probe(backend, environment)
        if filtered != [identity]:
            raise InspectionError("ROCr visibility did not resolve exactly the selected physical GPU.")
    return environment, identity


OPTION_ALIASES = {
    "-m": "--model", "-ngl": "--gpu-layers", "--n-gpu-layers": "--gpu-layers",
    "-c": "--ctx-size", "-n": "--n-predict", "-dev": "--device",
    "-mm": "--mmproj", "-mmu": "--mmproj-url",
    "-md": "--spec-draft-model", "--model-draft": "--spec-draft-model",
    "-hfd": "--spec-draft-hf", "-hfrd": "--spec-draft-hf", "--hf-repo-draft": "--spec-draft-hf",
    "-mg": "--main-gpu", "-ts": "--tensor-split", "-sm": "--split-mode",
    "-devd": "--device-draft", "--spec-draft-device": "--device-draft",
    "-mmdev": "--mmproj-device", "-ot": "--override-tensor",
    "-d": "--n-depth",
}


# Options the shipped profiles/actions put operands on. Help text supplies arity
# where it is line-structured (real llama.cpp); this set covers builds whose
# help is a flat option list, so a leading-dash operand (e.g. a prompt) is never
# mistaken for another option. Flag options are deliberately absent.
_VALUE_CANONICALS = {
    "--model", "-m", "--gpu-layers", "--n-gpu-layers", "-ngl", "--ctx-size", "-c", "--n-predict", "-n",
    "--seed", "--temp", "--prompt", "-p", "--repeat-penalty", "-r", "--output", "-o", "--alias",
    "--n-depth", "-d",
    "--host", "--port", "--api-key-file", "--api-key", "--models-dir", "--models-preset", "--models-max",
    "--n-cpu-moe", "--parallel", "--cache-type-k", "--cache-type-v", "--reasoning-format",
    "--n-cpu-moe", "--parallel", "--cache-type-k", "--cache-type-v", "--reasoning-format",
    "--timeout", "--main-gpu", "--tensor-split", "--split-mode", "--spec-type",
    "--device", "-dev", "--device-draft", "--mmproj-device", "--override-tensor",
    "--mmproj", "--mmproj-url", "--spec-draft-model", "--spec-draft-hf",
}


def _value_taking(help_text):
    """Options whose help line still carries an operand placeholder."""
    taking = set()
    for line in help_text.splitlines():
        options = re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*", line)
        if not options:
            continue
        tail = line.rsplit(options[-1], 1)[1]
        if re.match(r"\s*(?:[=,]\s*)?(?:<[^>]+>|(?:[A-Z][A-Z0-9_-]{1,10}(?:[,\s]|$)))", tail):
            taking.add(OPTION_ALIASES.get(options[-1], options[-1]))
    return taking


def _parsed_arguments(arguments, help_text):
    """Option tokens in order, with each operand consumed as data.

    A value-taking option's operand (inline or the next element) is never
    reported as another option, so values such as prompts that begin with "-"
    stay values. Every caller that reasons about options must use this view;
    scanning raw argument values re-opens the value/option confusion.
    """
    value_taking = _value_taking(help_text) | _VALUE_CANONICALS
    parsed = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-") or re.fullmatch(r"-\d+(?:\.\d+)?", argument):
            index += 1
            continue
        option, equals, inline = argument.partition("=")
        canonical = OPTION_ALIASES.get(option, option)
        has_value = bool(equals) or canonical in value_taking
        value = inline if equals else arguments[index + 1] if has_value and index + 1 < len(arguments) else ""
        parsed.append((option, canonical, value))
        index += 1
        if has_value and not equals:
            index += 1  # the operand is data; never re-validate it as an option
    return parsed


def validate_arguments(arguments, help_text, selector, *, router=False):
    """Validate supplied constraints without dropping or rewriting an argument.

    An option's operand (inline or the element after a value-taking option) is
    data and never re-validated as another option, so values such as prompts
    that begin with "-" pass through unchanged. Returns the parsed option facts
    (device present, gpu-layers values) so callers never rescan raw arguments.
    """
    supported = set(re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*", help_text))
    seen = set()
    gpu_layers = []
    has_primary_device = False
    for option, canonical, value in _parsed_arguments(arguments, help_text):
        if option not in supported:
            raise InspectionError(f"Selected executable does not advertise required option {option}.")
        if canonical in seen:
            raise InspectionError(f"Conflicting duplicate runtime option {canonical}.")
        seen.add(canonical)
        # Every device option is validated, but only the primary --device can
        # stand in for the selector the launcher must impose on the main model.
        if canonical in {"--device", "--device-draft", "--mmproj-device"} and value != selector:
            raise InspectionError(f"Profile option {option} conflicts with the selected physical GPU.")
        if canonical == "--device":
            has_primary_device = True
        if canonical == "--main-gpu" and value != "0":
            raise InspectionError(f"Profile option {option} conflicts with the single selected GPU.")
        if canonical == "--tensor-split" and not re.fullmatch(r"\d+(?:\.\d+)?", value):
            raise InspectionError(f"Profile option {option} requests more than the single selected GPU.")
        if canonical in {"--override-tensor", "--rpc", "--api-key", "--config", "--preset"}:
            raise InspectionError(f"Profile option {option} is not supported by the protected execution contract.")
        if canonical == "--gpu-layers":
            gpu_layers.append(value)
            if not value.isdigit() or int(value) < 1:
                raise InspectionError("Profile GPU layers require positive intentional offload; CPU-only execution is not supported.")
        if canonical == "--host" and value != "127.0.0.1":
            raise InspectionError("Profile --host conflicts with loopback-only execution.")
        if not router and canonical in {"--models-dir", "--models-preset", "--models-max"}:
            raise InspectionError(f"Profile option {option} belongs to the managed router.")
    return {"device": has_primary_device, "gpu-layers": gpu_layers}


def _prepare_launch(action, tool, arguments, gpu_layers=None):
    """Prepare one workload against the selected runtime, whatever it now is.

    A caller names what it wants to run, never where the backend lives or which
    one it is. The committed selection is the single record of both, so replacing
    a build is a selection change and no profile has to be requalified for it.
    """
    selection = read_selection()
    if selection is None:
        raise InspectionError("No runtime selection is configured; explicitly select an installation, backend, and GPU first.")
    installation, backend = selection["installation"], selection["backend"]
    if gpu_layers is not None and (not str(gpu_layers).isdigit() or int(gpu_layers) <= 0):
        raise InspectionError("GPU offload must be positive; CPU-only fallback is not permitted.")
    directory = descriptor = None
    try:
        environment, selected_identity = resolve_device(selection, execution_environment())
    except OSError:
        raise InspectionError("Selected physical GPU could not be resolved for this run.") from None
    try:
        directory, descriptor = _tool_descriptor(installation, tool)
    except (OSError, InspectionError) as error:
        # An active build that is missing is one failure and an active build this
        # account may not execute is another; both leave the operator needing to
        # know which executable, so the refusal names it either way.
        raise InspectionError(f"Selected build cannot be used: {installation}/{tool}: {error}") from None
    try:
        observed = {"executable": fingerprint(descriptor), "build": None}
        help_text = probe(descriptor, directory, "--help", environment)
        if not all(flag in help_text for flag in ("--device", "--list-devices")):
            raise InspectionError(f"Selected action requires {tool} device-selection capabilities.")
        if "--version" in help_text:
            observed["build"] = build_identity(probe(descriptor, directory, "--version", environment))
        expected_hash = os.environ.get("MATUTO_RUNTIME_EXPECTED_SHA256")
        if expected_hash and expected_hash != observed["executable"]["sha256"]:
            raise InspectionError("Selected executable changed during this test run; start a new explicit run after the update.")
        listing = probe(descriptor, directory, "--list-devices", environment)
        listed = [(selector, kind) for selector, kind in _device_listing(listing) if kind == backend]
        if len(listed) != 1:
            raise InspectionError("Selected runtime did not expose exactly one selected backend device.")
        selector = listed[0][0]
        parsed = _parsed_arguments(arguments, help_text)
        checked = validate_arguments(arguments, help_text, selector, router=action == "router")
        if action in ("smoke", "bench", "serve"):
            if checked["gpu-layers"] != [str(gpu_layers)]:
                raise InspectionError("Profile GPU_LAYERS must match exactly one runtime GPU-layer option.")
        if fingerprint(descriptor) != observed["executable"]:
            raise InspectionError("Selected executable changed during preparation; retry after the update.")
        if action in ("smoke", "bench", "serve") and gpu_layers is None:
            raise InspectionError("GPU offload is required for this workload.")
        # Only a caller that verified the weights against the profile may state the
        # digest; an unverified or malformed value stays unavailable rather than
        # being inferred from the path the launch happened to be handed.
        model_sha256 = os.environ.get("MATUTO_RUNTIME_MODEL_SHA256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", model_sha256):
            model_sha256 = "unavailable"
        identity = {"check": action, "installation": installation, "backend": backend, "pci": selection["pci"], "uuid": selected_identity["uuid"], "selector": selector, "executable": observed["executable"], "build": observed.get("build"), "gpuLayers": gpu_layers,
                    "request": {"model": requested_argument(parsed, "--model"), "model_sha256": model_sha256, "context": requested_argument(parsed, "--ctx-size"),
                                "mmproj": requested_argument(parsed, "--mmproj"), "mmproj_sha256": os.environ.get("MATUTO_RUNTIME_MMPROJ_SHA256", ""),
                                "spec_type": requested_argument(parsed, "--spec-type"), "draft_model": requested_argument(parsed, "--spec-draft-model"),
                                "draft_sha256": os.environ.get("MATUTO_RUNTIME_MTP_SHA256", "")},
                    "host": host_observation(), "driver": driver_observation(),
                    "offload": {"requested_gpu_layers": gpu_layers, "placement": "unavailable: per-layer placement is not observed; a requested GPU-layer count is not proof of full offload"}}
        environment["MATUTO_RUNTIME_IDENTITY"] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return directory, descriptor, environment, selector, identity, checked
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
        raise


def _atomic_json(directory, name, value):
    try:
        existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        pass
    else:
        try:
            info = os.fstat(existing)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InspectionError("Runtime record destination is unsafe.")
        finally:
            os.close(existing)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=f"/proc/self/fd/{directory}", prefix=".runtime.", delete=False) as output:
            temporary = output.name
            os.fchmod(output.fileno(), 0o600)
            output.write((json.dumps(value, sort_keys=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def _record_identity(path, identity):
    if not path:
        return
    target = Path(path)
    directory = protected_open(target.parent, directory=True, follow_links=target.parent == RUNTIME_ROOT)
    try:
        info = os.fstat(directory)
        if not directory_is_safe(info):
            raise InspectionError("Runtime identity output path is not a directory.")
        _atomic_json(directory, target.name, identity)
    finally:
        os.close(directory)


def _landlock_confine(executable):
    """Pin descendant execution to the checked executable and its ELF loader.

    Landlock's pathname execute rule otherwise permits a native router to respawn
    a replacement at the same pathname. REFER is allowed only at / so llama.cpp
    retains its model-download/rename behavior; no executable directory is opened.
    """
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    syscall = libc.syscall
    syscall.restype = ctypes.c_long
    def call(number, *args):
        result = syscall(number, *args)
        if result < 0:
            error = ctypes.get_errno()
            raise InspectionError("Selected router requires Landlock execution confinement; kernel capability is unavailable.") from OSError(error, os.strerror(error))
        return result
    # Landlock syscall numbers are stable on x86-64; this project targets x86-64 Linux.
    ruleset_attr = ctypes.c_uint64(1 | 8192)  # EXECUTE | REFER
    rules = call(444, ctypes.byref(ruleset_attr), 8, 0)
    root = os.open("/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    checked = loader = None
    try:
        call(445, rules, 1, ctypes.byref(Rule(8192, root)), 0)
        checked = os.open(f"/proc/self/fd/{executable}", os.O_PATH | os.O_CLOEXEC)
        call(445, rules, 1, ctypes.byref(Rule(1, checked)), 0)
        info = os.pread(executable, 64, 0)
        if len(info) < 64 or info[:4] != b"\x7fELF" or info[4] != 2 or info[5] != 1:
            raise InspectionError("Selected router executable is not a supported x86-64 ELF binary.")
        phoff = int.from_bytes(info[32:40], "little")
        phentsize = int.from_bytes(info[54:56], "little")
        phnum = int.from_bytes(info[56:58], "little")
        if phentsize < 56 or phnum > 128:
            raise InspectionError("Selected router ELF program headers are invalid.")
        headers = os.pread(executable, phentsize * phnum, phoff)
        interp = None
        for offset in range(0, len(headers), phentsize):
            header = headers[offset:offset + phentsize]
            if int.from_bytes(header[:4], "little") == 3:  # PT_INTERP
                start = int.from_bytes(header[8:16], "little"); size = int.from_bytes(header[32:40], "little")
                if size < 2 or size > 4096:
                    raise InspectionError("Selected router ELF interpreter is invalid.")
                raw = os.pread(executable, size, start)
                interp = raw.split(b"\0", 1)[0].decode("ascii")
                break
        if not interp or not interp.startswith("/") or ".." in Path(interp).parts:
            raise InspectionError("Selected router ELF interpreter is unavailable or unsafe.")
        resolved = Path(interp).resolve(strict=True)
        loader = protected_open(resolved, regular_data=True)
        loader_path = os.open(f"/proc/self/fd/{loader}", os.O_PATH | os.O_CLOEXEC)
        try:
            call(445, rules, 1, ctypes.byref(Rule(1, loader_path)), 0)
        finally:
            os.close(loader_path)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise InspectionError("Selected router requires no-new-privileges support for execution confinement.")
        call(446, rules, 0)
    except OSError as error:
        raise InspectionError("Selected router requires protected ELF loader execution; kernel capability is unavailable.") from error
    finally:
        for fd in (loader, checked, root, rules):
            if fd is not None:
                with contextlib.suppress(OSError): os.close(fd)


def launch_runtime(arguments, check_only=False):
    if len(arguments) < 3:
        raise InspectionError("Usage: matuto_runtime.py launch ACTION TOOL [GPU_LAYERS] -- ARGS...")
    action, tool = arguments[:2]
    if {"verify": "llama-server", "serve": "llama-server", "smoke": "llama-cli", "bench": "llama-bench"}.get(action) != tool:
        raise InspectionError("Unsupported action/tool combination; managed routers use matuto-manager router.")
    gpu_layers = arguments[2] if arguments[2] != "-" else None
    if "--" not in arguments[3:]:
        raise InspectionError("Launch arguments must use a fixed separator.")
    split = arguments.index("--", 3)
    command_arguments = arguments[split + 1:]
    lease = runtime_lease(False)
    try:
        directory, descriptor, environment, selector, identity, checked = _prepare_launch(action, tool, command_arguments, gpu_layers)
        if check_only:
            print(json.dumps(identity))
            os.close(descriptor)
            os.close(directory)
            return 0
        _record_identity(os.environ.get("MATUTO_RUNTIME_IDENTITY_PATH"), identity)
        if action in ("smoke", "serve"):
            # A launched check must report the configuration it actually runs. The
            # record is announced before exec because exec replaces this process.
            print("matuto-runtime observation: " + json.dumps({"check": action, "kind": "execution" if action == "smoke" else "serving", "state": "launching", "identity": identity}, sort_keys=True), file=sys.stderr, flush=True)
        os.set_inheritable(lease, True)
        os.set_inheritable(descriptor, True)
        command_arguments = list(command_arguments)
        # Parsed options only: a dash-leading prompt value must never register as
        # a device selection, or the runtime would silently use its own policy.
        if not checked["device"]:
            command_arguments += ["--device", selector]
        os.chdir(f"/proc/self/fd/{directory}")
        os.execve(f"/proc/self/fd/{descriptor}", [tool, *command_arguments], environment)
    finally:
        # exec closes neither inherited lease nor descriptor; failed exec must clean both.
        with contextlib.suppress(OSError): os.close(lease)


# The two speech runtimes take the model differently and advertise none of the
# llama.cpp device-selection options, so placement is imposed on synthesis and
# the CPU-only role carries no visibility constraint at all.
SPEECH_ROLES = {"synthesis": "GPU", "recognition": "CPU"}
SPEECH_BACKENDS = ("Vulkan", "HIP/ROCm", "CPU")
SPEECH_MODEL_OPTION = {"synthesis": (), "recognition": ("-m",)}
# The launcher owns the loopback binding and the model operand, so a profile may
# not restate either and quietly run somewhere else.
SPEECH_OWNED_OPTIONS = ("--host", "-h", "--port", "-p", "-m", "--model")


def _speech_request(role, backend, executable, model, host, port, extra):
    if role not in SPEECH_ROLES:
        raise InspectionError("Speech role must be synthesis or recognition.")
    if backend not in SPEECH_BACKENDS:
        raise InspectionError("Speech backend must be Vulkan, HIP/ROCm, or CPU.")
    if SPEECH_ROLES[role] == "GPU" and backend == "CPU":
        raise InspectionError("GPU synthesis cannot run on the CPU backend.")
    if SPEECH_ROLES[role] == "CPU" and backend != "CPU":
        raise InspectionError("Recognition is a CPU workload and cannot take a GPU backend.")
    if host != "127.0.0.1":
        raise InspectionError("Speech runtimes must bind to loopback only.")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise InspectionError("Speech port must be an integer from 1 to 65535.")
    for option in SPEECH_OWNED_OPTIONS:
        if any(argument.partition("=")[0] == option for argument in extra):
            raise InspectionError(f"Speech profile option {option} conflicts with the launcher-owned configuration.")
    for name, value in (("executable", executable), ("model", model)):
        if not value.startswith("/") or ".." in Path(value).parts:
            raise InspectionError(f"Speech {name} must be an absolute path without parent traversal.")
    return [*SPEECH_MODEL_OPTION[role], model, "--host", host, "--port", port, *extra]


def speech_launch(arguments, check_only=False):
    """Launch one speech runtime, or report the placement it would be launched with.

    The saved selection is the only record of the physical card, so a GPU part is
    placed on the card it names. Placement follows the profile's own backend, not
    the backend the model happens to be selected on: the two APIs name the same
    card differently, so the card's PCI address carries the identification and
    the profile's API supplies the visibility constraint. The llama.cpp-specific
    option corroboration is deliberately absent: these binaries do not advertise
    it, and the visibility constraint below is what places them on the card.
    """
    if len(arguments) < 6 or arguments[6:7] != ["--"]:
        raise InspectionError("Usage: matuto_runtime.py speech[-check] ROLE BACKEND EXECUTABLE MODEL HOST PORT -- ARGS...")
    role, backend, executable, model, host, port = arguments[:6]
    request = _speech_request(role, backend, executable, model, host, port, list(arguments[7:]))
    environment = execution_environment()
    model_sha256 = os.environ.get("MATUTO_RUNTIME_MODEL_SHA256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", model_sha256):
        model_sha256 = "unavailable"
    identity = {
        "check": "speech", "role": role, "backend": backend, "host": host_observation(), "driver": driver_observation(),
        "model": {"path": model, "sha256": model_sha256},
        "placement": "unavailable: the runtime does not advertise device selection; placement rests on the visibility constraint recorded here",
    }
    if SPEECH_ROLES[role] == "GPU":
        selection = read_selection()
        if selection is None:
            raise InspectionError("Synthesis requires a saved runtime selection; select an installation, backend, and GPU first.")
        try:
            environment, device = resolve_device(selection, environment, backend)
        except OSError:
            raise InspectionError("Selected physical GPU could not be resolved for this speech run.") from None
        identity.update({"installation": selection["installation"], "pci": selection["pci"], "uuid": device["uuid"],
                         "visibility": {name: value for name, value in environment.items() if name in VISIBILITY}})
    elif any(name in environment for name in VISIBILITY):
        raise InspectionError("A CPU recognition run cannot carry a GPU visibility constraint.")
    directory = descriptor = None
    try:
        try:
            directory = protected_open(Path(executable).parent, directory=True)
            descriptor = protected_open(executable)
        except OSError:
            # protected_open opens by name relative to the directory descriptor,
            # so a raw error names one path component; say which path was wanted.
            raise InspectionError(f"Speech runtime executable could not be opened: {executable}") from None
        identity["executable"] = fingerprint(descriptor)
        expected_hash = os.environ.get("MATUTO_RUNTIME_EXPECTED_SHA256")
        if expected_hash and expected_hash != identity["executable"]["sha256"]:
            raise InspectionError("Selected executable changed during this test run; start a new explicit run after the update.")
        if check_only:
            print(json.dumps(identity, sort_keys=True))
            return 0
        _record_identity(os.environ.get("MATUTO_RUNTIME_IDENTITY_PATH"), identity)
        environment["MATUTO_RUNTIME_IDENTITY"] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        # Announced before exec because exec replaces this process, so a launched
        # run reports the configuration it actually received.
        print("matuto-runtime observation: " + json.dumps({"check": "speech", "kind": "serving", "state": "launching", "identity": identity}, sort_keys=True), file=sys.stderr, flush=True)
        os.set_inheritable(directory, True)
        os.set_inheritable(descriptor, True)
        os.chdir(f"/proc/self/fd/{directory}")
        os.execve(f"/proc/self/fd/{descriptor}", [Path(executable).name, *request], environment)
    finally:
        for handle in (descriptor, directory):
            if handle is not None:
                with contextlib.suppress(OSError):
                    os.close(handle)


def _selection_record(value):
    required = {"installation", "backend", "pci", "corroborator", "executable"}
    if not isinstance(value, dict) or set(value) != required or value.get("backend") not in ("HIP/ROCm", "Vulkan"):
        raise InspectionError("Runtime selection record is invalid.")
    if not isinstance(value["installation"], str) or not value["installation"].startswith("/") or not isinstance(value["pci"], str) or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", value["pci"]) or not isinstance(value["executable"], dict) or not isinstance(value["corroborator"], dict) or not isinstance(value["corroborator"].get("uuid"), str) or not isinstance(value["corroborator"].get("host"), dict):
        raise InspectionError("Runtime selection record is invalid.")
    pattern = r"GPU-[0-9a-fA-F]{16}" if value["backend"] == "HIP/ROCm" else r"[0-9a-f]{32}"
    if not re.fullmatch(pattern, value["corroborator"]["uuid"]) or value["corroborator"]["host"].get("pciAddress") != value["pci"]:
        raise InspectionError("Runtime physical-device identity is invalid.")
    return value


def read_selection():
    if (RUNTIME_ROOT / ".selection-pending").exists() or (RUNTIME_ROOT / ".selection-pending").is_symlink():
        raise InspectionError("Runtime selection write was interrupted; explicitly save the selection again.")
    try:
        descriptor = protected_open(SELECTION_PATH, regular_data=True, follow_links=True)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if info.st_nlink > 1 or info.st_size > 65536:
            raise InspectionError("Runtime selection record is unsafe.")
        raw = os.read(descriptor, 65537)
    finally: os.close(descriptor)
    try: return _selection_record(json.loads(raw))
    except (UnicodeDecodeError, json.JSONDecodeError): raise InspectionError("Runtime selection record is invalid.") from None


def _write_selection(value):
    value = _selection_record(value)
    directory = _runtime_directory()
    try:
        # An interruption after rename but before fsync must not masquerade as a
        # confirmed choice. Retry replaces this marker only after validation.
        _atomic_json(directory, ".selection-pending", {"state": "interrupted"})
        _atomic_json(directory, "selection.json", value)
        os.unlink(".selection-pending", dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def select_runtime(installation, backend, pci, environment=None):
    """Atomically select a real UUID-correlated PCI device; never a name/index."""
    environment = execution_environment(environment)
    if not isinstance(installation, str) or not installation or len(installation) > 4096:
        raise InspectionError("Selection requires an absolute installation directory.")
    if not isinstance(backend, str) or not isinstance(pci, str) or backend not in ("HIP/ROCm", "Vulkan") or not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", pci):
        raise InspectionError("Selection requires an explicit supported backend and PCI address.")
    if any(name in environment for name in VISIBILITY):
        raise InspectionError("Selection environment already pins a device; remove the visibility constraint and retry.")
    lease = runtime_lease(True)
    try:
        installation = str(installation_bin(installation))
        inspection = inspect_installation(installation)
        tool_name = next((name for name, item in inspection["tools"].items() if item.get("state") == "completed" and {"--device", "--list-devices"} <= set(item.get("capabilities", ()))), None)
        if tool_name is None:
            raise InspectionError("Selected installation lacks a tool with device-selection capabilities.")
        tool = inspection["tools"][tool_name]
        host = next((item for item in pci_devices()["devices"] if item["pciAddress"] == pci), None)
        matches = [item for item in bounded_identity_probe(backend, environment) if item.get("pciAddress") == pci and isinstance(item.get("uuid"), str)]
        if host is None or len(matches) != 1:
            raise InspectionError("Selected backend cannot uniquely corroborate the selected physical PCI device.")
        selection = {"installation": installation, "backend": backend, "pci": pci, "corroborator": {"uuid": matches[0]["uuid"], "host": host}, "executable": tool["executable"]}
        resolved_environment, _identity = resolve_device(selection, environment)
        directory, descriptor = _tool_descriptor(installation, tool_name)
        try:
            listed = [item for item in _device_listing(probe(descriptor, directory, "--list-devices", resolved_environment)) if item[1] == backend]
            if len(listed) != 1:
                raise InspectionError("The installation cannot execute on exactly the selected backend device.")
            selection["executable"] = fingerprint(descriptor)
        finally:
            os.close(descriptor)
            os.close(directory)
        _write_selection(selection)
    finally: os.close(lease)

def settings_cli(arguments):
    """Read the host settings, and manage the build registry, from one place."""
    if arguments[0] == "model-root" and len(arguments) == 1:
        print(model_root())
        return 0
    if arguments[0] == "model-path" and len(arguments) == 3:
        print(model_location(arguments[1], arguments[2]))
        return 0
    if arguments[0] == "installation" and len(arguments) == 1:
        print(active_installation())
        return 0
    if arguments[0] == "builds":
        rest = arguments[1:]
        if not rest or rest[0] == "list":
            print(json.dumps(build_listing(), indent=2))
            return 0
        if rest[0] == "register" and 3 <= len(rest) <= 4:
            print(json.dumps(register_build(rest[1], rest[2], rest[3] if len(rest) == 4 else ""), indent=2))
            return 0
        if rest[0] == "forget" and len(rest) == 2:
            forget_build(rest[1])
            return 0
        if rest[0] == "use" and len(rest) == 2:
            print(json.dumps(activate_build(rest[1]), indent=2))
            return 0
    print("Usage: matuto_runtime.py [builds [list|register ID PATH [LABEL]|forget ID|use ID]|model-root|model-path SOURCE FILE|installation]", file=sys.stderr)
    return 2


def runtime_cli(arguments):
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    # Termination must unwind the finally blocks so in-flight identity probes
    # are reaped instead of leaking a new-session driver child.
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        if not arguments or arguments[0] == "show":
            try:
                selection = read_selection()
                print(json.dumps({"state": "unconfigured"} if selection is None else {"state": "configured", "selection": selection}, indent=2))
                return 0
            except InspectionError as error:
                print(f"matuto-runtime: {error}", file=sys.stderr)
                return 1
        if len(arguments) == 4 and arguments[0] == "select":
            try:
                select_runtime(arguments[1], arguments[2], arguments[3])
                print(json.dumps({"state": "configured", "selection": read_selection()}, indent=2))
                return 0
            except InspectionError as error:
                print(f"matuto-runtime: {error}", file=sys.stderr)
                return 1
        if arguments and arguments[0] in ("launch", "check"):
            try:
                return launch_runtime(arguments[1:], check_only=arguments[0] == "check")
            except (InspectionError, OSError) as error:
                print(f"matuto-runtime: {error}", file=sys.stderr)
                return 1
        if arguments and arguments[0] in ("speech", "speech-check"):
            try:
                return speech_launch(arguments[1:], check_only=arguments[0] == "speech-check")
            except (InspectionError, OSError) as error:
                print(f"matuto-runtime: {error}", file=sys.stderr)
                return 1
        if arguments and arguments[0] in ("builds", "model-root", "model-path", "installation"):
            try:
                return settings_cli(arguments)
            except (InspectionError, OSError) as error:
                print(f"matuto-runtime: {error}", file=sys.stderr)
                return 1
        print("Usage: matuto_runtime.py [show|select /absolute/installation BACKEND PCI|launch ACTION TOOL GPU_LAYERS -- ARGS...|check ACTION TOOL GPU_LAYERS -- ARGS...|speech[-check] ROLE BACKEND EXECUTABLE MODEL HOST PORT -- ARGS...|builds [list|register ID PATH [LABEL]|forget ID|use ID]|model-root|model-path SOURCE FILE|installation]", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("matuto-runtime: interrupted; no selection or workload state was changed. Retry after stopping the operation.", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--identity-probe":
        try:
            print(json.dumps(identity_probe(sys.argv[2])))
        except InspectionError:
            sys.exit(1)
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] in ("show", "select", "launch", "check", "speech", "speech-check", "builds", "model-root", "model-path", "installation"):
        sys.exit(runtime_cli(sys.argv[1:]))
    sys.exit(inspection_cli(sys.argv[1] if len(sys.argv) == 2 else None))
