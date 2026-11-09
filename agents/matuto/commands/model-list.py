"""Write the published language models as one consumer-neutral JSON file."""

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

from matuto import store


def run(argv):
    if len(argv) != 1:
        print("usage: matuto-manager model-list <file.json>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    try:
        opened = store.open_store()
        try:
            document = store.model_list(opened)
        finally:
            opened.close()
        _replace(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    except (OSError, UnicodeError, ValueError, store.StoreError) as error:
        print(f"matuto model-list: {error}; any previous file at {path} is unchanged", file=sys.stderr)
        return 1
    print(f"Exported {len(document['models'])} published model(s) to {path}.")
    unknown = [model["id"] for model in document["models"] if model["thinking"]["mode"] == "unknown"]
    if unknown:
        print("Thinking is exported as unknown until you choose it in the profile form: "
              + ", ".join(unknown))
    return 0


def _replace(path, text):
    """Write beside the target and rename over it, so a failure never leaves a partial file."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
