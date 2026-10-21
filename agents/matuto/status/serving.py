"""The header's serving item, discovered by name like every other provider.

The shared navigation contract requires: every signed-in page's header shows Not serving, Agentic serving or
Voice serving and links to Overview's Serving. The header itself, the card run's
"Card held by a run" item and the page section it links to belong to shared header status and run presentation;
this file only says what a serving state reads as.
"""

NAME = "serving"
# Overview's Serving section carries this id, so the item links to its route.
SECTION = "#/overview/serving"
LABELS = {"stopped": "Not serving", "agentic": "Agentic serving", "voice": "Voice serving"}
UNKNOWN = "Serving state unknown"


def item(state):
    """The header item for one serving state: its label and Overview's Serving.

    `state` is what `matuto.serving_state.read` reports. An unknown state is
    labelled as one: a failed read is not permission and not Not serving.
    """
    return {"name": NAME, "label": LABELS.get(getattr(state, "state", None), UNKNOWN),
            "href": SECTION, "state": getattr(state, "state", UNKNOWN)}
