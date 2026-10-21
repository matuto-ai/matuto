"""Refuse a change to a record that serving is using right now.

PROF-7 (live). A record is live when the router is serving it — a published
language profile — or when voice serving runs it: the bundle voice serving
started, and each of that bundle's language, synthesis and recognition members. A
card such a record points at is live with it, because the served configuration
carries the card's physical GPU and its default GPU layers.

Editing, importing or removing a live record is refused with a reason naming the
record and Overview's Stop serving. The rule makes no lifecycle call and loads
nothing, so a refusal leaves serving, the router files and the store exactly as
they were, and the operator stops serving from Overview in their own time.

A serving state the reader cannot answer is never read as plain stopped: while it
is unknown, every record that could be live is refused, because nothing here can
then tell the one the router is serving from the one it is not.

The rule is asked at the moment a change commits, so serving that started after a
review was opened still refuses the save. Creating a record reaches no live one,
which is why duplicating a published profile leaves it untouched and is not
refused.
"""

from matuto import admission, serving_state, store

# Overview holds the one control that stops agentic and voice serving alike, and
# no other page calls it anything else (UI-7).
STOP_SERVING = "Stop serving on Overview"
ROUTER_SERVES = f"is live: the router is serving it. {STOP_SERVING} before changing it."
VOICE_RUNS = f"is live: voice serving runs it. {STOP_SERVING} before changing it."
UNKNOWN = (f"may be live: the serving state is unknown, so which records are live cannot be"
           f" told. {STOP_SERVING} before changing it.")
# The table each kind of record is stored in, so a refusal names it from the store
# rather than from a second answer this rule keeps beside it.
_TABLES = {"card": "cards", "bundle": "bundles"}


def refuse_live_change(opened, kind, identifier):
    """Refuse a change to one stored record when serving is using it right now.

    Returns None when the change may go ahead and raises store.RecordRefusal with
    a reason naming the record and the Stop serving control when it may not. The
    serving state is read only for a record some readable state could call live,
    so an ordinary edit costs no systemd call.
    """
    connection = opened.connection()
    label = _could_be_live(connection, kind, identifier)
    if label is None:
        return
    reason = _reason(connection, read_state(opened), kind, identifier)
    if reason is not None:
        raise store.RecordRefusal(f"{label} {reason}")


def read_state(opened):
    """The serving state as it is now, over the voice record this store holds.

    The voice record is read on the connection deciding this change, so no second
    connection is opened: `read_store` refuses a store with a journal beside it,
    which a write in flight has. The store's own config root is passed with it, so
    the record comes from the store this change belongs to rather than the default
    one, whatever root the caller opened it under.
    """
    return serving_state.read(config_root=opened.path.parents[2],
                              store_connection=opened.connection())


def _could_be_live(connection, kind, identifier):
    """The record's own name when serving could be using it, or None when it could not.

    Every readable state draws its live records from published language profiles,
    bundles and their members, so a record outside that closure is live in none of
    them and this rule never asks what is serving for it.
    """
    profiles = {row["profile_id"]: row for row in connection.execute(
        "SELECT profile_id, kind, name, card_id, published FROM model_profiles")}
    members = {row["profile_id"] for row in connection.execute(
        "SELECT profile_id FROM bundle_members")}
    reachable = {profile for profile, row in profiles.items()
                 if profile in members or (row["kind"] == "language" and row["published"])}
    if kind == "card":
        holders = sorted(row["name"] for profile, row in profiles.items()
                         if profile in reachable and row["card_id"] == identifier)
        return _label(connection, kind, identifier) if holders else None
    if kind == "bundle":
        return _label(connection, kind, identifier)
    return _label(connection, kind, identifier) if identifier in reachable else None


def _label(connection, kind, identifier):
    """The record as a refusal names it: its kind, its own name and its stable ID."""
    row = connection.execute(
        f"SELECT name FROM {_TABLES.get(kind, 'model_profiles')} WHERE {store.ID_FIELDS[kind]} = ?",
        (identifier,)).fetchone()
    return f"{kind} record {row['name']!r} ({identifier})" if row else None


def _reason(connection, state, kind, identifier):
    """Why serving holds this record now, or None when it holds none of it."""
    if state.state == admission.STOPPED:
        return None
    if state.state == admission.AGENTIC:
        return _router_reason(connection, kind, identifier)
    if state.state == admission.VOICE and state.bundle:
        return _voice_reason(connection, state.bundle, kind, identifier)
    return UNKNOWN


def _router_reason(connection, kind, identifier):
    """Why the router is holding it, while the router serves."""
    if kind == "bundle":
        # The router serves language profiles, never a bundle, and bundle and
        # profile IDs are separate namespaces: a bundle ID equal to a published
        # profile's ID is not that profile.
        return None
    if kind == "card":
        holder = connection.execute(
            "SELECT name FROM model_profiles WHERE kind = 'language' AND published = 1"
            " AND card_id = ? ORDER BY name", (identifier,)).fetchone()
        if holder is None:
            return None
        return _held_by(f"published profile {holder['name']!r}")
    row = connection.execute(
        "SELECT kind, published FROM model_profiles WHERE profile_id = ?",
        (identifier,)).fetchone()
    if row is not None and row["kind"] == "language" and row["published"]:
        return ROUTER_SERVES
    return None


def _voice_reason(connection, bundle, kind, identifier):
    """Why voice serving is holding it, while voice serving runs `bundle`."""
    if kind == "bundle":
        return VOICE_RUNS if identifier == bundle else None
    if kind == "card":
        holder = connection.execute(
            "SELECT members.profile_id FROM bundle_members AS members"
            " JOIN model_profiles AS profiles ON profiles.profile_id = members.profile_id"
            " WHERE members.bundle_id = ? AND profiles.card_id = ?",
            (bundle, identifier)).fetchone()
        return _held_by(f"voice profile {holder['profile_id']!r}") if holder else None
    member = connection.execute(
        "SELECT 1 FROM bundle_members WHERE bundle_id = ? AND profile_id = ?",
        (bundle, identifier)).fetchone()
    return VOICE_RUNS if member else None


def _held_by(what):
    return f"is live: {what} points to it. {STOP_SERVING} before changing it."