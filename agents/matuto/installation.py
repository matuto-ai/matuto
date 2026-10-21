"""Check one runtime installation at its final path. See README.md for the conventions.

The check runs after the whole staged build tree has been renamed to its final
path, from a working directory unrelated to the installation, and it answers one
question: is this tree the complete, pinned installation the recipe describes.
Everything it reads statically comes first — the dynamic section of the server
and of every shared library in the tree — so a tree with an unsafe loader path
is rejected before any executable in it runs. Only then does it run the server's
bounded `--help`, `--version` and `--list-devices` probes.

The safety rule is the install probe's (loader-path safety): every RPATH and
RUNPATH entry must be non-empty and start with `$ORIGIN`, and every project
library a server or a library loads must resolve inside the installation. An
empty entry is searched as the working directory, and the router changes into a
models directory before it starts a server, so a planted library there would win.
A dependency name that carries a slash is a path rather than a library name: the
loader reads it against the working directory, behind RUNPATH, so it is refused
too. A dependency the tree provides is inspected and held to the same rule even
when its file name does not end in `.so`.

The probes are observations, never proof: a listed device shows the driver sees
a card, and a server that starts proves neither GPU inference nor model
readiness. `limits` says so on every result.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from matuto import recipes
from matuto.matuto_runtime import (InspectionError, build_identity, fingerprint, probe,
                            protected_open)

STATE_CHECKED = "checked"
STATE_UNCHECKED = "unchecked"

# Where each recipe puts its server in the renamed build tree, from the install
# probe that built, moved and checked all four recipes.
SERVER_PATHS = {
    recipes.LLAMA_HIP: "bin/llama-server",
    recipes.LLAMA_VULKAN: "bin/llama-server",
    recipes.BREEZE: "breeze-server",
    recipes.WHISPER: "bin/whisper-server",
}

# The shared libraries each project builds for itself, by name prefix. A library
# the server loads that belongs to the project must be provided by the
# installation and resolve inside it. Every other name is a system library: the
# loader finds it through the system cache, which this check does not model.
PROJECT_LIBRARIES = {
    recipes.LLAMA_HIP: ("libggml", "libllama", "libmtmd"),
    recipes.LLAMA_VULKAN: ("libggml", "libllama", "libmtmd"),
    recipes.BREEZE: ("libggml", "libbreeze"),
    recipes.WHISPER: ("libggml", "libwhisper"),
}

# The option a server must advertise before Matuto can start it. The model
# option is read from the help text, never from an exit status: whisper-server
# exits 0 for an unknown option. Breeze takes its model positionally, so it has
# no required option of its own.
REQUIRED_CAPABILITIES = {
    recipes.LLAMA_HIP: ("--model",),
    recipes.LLAMA_VULKAN: ("--model",),
    recipes.BREEZE: (),
    recipes.WHISPER: ("--model",),
}

LIMITS = (
    "A listed device or a successful start does not show that inference uses a GPU.",
    "No model is loaded here, so this does not establish model readiness or performance.",
)

RADV_WARNING = "radv is not a conformant Vulkan implementation"
COMMIT = re.compile(r"[0-9a-f]{40}")
OPTION = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
SHARED_OBJECT = re.compile(r"\.so(?:\.|$)")
MAX_LIBRARIES = 4096
MAX_STRING_TABLE = 1 << 20

ELF_MAGIC = b"\x7fELF"
PT_LOAD = 1
PT_DYNAMIC = 2
DT_NEEDED = 1
DT_STRTAB = 5
DT_STRSZ = 10
DT_SONAME = 14
DT_RPATH = 15
DT_RUNPATH = 29


@dataclass(frozen=True)
class Finding:
    """One static refusal: the file, the offending entry, and why."""

    file: str
    entry: str
    reason: str


@dataclass(frozen=True)
class Check:
    """The result for one installation. Only `checked` means every step passed."""

    state: str
    reason: str = ""
    server: str = ""
    digests: dict = field(default_factory=dict)
    libraries: tuple = ()
    loader_findings: tuple = ()
    identity: dict = field(default_factory=dict)
    observations: dict = field(default_factory=lambda: {"capabilities": (), "build": None,
                                                        "devices": (), "warnings": ()})
    limits: tuple = LIMITS


def static_check(location, recipe):
    """Return static loader findings and file identities without running the tree."""
    chosen = _recipe(recipe)
    findings, digests, _libraries, _server, _root, server_fd = _inspect(location, chosen)
    if server_fd is not None:
        os.close(server_fd)
    return findings, digests


def loader_findings(location, recipe):
    """The static loader findings of one installation; runs nothing from the tree.

    This is the part of the check an external tree may be held to before anybody
    consents to running it, so it must be able to run and to fail on its own.
    """
    return static_check(location, recipe)[0]


def check(location, recipe, *, source_commit=None, submodules=None, verified_checkout=False,
          working_directory=None, expected_digests=None):
    """Check one installation at its final path and report what it proves.

    `source_commit` and `submodules` are the pins the build was made for; they
    may be None, meaning provenance is unknown, as it stays for an external tree.
    `verified_checkout` is the build job's handoff that it confirmed those pins
    in the clean source checkout before the rename, which is the only channel a
    renamed building tree has. A supplied pin that neither the tree records nor
    the executable reports and that no such handoff covers leaves the tree
    unchecked. `working_directory` is the caller's unrelated directory the
    probes run from; it defaults to this process's working directory.
    """
    chosen = _recipe(recipe)
    source_commit = _commit(source_commit, "source commit")
    submodules = _submodule_pins(submodules)
    findings, digests, libraries, server, root, server_fd = _inspect(location, chosen)
    if findings:
        return Check(STATE_UNCHECKED, _reason(findings), server=server, digests=digests,
                     libraries=libraries, loader_findings=tuple(findings))
    if expected_digests is not None and digests != expected_digests:
        os.close(server_fd)
        return Check(STATE_UNCHECKED,
                     "The installation changed after its static findings; review it again before probing.",
                     server=server, digests=digests, libraries=libraries)
    try:
        identity, mismatch = _verify_git(root, source_commit, submodules, verified_checkout)
        if mismatch:
            return Check(STATE_UNCHECKED, mismatch, server=server, digests=digests,
                         libraries=libraries, identity=identity)
        return _probe(server_fd, server, chosen, source_commit, identity, digests, libraries,
                      working_directory, verified_checkout)
    finally:
        os.close(server_fd)


def _probe(server_fd, server, chosen, source_commit, identity, digests, libraries, working_directory,
           verified_checkout):
    """The executable part, reached only after the static part passed."""
    directory_path = Path(working_directory) if working_directory is not None else Path(os.getcwd())
    try:
        directory = protected_open(directory_path, directory=True)
    except (InspectionError, OSError) as error:
        return Check(STATE_UNCHECKED, f"The working directory cannot be opened: {error}", server=server,
                     digests=digests, libraries=libraries, identity=identity)
    try:
        try:
            help_text = probe(server_fd, directory, "--help")
        except InspectionError as error:
            return Check(STATE_UNCHECKED, f"The {server} help probe failed: {error}", server=server,
                         digests=digests, libraries=libraries, identity=identity)
        capabilities = tuple(sorted(set(OPTION.findall(help_text))))
        observations = {"capabilities": capabilities, "build": None, "devices": (), "warnings": ()}
        missing = tuple(flag for flag in REQUIRED_CAPABILITIES[chosen] if flag not in capabilities)
        if missing:
            return Check(STATE_UNCHECKED, f"The {server} help text does not offer {', '.join(missing)}",
                         server=server, digests=digests, libraries=libraries, identity=identity,
                         observations=observations)
        output = help_text
        build = None
        if "--version" in capabilities:
            try:
                version_text = probe(server_fd, directory, "--version")
            except InspectionError as error:
                return Check(STATE_UNCHECKED, f"The {server} version probe failed: {error}", server=server,
                             digests=digests, libraries=libraries, identity=identity,
                             observations=observations)
            output += version_text
            build = build_identity(version_text)
        observations["build"] = build
        identity, mismatch = _verify_reported(identity, source_commit, build["revision"] if build else None,
                                             verified_checkout)
        if mismatch:
            return Check(STATE_UNCHECKED, mismatch, server=server, digests=digests, libraries=libraries,
                         identity=identity, observations=observations)
        devices = ()
        if "--list-devices" in capabilities:
            try:
                listing = probe(server_fd, directory, "--list-devices")
                devices = _device_lines(listing)
                output += listing
            except InspectionError as error:
                # The step was attempted and failed, so the installation is not
                # checked. The lines it does list stay observations, never proof.
                return Check(STATE_UNCHECKED, f"The {server} device probe failed: {error}",
                             server=server, digests=digests, libraries=libraries, identity=identity,
                             observations=observations)
        warnings = (RADV_WARNING,) if RADV_WARNING in output else ()
        observations["devices"] = tuple(devices)
        observations["warnings"] = warnings
        return Check(STATE_CHECKED, "", server=server, digests=digests, libraries=libraries,
                     identity=identity, observations=observations)
    finally:
        os.close(directory)


def _inspect(location, chosen):
    """The static part: the server, every shared library, and their loader paths.

    Returns the server descriptor open on success so the static check and the
    probes read the same inode; the caller closes it.
    """
    server = SERVER_PATHS[chosen]
    if not isinstance(location, str) or not location or len(location) > 4096 or any(
            ord(character) < 32 or ord(character) == 127 for character in location):
        raise ValueError("Supply one absolute installation directory, not a command or options.")
    root = Path(location)
    try:
        root_fd = protected_open(root, directory=True)
    except FileNotFoundError:
        return (Finding("", "", f"Installation directory is absent: {location}"),), {}, (), server, root, None
    except (InspectionError, OSError) as error:
        return (Finding("", "", f"The installation cannot be opened: {error}"),), {}, (), server, root, None
    os.close(root_fd)
    try:
        server_fd = protected_open(root / server)
    except FileNotFoundError:
        return (Finding(server, "", "the expected executable is absent"),), {}, (), server, root, None
    except (InspectionError, OSError) as error:
        return (Finding(server, "", f"the expected executable cannot be checked: {error}"),), \
            {}, (), server, root, None
    try:
        dynamic = _dynamic(server_fd)
    except InspectionError as error:
        os.close(server_fd)
        return (Finding(server, "", f"is not a shared object this check can read: {error}"),), \
            {}, (), server, root, None
    findings = []
    try:
        digests = {server: _checked_identity(server_fd, server)}
    except InspectionError as error:
        os.close(server_fd)
        return (Finding(server, "", f"the expected executable cannot be read: {error}"),), \
            {}, (), server, root, None
    libraries = []
    objects = [(server, dynamic, str((root / server).parent))]
    names = set()
    inspected = {os.path.realpath(root / server)}
    for relative, path in _shared_objects(root, server):
        if len(objects) > MAX_LIBRARIES:
            findings.append(Finding("", "", f"the installation holds more than {MAX_LIBRARIES} shared libraries"))
            break
        try:
            descriptor = _open_library(root, path)
        except (InspectionError, FileNotFoundError, OSError) as error:
            findings.append(Finding(relative, "", f"the shared library cannot be checked: {error}"))
            continue
        try:
            try:
                library = _dynamic(descriptor)
            except InspectionError:
                # A `*.so` name that is not a mapped shared object (a linker
                # script) is not something the loader loads.
                continue
            try:
                digests[relative] = _checked_identity(descriptor, relative)
            except InspectionError as error:
                findings.append(Finding(relative, "", f"the shared library cannot be read: {error}"))
                continue
        finally:
            os.close(descriptor)
        names.add(relative.rsplit("/", 1)[-1])
        if library["soname"]:
            names.add(library["soname"])
        inspected.add(os.path.realpath(path))
        objects.append((relative, library, str(path.parent)))
    prefixes = PROJECT_LIBRARIES[chosen]
    for relative, library, directory in objects:
        findings.extend(_loader_findings(relative, library))
        entries = library["runpath"] or library["rpath"]
        for name in library["needed"]:
            if "/" in name:
                # A name holding a slash is a path: the loader reads it against
                # the child's working directory, behind RUNPATH, so it can load
                # a planted library without any unsafe entry.
                findings.append(Finding(relative, name,
                                        f"loads {name!r}, which is a path rather than a library name"))
                continue
            resolved = _resolve(name, directory, entries)
            inside = resolved is not None and Path(resolved).is_relative_to(Path(os.path.realpath(root)))
            if name in names or name.startswith(prefixes):
                libraries.append((name, resolved or "", inside))
                if not inside:
                    findings.append(Finding(relative, name,
                                            f"loads {name}, which does not resolve inside the installation"))
                    continue
            elif not inside:
                continue
            if resolved is None or resolved in inspected:
                continue
            # A library the tree provides is held to the same checks even when
            # its file name does not end in `.so` and the walk above missed it.
            if len(objects) > MAX_LIBRARIES:
                findings.append(Finding("", "", f"the installation holds more than {MAX_LIBRARIES} shared libraries"))
                break
            inspected.add(resolved)
            nested_relative = os.path.relpath(resolved, os.path.realpath(root))
            try:
                descriptor = _open_library(root, resolved)
            except (InspectionError, FileNotFoundError, OSError) as error:
                findings.append(Finding(relative, name, f"loads {name}, which cannot be checked: {error}"))
                continue
            try:
                try:
                    nested = _dynamic(descriptor)
                except InspectionError as error:
                    findings.append(Finding(relative, name,
                                            f"loads {name}, which this check cannot read as a shared object: {error}"))
                    continue
                try:
                    digests[nested_relative] = _checked_identity(descriptor, nested_relative)
                except InspectionError as error:
                    findings.append(Finding(relative, name, f"loads {name}, which cannot be read: {error}"))
                    continue
            finally:
                os.close(descriptor)
            if nested["soname"]:
                names.add(nested["soname"])
            objects.append((nested_relative, nested, str(Path(resolved).parent)))
    if findings:
        os.close(server_fd)
        return tuple(findings), digests, tuple(libraries), server, root, None
    return (), digests, tuple(libraries), server, root, server_fd


def _checked_identity(descriptor, relative):
    status = os.fstat(descriptor)
    if status.st_uid != os.getuid():
        raise InspectionError(f"{relative} belongs to another account")
    if status.st_mode & 0o022:
        raise InspectionError(f"{relative} is writable by group or others")
    identity = fingerprint(descriptor)
    after = os.fstat(descriptor)
    if (status.st_uid, status.st_mode) != (after.st_uid, after.st_mode):
        raise InspectionError(f"{relative} changed ownership or permissions during inspection")
    identity["owner"] = after.st_uid
    identity["mode"] = oct(after.st_mode & 0o777)
    return identity


def _recipe(recipe):
    if recipe not in SERVER_PATHS:
        raise ValueError(f"{recipe!r} is not one of the four fixed recipes")
    return recipe


def _commit(value, what):
    if value is None:
        return None
    if not isinstance(value, str) or not COMMIT.fullmatch(value):
        raise ValueError(f"{value!r} is not a full 40-character {what}")
    return value


def _submodule_pins(submodules):
    if submodules is None:
        return {}
    if not isinstance(submodules, dict):
        raise ValueError("Submodule commits must be a mapping of path to full commit")
    pins = {}
    for path, commit in submodules.items():
        if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"{path!r} is not a relative submodule path")
        pins[path] = _commit(commit, "submodule commit")
    return pins


def _shared_objects(root, server):
    """Every shared library in the tree, in a stable order, following no links."""
    found = []
    for directory, subdirectories, filenames in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if name != ".git")
        for name in sorted(filenames):
            if not SHARED_OBJECT.search(name):
                continue
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if relative != server:
                found.append((relative, path))
    return found


def _open_library(root, path):
    """The library's descriptor, following only a link that stays in the tree."""
    if os.path.islink(path):
        target = os.path.realpath(path)
        if not Path(target).is_relative_to(Path(os.path.realpath(root))):
            raise InspectionError("the shared library is a link out of the installation")
        return protected_open(target)
    return protected_open(path)


