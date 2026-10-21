"""Delete the login, so the next visit shows Create login. AUTH-5.

The file name is the command name the operator types: `matuto reset-login`.
"""

import sys

from matuto.login import LoginError, reset_login


def run(argv):
    if argv:
        print("reset-login takes no arguments", file=sys.stderr)
        return 2
    try:
        removed = reset_login()
    except LoginError as error:
        print(f"matuto reset-login: {error}", file=sys.stderr)
        return 1
    print("Login removed." if removed else "No login was set.")
    return 0