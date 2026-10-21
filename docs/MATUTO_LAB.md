# Matuto host lab

Use Matuto's Runs & jobs page or the `matuto-manager` CLI to run procedures against stored records. A run always uses one record at its current fingerprint; editing a profile file, creating a Bash profile, or sourcing host defaults is no longer part of the workflow. Runs and their evidence live in the Matuto store. Qualification is an explicit acceptance in the browser, never an automatic result of a completed run.

For installation, runtime selection, and serving, see the [Matuto router operator guide](MATUTO_MANAGER.md). For real-host checks, use the [physical-host acceptance procedure](testing/physical-host-acceptance.md).

## Host prerequisites

Matuto installs no driver, runtime, compiler, or system package. The operator installs and maintains those prerequisites. The documented targets are Ubuntu Server 24.04 LTS and Omarchy stable on x86-64 with a discrete AMD GPU. Other x86-64 distributions and releases may work but remain untested until observations are recorded; a successful run is not AMD certification or general hardware qualification.

Install the backend you intend to use. HIP/ROCm and Vulkan are independent prerequisites; Matuto does not switch between them automatically.

### Ubuntu Server 24.04 LTS

AMD's [ROCm 7.2.1 Radeon support matrix](https://rocm.docs.amd.com/projects/radeon-ryzen/en/docs-7.2.1/docs/compatibility/compatibilityrad/native_linux/native_linux_compatibility.html) lists Ubuntu 24.04.4 Desktop with HWE (kernel 6.17) and Ubuntu 22.04.5 Desktop with HWE (kernel 6.8). Ubuntu Server is not in that matrix, so it is an untested configuration.

Install AMD's unified driver and ROCm 7.2.1 with its installer. This privileged installation belongs to the operator:

```bash
sudo apt update
sudo apt install python3-setuptools python3-wheel
wget https://repo.radeon.com/amdgpu-install/7.2.1/ubuntu/noble/amdgpu-install_7.2.1.70201-1_all.deb
sudo apt install ./amdgpu-install_7.2.1.70201-1_all.deb
sudo amdgpu-install -y --usecase=graphics,rocm
sudo usermod -a -G render,video "$LOGNAME"
sudo reboot
```

After reboot, check the driver and devices:

```bash
groups
dkms status
rocminfo
clinfo
```

The user must appear in `render` and `video`; `dkms status` should show the `amdgpu` module installed; and `rocminfo` should list the GPU as an agent with a `gfx` target. Install `vulkan-tools` and use `vulkaninfo --summary` only when you intend to use Vulkan. Record the kernel, driver, and ROCm versions with any result.

### Omarchy stable

Omarchy is an Arch-based rolling channel. Record exact kernel and package versions with every result; one observation does not qualify later updates.

```bash
sudo pacman -Syu
sudo pacman -S rocminfo hip-runtime-amd hipblas rocblas amdsmi \
  vulkan-icd-loader vulkan-radeon vulkan-tools
```

ROCm packages install under `/opt/rocm` and export their environment from `/etc/profile.d/rocm.sh`; open a new login shell before probing. Vulkan uses the `vulkan-icd-loader` loader and `vulkan-radeon` driver. `vulkan-tools` provides `vulkaninfo`.

```bash
uname -r
pacman -Q rocminfo hip-runtime-amd hipblas rocblas amdsmi vulkan-radeon vulkan-tools
rocminfo | grep -E 'Name:|Marketing Name:|gfx' | head -20
vulkaninfo --summary
amd-smi version
```

A package not available from pacman is unavailable evidence for that backend, not a reason to substitute a different install method.

Record the ROCm version and exact `gfx` target reported by the GPU. A build for one target is not a build for another.

## Check the host without installing anything

These commands report what the host and installed software expose. They install no packages, select no runtime, load no model, and qualify neither a model nor a machine.

### What the operating system exposes

```bash
lspci -nnk | grep -EA3 'VGA|Display|3D'
ls -l /dev/kfd /dev/dri
id -nG
```

