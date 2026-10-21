"""Serve the manager and model console at their fixed loopback addresses."""

import sys
import threading

from matuto.console import CONSOLE_ADDRESS, build_console_server
from matuto.web import MANAGER_ADDRESS, build_server


def run(argv):
    """Serve the discovered routes behind the browser protections; never move the address."""
    if argv:
        print("usage: matuto serve", file=sys.stderr)
        return 2
    try:
        console = build_console_server()
    except OSError:
        print(f"matuto: {CONSOLE_ADDRESS} is already in use", file=sys.stderr)
        return 1
    try:
        server = build_server()
    except OSError:
        console.server_close()
        print(f"matuto: {MANAGER_ADDRESS} is already in use", file=sys.stderr)
        return 1
    thread = threading.Thread(target=console.serve_forever, daemon=True)
    thread.start()
    print(f"matuto: serving {MANAGER_ADDRESS} and {CONSOLE_ADDRESS}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.shutdown()
        console.server_close()
        server.server_close()
        thread.join(timeout=5)
    return 0
