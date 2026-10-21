"""Read the serving state for the Model console page."""

from matuto import serving_state
from matuto.status import console as console_status

PATH = "/api/console"


def handle(request):
    if request.method not in ("GET", "HEAD"):
        from matuto.web import RefusedError
        raise RefusedError("Model console status is read-only.")
    state = serving_state.read()
    return 200, {"state": state.state, "healthy": state.healthy,
                 "reason": state.reason, "console": console_status.item(state)}
