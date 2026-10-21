# Serve voice exclusively with an external client

- Status: Accepted
- Extends: [0002](0002-manage-the-native-router-in-a-browser.md)

## Question

How should one selected GPU support both agentic serving and a spoken conversation, and where should the conversation interface live?

## Decision

Agentic serving and voice serving are mutually exclusive. Voice uses a bundle containing a language profile, GPU speech synthesis, and CPU speech recognition. Matuto owns the runtime processes and the explicit transition between serving modes; it does not own the conversation client.

Switching modes stops the previous workload before starting the requested one. Turning voice off stops its speech services. Both modes retain one language-model slot rather than sharing capacity between simultaneous conversations.

The microphone, turn detection, interruption, playback, and conversation interface belong to an external client. Matuto does not launch or configure that client's agent.

## Reasoning

Memory capacity alone does not establish whether a voice bundle works. Speech synthesis competes with language generation for GPU compute. A bundle therefore needs joint qualification under generation load, with checks for memory fit, synthesis faster than playback, and a timely recognition round trip. Qualification of its components in isolation is insufficient.

CPU recognition avoids adding another recurring GPU workload during speech. Model size, context, offload, and runtime choices remain per-bundle qualification decisions, not hardware-specific defaults shipped by Matuto.

Keeping the conversation client external avoids adding microphone permissions, global shortcuts, turn management, and a second chat interface to the model host. A native client can manage foreground and background interaction independently of a browser tab.

## Runtime boundary

Speech runtimes use a dedicated protected launch path because their command-line interfaces differ from llama.cpp. They still require protected executables, loopback-only listeners, argument validation, and explicit device placement. Recognition remains a CPU workload. Missing or ambiguous physical-device identity refuses a launch rather than selecting a device by enumeration order.

## Accepted risk

An external client may run tools with its user's permissions. Spoken input has no reliable author identity, so explicit activation and client-side consent controls remain important. Matuto's serving isolation does not sandbox a consumer application.

## Rejected alternatives

- Run both serving modes simultaneously: memory and compute contention would undermine their qualification assumptions.
- Share one model slot between independent conversations: alternating requests disrupts context reuse and predictable latency.
- Put the conversation client in the manager: that expands the manager beyond hosting and qualifying runtimes.
- Ship one measured hardware profile as a universal default: qualification belongs to the installed configuration.

## What would change the answer

Revisit exclusivity for independently assigned GPUs or a runtime that provides integrated speech with a demonstrably different contention model. Any such change needs joint qualification and explicit lifecycle behavior; historical measurements from another installation are not sufficient.
