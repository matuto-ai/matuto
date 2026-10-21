# Close the initiative on the GGUF lane

- Status: Accepted

## Question

Should the original serving initiative retain non-GGUF work after selecting the GGUF and llama.cpp path?

## Decision

Complete the initiative on the GGUF and llama.cpp path. Cancel the alternative serving adapter, non-GGUF starter profiles, directory-backed checkpoint manifest validation, and corresponding consumer-integration work. These are cancelled requirements, not delivered capabilities.

## Reasoning

Another runtime is a separate maintenance and security boundary. It needs reproducible installation, exact-checkpoint compatibility, artifact integrity, explicit custom-code and license decisions, credential-safe operation, and target-hardware integration checks.

Keeping unused alternatives inside the delivered scope would imply support the project does not provide. Recording their cancellation preserves the boundary without retaining speculative code or an operator's model catalogue.

## Rejected alternatives

- Implement the alternative adapter for completeness: it adds maintenance without a current product requirement.
- Describe the original non-GGUF requirements as complete: that would misstate supported behavior.
- Substitute converted GGUF artifacts: conversion does not establish support for the original format.
- Leave placeholder adapters for later: unused production interfaces still require maintenance.

## What would change the answer

A new non-GGUF requirement needs a fresh decision under [the separate-runtime qualification rule](0001-keep-llama-cpp-and-qualify-non-gguf-runtimes.md), including artifact integrity, trust, credential safety, and target-hardware evidence. It must not inherit an assumption that a particular runtime is suitable.
