"""The llama.cpp build registry: register, forget and discover installation trees.

Build, Inspect, Register and Use stay separate. A registration is one stable ID,
one editable label and the tree that ID names; it is written by `register` alone,
so a completed build records its installation and registers nothing (RT-7). An ID
never moves to another tree, and `forget` removes only the registration.

The recorded installation is the build job's: it writes one row per tree it
renamed into place, with the result of the installation check at the final path, and that
row outlives the job's log. This module owns its shape because discovery reads
it. `inspect` re-runs that check on a tree a Matuto build installed and records
the new result; it registers and selects nothing, and a tree Matuto did not
install is refused, because running an unknown tree's executables is the
explicit external-build trust-consent path.

Discovery reads the store and the filesystem and runs nothing from any tree. A
tree a Matuto build installed and checked appears as discovered while it is
unregistered and still runnable; a check that failed or never finished, or a
tree missing its recipe's server, leaves it found but unchecked with the reason,
which is also how an old checkout build under the build location stays
discoverable without being moved (CUT-4).
Matuto's staging tree lives outside the scanned location, so partial or
unverified staging never appears. Nothing here writes the runtime selection, so
there is no Clear selection.
"""

import contextlib
import datetime
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from matuto import installation, recipes, store

# Today's registry's rules, unchanged: a short name of letters, digits, dot,
# dash or underscore, and a one-line label of bounded length.
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LABEL_LIMIT = 200
# The two llama.cpp recipes are the only trees this registry holds; a Breeze or
# whisper installation is a speech executable, not a build.
LLAMA_BUILDS = {
    recipes.LLAMA_HIP: ("llama-hip", "hip"),
    recipes.LLAMA_VULKAN: ("llama-vulkan", "vulkan"),
}
CHECKED = installation.STATE_CHECKED
UNCHECKED = installation.STATE_UNCHECKED
# Where a llama.cpp build puts its server: in `bin/` after the install, at the
# tree root in the layout some older builds used.
SERVER_NAMES = ("bin/llama-server", "llama-server")


def build_root():
    """The build location Matuto scans for installation trees.

    It is the data directory's build folder, where a Matuto build installs a tree
    and where the checkout install's builds already sit, so those stay
    discoverable where they are (CUT-4). Staging is not here.
    """
    data = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(data) / "ai-agents-sandbox" / "builds"


def inspect(connection, location):
    """Run the installation check on a tree a Matuto build installed, and record the result.

    Inspect registers and selects nothing. A tree Matuto did not install is
    refused, because running its executables needs the explicit trust consent of
    the external-registration path. The probes run from an empty directory of
    this call's own, never from the caller's, so a caller standing in the tree
    cannot certify an installation that only works from its own directory.
    """
    path = _tree(location)
    recorded = _installation(connection, path)
    if recorded is None:
        raise store.StoreError(
            f"no Matuto build recorded an installation at {path}, so it is not inspected here")
    if recorded["source_revision"] is None:
        raise store.StoreError(
            f"{path} has unknown source provenance; inspect it through the external trust flow")
    with tempfile.TemporaryDirectory(prefix="matuto-installation-check-") as working:
        result = installation.check(path, recorded["recipe"], source_commit=recorded["source_revision"],
                                    # The installation record exists because a build job
                                    # confirmed these pins in its clean checkout before
                                    # the rename, which the renamed tree cannot show.
                                    verified_checkout=True, working_directory=working)
    connection.execute(
        "UPDATE installations SET check_state = ?, check_reason = ?, check_digests = ?,"
        " recorded_at = ? WHERE location = ?",
        (result.state, str(result.reason or ""), json.dumps(result.digests, sort_keys=True),
         _now(), path))
    return result


def register(connection, build_id, location, label=None):
    """Record one stable ID and an editable label for one installation tree.

    Registering the same ID for the same tree only changes its label, even when
    that tree has since lost its files. An ID that already names another tree is
    refused, naming that tree, so an ID never moves. Register runs, selects and
    certifies nothing: a first registration copies the provenance and the passed
    check the store already records for the tree, and a tree Matuto did not
    install and check is refused rather than registered as one.
    """
    identifier = _identifier(build_id)
    path = _tree(location)
    text = _label(label, identifier)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = _build(connection, identifier)
        if existing is not None and existing["location"] != path:
            raise store.StoreError(
                f"{identifier} already names {existing['location']}; "
                "choose another ID or forget it first")
        if existing is not None:
            # Only the label changes: the tree, its ID and its provenance stay.
            connection.execute("UPDATE builds SET label = ? WHERE build_id = ?",
                               (text, identifier))
        else:
            recorded = _installation(connection, path)
            if recorded is None:
                raise store.StoreError(
                    f"no Matuto build recorded an installation at {path}, "
                    "so it cannot be registered as one")
            if recorded["source_revision"] is None:
                raise store.StoreError(
                    f"{path} has unknown source provenance; register it through the external trust flow")
            if recorded["check_state"] != CHECKED:
                raise store.StoreError(
                    f"the installation check for {path} did not pass: "
                    f"{recorded['check_reason'] or 'it was never checked'}")
            missing = _missing(path, recorded["recipe"])
            if missing is not None:
                raise store.StoreError(f"the installation tree cannot be registered: {missing}")
            holder = _build_at(connection, path)
            if holder is not None:
                raise store.StoreError(
                    f"{path} is already registered as {holder['build_id']}; "
                    "change that label or forget it first")
            kind, backend = _llama(recorded["recipe"])
            connection.execute(
                "INSERT INTO builds (build_id, label, kind, location, backend, source_revision,"
                " target, recipe, toolchain, state, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', '')",
                (identifier, text, kind, path, backend, recorded["source_revision"],
                 recorded["target"], recorded["recipe"], recorded["toolchain"]))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return _entry_for(connection, identifier)


