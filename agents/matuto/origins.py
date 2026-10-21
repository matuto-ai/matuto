"""The operator's allowed public Hugging Face download origins."""

import datetime
import re
import sqlite3

from matuto import store

# Hugging Face usernames and repository names use these characters. Segments
# cannot be empty, dot paths or whitespace, so they remain one source identity.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def add(connection, kind, name, notes=""):
    """Add one organisation or repository origin, refusing malformed identity."""
    if kind not in ("organisation", "repository"):
        raise store.StoreError("a download origin must be an organisation or repository")
    if not isinstance(name, str):
        raise store.StoreError("a download origin name must be text")
    parts = name.split("/")
    expected = 2 if kind == "repository" else 1
    if (len(parts) != expected or any(not _SEGMENT.fullmatch(part) for part in parts)
            or any(part in (".", "..") for part in parts)):
        shape = "owner/repository" if kind == "repository" else "one organisation name"
        raise store.StoreError(f"{name!r} is not {shape}")
    if not isinstance(notes, str):
        raise store.StoreError("download-origin notes must be text")
    connection.execute("BEGIN IMMEDIATE")
    try:
        rows = connection.execute(
            "SELECT origin_id, kind, name, notes, created_at, enabled FROM download_origins").fetchall()
        match = next((row for row in rows if row["kind"] == kind
                      and row["name"].casefold() == name.casefold()), None)
        if match is not None:
            if match["enabled"]:
                raise store.StoreError(f"download origin {name!r} is already configured")
            connection.execute(
                "UPDATE download_origins SET enabled = 1, notes = ? WHERE origin_id = ?",
                (notes, match["origin_id"]))
            connection.execute("COMMIT")
            return {"origin_id": match["origin_id"], "kind": kind, "name": match["name"],
                    "notes": notes, "created_at": match["created_at"]}
        existing = {row["origin_id"] for row in rows}
        index = 1
        while f"origin-{index}" in existing:
            index += 1
        origin_id = f"origin-{index}"
        created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        connection.execute(
            "INSERT INTO download_origins (origin_id, kind, name, notes, created_at)"
            " VALUES (?, ?, ?, ?, ?)", (origin_id, kind, name, notes, created))
        connection.execute("COMMIT")
    except sqlite3.IntegrityError as error:
        connection.execute("ROLLBACK")
        if "UNIQUE constraint failed" in str(error):
            raise store.StoreError(f"download origin {name!r} is already configured") from None
        raise
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return {"origin_id": origin_id, "kind": kind, "name": name,
            "notes": notes, "created_at": created}


def list_all(connection):
    """List configured origins in stable display order."""
    return [dict(row) for row in connection.execute(
        "SELECT origin_id, kind, name, notes, created_at FROM download_origins"
        " WHERE enabled = 1 ORDER BY kind, name, origin_id")]


def remove(connection, origin_id):
    """Disable future admission while retaining identity for pinned downloads."""
    cursor = connection.execute(
        "UPDATE download_origins SET enabled = 0 WHERE origin_id = ? AND enabled = 1",
        (origin_id,))
    if cursor.rowcount != 1:
        raise store.StoreError(f"no download origin is stored under {origin_id!r}")
