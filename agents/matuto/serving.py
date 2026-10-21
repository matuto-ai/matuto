"""Pure serving-transition rules: the controls per state and their consequences.

The rules take what is true now — the serving state with any speech part still
running, the current work, the runtime selection, the storage and the bundles'
facts — and say which serving controls Overview offers, why a refused one is
refused, which of them confirms, and what the confirmed transition stops, starts
and keeps. They start, stop and record nothing. Which control Overview offers is
this module's answer; whether an activation is admitted stays with
`matuto.admission` at the launcher.
"""

from dataclasses import dataclass

from matuto.admission import (AGENTIC, BUILD, NO_CARD_RUNS, RUN_KINDS, SERVE_VOICE, START_AGENTIC,
                              STOPPED, VOICE)

# The controls SERVE-2 names. Start agentic serving and Serve voice reuse
# admission's request names, so a control and the launcher request it becomes are
# the same value.
STOP_SERVING = "stop_serving"
RESTART = "restart"
SWITCH_TO_AGENTIC = "switch_to_agentic"
CONTROLS = (START_AGENTIC, STOP_SERVING, RESTART, SERVE_VOICE, SWITCH_TO_AGENTIC)
# SERVE-3: every control except Start agentic serving confirms first.
CONFIRMED = frozenset((STOP_SERVING, RESTART, SERVE_VOICE, SWITCH_TO_AGENTIC))

LABELS = {
    START_AGENTIC: "Start agentic serving",
    STOP_SERVING: "Stop serving…",
    RESTART: "Restart…",
    SERVE_VOICE: "Serve voice…",
    SWITCH_TO_AGENTIC: "Switch to agentic serving…",
}
# The choice a confirmation focuses first: it performs nothing (SERVE-3).
SAFE_CHOICE = "Cancel"

# Bundle qualification states (PROF-4) and the workloads a confirmation names.
QUALIFIED = "qualified"
CANDIDATE = "candidate"
CHANGED = "changed"
AGENTIC_SERVING = "agentic serving"
NOTHING = "nothing"


@dataclass(frozen=True)
class Serving:
    """The serving state as the reader reports it.

    `healthy` is the router's health while agentic; the offered controls do not
    depend on it (SERVE-2 gives a healthy and an unhealthy agentic state the same
    three controls, and the reader uses the health for the headline instead).
    `parts` names each speech part still running after a stop; a voice state
    carrying one offers Stop serving for a retry and refuses Switch. `bundle`
    names the running bundle.
    """

    state: str
    healthy: bool = True
    parts: tuple = ()
    bundle: str = ""


@dataclass(frozen=True)
class Runtime:
    """The saved runtime selection: whether it can serve, and why it cannot."""

    usable: bool
    detail: str = ""


@dataclass(frozen=True)
class Storage:
    """The model storage: whether it is reachable, and why it is not."""

    reachable: bool
    detail: str = ""


@dataclass(frozen=True)
class Bundle:
    """One bundle as the Serve voice picker sees it."""

    name: str
    qualification: str
    available: bool = True
    detail: str = ""


@dataclass(frozen=True)
class Control:
    """One serving control for a state: offered when it carries no refusal."""

    name: str
    label: str
    confirmation: bool = False
    reasons: tuple = ()

    @property
    def offered(self):
        return not self.reasons


@dataclass(frozen=True)
class BundleChoice:
    """One picker entry: offered when it carries no refusal."""

    name: str
    reasons: tuple = ()

    @property
    def offered(self):
        return not self.reasons


@dataclass(frozen=True)
class Consequence:
    """What a confirmed transition stops, starts and keeps (Stays)."""

    stops: tuple
    starts: tuple
    stays: tuple
    safe_choice: str = SAFE_CHOICE


_STATE_CONTROLS = {
    STOPPED: (START_AGENTIC, SERVE_VOICE),
    AGENTIC: (STOP_SERVING, RESTART, SERVE_VOICE),
    VOICE: (STOP_SERVING, SWITCH_TO_AGENTIC),
}


def controls(serving, current_work=None, runtime=None, storage=None, bundles=()):
    """The controls for the current state, each offered or refused with its blocker.

    Overview shows every control the state names, so a refused one keeps its place
    beside its reason. An unrecognized state offers nothing: a failed state read is
    not permission, the rule `matuto.admission` already applies to the same states.
    A card holder the current work does not name (a foreign llama-server) refuses
    the activation at the launcher, not this control.
    """
    names = _STATE_CONTROLS.get(serving.state)
    if names is None:
        return ()
    found = []
    for name in names:
        if name == SERVE_VOICE:
            reasons = _serve_voice_reasons(current_work, runtime, storage, bundles)
        elif name == START_AGENTIC and _holds_card(current_work):
            reasons = (_card_holding_reason(current_work, "starting agentic serving"),)
        elif name == SWITCH_TO_AGENTIC and serving.parts:
            reasons = (f"A speech part is still running ({', '.join(serving.parts)}). "
                       "Stop every voice part before switching.",)
        else:
            reasons = ()
        found.append(Control(name, LABELS[name], name in CONFIRMED, reasons))
    return tuple(found)


