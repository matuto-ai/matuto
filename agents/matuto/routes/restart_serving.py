"""Restart for agentic serving: the route the Overview's Restart… acts through.

SERVE-3: this control confirms, so a request without the operator's confirmation
changes nothing and answers the Stops, Starts and Stays preview the rules
describe. The confirmation restarts the router through the same launcher every
other start runs, so the model unloads until the next request loads it again.
"""

import json

from matuto import route_gate, serving_actions
from matuto.web import RefusedError

PATH = "/api/serving/restart"
CONTROL = serving_actions.RESTART


def handle(request):
    if request.method not in ("POST",):
        raise RefusedError("Restart is a POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    try:
        confirmed = _confirmed(request)
        return 200, serving_actions.activate(CONTROL, confirmed=confirmed)
    except serving_actions.Refused as refusal:
        raise RefusedError(str(refusal)) from None


def _confirmed(request):
    """Whether this request carries the operator's confirmation of the preview."""
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        raise RefusedError("Expected a JSON body carrying the confirmation.") from None
    if not isinstance(payload, dict):
        raise RefusedError("Expected a JSON body carrying the confirmation.")
    return payload.get("confirm") is True