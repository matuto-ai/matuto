# Physical-host acceptance

This procedure validates the Matuto manager and router on a physical host after setup.

The relative manager route used below is part of Matuto's checked-in manager interface; the procedure contains no host, credential, or capability URL.

## Installed-build inspection

Run this check without starting the router or loading a model. Record the exact Matuto commit, distribution/kernel, browser/version, installed llama.cpp identity and backend, and each observed outcome. This check does not qualify a GPU or exercise saved selection.

- Inspect one protected current-user-owned build and one protected root-owned distribution installation in place, through both `matuto-lab inspect /absolute/path` and the manager's **Inspect** control. Use canonical paths. Compare executable hashes, reported versions, available tools/backends, and device records. Keep a missing optional tool unavailable without disabling other installed tools. On installations with shared libraries beside the executable, confirm descriptor execution still resolves those libraries.
- On an integrated-plus-discrete AMD host, confirm the separate PCI addresses and hardware IDs remain distinguishable and backend enumeration is shown as an unresolved physical association rather than a guessed match. Repeat for the available HIP/ROCm and Vulkan configurations; record unavailable combinations explicitly. Device listing is not evidence of model offload.
- In a real browser at desktop and narrow phone widths, reach the location field and **Inspect** using only the keyboard, submit with Enter and the button, and verify visible focus, readable results, and no horizontal page overflow. Check pending, completed, missing-installation, and failed-probe states. Edit the location or refresh during a pending request and confirm a late response cannot restore old devices. Reload and use browser back/forward restore; neither may claim the previous snapshot was freshly inspected.
- Repeat inspection while the managed runtime is absent or unusable. The input remains usable and the defect is specific. Stop the manager during a blocked probe, confirm the probe exits, restart the manager, and retry without configuration repair or selection changes.

Capture only sanitized state/results or screenshots cropped away from the address bar. Do not include credentials, capability URLs, raw backend logs, or private installation paths. A missing browser or inability to provision a protected root-owned fixture leaves the corresponding named check pending; automated fake-device results do not replace these observations.

## Portable host setup and runtime selection

Run this on each documented target distribution with a discrete AMD GPU, and record one result per host and distribution. Exercise the documented target distributions with representative supported GPUs; do not treat one installation as a universal reference. State the exact combination you exercised and leave every other combination untested rather than generalizing from one host.

Record the prerequisite evidence the host guide's non-installation commands produced: distribution release and kernel, installed driver and runtime versions, the `gfx` target and ROCr UUID from `rocminfo`, which backends the installed build enumerated, and whether the current user is in `render` and `video`. A package that was absent, a backend that did not enumerate, and a command that was unavailable are recorded as unavailable evidence, not as a failure of the host and not as a pass.

Then exercise the whole operator path:

- Add the installation in the manager's Router tab, inspect it, choose the backend, choose the physical GPU explicitly, and save the selection. Repeat the same outcome through the documented terminal fallback (`matuto-lab runtime select`, `matuto-lab runtime show`). Confirm the saved record is owner-only and that a second Linux account does not see it.
- Start the router under that selection and make one real model request. Confirm from the server's own placement output and measured memory that the selected GPU did the work, and keep that distinct from enumeration and from a requested `-ngl` value. On a machine with an integrated and a discrete GPU, confirm the explicit choice was required and that the unselected device was not used.
- Demonstrate each backend the host has: HIP/ROCm and Vulkan separately, each with its own saved selection and its own build. Record which combinations you actually demonstrated.
- Record intentional partial offload (`--n-cpu-moe` or an equivalent) as part of the tested configuration, with the number of CPU layers and the reason.
- Restart the router and reuse the saved choice without re-selecting. Stop the router, change the selection, and confirm the change is refused while a workload holds the lease and succeeds once it stops.
- Provoke the actionable failures: an installation that is not there, an unsafe executable, a GPU that no longer enumerates, and a backend that cannot corroborate the PCI address. Confirm each is refused with a specific reason and that nothing falls back to the CPU or to another device.
- Install an operator package update over the selected build and confirm the next run reports the new build identity while earlier results still describe the old one.

Record the actual host/build/model/context/offload identities the runs captured, and never restate a saved choice as an effective run identity: an identity that is unavailable stays unavailable. Capture only sanitized state, and never capture the capability URL, credentials, or private installation paths that must stay out of published evidence. If a target host or GPU is unreachable, record the missing observation and its residual risk in the pull request instead of marking the check complete.

## Complete the physical-host evidence

After setup, confirm the manager is active and setup did not start or enable the router:

```bash
systemctl --user is-active matuto-manager.service
systemctl --user show local-llama-router.service -p ActiveState -p SubState -p UnitFileState -p MainPID
```

Use the manager to start the router. Confirm ports 8888, 8889, and 8890 listen only on loopback, then exercise stopped, starting, healthy, unhealthy, and stopping states. At narrow phone width and desktop width, use keyboard and touch controls, refresh during transitions, and confirm loading, empty, error, and success states remain usable. Do not capture the authenticated launcher URL.

On a stable state, select text in the model or log display, install a real-browser observer in DevTools, wait at least five seconds for two unchanged polls, and confirm both zero mutations and retained selection:

```js
window.managerMutations = [];
window.managerObserver?.disconnect();
window.managerObserver = new MutationObserver(records => window.managerMutations.push(...records));
window.managerObserver.observe(document.querySelector("main"), {subtree: true, childList: true, characterData: true, attributes: true});
```

Run `console.assert(window.managerMutations.length === 0, window.managerMutations)` after the wait. Clear the array, use a lifecycle control that changes state, and confirm `window.managerMutations.length > 0` before disconnecting the observer. While the router is healthy, run the following in DevTools, click **Restart**, and confirm the visible action failure survives more than one status poll:

```js
window.managerFetch = window.fetch;
window.fetch = (input, init) => input === "/api/action" ? Promise.resolve(new Response("{}", {status: 500})) : window.managerFetch(input, init);
```

Restore with `window.fetch = window.managerFetch`. When the router is stopped, confirm the already-selected build action is disabled.

Open the model console, discover and load the qualified GGUF, unload and reload it, and complete one streaming chat. Stop the router from the manager and confirm systemd reports it inactive with no remaining llama-server process:

```bash
systemctl --user show local-llama-router.service -p ActiveState -p SubState -p MainPID
! pgrep -x llama-server
```

Log out and back in once. Reopen the manager and confirm the stopped state remains accurate and neither unit gained autostart. Capture only sanitized states and results; never capture the API key, capability URL, prompts, model output, or unbounded journal content.
