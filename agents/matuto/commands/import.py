"""Import a portable record document through the store's shared save path."""

import json
import sys
from pathlib import Path

from matuto import store


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON field {key!r}")
        value[key] = item
    return value


def run(argv):
    if len(argv) != 1:
        print("usage: matuto import <document.json>", file=sys.stderr)
        return 2
    try:
        document = json.loads(Path(argv[0]).read_text(encoding="utf-8"),
                              object_pairs_hook=_unique_object)
        opened = store.open_store()
        try:
            results = store.import_records(opened, document)
        finally:
            opened.close()
    except (OSError, UnicodeError, ValueError, store.StoreError, store.RecordRefusal) as error:
        print(f"matuto import: {error}", file=sys.stderr)
        return 1
    for result in results:
        print(f"{result['action']} {result['kind']} {result['id']}")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
    return 0
