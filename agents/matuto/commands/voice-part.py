"""The launcher run by every generated voice speech unit."""

import sys

from matuto import voice


def run(argv):
    if len(argv) < 8 or argv[6] != "--":
        print("usage: matuto voice-part ROLE BACKEND EXECUTABLE MODEL HOST PORT -- ARGS...",
              file=sys.stderr)
        return 2
    role, backend, executable, model, host, port = argv[:6]
    try:
        voice.launch_part(role, backend, executable, model, host, int(port), argv[7:])
    except (ValueError, voice.VoiceRefused) as error:
        print(f"matuto voice-part: {error}", file=sys.stderr)
        return 1
    return 0
