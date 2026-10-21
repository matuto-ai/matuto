# Author manager-owned configuration in the manager page

- Status: Accepted
- Amends: [0002](0002-manage-the-native-router-in-a-browser.md)

> The browser-app decisions on profile authoring, qualification invalidation, and verified model acquisition amend this one: the browser app supersedes this narrower controller, which now covers form authoring, browser runs, acquisition ownership with the console's download and delete refused, and Custom arguments, the last of which amends this record's ban on free-text llama.cpp options.

> The browser-app consumer-integration decision amends this one: Matuto owns serving and server authentication, ai-agents-sandbox owns Pi configuration, model selection and thinking preferences, so the Pi consumer-selection authority this record granted is superseded and the consumer `selection.json` is no longer Matuto state.

## Question

Decision record 0002 fixed the manager surface at setup, fixed lifecycle actions, status, and bounded redacted logs. Does that surface allow the manager page to author the configuration files the manager already owns?

## Decision

Yes, through fixed-field actions, for `presets.ini`, the consumer `selection.json`, and the private per-account runtime selection. The implemented Linux portability specification extends this decision to an installed build, available GPU backend, and explicitly chosen physical GPU. The manager may create, edit, publish, unpublish, and delete entries in configuration it already creates, validates, and emits. The native router keeps discovery, loading, unloading, downloading, and the model console; the manager page shows load state read-only and links across to the console.

The runtime selection is separate from consumer model choice and operator qualification. Browser and terminal controls share one protected record and refuse changes while affected workloads run. Selection does not install packages, move existing data, or automatically start, stop, or restart workloads.

Two limits hold. Preset editing exposes a fixed field set and never a free-text llama.cpp option. The manager does not write lab profiles, because those are Bash that `matuto-lab` sources.

## Reasoning

The router console cannot author a preset. `llama-server` reads `--models-preset` and has no API to write one, and it has never heard of the consumer selection. So letting the router remain the source of truth answers discovery and load state, but leaves the operator hand-editing files for everything else. That gap is the problem 0002 did not have to solve and this record does.

Editing `presets.ini` from the page is not the project catalogue 0002 rejected. The manager already creates that file, validates it, and emits `models.json` from it. A form over a file one component already owns end to end adds no second store, so there is nothing new that can drift.

The limits preserve the boundaries 0002 actually drew. A free-text flag field would be the configuration and command-execution surface it rejected by name. Mirroring load and unload in the manager page would create the second source of truth about model state it also rejected. Profile authoring stays out because a profile is executed rather than read, and creating one happens about once per model, which does not justify a permanently wider trust surface.

## Rejected alternatives

- Leave the surface unchanged: operators keep hand-editing `presets.ini` and running a Python snippet to change the consumer selection, which is the workflow that produced this record.
- Move preset and selection editing into the router console: `llama-server` owns neither file and cannot be asked to.
- Accept arbitrary llama.cpp options in the page: rejected in 0002, and rejected again here for the same reason.
- Let the page write lab profiles as well: that makes a web form the author of code the lab later sources.

## What would change the answer

Revisit if lab profiles stop being executable, because profile authoring would then carry the same risk as preset authoring rather than a larger one. Revisit if the manager grows a job surface for qualification runs, because a job runner is a different kind of controller rather than an extension of this one.
