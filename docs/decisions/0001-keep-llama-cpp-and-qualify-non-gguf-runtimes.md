# Keep llama.cpp and qualify non-GGUF runtimes separately

- Status: Accepted

## Question

Should one serving runtime be assumed to support GGUF, AWQ, and INT4 artifacts across supported hardware?

## Decision

Keep llama.cpp as the serving path for GGUF artifacts. A non-GGUF adapter requires separate qualification of the exact checkpoint, pinned runtime, and target hardware against the same API, security, lifecycle, and memory requirements.

## Reasoning

Artifact format is part of model identity. Converting an AWQ or INT4 checkpoint to GGUF produces a different artifact and cannot demonstrate support for the original checkpoint. Driver support or successful enumeration alone also cannot establish that a runtime supports a particular architecture or quantization.

A container can isolate dependencies, but it cannot supply missing runtime or hardware support. Adding another adapter introduces its own installation, authentication, recovery, and qualification obligations. Keeping that work separate preserves a working serving path without presenting experimental compatibility as a guarantee.

## Rejected alternatives

- Assume one general-purpose runtime supports every checkpoint: compatibility must be established for the actual artifact and hardware.
- Convert unsupported artifacts and claim equivalent support: that changes the artifact being evaluated.
- Add speculative adapters or a raw generation loop: neither supplies the required operational contract without further work.

## What would change the answer

Reconsider a non-GGUF adapter when a reproducible, pinned runtime serves a required checkpoint, passes the API and operational checks on its target hardware, and has an acceptable maintenance cost. That can justify another adapter without replacing llama.cpp.
