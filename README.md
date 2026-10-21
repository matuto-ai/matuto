# Matuto

Host-side tooling for running local models on your own GPU. Qualify a model against the hardware first, then serve the qualified one to a consumer through the native llama.cpp router.

Everything here runs on the machine holding the GPU, not inside a consumer sandbox.

## Install

Matuto installs from signed releases at this release address:

```text
https://github.com/matuto-ai/matuto/releases/latest/download/
```

Every release list is signed with this release key, and bootstrap installs nothing that does not verify against it:

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKyyUI+GyC0vUCnp0mpz89du12bj/Qbtoj5T2LOEiIME matuto-release
```

Its fingerprint is `SHA256:uNhP5M+oK6Q2qrl/Sj8pRCrACugJywFcCcLC1/YBdTc`. The repository holds the same key as `release-key.pub`.

Install the prerequisites. On Ubuntu Server 24.04 LTS:

```bash
sudo apt update
sudo apt install git python3 openssh-client
```

On Omarchy:

```bash
sudo pacman -S --needed git python openssh
```

Then, as the Linux account that will run Matuto, get the bootstrap command and run it against the release address and key:

```bash
git clone https://github.com/matuto-ai/matuto.git
cd matuto
agents/matuto-bootstrap --feed https://github.com/matuto-ai/matuto/releases/latest/download/ --trusted-key release-key.pub
```

Bootstrap prints the key's fingerprint. Compare it with the fingerprint above, and type it back only if the two match; anything else stops the install. Bootstrap then downloads the newest release, checks it against the key, and installs it. Start the manager and open it:

```bash
~/.local/bin/matuto-manager open
```

Your browser opens `http://matuto.localhost:8890/`; choose **Create login**. On a machine without a desktop, start the manager with `systemctl --user start matuto-manager.service` instead. Then, from a computer with a browser, forward the address with `ssh -L 8890:127.0.0.1:8890 <account>@<machine>` and open `http://matuto.localhost:8890/` there. The [operator guide](docs/MATUTO_MANAGER.md) continues from the login, and the [host lab guide](docs/MATUTO_LAB.md) covers the GPU drivers and runtimes Matuto does not install.

## What it ships

- `agents/matuto-lab` — the qualification workflow, over stored records. `matuto-lab <profile> wq` submits worker qualification and `matuto-lab --store spec KIND ID` prints the launch spec a record resolves to; the runs and their evidence live in the browser's Runs & jobs.
- `agents/matuto/` — the browser-first Matuto app and its `matuto-manager` CLI. It owns the fixed-address authenticated manager, runtime support, profile store, and generated router presets. The router answers on loopback at `http://127.0.0.1:8888/v1`.

## Where to start

- [Matuto host lab](docs/MATUTO_LAB.md) — Ubuntu Server 24.04 LTS and Omarchy stable prerequisites, non-installation host diagnostics, building llama.cpp for your GPU target, and qualifying a model. Start here on a fresh host.
- [Matuto router operator guide](docs/MATUTO_MANAGER.md) — setup, build/backend/GPU selection, browser controls, consumer integration, presets, model selection, troubleshooting, and removal.
- [Physical-host acceptance](docs/testing/physical-host-acceptance.md) — the procedure to run before relying on the managed router.
- [Decision records](docs/decisions/) — why llama.cpp stayed the serving path, why the router is managed in a browser, and why the non-GGUF lane was cancelled.
- [Glossary](CONTEXT.md) — the terms for profiles, bundles, cards and qualification.

## Validation

```bash
./validate.sh
```

Requires `shellcheck` on `PATH`.

The whole validation run has a 150-second budget, set by `VALIDATE_BUDGET_SECONDS` in `validate.sh` and measured by `TIMING validate.total`. An otherwise passing run over the budget fails; a failed check keeps its original exit status. Full PR CI also permits at most 20% growth against a verified baseline on its exact main commit; see [CI runtime baselines and recovery](docs/testing/ci-runtime-budget.md). To stay under the budget, test each rule in the module that enforces it, keep full-install scenarios for whole-install promises, and build shared fixtures once per test class.
