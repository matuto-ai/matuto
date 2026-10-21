"""Export selected records, or every record, as one portable JSON document."""

import json
import sys
from pathlib import Path

from matuto import store


def run(argv):
    if not argv:
        print("usage: matuto export <document.json> [kind:id ...]", file=sys.stderr)
        return 2
    path, *selectors = argv
    records = []
    try:
        for selector in selectors:
            kind, separator, identifier = selector.partition(":")
            if not separator or kind not in store.RECORD_KINDS or not identifier:
                raise ValueError(f"invalid record selector {selector!r}; expected kind:id")
            records.append((kind, identifier))
        opened = store.open_store()
        try:
            document = store.export_records(opened, records if selectors else None)
        finally:
            opened.close()
        Path(path).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    except (OSError, UnicodeError, ValueError, store.StoreError) as error:
        print(f"matuto export: {error}", file=sys.stderr)
        return 1
    print(f"Exported {len(document['records'])} record(s) to {path}.")
    return 0