def _serve_voice_reasons(current_work, runtime, storage, bundles):
    """Why Serve voice is refused now (SERVE-4). A download refuses nothing."""
    reasons = []
    if runtime is None or not runtime.usable:
        reasons.append(_with_detail("No usable runtime is selected",
                                    runtime.detail if runtime else ""))
    if storage is None or not storage.reachable:
        reasons.append(_with_detail("The model storage is unreachable",
                                    storage.detail if storage else ""))
    if current_work is not None:
        if current_work.kind == BUILD:
            reasons.append(f"A build for {current_work.purpose} is running. "
                           "Stop it before serving voice.")
        elif _holds_card(current_work):
            reasons.append(_card_holding_reason(current_work, "serving voice"))
    if not any(choice.offered for choice in bundle_choices(bundles)):
        reasons.append("No qualified, available bundle is offered.")
    return tuple(reasons)


def bundle_choices(bundles):
    """Each bundle with the reason Serve voice does not offer it (SERVE-4).

    Only a qualified, available bundle is offered, and a candidate or an
    unavailable bundle carries the reason instead of disappearing from the picker.
    """
    choices = []
    for bundle in bundles:
        reasons = []
        if bundle.qualification == CANDIDATE:
            reasons.append("Not qualified: a candidate.")
        elif bundle.qualification == CHANGED:
            reasons.append("Not qualified: changed since qualification.")
        elif bundle.qualification != QUALIFIED:
            reasons.append(f"Not qualified: unknown qualification state "
                           f"({bundle.qualification!r}).")
        if not bundle.available:
            reasons.append(_with_detail("Unavailable", bundle.detail))
        choices.append(BundleChoice(bundle.name, tuple(reasons)))
    return tuple(choices)


def consequence(control, serving, current_work=None, bundle=None):
    """What one confirmed transition stops, starts and keeps (SERVE-3).

    `bundle` names the bundle a Serve voice activation starts, chosen from the
    picker's offered entries; a bundle the picker does not offer is refused by
    `controls()` and `bundle_choices()`, not here. A control the state does not
    offer, and Start agentic serving, which confirms nothing, are refused rather
    than described.
    """
    state = serving.state
    if control == STOP_SERVING:
        if state == VOICE:
            stops = (f"Every voice part of {serving.bundle}",) if serving.bundle \
                else ("Every voice part",)
        elif state == AGENTIC:
            stops = ("Agentic serving; consumers lose access",)
        else:
            _refuse(control, state)
        return Consequence(stops, ("Nothing",),
                           (_work(current_work) or "Model files, profiles and evidence",))
    if control == RESTART:
        if state != AGENTIC:
            _refuse(control, state)
        return Consequence(("Agentic serving briefly; the loaded model unloads",),
                           ("Agentic serving with the published profiles",),
                           ("Downloads and builds",))
    if control == SERVE_VOICE:
        if state not in (STOPPED, AGENTIC) or not bundle:
            _refuse(control, state)
        return Consequence(("Agentic serving; consumers lose the router",) if state == AGENTIC
                           else ("Nothing",),
                           (f"{bundle}: language, synthesis and recognition",),
                           (_work(current_work) or "Downloads may continue",))
    if control == SWITCH_TO_AGENTIC:
        if state != VOICE:
            _refuse(control, state)
        if serving.parts:
            raise ValueError(f"{control!r} is refused while a speech part is still running.")
        return Consequence(("Every voice part",),
                           ("Agentic serving with the published profiles",),
                           ("Downloads",))
    raise ValueError(f"{control!r} is not a serving control.")


def workload_to_restore(prior):
    """The workload a failed voice start restores: exactly the prior one (SERVE-4)."""
    if prior.state == AGENTIC:
        return AGENTIC_SERVING
    if prior.state == STOPPED:
        return NOTHING
    raise ValueError(f"Serve voice is not offered in the {prior.state!r} state, so a voice "
                     "start there restored nothing.")


def _work(current_work):
    return f"{current_work.kind} for {current_work.purpose}" if current_work else ""


def _holds_card(current_work):
    """Whether the current work is a run that holds the card (RUN-4)."""
    return (current_work is not None and current_work.kind in RUN_KINDS
            and current_work.kind not in NO_CARD_RUNS)


def _card_holding_reason(current_work, before):
    # A run's purpose already reads "<kind> for <label>", so the kind is named once.
    detail = current_work.purpose
    prefix = f"{current_work.kind} for "
    if detail.startswith(prefix):
        detail = detail[len(prefix):]
    return (f"The card is held by the {current_work.kind} run for {detail}. "
            f"Stop it before {before}.")


def _with_detail(stated, detail):
    return f"{stated} ({detail})." if detail else f"{stated}."


def _refuse(control, state):
    raise ValueError(f"{control!r} is not offered in the {state!r} state.")
