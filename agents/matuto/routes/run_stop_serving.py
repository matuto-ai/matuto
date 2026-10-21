"""Carry out a confirmed card run after stopping the serving workload."""

import json

from matuto import admission, route_gate, serving, serving_actions, serving_state, voice
from matuto.routes import runs
from matuto.web import RefusedError

PATH = "/api/runs/stop-serving"


def handle(request):
    if request.method != "POST":
        raise RefusedError("Stop serving and run is a POST.")
    refused = route_gate.require_session(request)
    if refused is not None:
        return refused
    try:
        body = json.loads(request.body or b"{}")
        if not isinstance(body, dict):
            raise ValueError
    except (UnicodeDecodeError, ValueError):
        raise RefusedError("Expected a JSON body carrying the run and its confirmation.") from None
    if body.get("confirm") is not True:
        raise RefusedError("Confirm Stop serving and run before starting the run.")

    start_body = dict(body)
    start_body.pop("confirm", None)
    start_body["action"] = "start"
    start_request = request._replace(path=runs.PATH, body=json.dumps(start_body).encode())
    status, result = runs.handle(start_request)
    if not result.get("offer"):
        return status, result

    try:
        state = serving_state.read()
        if state.state == admission.AGENTIC:
            stopped = serving_actions.activate(serving_actions.STOP_SERVING, confirmed=True)
        elif state.state == admission.VOICE:
            stopped = voice.activate(serving.STOP_SERVING, confirmed=True)
        else:
            # Serving changed after the preview; retry admission without stopping anything.
            return runs.handle(start_request)
        if not stopped.get("stopped"):
            return 409, {"error": stopped.get("reason") or "Serving did not stop; the run was not started."}
        if serving_state.read().state != admission.STOPPED:
            return 409, {"error": "Serving is still running; the run was not started."}
    except (OSError, serving_actions.Refused, voice.VoiceRefused) as error:
        return 409, {"error": str(error)}

    # Admission is checked again after the stop; a concurrent start can take the
    # slot in that interval, in which case serving remains stopped as promised.
    return runs.handle(start_request)
