"""Stop voice, then start the agentic router when no voice part remains."""

import json

from matuto import route_gate, router, serving, voice
from matuto.web import RefusedError

PATH = "/api/serving/voice/switch"
CONTROL = serving.SWITCH_TO_AGENTIC


def handle(request):
    if request.method != "POST":
        raise RefusedError("Switch to agentic serving is a POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        raise RefusedError("Expected a JSON body carrying the confirmation.") from None
    try:
        return 200, voice.activate(CONTROL, confirmed=payload.get("confirm") is True)
    except voice.VoiceRefused as refusal:
        raise RefusedError(str(refusal)) from None
    except router.LauncherRefused as refusal:
        raise RefusedError(str(refusal)) from None
