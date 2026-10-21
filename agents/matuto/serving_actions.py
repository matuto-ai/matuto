"""Carrying out the agentic serving transitions the serving rules offer.

The routes are where the operator asks; this module is what they ask. It reads
the current serving state, asks `matuto.serving` which control that state
offers and why a refused one is refused, and only then starts, stops or restarts
the router unit through `matuto.router`. A control the state does not offer, or
one carrying a refusal, changes nothing and returns the rules' own reason; no
refusal is queued and nothing is carried out later (SERVE-2).

Stop serving and Restart act only on a request carrying the operator's
confirmation. Without it the answer is the Stops, Starts and Stays preview the
same rules describe for the confirmation dialog, and no unit is touched (SERVE-3).
Start agentic serving confirms nothing and acts directly.
"""

from matuto import admission, router, serving, serving_state, store
from matuto.runner import _current_work

START_AGENTIC = serving.START_AGENTIC
STOP_SERVING = serving.STOP_SERVING
RESTART = serving.RESTART

# What each serving state is called when a control the state does not offer is
# refused, and what the state does offer in its place.
STATE_NAMES = {admission.STOPPED: "not serving", admission.AGENTIC: "agentic serving",
               admission.VOICE: "voice serving",
               admission.SERVING_UNKNOWN: "in an unknown state"}
# The wait a start gives the router to answer healthy before it is reported as not
# healthy. It bounds the route, not the router: the unit keeps running either way.
READY_SECONDS = 15.0


class Refused(Exception):
    """This activation is refused; the reason is the rules' own."""


def read_state(config_root=None, *, port=None, key_path=None):
    """The reported serving state, at the router's own address."""
    return serving_state.read(config_root=config_root,
                              router_port=port or serving_state.ROUTER_PORT, key_path=key_path)


def current_work(config_root=None):
    """The work the runner has recorded, or None. Read alone, so a preview never settles it."""
    opened = store.read_store(config_root)
    if opened is None:
        return None
    try:
        return _current_work(opened.connection())
    finally:
        opened.close()


def activate(control, *, confirmed, config_root=None, timeout=None, sleep=None,
             port=None, key_path=None):
    """Carry out one agentic serving control, or answer the preview asking for it.

    `confirmed` is the operator's confirmation of the consequences for the
    controls the serving rules mark as confirmed; a control that confirms nothing ignores it.
    The answer reports what was done and the state read afterwards. The
    router's API key is in none of it.
    """
    read = (lambda: read_state(config_root, port=port, key_path=key_path))
    state = read()
    work = current_work(config_root)
    offered = _offered(control, state, work)
    if offered is None:
        raise Refused(_not_offered(control, state, work))
    if not offered.offered:
        raise Refused(" ".join(offered.reasons))
    if offered.confirmation and not confirmed:
        consequence = serving.consequence(offered.name, _view(state), work)
        return {"control": offered.name, "acted": False, "preview": _consequence(consequence)}
    if offered.name == START_AGENTIC:
        return _start(config_root, timeout=timeout, sleep=sleep, port=port, key_path=key_path)
    stopped, detail = router.stop_unit()
    if not stopped:
        return _report(offered.name, state, acted=True, stopped=False, reason=detail)
    if offered.name == RESTART:
        return _start(config_root, timeout=timeout, sleep=sleep, port=port, key_path=key_path)
    return _report(offered.name, read(), acted=True, stopped=True)


def _start(config_root, *, timeout, sleep, port, key_path):
    """Start the router through its launcher and report the state it reaches."""
    try:
        router.start_unit(config_root)
    except router.LauncherRefused as refusal:
        raise Refused(str(refusal)) from None
    wait = dict(timeout=READY_SECONDS if timeout is None else timeout, port=port, key_path=key_path)
    if sleep is not None:
        wait["sleep"] = sleep
    return _report(START_AGENTIC, router.wait_ready(config_root, **wait), acted=True)


def _report(control, state, *, acted, stopped=None, reason=""):
    """What one activation did and the serving state read after it."""
    answer = {"control": control, "acted": acted, "state": state.state,
              "healthy": state.healthy, "reason": reason or state.reason,
              "model": state.model, "address": state.address}
    if stopped is not None:
        answer["stopped"] = stopped
    return answer


def _offered(control, state, work):
    """The rules' own entry for this control in this state, or None.

    The inputs Serve voice needs belong to the voice transition and are not read here; the three
    controls this module carries decide on the serving state and the current work
    alone, which is what `matuto.serving` reads for them. Stop from voice belongs
    to the voice transition, not this agentic route.
    """
    if control == STOP_SERVING and state.state == admission.VOICE:
        return None
    for entry in serving.controls(_view(state), work):
        if entry.name == control:
            return entry
    return None


def _view(state):
    """What the rules take: the reader's state in the rules' own shape."""
    return serving.Serving(state.state, state.healthy,
                           tuple(part.profile for part in state.parts), state.bundle)


def _not_offered(control, state, work):
    """Why this control is not offered here: the state is the blocker, named as one."""
    if control == STOP_SERVING and state.state == admission.VOICE:
        return ("Stop serving through the agentic route is not offered while Matuto is voice"
                " serving; use the voice serving controls.")
    offered = serving.controls(_view(state), work)
    names = ", ".join(entry.label for entry in offered if entry.offered) or "no serving control"
    return (f"{serving.LABELS[control]} is not offered while Matuto is"
            f" {STATE_NAMES.get(state.state, state.state)}: {names} are.")


def _consequence(consequence):
    """One consequence in the shape a confirmation preview is read as."""
    return {"stops": list(consequence.stops), "starts": list(consequence.starts),
            "stays": list(consequence.stays), "safe_choice": consequence.safe_choice}