def register_external(connection, build_id, location, recipe, label=None):
    """Register unknown code as unavailable metadata; inspect nothing and run nothing."""
    identifier = _identifier(build_id)
    path = _tree(location)
    kind, backend = _llama(recipe)
    text = _label(label, identifier)
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = _build(connection, identifier)
        if existing is not None:
            if existing["location"] != path:
                raise store.StoreError(
                    f"{identifier} already names {existing['location']}; choose another ID.")
            if existing["state"] != "external":
                raise store.StoreError(f"{identifier} is already registered as a Matuto build.")
            connection.execute("UPDATE builds SET label = ? WHERE build_id = ?", (text, identifier))
        else:
            recorded = _installation(connection, path)
            if recorded is not None and (recorded["recipe"] != recipe
                                         or recorded["source_revision"] is not None
                                         or recorded["target"] is not None
                                         or recorded["toolchain"] is not None):
                raise store.StoreError(f"{path} is recorded as a Matuto installation, not an external build.")
            holder = _build_at(connection, path)
            if holder is not None:
                raise store.StoreError(f"{path} is already registered as {holder['build_id']}.")
            connection.execute(
                "INSERT INTO builds (build_id, label, kind, location, backend, source_revision,"
                " target, recipe, toolchain, state, notes)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, NULL, 'external', '')",
                (identifier, text, kind, path, backend, recipe))
            if recorded is None:
                connection.execute(
                    "INSERT INTO installations (location, recipe, source_revision, target, toolchain,"
                    " check_state, check_reason, check_digests, recorded_at)"
                    " VALUES (?, ?, NULL, NULL, NULL, ?, ?, '{}', ?)",
                    (path, recipe, UNCHECKED,
                     "external build has not passed consented inspection", _now()))
            else:
                _save_check(connection, path, UNCHECKED,
                            "external build has not passed consented inspection", {})
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return _entry_for(connection, identifier)


def inspect_external(connection, build_id, *, trust_digest=None):
    """Show static findings first; run probes only with consent for that snapshot."""
    identifier = _identifier(build_id)
    row = _build(connection, identifier)
    if row is None or row["state"] != "external":
        raise store.StoreError(f"{identifier} is not an external build registration.")
    try:
        findings, digests = installation.static_check(row["location"], row["recipe"])
    except (OSError, ValueError, installation.InspectionError) as error:
        findings, digests = (), {}
        reason = str(error)
    else:
        reason = "; ".join(
            f"{finding.file}: {finding.reason}" if finding.file else finding.reason
            for finding in findings)
    if findings or reason:
        _save_check(connection, row["location"], UNCHECKED, reason, digests)
        return {"state": UNCHECKED, "reason": reason, "findings": findings,
                "needs_consent": False, "observations": {}}
    consent_digest = _consent_digest(identifier, row["location"], row["recipe"], digests)
    if trust_digest != consent_digest:
        reason = ("explicit trust consent is required" if trust_digest is None else
                  "consent does not match this external build and tree; review them again")
        return {"state": UNCHECKED, "reason": reason, "findings": (),
                "needs_consent": True, "consent_digest": consent_digest, "observations": {}}
    with tempfile.TemporaryDirectory(prefix="matuto-external-check-") as working:
        result = installation.check(row["location"], row["recipe"],
                                    working_directory=working, expected_digests=digests)
    _save_check(connection, row["location"], result.state, result.reason, result.digests)
    return {"state": result.state, "reason": result.reason,
            "findings": result.loader_findings, "needs_consent": False,
            "observations": result.observations}


