"""Follow one recorded run by ID."""

import sys

from matuto import admission
from matuto.work_cli import follow as follow_work, open_runner


def run(argv):
    if len(argv) != 1:
        print("usage: matuto follow <run-id>", file=sys.stderr)
        return 2
    running = open_runner()
    if running is None:
        return 1
    try:
        work = running.work(argv[0])
        if work is None or work.kind not in admission.RUN_KINDS:
            print(f"matuto: no run has ID {argv[0]!r}", file=sys.stderr)
            return 1
        return follow_work(running, argv[0])
    finally:
        running.close()
