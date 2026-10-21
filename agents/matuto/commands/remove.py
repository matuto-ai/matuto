"""Preview and remove Matuto while keeping the operator's data."""

import sys

from matuto import removal


def run(argv):
    if argv:
        print("usage: matuto remove", file=sys.stderr)
        return 2
    try:
        plan = removal.preview()
        print("Remove Matuto:")
        for item in plan["remove"]:
            print(f"  Remove: {item['item']} — {item['path']} ({item['size_bytes']} bytes)")
        print("Keep:")
        for item in plan["keep"]:
            print(f"  Keep: {item['item']} — {item['path']} ({item['size_bytes']} bytes)")
        print(f"  {plan['consumer_configuration']}")
        print(f"  {plan['later_bootstrap']}")
        if plan["blockers"]:
            for reason in plan["blockers"]:
                print(f"Cannot remove: {reason}")
            return 1
        if input("Type REMOVE to continue: ").strip() != "REMOVE":
            print("Removal cancelled; nothing was removed.")
            return 1
        def report_kept(items):
            for item in items:
                print(f"Kept: {item['item']} — {item['path']} ({item['size_bytes']} bytes)")
            print("Next: disable login start and remove the manager and updater.")

        removal.remove(confirm=True, on_kept=report_kept)
    except (EOFError, OSError, removal.RemovalError) as error:
        print(f"matuto remove: {error}", file=sys.stderr)
        return 1
    print("Matuto and its updater were removed.")
    return 0