def _consent_digest(build_id, location, recipe, digests):
    payload = {"build_id": build_id, "location": location, "recipe": recipe, "files": digests}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _save_check(connection, location, state, reason, digests):
    connection.execute(
        "UPDATE installations SET check_state = ?, check_reason = ?, check_digests = ?,"
        " recorded_at = ? WHERE location = ?",
        (state, reason, json.dumps(digests, sort_keys=True), _now(), location))


def forget(connection, build_id):
    """Remove only the registration. The tree, its files and its evidence stay.

    The entry the runtime selection names is refused, even when its files are
    missing: another build must be selected first. Nothing here clears the
    selection itself.
    """
    identifier = _identifier(build_id)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _build(connection, identifier)
        if row is None:
            raise store.StoreError(_unregistered(connection, identifier))
        selected = connection.execute(
            "SELECT build_id FROM runtime_selection WHERE selection_id = 1").fetchone()
        if selected is not None and selected["build_id"] == identifier:
            raise store.StoreError(
                f"the runtime selection names {identifier}, so it cannot be forgotten; "
                "select another build first")
        connection.execute("DELETE FROM builds WHERE build_id = ?", (identifier,))
        connection.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
    return row["location"]


def availability(connection, build_id):
    """(available, reason) for one registered build, read now and writing nothing.

    A registration is available when its tree is still there with its server, the
    registration itself is not recorded unavailable, and the installation check
    recorded for that tree passed. Nothing here substitutes another build.
    """
    row = _build(connection, build_id)
    if row is None:
        return False, f"no build is registered as {build_id}"
    status, reason = _condition(row, _installation(connection, row["location"]))
    return status == "ready", reason or None


def listing(connection, root=None):
    """Every llama.cpp tree Matuto knows, once, with its status and its check.

    Discovery runs nothing from any tree: a status comes from the store's
    recorded installation and the tree's presence, never from an executable. A
    tree under the build location with no recorded passed check is found but
    unchecked, which is how an old checkout build stays discoverable for
    re-registration.
    """
    root = build_root() if root is None else Path(root)
    selected = connection.execute(
        "SELECT build_id FROM runtime_selection WHERE selection_id = 1").fetchone()
    active = selected["build_id"] if selected is not None else None
    recorded = {row["location"]: row for row in _installations(connection)}
    entries = []
    registered = set()
    for row in _builds(connection):
        registered.add(row["location"])
        entries.append(_entry(row, recorded.get(row["location"]), row["build_id"] == active))
    for path in _trees(root):
        if path not in registered:
            entries.append(_entry(None, recorded.get(path), False, location=path))
    return sorted(entries, key=lambda entry: entry["location"])


def _trees(root, depth=2):
    """The installation-shaped trees under the build location, links not followed."""
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path)
        if any((path / name).is_file() for name in SERVER_NAMES):
            yield str(path)
        elif depth > 1:
            yield from _trees(path, depth - 1)


def _entry_for(connection, build_id):
    row = _build(connection, build_id)
    if row is None:
        raise store.StoreError(f"no build is registered as {build_id}")
    selected = connection.execute(
        "SELECT build_id FROM runtime_selection WHERE selection_id = 1").fetchone()
    active = selected["build_id"] if selected is not None else None
    return _entry(row, _installation(connection, row["location"]), row["build_id"] == active)


def _entry(row, recorded, selected, location=None):
    """One tree's listing: its identity, its recorded check and its status."""
    path = location if row is None else row["location"]
    identity = dict(row) if row is not None else _provenance(recorded)
    check = _check(recorded)
    if row is None:
        missing = None if recorded is None else _missing(path, recorded["recipe"])
        is_external = recorded is not None and recorded["source_revision"] is None
        if not is_external and check is not None and check["state"] == CHECKED and missing is None:
            status, reason = "discovered", ""
        else:
            status = "found but unchecked"
            reason = missing or _reason(recorded) or (
                "external build registration was removed" if is_external else
                "no Matuto build recorded a passed installation check for it")
    else:
        status, reason = _condition(row, recorded)
        # A selected entry that became unavailable stays selected and is shown
        # unavailable with its reason, so nothing is substituted for it.
        if status != "unavailable" and selected:
            status, reason = "selected", ""
    return {"location": path, "status": status, "reason": reason, "selected": bool(selected),
            "build_id": identity.get("build_id"), "label": identity.get("label"),
            "kind": identity.get("kind"), "backend": identity.get("backend"),
            "recipe": identity.get("recipe"), "source_revision": identity.get("source_revision"),
            "target": identity.get("target"), "toolchain": identity.get("toolchain"),
            "state": identity.get("state"), "check": check}