def _loader_findings(relative, dynamic):
    """RPATH and RUNPATH entries that must not be there, each named."""
    findings = []
    for kind, entries in (("RPATH", dynamic["rpath"]), ("RUNPATH", dynamic["runpath"])):
        for entry in entries:
            if entry == "":
                findings.append(Finding(relative, "", f"has an empty {kind} entry; "
                                                       "the loader searches the working directory for it"))
            elif not entry.startswith("$ORIGIN"):
                findings.append(Finding(relative, entry,
                                        f"has the {kind} entry {entry!r}, which does not start with $ORIGIN"))
    return findings


def _resolve(name, directory, entries):
    """Where the loader would find a library beside its RPATH/RUNPATH, or None."""
    for entry in entries:
        candidate = os.path.join(entry.replace("$ORIGIN", directory), name)
        if os.path.exists(candidate):
            return os.path.realpath(candidate)
    return None


def _verify_git(root, source_commit, submodules, verified_checkout):
    """The pins against the git metadata a renamed checkout would carry."""
    identity = {"source": {"commit": source_commit, "recorded": None, "reported": None, "verified": False},
                "submodules": {}}
    mismatch = None
    if source_commit is not None:
        recorded = _git_head(root / ".git")
        identity["source"]["recorded"] = recorded
        if recorded is not None and not _same_commit(source_commit, recorded):
            mismatch = f"The installation records source commit {recorded}, not the pinned {source_commit}"
    for path, commit in submodules.items():
        recorded = _git_head(root / ".git" / "modules" / path)
        entry = {"commit": commit, "recorded": recorded, "verified": False}
        identity["submodules"][path] = entry
        if recorded is not None:
            if _same_commit(commit, recorded):
                entry.update(verified=True, channel="checkout metadata")
            elif mismatch is None:
                mismatch = f"The installation records submodule {path} at {recorded}, not the pinned {commit}"
        elif verified_checkout:
            entry.update(verified=True, channel="build checkout")
        elif mismatch is None:
            mismatch = (f"The installation records no commit for submodule {path} to verify the pinned "
                        f"{commit}; the build job must confirm it in the clean checkout")
    return identity, mismatch


