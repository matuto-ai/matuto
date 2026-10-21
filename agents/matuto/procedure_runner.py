"""Load one named procedure and pass it one stored record."""

import json
import os
import signal
import sqlite3
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matuto.discovery import item_names, load_item
from matuto import store


def run(argv):
    if len(argv) not in (4, 5):
        print("usage: matuto procedure <name> <record-kind> <record-id> <fingerprint> [pinned-spec]",
              file=sys.stderr)
        return 2
    name, kind, record_id, expected_fingerprint, *pinned = argv
    try:
        pinned_spec = json.loads(pinned[0]) if pinned else None
    except ValueError:
        print("matuto: invalid pinned procedure spec", file=sys.stderr)
        return 2
    if name not in item_names("procedures"):
        print(f"matuto: unknown procedure {name!r}", file=sys.stderr)
        return 2
    opened = snapshot = None
    try:
        opened = store.open_store(os.environ.get("MATUTO_STORE_CONFIG_ROOT"))
        snapshot = store.snapshot(opened)
        record = store.read_record(snapshot, kind, record_id)
        if store.fingerprint(snapshot, record) != expected_fingerprint:
            raise ValueError("the stored record changed after admission; refusing to execute")
        procedure = load_item("procedures", name)
        if name == "wq":
            # Let WQ's child handle unit-wide SIGTERM and return sanitized completed rows.
            signal.signal(signal.SIGTERM, lambda *_args: None)
        return (procedure.run(snapshot, record, pinned_spec) if pinned
                else procedure.run(snapshot, record))
    except (OSError, sqlite3.Error, store.StoreError, ValueError) as error:
        print(f"matuto: {error}", file=sys.stderr)
        return 1
    finally:
        if snapshot is not None:
            snapshot.close()
        if opened is not None:
            opened.close()


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
