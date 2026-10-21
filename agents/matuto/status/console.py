"""Whether the Model console can be used, from the serving-state read."""

from matuto.admission import AGENTIC
from matuto.serving_state import TRANSITION

NAME = "console"


def item(state):
    """The shell hint and page state derived from one serving-state reading."""
    usable = state.state == AGENTIC and state.healthy
    transition = any(unit_state in TRANSITION for _unit, unit_state in state.units)
    return {"usable": usable, "state": state.state, "healthy": state.healthy,
            "transition": transition, "reason": state.reason}
