"""Read what Matuto is serving right now, and change none of it.

SERVE-1 (state). The reader asks systemd for the router and voice units, both
from their unit files and from systemd's own list, the router for its health and
its model list, and the store for the voice record, then reports one serving
state — not serving, agentic serving (healthy or not) or voice serving — with the
loaded model, the running bundle, each voice part and the same-machine addresses
serving answers on. The units are read as well as the record, so a partly running
voice, a speech part left after a stop and a speech unit no record names are
reported rather than hidden. It starts, stops and loads nothing, and a systemd or
a router that does not answer, or answers with something unrecognized, leaves the
affected part unknown rather than stopped or healthy.

The unit names and the voice record are this module's own, and differ from the
checkout install's units, so a rerun of its cutover can never act on them.
"""

import http.client
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from http.client import HTTPConnection
from pathlib import Path

from matuto import store
from matuto.admission import AGENTIC, SERVING_UNKNOWN as UNKNOWN_STATE, STOPPED, VOICE
from matuto.matuto_runtime import InspectionError, read_private_text

# The units the serving start tickets own. They differ from the checkout
# install's local-llama-router.service, matuto-voice-router.service and
# matuto-voice-part-*.service, which its cutover deletes by those names.
ROUTER_UNIT = "matuto-app-router.service"
VOICE_ROUTER_UNIT = "matuto-app-voice-router.service"
VOICE_ROUTER_PORT = 8887
VOICE_PART_PREFIX = "matuto-app-voice-part-"
HOST = "127.0.0.1"
ROUTER_PORT = 8888
# The router credential stays in the app's own private directory: it goes out as
# the bearer token and never appears in what this module reports. CUT-4 makes
# `pi-local/api-key` the one key path, so consumers keep working across the
# cutover; nothing here creates or rotates a second credential.
ROUTER_KEY = ("pi-local", "api-key")
# The unit states systemd answers with. Anything else, a systemctl that does not
# answer included, is unknown: an unread unit is not an inactive one.
ANSWERED = frozenset(("active", "activating", "deactivating", "reloading", "failed", "inactive"))
LIVE = frozenset(("active", "activating", "deactivating", "reloading"))
TRANSITION = frozenset(("activating", "deactivating", "reloading"))
UNKNOWN = "unknown"
# Said when the speech units could not be listed: a speech unit no voice record
# names is then a speech part that cannot be ruled out, so nothing here is read
# as plain not serving.
UNLISTED = ("the speech units could not be listed, so a speech unit no voice "
            "record names cannot be ruled out")
# The shape of the router's own bearer token. A file holding anything else is a
# credential this reader refuses to put in a request or in a message.
CREDENTIAL = re.compile(r"[A-Za-z0-9_-]{32,128}")
# The line systemd prints instead of units when a pattern matches nothing.
LISTED_COUNT = re.compile(r"\d+ units? files? listed\.")
# The load states the router reports for a catalogue entry; anything else is an
# answer this reader does not recognize, which is not an unloaded model.
LOAD_STATUSES = ("loaded", "unloaded", "loading", "unloading", "loading-failed")
MAX_RESPONSE = 1_048_576
SECONDS = 5.0


@dataclass(frozen=True)
class Part:
    """One voice part: its profile, the unit's own state and where it answers.

    `stray` marks a speech unit the voice record does not name; it is reported
    with its state rather than left out.
    """

    profile: str
    role: str
    state: str
    address: str = ""
    stray: bool = False


@dataclass(frozen=True)
class State:
    """The serving state as the reader reports it.

    `state` is `stopped`, `agentic`, `voice` or `unknown`; only agentic and voice
    have something to serve, and `unknown` is a failed read, never an answer.
    `healthy` says nothing was found wrong with what is serving: while agentic
    that is the router unit, its health and its model list; while voice it is the
    units against the record. `reason` says what is wrong whenever the state is
    not plain, `model` names the loaded model, `bundle` the
    running voice bundle, `parts` the speech parts and `units` every unit read
    with its own state, so nothing systemd reported is hidden.
    """

    state: str
    healthy: bool = False
    reason: str = ""
    model: str = ""
    bundle: str = ""
    parts: tuple = ()
    units: tuple = ()
    address: str = ""


def read(*, config_root=None, router_port=ROUTER_PORT, key_path=None, timeout=SECONDS,
         store_connection=None):
    """The serving state as it is now. Reads systemd, the router and the store."""
    key_path = router_key_path() if key_path is None else Path(key_path)
    record = voice_record(config_root, connection=store_connection)
    expected = {VOICE_ROUTER_UNIT, *(part_unit(part["profile"]) for part in parts_of(record))}
    stray, complete = stray_units(record)
    note = "" if complete else UNLISTED
    units = {ROUTER_UNIT, VOICE_ROUTER_UNIT} | expected | set(stray)
    states = {unit: unit_state(unit) for unit in sorted(units)}
    reported = _parts(record, stray, states)
    if any(state in LIVE for unit, state in states.items() if unit != ROUTER_UNIT):
        return _voice(states, record, expected, reported, router_port, note)
    return _agentic(states, reported, router_port, key_path, timeout, note)