def _verify_reported(identity, source_commit, reported, verified_checkout):
    """The source pin against the commit the executable reports, if it reports one."""
    identity["source"]["reported"] = reported
    if source_commit is None:
        return identity, ""
    if reported is not None and not _same_commit(source_commit, reported):
        return identity, f"The executable reports commit {reported}, not the pinned {source_commit}"
    reported_matches = reported is not None and _same_commit(source_commit, reported)
    recorded = identity["source"]["recorded"]
    recorded_matches = recorded is not None and _same_commit(source_commit, recorded)
    if reported_matches:
        identity["source"].update(verified=True, channel="executable")
    elif recorded_matches:
        identity["source"].update(verified=True, channel="checkout metadata")
    elif verified_checkout:
        identity["source"].update(verified=True, channel="build checkout")
    else:
        return identity, (f"The installation records no source commit to verify the pinned {source_commit}; "
                          "the build job must confirm it in the clean checkout")
    return identity, ""


def _same_commit(pinned, recorded):
    """Full commits, or the abbreviation a build reports (llama.cpp uses nine)."""
    return isinstance(recorded, str) and len(recorded) >= 7 and pinned.startswith(recorded)


def _git_head(dotgit):
    """The commit a git directory records. Reads files; runs nothing."""
    try:
        head = (dotgit / "HEAD").read_text().strip()
    except OSError:
        return None
    if COMMIT.fullmatch(head):
        return head
    if not head.startswith("ref: "):
        return None
    reference = head[5:].strip()
    try:
        value = (dotgit / reference).read_text().strip()
        if COMMIT.fullmatch(value):
            return value
    except OSError:
        pass
    try:
        for line in (dotgit / "packed-refs").read_text().splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == reference and COMMIT.fullmatch(parts[0]):
                return parts[0]
    except OSError:
        pass
    return None


