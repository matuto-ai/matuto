"""Pure admission rules for the one slot, the card lease and serving.

The rules take what is true now — the current work, the serving state, the card's
holder — and answer admit, refuse with a reason, or offer "Stop serving and run".
They start, stop and record nothing; the runner that holds the slot and the lease
decides from this answer at every launcher, browser and CLI alike. The host
evidence for these rules is the Python API below.
"""

from dataclasses import dataclass

ADMIT = "admit"
REFUSE = "refuse"
STOP_SERVING_AND_RUN = "stop serving and run"

STOPPED = "stopped"
AGENTIC = "agentic"
VOICE = "voice"
SERVING_UNKNOWN = "unknown"

CARD_FREE = "free"
CARD_RUN = "run"
CARD_FOREIGN = "foreign"
CARD_UNKNOWN = "unknown"

# RUN-1: every run kind, the no-card Verify among them. A temporary server (serve)
# is a run, and so is every context search rung's run.
RUN_KINDS = ("verify", "smoke", "bench", "serve", "bundle", "wq", "context_search")
NO_CARD_RUNS = frozenset({"verify"})
BUILD = "build"
DOWNLOAD = "download"
START_AGENTIC = "start_agentic"
SERVE_VOICE = "serve_voice"
SLOT_KINDS = frozenset(RUN_KINDS) | {BUILD, DOWNLOAD}
SERVING_KINDS = (START_AGENTIC, SERVE_VOICE)
# Only these three are readable serving states. Every other value, SERVING_UNKNOWN
# included, is an unread state and fails closed rather than admitting work.
SERVING_STATES = frozenset((STOPPED, AGENTIC, VOICE))


@dataclass(frozen=True)
class Work:
    """The current work: its kind and what it is for. `detail` names a holder."""

    kind: str
    purpose: str


@dataclass(frozen=True)
class Card:
    """Who holds the card. `detail` describes a run or the foreign llama-server."""

    state: str
    detail: str = ""


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str = ""


def decide(kind, purpose, current_work, serving, card):
    """Answer one request from the state as it is. Never admits twice or queues."""
    if kind in SLOT_KINDS:
        return _slot_request(kind, purpose, current_work, serving, card)
    if kind in SERVING_KINDS:
        return _serving_request(kind, current_work, serving, card)
    return Decision(REFUSE, f"{kind} is not a request these rules admit.")


def _slot_request(kind, purpose, current_work, serving, card):
    """Runs, builds and downloads: one slot, and the serving rules each kind carries."""
    if current_work is not None:
        return Decision(REFUSE, f"The admission slot is held by the {current_work.kind} "
                                 f"for {current_work.purpose}. Stop it first; nothing is queued.")
    if kind in NO_CARD_RUNS or kind == DOWNLOAD:
        # Neither uses the card, and both may run beside serving and voice.
        return Decision(ADMIT)
    if serving not in SERVING_STATES:
        return Decision(REFUSE, f"The serving state is unknown ({serving!r}), so the {kind} for "
                                 f"{purpose} is refused rather than started beside unknown work.")
    if kind == BUILD:
        if serving == VOICE:
            return Decision(REFUSE, f"Voice serving is up. Stop serving before building {purpose}.")
        return Decision(ADMIT)
    # The card is read before serving: stopping serving cannot free a card an unknown
    # or foreign holder still has, so offering a run there would fail closed late.
    held = _card_refusal(card, purpose)
    if held is not None:
        return held
    if serving != STOPPED:
        return Decision(STOP_SERVING_AND_RUN, f"Stop serving and run: {serving} serving is up. "
                                              "Stop serving first; Consumers lose the model, and "
                                              "serving stays stopped afterwards.")
    return Decision(ADMIT)


def _serving_request(kind, current_work, serving, card):
    """Start agentic serving and Serve voice: the card must be free and known."""
    if serving not in SERVING_STATES:
        return Decision(REFUSE, f"The serving state is unknown ({serving!r}), so {kind} is refused "
                                 "rather than started over unknown work.")
    held = _card_refusal(card, f"to {kind}")
    if held is not None:
        return held
    if kind == SERVE_VOICE and current_work is not None and current_work.kind == BUILD:
        return Decision(REFUSE, f"A build for {current_work.purpose} is running. Stop it before "
                                 "serving voice.")
    return Decision(ADMIT)


def _card_refusal(card, before):
    """The one card answer every request needing a free card shares.

    Only an explicitly free card admits: an unread or unrecognized holder is an
    unknown state and refuses, because a failed state read is not permission.
    """
    state = card.state if card is not None else CARD_UNKNOWN
    if state == CARD_FREE:
        return None
    if state == CARD_FOREIGN:
        return Decision(REFUSE, f"The card is held by a llama-server Matuto does not own "
                                 f"({card.detail}). Stop it before starting {before}.")
    if state == CARD_RUN:
        return Decision(REFUSE, f"The card is held by the {card.detail} run. Stop it before "
                                 f"starting {before}.")
    return Decision(REFUSE, f"The card's holder is unknown ({state!r}), so nothing may start "
                             f"{before} until it is known.")