def unit_state(unit):
    """One unit's state, read without changing it. Never guesses inactive."""
    try:
        result = subprocess.run(["systemctl", "--user", "is-active", unit], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False, timeout=SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN
    answer = result.stdout.strip()
    return answer if answer in ANSWERED else UNKNOWN


def part_unit(profile):
    """The speech unit of one voice part's profile."""
    return f"{VOICE_PART_PREFIX}{profile}.service"


def unit_directory():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def router_key_path():
    """Where the one router credential lives (CUT-4). Read only, never written."""
    return (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / store.CONFIG_ROOT_NAME / ROUTER_KEY[0] / ROUTER_KEY[1])


def parts_of(record):
    return record["parts"] if record else ()


def generated_units():
    """The speech units systemd lists and the unit files, and whether both answered.

    What each source found is kept even when the other failed, so a speech unit
    systemd knows about is still read and reported; `complete` says only whether
    their absence can be believed.
    """
    listed, files = _listed_units(), _unit_files()
    return set(listed or ()) | set(files or ()), listed is not None and files is not None


def _listed_units():
    """The speech units systemd reports, or None when it does not answer plainly."""
    try:
        result = subprocess.run(["systemctl", "--user", "list-units", "--all", "--no-legend", "--plain",
                                 f"{VOICE_PART_PREFIX}*.service"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False, timeout=SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    names = []
    for line in result.stdout.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0].startswith(VOICE_PART_PREFIX) and tokens[0].endswith(".service"):
            names.append(tokens[0])
        elif not LISTED_COUNT.fullmatch(line.strip()):
            # An answer this reader does not recognize cannot show that no speech
            # unit runs, which is the only thing this listing is read for.
            return None
    return names


def _unit_files():
    """The speech unit files in the user unit directory, or None when it will not list."""
    directory = unit_directory()
    if not directory.exists():
        return set()
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    return {name for name in names
            if name.startswith(VOICE_PART_PREFIX) and name.endswith(".service")}


def stray_units(record):
    """The generated speech units the voice record does not name, and completeness.

    `complete` is False when an enumeration did not answer: what was found is
    still reported, but nothing here can then show that no further unit runs.
    """
    found, complete = generated_units()
    named = {part_unit(part["profile"]) for part in parts_of(record)}
    return sorted(unit for unit in found if unit not in named), complete


def voice_record(config_root=None, *, connection=None):
    """The voice serving record: the running bundle and its parts, or None.

    None means the record is not readable as one voice start: the store is
    refused, missing this block or unreachable, or parts are present with no
    bundle naming them. A store that does not exist yet, or names no bundle, is
    no voice record at all, and reading it creates nothing.
    """
    try:
        absent = not store.store_path(config_root).exists()
    except OSError:
        # Unreachable is not absent: the voice record cannot be read or reported
        # as absent, and nothing here changes a permission to find that out.
        return None
    if absent:
        return {"bundle": "", "parts": []}
    opened = None
    if connection is None:
        # `read_store` is the only opener that cannot create, recover or write.
        opened = store.read_store(config_root)
        if opened is None:
            return None
        connection = opened.connection()
    try:
        serving = connection.execute("SELECT bundle_id FROM voice_serving").fetchall()
        rows = connection.execute(
            "SELECT parts.bundle_id, parts.part_role, members.profile_id, parts.port "
            "FROM voice_serving_parts AS parts JOIN bundle_members AS members "
            "ON members.bundle_id = parts.bundle_id AND members.role = parts.part_role "
            "ORDER BY parts.part_role").fetchall()
    except sqlite3.Error:
        return None
    finally:
        if opened is not None:
            opened.close()
    if not serving:
        return None if rows else {"bundle": "", "parts": []}
    bundle = serving[0][0]
    return {"bundle": bundle,
            "parts": [{"profile": profile, "role": role, "address": f"http://{HOST}:{port}"}
                      for part_bundle, role, profile, port in rows if part_bundle == bundle]}


def _parts(record, stray, states):
    """Every speech part, recorded or not, with its unit's state."""
    return tuple(Part(part["profile"], part["role"], states.get(part_unit(part["profile"]), UNKNOWN),
                     part["address"]) for part in parts_of(record)) + \
        tuple(Part(unit.removeprefix(VOICE_PART_PREFIX).removesuffix(".service"), "",
                   states.get(unit, UNKNOWN), stray=True) for unit in stray)


def _voice(states, record, expected, parts, router_port, note):
    """Voice serving, naming every unit that is not as the record says it is."""
    reasons = [note] if note else []
    if record is None:
        reasons.append("the voice record cannot be read, so the running bundle is unknown")
    elif not record["bundle"]:
        reasons.append("no voice record names the bundle these units are serving")
    elif len(parts_of(record)) != 2:
        reasons.append("the voice record does not name every bundle part")
    reasons += [_unit_reason(unit, states.get(unit, UNKNOWN)) for unit in sorted(expected)
                if states.get(unit, UNKNOWN) != "active"]
    reasons += [_unit_reason(unit, state) for unit, state in sorted(states.items()) if state == UNKNOWN]
    reasons += [f"no voice record names {unit}" for unit, state in sorted(states.items())
                if unit not in expected and state in LIVE]
    # A unit or an enumeration that did not answer leaves the whole state unknown
    # rather than voice, which is what `matuto.admission` refuses a start over.
    failed = bool(note) or any(state == UNKNOWN for state in states.values())
    return State(UNKNOWN_STATE if failed else VOICE, healthy=not reasons,
                 bundle=(record["bundle"] if record else ""),
                 reason=_sentence(list(dict.fromkeys(reasons))), parts=parts, units=_units(states),
                 address=f"http://{HOST}:{VOICE_ROUTER_PORT}")


def _agentic(states, parts, router_port, key_path, timeout, note):
    """Agentic serving, or nothing serving, or a read that did not answer."""
    router = states[ROUTER_UNIT]
    unread = sorted(unit for unit, state in states.items() if state == UNKNOWN)
    if note or unread:
        reasons = ([note] if note else []) + [f"systemd did not report {unit}" for unit in unread]
        return State(UNKNOWN_STATE, reason=_sentence(reasons), parts=parts, units=_units(states))
    if router in TRANSITION:
        return State(UNKNOWN_STATE, reason=f"{ROUTER_UNIT} is {router}, so serving is in transition",
                     parts=parts, units=_units(states))
    if router != "active":
        return State(STOPPED, reason=(f"{ROUTER_UNIT} failed" if router == "failed" else ""),
                     parts=parts, units=_units(states))
    models = router_models(key_path, router_port, timeout)
    reasons = []
    if models is None:
        reasons.append("the router did not answer with a model list")
    if not router_health(key_path, router_port, timeout):
        reasons.append("the router's health did not answer")
    return State(AGENTIC, healthy=not reasons, reason=_sentence(reasons), model=", ".join(models or ()),
                 parts=parts, units=_units(states), address=f"http://{HOST}:{router_port}")


def router_get(path, key_path, router_port, timeout):
    """One read-only router request, or None when it does not answer plainly."""
    key = router_key(key_path)
    if not key:
        return None
    connection = None
    try:
        connection = HTTPConnection(HOST, router_port, timeout=timeout)
        connection.request("GET", path, headers={"authorization": f"Bearer {key}",
                                                 "accept": "application/json"})
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(body) > MAX_RESPONSE:
            return None
        return body
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        if connection is not None:
            connection.close()


def router_key(key_path):
    """The router credential, or None: a missing, unsafe or unusable key is no answer.

    Only the router's own token shape is accepted, so a file holding anything else
    never reaches a request header, where it would raise with its own bytes in
    the message.
    """
    try:
        key = read_private_text(key_path, 4_096).strip()
    except (InspectionError, OSError):
        return None
    return key if CREDENTIAL.fullmatch(key) else None


def router_health(key_path, router_port, timeout):
    """Whether the router's health answers with its own status, or False when not.

    The router answers `{"status": "ok"}`, so a body that is not a JSON object
    with a status is not an answer and leaves agentic serving unhealthy.
    """
    body = router_get("/health", key_path, router_port, timeout)
    if body is None:
        return False
    try:
        answer = json.loads(body)
    except ValueError:
        return False
    return isinstance(answer, dict) and isinstance(answer.get("status"), str) \
        and bool(answer["status"].strip())


def router_models(key_path, router_port, timeout):
    """The loaded model ids the router's own model list names, or None.

    An entry that is not a model with an id is an answer this reader does not
    recognize, so it leaves agentic serving unhealthy rather than reporting the
    models it can still make sense of. An entry the router reports as not loaded
    is not the loaded model and is left out.
    """
    body = router_get("/models", key_path, router_port, timeout)
    if body is None:
        return None
    try:
        listed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(listed, dict) or not isinstance(listed.get("data"), list):
        return None
    loaded = []
    for entry in listed["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            return None
        status = entry.get("status")
        if not isinstance(status, dict) or status.get("value") not in LOAD_STATUSES:
            return None
        if status["value"] == "loaded":
            loaded.append(entry["id"])
    return loaded


def _unit_reason(unit, state):
    if state in TRANSITION:
        return f"{unit} is {state}, so serving is in transition"
    if state == UNKNOWN:
        return f"systemd did not report {unit}"
    return f"{unit} is not running"


def _units(states):
    return tuple(sorted(states.items()))


def _sentence(reasons):
    return "; ".join(reasons) + "." if reasons else ""