def _device_lines(listing):
    """The listed devices, reported as observations and never as GPU proof."""
    if "Available devices:" not in listing:
        return ()
    return tuple(line.strip() for line in listing.split("Available devices:", 1)[1].splitlines() if line.strip())


def _reason(findings):
    return "; ".join(f"{finding.file}: {finding.reason}" if finding.file else finding.reason
                     for finding in findings)


def _dynamic(descriptor):
    """The NEEDED libraries, SONAME and RPATH/RUNPATH entries of one ELF object."""
    head = os.pread(descriptor, 64, 0)
    if len(head) < 64 or head[:4] != ELF_MAGIC:
        raise InspectionError("not an ELF object")
    if head[4] != 2 or head[5] != 1:
        raise InspectionError("not a 64-bit little-endian ELF object")
    phoff = int.from_bytes(head[32:40], "little")
    phentsize = int.from_bytes(head[54:56], "little")
    phnum = int.from_bytes(head[56:58], "little")
    if phoff == 0 or phnum == 0:
        return _empty_dynamic()
    if phentsize < 56 or phentsize > 4096 or phnum > 1024:
        raise InspectionError("the ELF program headers are invalid")
    loads = []
    dynamic = None
    for index in range(phnum):
        raw = os.pread(descriptor, phentsize, phoff + index * phentsize)
        if len(raw) < 56:
            raise InspectionError("the ELF program headers are truncated")
        kind = int.from_bytes(raw[0:4], "little")
        if kind == PT_LOAD:
            loads.append((int.from_bytes(raw[16:24], "little"), int.from_bytes(raw[8:16], "little"),
                          int.from_bytes(raw[32:40], "little")))
        elif kind == PT_DYNAMIC:
            dynamic = (int.from_bytes(raw[8:16], "little"), int.from_bytes(raw[32:40], "little"))
    if dynamic is None:
        return _empty_dynamic()
    if dynamic[1] == 0 or dynamic[1] > MAX_STRING_TABLE or dynamic[1] % 16:
        raise InspectionError("the ELF dynamic section is invalid")
    raw = os.pread(descriptor, dynamic[1], dynamic[0])
    if len(raw) != dynamic[1]:
        raise InspectionError("the ELF dynamic section is truncated")
    values = {}
    for index in range(0, len(raw), 16):
        tag = int.from_bytes(raw[index:index + 8], "little", signed=True)
        if tag == 0:
            break
        value = int.from_bytes(raw[index + 8:index + 16], "little")
        if tag in (DT_NEEDED, DT_SONAME, DT_RPATH, DT_RUNPATH):
            values.setdefault(tag, []).append(value)
        elif tag in (DT_STRTAB, DT_STRSZ):
            values[tag] = value
    string_table = values.get(DT_STRTAB)
    size = values.get(DT_STRSZ, 0)
    if string_table is None or not 0 < size <= MAX_STRING_TABLE:
        raise InspectionError("the ELF string table is missing")
    offset = _file_offset(loads, string_table)
    if offset is None:
        raise InspectionError("the ELF string table is not mapped")
    table = os.pread(descriptor, size, offset)
    if len(table) != size:
        raise InspectionError("the ELF string table is truncated")

    def string(index):
        if index >= len(table):
            raise InspectionError("an ELF string offset is out of range")
        end = table.find(b"\0", index)
        if end < 0:
            raise InspectionError("an ELF string is not terminated")
        return table[index:end].decode("utf-8", "replace")

    return {"needed": tuple(string(value) for value in values.get(DT_NEEDED, ())),
            "soname": string(values[DT_SONAME][0]) if values.get(DT_SONAME) else None,
            "rpath": _entries(values.get(DT_RPATH), string),
            "runpath": _entries(values.get(DT_RUNPATH), string)}


def _empty_dynamic():
    return {"needed": (), "soname": None, "rpath": (), "runpath": ()}


def _entries(values, string):
    if not values:
        return ()
    return tuple(part for part in string(values[0]).split(":"))


def _file_offset(loads, address):
    for vaddr, offset, size in loads:
        if vaddr <= address < vaddr + size:
            return offset + (address - vaddr)
    return None
