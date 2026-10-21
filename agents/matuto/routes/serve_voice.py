"""Serve one offered voice bundle after the operator confirms its consequences."""

import json

from matuto import route_gate, serving, voice
from matuto.web import RefusedError

PATH = "/api/serving/voice"
CONTROL = serving.SERVE_VOICE


def handle(request):
    if request.method not in ("GET", "POST"):
        raise RefusedError("Serve voice accepts GET and POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    if request.method == "GET":
        return 200, {"bundles": voice.bundle_options()}
    try:
        payload = _payload(request)
        return 200, voice.activate(CONTROL, confirmed=payload.get("confirm") is True,
                                   bundle=payload.get("bundle"))
    except voice.VoiceRefused as refusal:
        raise RefusedError(str(refusal)) from None


def _payload(request):
    try:
        value = json.loads(request.body or b"{}")
    except ValueError:
        raise RefusedError("Expected a JSON body with the selected bundle and confirmation.") from None
    if not isinstance(value, dict):
        raise RefusedError("Expected a JSON body with the selected bundle and confirmation.")
    return value