def _condition(row, recorded):
    """(status, reason) for one registration: ready, registered or unavailable.

    Registered means the registration is well-formed and the tree is there, but
    no installation check is recorded for it, which is how an entry registered as
    metadata stays ineligible without reading as broken. A check that ran and
    failed makes the entry unavailable with its reason.
    """
    if row["state"] == "unavailable":
        return "unavailable", f"the registration {row['build_id']} is recorded unavailable"
    path = row["location"]
    missing = _missing(path, row["recipe"])
    if missing is not None:
        return "unavailable", missing
    if recorded is None:
        return "registered", f"no installation check is recorded for {path}"
    if recorded["check_state"] != CHECKED:
        return "unavailable", (f"the installation check for {path} did not pass: "
                               f"{recorded['check_reason'] or 'it was not checked'}")
    if row["state"] == "external":
        try:
            expected = json.loads(recorded["check_digests"])
            findings, actual = installation.static_check(path, row["recipe"])
        except (OSError, ValueError, installation.InspectionError) as error:
            return "unavailable", f"the external tree cannot be rechecked safely: {error}"
        if findings:
            reason = "; ".join(
                f"{finding.file}: {finding.reason}" if finding.file else finding.reason
                for finding in findings)
            return "unavailable", reason
        if not expected or actual != expected:
            return "unavailable", "the external tree changed since its consented check; inspect it again"
    return "ready", ""


def _missing(path, recipe):
    """Why this tree cannot run, or None when it is there with a runnable server.

    Registration, a registration's availability and discovery all judge a tree
    to be there the same way, so a tree one of them refuses is never offered by
    another as a complete installation.
    """
    if not os.path.isdir(path):
        return f"its files are missing at {path}"
    server = _server(path, recipe)
    if not os.path.isfile(server) or not os.access(server, os.X_OK):
        return f"its server is missing at {server}"
    return None


def _server(path, recipe):
    """Where the recipe's server sits in one tree: in `bin/`, or at its root."""
    return Path(path) / installation.SERVER_PATHS.get(
        recipe, installation.SERVER_PATHS[recipes.LLAMA_HIP])


def _provenance(recorded):
    if recorded is None:
        return {}
    kind, backend = _llama(recorded["recipe"])
    return {"recipe": recorded["recipe"], "kind": kind, "backend": backend,
            "source_revision": recorded["source_revision"], "target": recorded["target"],
            "toolchain": recorded["toolchain"]}


def _check(recorded):
    if recorded is None:
        return None
    try:
        digests = json.loads(recorded["check_digests"])
    except (TypeError, ValueError):
        digests = {}
    return {"state": recorded["check_state"], "reason": recorded["check_reason"],
            "digests": digests, "recorded_at": recorded["recorded_at"]}


def _reason(recorded):
    return recorded["check_reason"] if recorded is not None else ""


def _identifier(build_id):
    if not isinstance(build_id, str) or not BUILD_ID.fullmatch(build_id):
        raise store.StoreError(
            "A build ID is letters, digits, dot, dash or underscore, up to 64 characters.")
    return build_id


def _label(label, identifier):
    if label is None or label == "":
        return identifier
    return _one_line(label, "build label") or identifier


def _one_line(value, what):
    if not isinstance(value, str) or any(character in value for character in "\r\n\0") \
            or len(value) > LABEL_LIMIT:
        raise store.StoreError(f"A {what} is one line of at most {LABEL_LIMIT} characters.")
    return value


def _tree(location):
    try:
        text = os.fspath(location)
    except TypeError:
        raise store.StoreError("An installation tree is one absolute path.") from None
    if not isinstance(text, str) or not text.startswith("/"):
        raise store.StoreError(f"An installation tree is one absolute path: {location!r}")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise store.StoreError(
            f"An installation tree holds no control character: {location!r}")
    if os.pardir in text.split("/"):
        raise store.StoreError(
            f"An installation tree holds no parent (..) component: {location!r}")
    return os.path.normpath(text)


def _llama(recipe):
    chosen = LLAMA_BUILDS.get(recipe)
    if chosen is None:
        raise store.StoreError(
            f"{recipe!r} is not a llama.cpp recipe, so it is not a registry build.")
    return chosen


def _builds(connection):
    return connection.execute("SELECT * FROM builds ORDER BY build_id").fetchall()


def _installations(connection):
    return connection.execute("SELECT * FROM installations ORDER BY location").fetchall()


def _installation(connection, location):
    return connection.execute(
        "SELECT * FROM installations WHERE location = ?", (location,)).fetchone()


def _build(connection, build_id):
    return connection.execute("SELECT * FROM builds WHERE build_id = ?", (build_id,)).fetchone()


def _build_at(connection, location):
    return connection.execute(
        "SELECT * FROM builds WHERE location = ? ORDER BY build_id LIMIT 1", (location,)).fetchone()


def _unregistered(connection, identifier):
    names = sorted(row["build_id"] for row in _builds(connection))
    if not names:
        return f"no builds are registered at all, so no build is registered as {identifier}"
    return f"no build is registered as {identifier}; the registry holds {', '.join(names)}"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
