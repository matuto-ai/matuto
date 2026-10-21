"""Stop one recorded run by ID through the shared runner."""

import sys

from matuto import admission
from matuto.work_cli import open_runner


def run(argv):
    if len(argv) != 1:
        print("usage: matuto cancel <run-id>", file=sys.stderr)
        return 2
    running = open_runner()
    if running is None:
        return 1
    try:
        work = running.work(argv[0])
        if work is None or work.kind not in admission.RUN_KINDS:
            print(f"matuto: no run has ID {argv[0]!r}", file=sys.stderr)
            return 1
        stopped = running.stop(argv[0])
        if stopped is None:
            print(f"matuto: run {argv[0]!r} no longer exists", file=sys.stderr)
            return 1
        print(f"Run {stopped.work_id}: {stopped.outcome or stopped.state}")
        return 0 if stopped.outcome else 1
    finally:
        running.close()
