"""Start agentic serving: the route the Overview's Start acts through.

SERVE-3: this control confirms nothing, so the route acts directly. It is a
mutation like every other route: the HTTP guard's Origin and CSRF checks have already run,
and `matuto.route_gate` refuses it without a live session.
"""

from matuto import route_gate, serving_actions
from matuto.web import RefusedError

PATH = "/api/serving/start"
CONTROL = serving_actions.START_AGENTIC


def handle(request):
    if request.method not in ("POST",):
        raise RefusedError("Start agentic serving is a POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    try:
        return 200, serving_actions.activate(CONTROL, confirmed=True)
    except serving_actions.Refused as refusal:
        raise RefusedError(str(refusal)) from None