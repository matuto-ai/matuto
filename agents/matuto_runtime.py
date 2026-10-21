#!/usr/bin/env python3
"""Compatibility entry for the runtime support owned by the Matuto app."""
import json
import sys

from matuto import matuto_runtime as runtime

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--identity-probe":
        try:
            print(json.dumps(runtime.identity_probe(sys.argv[2])))
        except runtime.InspectionError:
            sys.exit(1)
    else:
        sys.exit(runtime.runtime_cli(sys.argv[1:]) if len(sys.argv) > 1 and sys.argv[1] in (
            "show", "select", "launch", "check", "builds", "model-root", "model-path",
            "installation", "speech", "speech-check",
        ) else runtime.inspection_cli(sys.argv[1] if len(sys.argv) == 2 else None))
else:
    sys.modules[__name__] = runtime
