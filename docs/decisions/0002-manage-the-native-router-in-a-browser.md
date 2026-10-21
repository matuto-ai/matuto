# Manage the native router in a browser

- Status: Accepted

> Record [0004](0004-author-manager-owned-config-in-the-page.md) amends this one: the manager page may author the configuration the manager already owns, within the limits recorded there.

> Record [0008](0008-keep-matuto-configuration-in-one-sqlite-store.md) amends this one: Matuto keeps its own configuration and qualification records in one SQLite store, while the router stays the source of truth for model discovery and state.

> The browser-app decisions on profile authoring, qualification invalidation, and verified model acquisition amend this one: the browser app supersedes this narrower controller, which now covers form authoring, browser runs, acquisition ownership with the console's download and delete refused, and Custom arguments. The rule that phone or network access needs its own authentication design stays.

## Question

Should local model operation use a numbered terminal chooser and a project-owned catalogue, or a browser manager around the native llama.cpp router?

## Decision

Use a small local-only browser manager around the native llama.cpp router and the user service manager. Let the router remain the source of truth for GGUF discovery, presets, loading, unloading, authentication, API behavior, and its model console. The manager supplies setup, fixed lifecycle actions, status, and bounded redacted logs. It does not own a second model catalogue, download pipeline, chat client, or database, and it does not start Pi.

## Reasoning

The native router already provided the hard model-facing behavior: discovery, router-mode loading and unloading, model presets, authenticated OpenAI-compatible requests, tool calling, and a browser model console. Reimplementing those features would create two sources of truth and make every router upgrade a synchronization problem.

The remaining problem was operational. A terminal chooser still required operators to manage service state, protected files, paths, and networking outside the tool. A browser manager made start, stop, repair, inspection, and direct model testing available on phone and desktop while the user service manager kept the router alive after the launcher or browser closed.

A fixed action surface also kept the controller narrow. Profiles retain reviewed model-specific tuning, while listener, authentication, process control, and model management stay with the native components that already enforce them.

## Rejected alternatives

- Keep the numbered terminal chooser as the primary interface: it did not remove the manual lifecycle and configuration work that remained after qualification.
- Add a project catalogue or database: it would duplicate router discovery and presets and could drift from actual model state.
- Build a custom downloader or chat client: the native model console already supplied those functions.
- Expose arbitrary commands or every llama.cpp flag: that would turn a fixed local controller into a command-execution and configuration surface.
- Have the manager launch Pi: sandbox startup and provider selection belong to the sandbox, not the model host controller.

## What would change the answer

Revisit this seam if the native router stops providing the required discovery, preset, model-management, or console behavior, or if a future multi-backend requirement cannot be represented without a separate normalized catalogue. Remote or shared-host management would be a different security design rather than an extension of this local-only manager.
