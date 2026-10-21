"""Install a verified release file and signed list from disk."""

import argparse
import subprocess
import sys

from matuto import updater


def run(argv):
    parser = argparse.ArgumentParser(prog="matuto install-from-file")
    parser.add_argument("release_file", help="path to matuto-VERSION.release")
    parser.add_argument("release_list", help="path to signed release-list.json (and .sig)")
    args = parser.parse_args(argv)
    try:
        updater.trusted_key()
        script = updater.independent_updater_script()
        print("Installing verified Matuto release from disk...", flush=True)
        result = subprocess.run(
            [sys.executable, str(script), "--install-from-file",
             args.release_file, args.release_list], check=False)
    except (OSError, updater.UpdateError) as error:
        print(f"matuto install-from-file: {error}", file=sys.stderr)
        return 1
    return result.returncode
