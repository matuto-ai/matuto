"""Matuto CLI entry: every file in the commands location is offered by its own name."""

import sys
from pathlib import Path

# Repair must not mistake this CLI's bytecode cache for an unowned release file.
sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from matuto.discovery import item_names, load_item

COMMANDS = "commands"


def main(argv):
    commands = item_names(COMMANDS)
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print("usage: matuto-manager <command> [arguments]")
        for name in commands or ["(no commands yet)"]:
            print(f"  {name}")
        return 0
    if argv[1] not in commands:
        print(f"unknown command: {argv[1]}", file=sys.stderr)
        return 2
    return load_item(COMMANDS, argv[1]).run(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))