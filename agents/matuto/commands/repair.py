"""Run Repair through the independently installed updater."""

import subprocess
import sys

from matuto.updater import UpdateError, data_directory, independent_updater_script


def run(argv):
    if argv:
        print("usage: matuto repair", file=sys.stderr)
        return 2
    try:
        script = independent_updater_script(data_directory())
        result = subprocess.run([sys.executable, str(script), "repair"], check=False)
    except (OSError, UpdateError) as error:
        print(f"matuto-repair: {error}", file=sys.stderr)
        return 1
    return result.returncode
