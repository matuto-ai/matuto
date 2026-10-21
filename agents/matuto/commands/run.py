"""Start a named stored-record procedure through the shared runner."""

import sys

from matuto.work_cli import run_procedure


def run(argv):
    if len(argv) != 2:
        print("usage: matuto run <procedure> <record>", file=sys.stderr)
        return 2
    return run_procedure(*argv)