`lspci` shows physical GPUs, vendor/device IDs, and the kernel driver. `/dev/kfd` is present when an AMD compute driver is loaded; `/dev/dri/renderD*` nodes are what a backend opens. Missing nodes or a user absent from `render` prevent runtime access. These checks do not establish which backend will use a device or whether a build supports it.

`dmesg` and `journalctl` can provide driver-load and reset details. Keep any recorded excerpt short and sanitized; do not paste a whole host journal.

### What the installed backend enumerates

```bash
rocminfo
vulkaninfo --summary
clinfo
```

`rocminfo` lists HIP/ROCm agents and their `gfx` targets and ROCr UUIDs. Correlate a device with a PCI address by UUID, never by listing order. `vulkaninfo --summary` lists Vulkan physical devices and drivers. Enumeration does not show that a llama.cpp build can use a card or establish where a model will run.

### Inspect and select a runtime

In **Runtime › llama.cpp builds**, inspect a discovered build, register it when needed, then choose **Select runtime** to select its backend and physical card. Inspection runs no profile and starts no inference; selection authorizes later workloads but does not start a model or qualify a card. For installation and runtime details, see the [Matuto router operator guide](MATUTO_MANAGER.md#use-the-app). An absent backend is unavailable evidence, not proof that a build cannot support it.

### What actually ran on the selected GPU

Enumeration and a requested GPU-layer count do not prove execution. Run a stored record's verify, smoke, or bench procedure and inspect the run's recorded checks and observations in Runs & jobs. Confirm placement from the run's own output rather than the requested flag: a positive `-ngl` is only a request, and `--n-cpu-moe` or an equivalent intentionally leaves layers on the CPU. Partial offload is part of the tested configuration.

### Optional telemetry

```bash
amd-smi version
amd-smi metric
rocm-smi
```

These tools report clocks, temperature, power, and VRAM when available. Missing telemetry is unavailable evidence, never zero or a pass. A bench run records unavailable telemetry; its byte-valued VRAM counter is a separate required measurement and the run fails if that counter is missing or invalid.

## Create or import records

Create cards in **Settings › Cards** and model profiles in **Profiles**. To bring records from another Matuto instance, open **Profiles › Import / export** and import a JSON document, or run `matuto-manager import <document.json>`. Imported records carry no qualification; new profiles and bundles arrive as candidates. Model files and speech executables must already be acquired or adopted on this host. The CLI supports importing and exporting record documents, not other record authoring.

## Run procedures

A run targets one stored profile or bundle at its current fingerprint. Run it from **Runs & jobs** in the browser, or use the `matuto-manager` CLI. The browser forms offer the record and procedure; opening Runs & jobs with a record in the address preselects that record. No form starts work until you choose Start.

The CLI command names the procedure and the stored record, then follows the resulting run in the terminal:

```text
matuto-manager run verify <profile>
matuto-manager run smoke <profile>
matuto-manager run bench <profile>
matuto-manager run serve <profile>
matuto-manager run wq <profile>
matuto-manager run bundle <bundle>
matuto-manager run context_search <profile-or-bundle>
matuto-manager list
matuto-manager follow <run-id>
matuto-manager cancel <run-id>
```

`verify` checks prerequisites for the record. `smoke` runs a bounded model request. `bench` measures the configured prompt and generation workloads. `serve` starts a temporary loopback server and keeps it running until stopped. `wq` runs the installed worker-qualification procedure; it does not accept qualification. The lab CLI form `matuto-lab <profile> wq` also starts worker qualification. Bundle runs measure fit, real-time performance, and the recognition round trip; speech profiles run only as bundle members. A context search uses the record's trained-context limits and starts from the largest rung the CLI offers; choose a starting rung in the browser form. Follow or cancel any run by its run ID, and list runs, builds, and downloads with `matuto-manager list`.

The browser and CLI submit work to one runner and share one admission slot. There is no queue: when work is active, a new start is refused with the current work named. Verify also uses the slot. Card runs cannot compete with serving. If serving is up, use the browser's **Stop serving and run** action and confirm its consequences; serving stays stopped afterward. The CLI does not silently stop serving for you. A run started in a terminal remains visible and stoppable in the browser; closing the page or restarting the manager does not stop it. Reopen Runs & jobs to reconnect. Logout or reboot interrupts a run.

Stop a run from Runs & jobs or press Ctrl-C while following it in the terminal. Stopping ends its whole unit and records it cancelled unless normal completion already won the race. Runs end as completed, errored, cancelled, or interrupted. Each check is reported separately as PASS, FAIL, ERROR, or SKIP. A completed run, including a passing run, never grants qualification by itself.

## Accept qualification and retain evidence

Accept or withdraw qualification in the browser, not the CLI. In Runs & jobs, open the profile or bundle and choose its acceptance action. Acceptance cites completed runs recorded at the record's current fingerprint. A language profile needs a passed smoke and a completed bench; a bundle needs a passed bundle run. Those minimums cannot be overridden. Other failed checks need a named exception and reason. The acceptance records its scope, cited runs, exceptions, residual risks, date, and login username. Withdrawal returns the record to candidate, retains the acceptance in its history, and stops nothing.

Run evidence—including checks, summary, logs, and artifacts—stays in the Matuto store. Each run can be downloaded from Runs & jobs. Keep downloaded evidence where it belongs; Matuto does not copy run evidence into the repository. Errors, cancellations, interruptions, and legacy runs remain browsable but cannot be cited for acceptance.

## Context searches, bundles, and worker qualification

A context search starts at a rung the browser offers for the selected record, at or below its trained context. The search records each rung as its own run. It stops at the first working rung and checks the next lower rung for consistency; if that check fails, or no rung works, it names no winner. **Set context to N** opens the ordinary profile edit; after saving that edit, the matching run can be cited without rerunning because its fingerprint matches the updated record.

A bundle run measures its stored language, synthesis, and recognition members together. Synthesis and recognition profiles have no independent qualification state. The bundle must fit the card and run faster than real time; recognition must return non-empty text faster than the clip lasts. Recognised-word count is an observation, not a pass rule.

Worker qualification runs from Runs & jobs, from the `matuto-manager run wq <profile>` command, or from the lab CLI as `matuto-lab <profile> wq`. The browser form shows editable limits before Start; its VRAM limit defaults to the stored card's ceiling, and every changed limit is frozen when the run starts. The report identifies dirty-checkout runs, which cannot be cited for acceptance. Completion records evidence only; acceptance remains a browser action. `wq selftest`, if used, is a developer command, not an operator qualification run.

## Interpreting measurements

Use the record's observed runtime build, model fingerprint, card, context, and check results when comparing runs. A benchmark does not establish an agent worker's throughput: a bench measures one server without concurrent requests, tool calls, reasoning budgets, or prompt-cache reuse. Compare throughput only when model fingerprint, runtime build, prompt and generation sizes, context, repetitions, and thermal/power conditions match. A profile's requested GPU layers are not proof of placement.

A run establishes only the tested configuration and conditions. Idle memory readings do not establish peak usage, mapped bytes do not establish residency, and a requested GPU-layer count does not establish placement. Missing telemetry stays unavailable rather than becoming a zero measurement.

Qualification does not establish all-ambient safety, acoustic certification, full-window model quality, or unattended operation. Keep installation-specific tuning, results, and run history in the private store rather than publishing them as project defaults.

For benchmark interpretation, readiness is the time to the first `/health` 200 from a fresh process, not cold-storage latency: the page cache is not cleared and polling has 0.02-second resolution. Shallow and near-full generation rates are measured together in one artifact. Compare runs only when model fingerprint, runtime build, depth, KV types, context, repetitions, and thermal/power conditions match. A bench has no concurrent request, tool call, reasoning budget, or prompt-cache reuse, so it does not establish a served worker's throughput.

For consumer configuration and serving, continue to the [Matuto router operator guide](MATUTO_MANAGER.md).
