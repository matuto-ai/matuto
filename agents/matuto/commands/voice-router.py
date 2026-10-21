"""The launcher run by the generated voice router unit."""

import sys

from matuto import voice


def run(argv):
    if len(argv) != 1:
        print("usage: matuto voice-router BUNDLE_ID", file=sys.stderr)
        return 2
    try:
        voice.launch_router(argv[0])
    except (OSError, voice.VoiceRefused) as error:
        print(f"matuto voice-router: {error}", file=sys.stderr)
        return 1
    return 0
