"""Turn a profile's argument rows into argv, refusing what the launcher owns.

Pure: it reads no file, starts no process and changes no record. See README.md
for the conventions.
"""

import re

from matuto.matuto_runtime import OPTION_ALIASES, _VALUE_CANONICALS, _parsed_arguments, _value_taking

# The runtime's hard refusals, device conflicts, duplicates and the host, port
# and model options the launcher owns. A Custom row carries no exemption.
REFUSED = ("--override-tensor", "--rpc", "--api-key", "--config", "--preset")
LAUNCHER_OWNED = ("--host", "--port", "--model")
DEVICE_OPTIONS = ("--device", "--device-draft", "--mmproj-device")
TOOLS = ("server", "smoke", "bench")


class Refusal(Exception):
    """One row the configuration may not carry, named with its reason."""


def save_checks(rows, catalogue=(), *, selector=None, help_text=None):
    """Every tool's argv; an option the active build does not advertise warns.

    help_text maps each tool to that tool's `--help` output from the active
    build: the same text the runtime reads, so a row is measured against what
    will actually run. Without it only a row's own spelling is checked.
    """
    return _checked(rows, catalogue, selector, help_text, launch=False)


def launch_check(rows, catalogue=(), *, selector=None, help_text=None):
    """Every tool's argv; an unadvertised option, or no help at all, refuses."""
    return _checked(rows, catalogue, selector, help_text, launch=True)


def _checked(rows, catalogue, selector, help_text, launch):
    entries = {entry["flag"]: entry for entry in catalogue or ()}
    argv, warnings = {}, []
    for tool, tool_rows in _grouped(rows).items():
        # The build's help is what says which options it advertises and which
        # take a value. A tool with rows cannot be launched without it.
        help = _help(help_text, tool)
        if tool_rows and launch and not help:
            raise Refusal(f"{tool}: the active build's advertised options are unavailable, so nothing can be checked against them.")
        supported = _supported(help) if help else None
        aliases = _short_option_aliases(help) if help else {}
        taking = (_value_taking(help) | set(_VALUE_CANONICALS)) if help else set(_VALUE_CANONICALS)
        elements, seen = [], set()
        for position, row in enumerate(tool_rows, 1):
            flag = _flag(row, entries, tool, position)
            value = "" if row.get("value") is None else str(row["value"])
            spelling = flag.partition("=")[0]
            canonical = OPTION_ALIASES.get(spelling, aliases.get(spelling, spelling))
            inline = "=" in flag
            advertised = supported is not None and spelling in supported
            # A value has a place only as the flag's operand. A flag the build
            # says takes no value, or one that already carries an inline
            # operand, leaves the value to be read as another option.
            operand_value = bool(value) and not inline and canonical in taking
            operands = [flag, value] if operand_value else [flag]
            # The runtime's own view decides what the flag means, so a refused
            # or protected spelling is named before the value is judged.
            for option, parsed, operand in _parsed_arguments(operands, help or ""):
                parsed = OPTION_ALIASES.get(option, aliases.get(option, parsed))
                _refuse(parsed, operand, selector, tool, position, flag, seen)
                if supported is not None and option not in supported:
                    message = _named(tool, position, flag, f"{option} is not advertised by the active build")
                    if launch:
                        raise Refusal(message + ", so the launch refuses it")
                    warnings.append(message + "; the launch will refuse it")
                seen.add(parsed)
            if value and not operand_value:
                if inline:
                    raise Refusal(_named(tool, position, flag, f"already carries its value inline, so {value} would be read as another option"))
                if advertised:
                    raise Refusal(_named(tool, position, flag, f"takes no value, so {value} would be read as another option"))
            elif not value and not inline and canonical in taking:
                # A missing operand would let the runtime read the next row's
                # flag as this row's value, hiding that flag from the checks.
                raise Refusal(_named(tool, position, flag, "takes a value but carries none"))
            # A value is one argv element whatever it holds: it is data, never
            # split and never passed through a shell. An unadvertised flag has
            # no known arity, so its value is never re-read as an option either.
            elements.append(flag)
            if value:
                elements.append(value)
        argv[tool] = elements
    return {"argv": argv, "warnings": warnings}


def _refuse(canonical, operand, selector, tool, position, flag, seen):
    if canonical in REFUSED:
        raise Refusal(_named(tool, position, flag, f"{canonical} is refused by the protected execution contract"))
    if canonical in LAUNCHER_OWNED:
        raise Refusal(_named(tool, position, flag, f"{canonical} is owned by the launcher, not the profile"))
    if canonical in DEVICE_OPTIONS and operand != selector:
        raise Refusal(_named(tool, position, flag, f"{canonical} conflicts with the card the launcher selects"))
    if canonical == "--main-gpu" and operand != "0":
        raise Refusal(_named(tool, position, flag, f"{canonical} conflicts with the single selected GPU"))
    if canonical == "--tensor-split" and not re.fullmatch(r"\d+(?:\.\d+)?", operand):
        raise Refusal(_named(tool, position, flag, f"{canonical} requests more than the single selected GPU"))
    if canonical in seen:
        raise Refusal(_named(tool, position, flag, f"{canonical} is already set for {tool}"))


def _flag(row, entries, tool, position):
    """The flag a row names; a Custom row is the flag it carries itself."""
    catalogue = row.get("catalogue")
    if catalogue is not None:
        entry = entries.get(catalogue)
        if entry is None:
            raise Refusal(_named(tool, position, catalogue, "names no argument-catalogue entry"))
        if row.get("value") and not entry.get("takesValue"):
            raise Refusal(_named(tool, position, entry["flag"], "names a catalogue entry that takes no value"))
        return entry["flag"]
    if not row.get("option"):
        raise Refusal(_named(tool, position, row.get("option"), "carries neither a catalogue entry nor a Custom flag"))
    return row["option"]


def _grouped(rows):
    grouped = {tool: [] for tool in TOOLS}
    for row in rows:
        grouped.setdefault(row.get("tool"), []).append(row)
    return grouped


def _help(help_text, tool):
    """One tool's help from the active build, or nothing when it is unreadable."""
    help = help_text.get(tool) if hasattr(help_text, "get") else None
    return help if isinstance(help, str) else None


def _supported(help):
    """The option spellings the build's help lists, read as the runtime reads them."""
    return set(re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*", help))


def _short_option_aliases(help):
    aliases = {}
    for line in help.splitlines():
        options = re.findall(r"(?<![\w-])--?[A-Za-z][A-Za-z0-9-]*", line)
        pending = []
        for option in options:
            if option.startswith("--"):
                aliases.update((short, option) for short in pending)
                pending.clear()
            else:
                pending.append(option)
    return aliases


def canonicalize_server_aliases(rows, help_text):
    """Persist advertised server short flags by their equivalent long spelling."""
    help = _help(help_text, "server")
    aliases = _short_option_aliases(help) if help else {}
    if not aliases:
        return rows
    canonical = []
    for row in rows:
        if row.get("tool") != "server":
            canonical.append(row)
            continue
        flag = row.get("catalogue") or row.get("option") or ""
        spelling, separator, value = flag.partition("=")
        long_option = aliases.get(spelling)
        if long_option is None:
            canonical.append(row)
            continue
        normalized = dict(row)
        normalized.pop("catalogue", None)
        normalized["option"] = long_option + ("=" + value if separator else "")
        canonical.append(normalized)
    return canonical


def _named(tool, position, flag, reason):
    return f"{tool} argument row {position} ({flag}): {reason}"
