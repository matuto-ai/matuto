"""Read and save the authenticated lab-port setting."""

import contextlib
import json
import sqlite3

from matuto import lab_port, store
from matuto.web import RefusedError

PATH = "/api/lab-port"


def handle(request):
    if request.method not in ("GET", "HEAD", "POST"):
        raise RefusedError("Lab port accepts GET and POST requests only.")
    try:
        with contextlib.closing(store.open_store()) as opened:
            if request.method in ("GET", "HEAD"):
                return 200, {"port": lab_port.saved_port(opened.connection()), "csrf": request.csrf}
            try:
                body = json.loads(request.body or b"{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RefusedError("The lab-port setting is not valid JSON.") from None
            if not isinstance(body, dict) or set(body) != {"port"}:
                raise RefusedError("Only the lab port number can be saved.")
            port = body["port"]
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise RefusedError("The lab port must be a whole number from 1 to 65535.")
            return 200, {"port": lab_port.save_port(opened, port)}
    except RefusedError:
        raise
    except (OSError, sqlite3.Error, store.StoreError, ValueError) as error:
        raise RefusedError(str(error)) from None
