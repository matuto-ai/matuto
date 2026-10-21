# Profiles pin identity, not values a default already supplies

- Status: Accepted

## Question

A lab profile is the record of what was qualified. Which values belong in it, and which belong to the tool that loads it?

## Decision

A profile states only what distinguishes it: the model identity that was qualified, the context it serves, and the arguments that make this profile different from its neighbours. It does not restate a value the loader already supplies, a location the host resolves, or a fact about the card it runs on.

`SERVER_OUTPUT_TOKENS="$CONTEXT_SIZE"` is the standing example. `agents/matuto-lab` already defaults it to `CONTEXT_SIZE`, and the qualification runner refuses any profile where the two disagree, so writing it into a profile adds a line that can only ever restate what the tool already guarantees or break the run. New profiles omit it. The same applies to a build directory that tracks `BACKEND_VERSION`, a model path that is a model root plus a source plus a file name, and a VRAM ceiling that belongs to the device rather than the profile.

`BACKEND_VERSION` stays, and stays provenance. It names the backend a profile was qualified on. Nothing compares it before a launch, and replacing the build requalifies nothing.

## Reasoning

Repeating a host-resolved model root or runtime location in each profile creates competing sources of truth. Moving model storage or selecting a rebuilt runtime can then invalidate otherwise usable profiles without changing their model identity or tuned behavior.

A pinned default also makes every profile a place where the tool's behavior can be contradicted. Leaving shared defaults and host locations to their owner means one place defines them and one place can be checked. Preserve the distinction between historical qualification provenance and the runtime currently selected to serve.

## Rejected alternatives

- Keep every field explicit so a profile reads as a complete configuration: it reads as complete while being wrong, because the explicit copy is not the value the tool uses when the two disagree.
- Keep the pins and add a check that they match the defaults: that is a test asserting a value equals itself, and it still requires every profile to track a shared default.
- Drop `BACKEND_VERSION` along with the rest: it is the one field here that records something about the qualification run, not about the current host.

## What would change the answer

Reconsider when a profile genuinely needs to differ from the default: a model that must cap its answer below its context, or a second card with its own ceiling. That is a per-profile fact and belongs in the profile. The test is whether the value distinguishes this profile from another one, not whether stating it feels safer.
