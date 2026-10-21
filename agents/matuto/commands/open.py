"""Open the fixed, bookmarkable Matuto manager address."""

import sys

from matuto.updater import open_manager


def run(argv):
    if argv:
        print("usage: matuto open", file=sys.stderr)
        return 2
    return open_manager()
