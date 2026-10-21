"""List runs, builds and downloads from the runner's shared store list."""

import sys

from matuto.work_cli import list_work, open_runner


def run(argv):
    if argv:
        print("usage: matuto list", file=sys.stderr)
        return 2
    running = open_runner()
    if running is None:
        return 1
    try:
        return list_work(running)
    finally:
        running.close()
