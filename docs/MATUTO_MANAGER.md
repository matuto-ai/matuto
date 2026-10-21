# Matuto operator guide

Matuto is a browser app for managing local model files, profiles, runtimes, serving, runs, settings and updates. Open [http://matuto.localhost:8890/](http://matuto.localhost:8890/) to sign in; the manager listens only on this account's loopback interface. Model qualification procedures are covered by [Matuto host lab](MATUTO_LAB.md); target-host checks belong to the [physical-host acceptance procedure](testing/physical-host-acceptance.md).

## Open Matuto and sign in

After bootstrap, the Matuto manager starts at every login to this Linux account, including SSH login. It uses no GPU; login does not start serving, runs, builds or downloads. Matuto does not enable lingering, so the manager stops when the last login session ends. The app-menu entry **Matuto**, or `matuto-manager open`, opens the fixed address above. The address has no secret in it and can be bookmarked.

On first use, choose **Create login** and set a username and password. The login is stored locally and the browser stays signed in for seven days, including across manager restarts and updates. **Sign out** ends that browser session. To change the password, use Settings › Account. To reset a forgotten login from a terminal, run `matuto-manager reset-login`; this ends every session and the next visit asks you to create a login again.

To turn off login start, run:

```bash
systemctl --user disable matuto-manager.service
```

The manager remains available until the current login session ends. Repair and updates preserve whether login start is enabled or disabled. To enable it again, run `systemctl --user enable matuto-manager.service`. Matuto never enables lingering.

## Use the app

The app has seven pages: **Overview**, **Model files**, **Profiles**, **Runs & jobs**, **Runtime**, **Settings** and **Model console**. The page list folds into the **Pages** menu in a narrow window. A light/dark switch changes the browser's appearance without changing your records.

**Overview** shows serving and current work. Use **Start agentic serving** to start the published model, and **Stop serving** to stop agentic or voice serving. Starting agentic serving is direct; actions that stop or replace serving show a consequence review first. Matuto does not start serving at login or sign-in.

**Model files** downloads or adopts model files and shows their verification state. Delete model files through external file management; Matuto does not delete them. **Profiles** manages model profiles, speech profiles, bundles and qualification. **Runs & jobs** shows and controls work such as runs, builds and downloads. **Runtime** installs, inspects, registers and selects runtimes. **Settings** contains Storage & artifacts, Download origins, Cards, Argument catalogue, Lab port, Account and Maintenance. Configure model storage before downloading; choose a runtime and card before running a workload.

**Model console** is available only while healthy agentic serving is running. It is unavailable during voice serving or when the manager cannot confirm healthy serving. The console's native download and delete controls are disabled; use Model files to get model files and external file management to delete them. Console requests and consumer requests share the serving model slot.

## Recover or repair Matuto

Bootstrap installs all three app-menu entries: **Matuto**, **Repair Matuto** and **Update Matuto**, each running the installed updater outside the versioned release folders. **Update Matuto** remains available when no newer release is offered; it opens Settings › Maintenance's update screen.

Use **Matuto** when the manager does not answer. The entry starts it, waits for the fixed address, and restarts the manager once if needed. It checks that this account owns the address before opening it. If recovery fails, it opens a private local error page with the cause, a bounded log excerpt and available recovery actions. A bookmark cannot perform this owner check. A manager failure does not itself stop serving or jobs.

**Repair** has the same meaning in Settings › Maintenance, the **Repair Matuto** menu entry and `matuto-manager repair`. It keeps the installed version. Repair finishes an interrupted update forward; otherwise it checks installed files and, if files are missing or altered, reinstalls that same version (which needs the network). It regenerates service files, menu entries and router presets, preserves the login-start choice, restores permissions on Matuto's own folders, creates the router API key only if it is missing, and restarts only the manager. Outside finishing an interrupted update, it keeps the login, sessions, store, runtime selection, build registry, weights, builds, runs and consumer files unchanged, and does not stop serving or cancel jobs. Repair cannot recover a corrupted store.

If the installed version cannot start and a newer release is available, the error page names the version, release notes, download size, prerequisites, database migration and work that must stop. **Update Matuto** installs that exact release only if the offer and running state still match and every prerequisite is present; it snapshots the store first and never stops serving or jobs for you. If anything changed or a prerequisite is missing, it refuses and rewrites the error page.

From the running app, check for updates and review the offer before choosing **Install**. An update requires serving to be stopped and every run, build and download to be finished or cancelled. Matuto names each blocker and never stops work itself. It checks prerequisites, saves and verifies a pre-update snapshot, and updates forward; it does not roll back or restore the snapshot automatically. New browser, CLI and serving starts are refused while installation is in progress. Only the manager restarts after an update; serving remains stopped.

## Remove Matuto

Use **Remove Matuto** in Settings › Maintenance or run `matuto-manager remove`. Removal refuses while serving, a run, build, download or update is active or its state is unknown. Stop serving on Overview and finish or cancel work in Runs & jobs, then retry; removal never stops work itself. Before confirmation, review the separate removed and kept lists.

Removal deletes Matuto's release files and commands, the independent updater, manager/router/voice service files, generated router presets and the three Matuto menu entries. It keeps the SQLite store, pre-update snapshot, login and sessions, router API key, partial downloads, build source and staging, model weights, runtime builds and speech executables. It never touches consumer configuration. After listing the kept items, it disables login start and deletes the manager and updater. If interrupted, rerun `remove` to finish.

If the updater is already gone, the installed removal action is unavailable. The paths below assume the standard XDG defaults; if `XDG_CONFIG_HOME` or `XDG_DATA_HOME` is set, substitute that directory for `~/.config` or `~/.local/share`. First make sure serving and other work have stopped, then stop and disable the manager before removing its files:

```bash
systemctl --user stop matuto-manager.service
systemctl --user disable matuto-manager.service
```

Remove only these Matuto-owned install paths:

- Release files and commands: `${XDG_DATA_HOME:-$HOME/.local/share}/matuto/releases/`.
- Independent updater: `${XDG_DATA_HOME:-$HOME/.local/share}/matuto/updater/`.
- User services: `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/matuto-manager.service`, `matuto-app-router.service`, `matuto-app-voice-router.service` and `matuto-app-voice-part-*.service`.
- Menu entries: `${XDG_DATA_HOME:-$HOME/.local/share}/applications/matuto.desktop`, `repair-matuto.desktop` and `update-matuto.desktop`.
- Generated router presets: `${XDG_CONFIG_HOME:-$HOME/.config}/ai-agents-sandbox/matuto-manager/router-presets.ini` and `voice-presets.ini`.

After removing those service files, reload the user manager:

```bash
systemctl --user daemon-reload
```

Do not delete the configuration directory, store, login, API key, downloads, build folders or model storage as part of manual cleanup. If uncertain which paths are safe, leave them in place and use a fresh bootstrap or ask the system administrator for help.

## Move from the checkout install

The one-off cutover and conversion scripts are for moving an existing checkout installation to the browser app. Complete this sequence:

1. In the old page, choose **Stop voice** and **Stop**, and let any build or lab run finish.
2. Run `cutover/matuto-cutover` from the checkout. It refuses if old serving, voice, a build or a lab run is still active. It removes only the old manager/router/voice service files, five old commands and old menu entry; it leaves old configuration and state files in place.
3. Run the new bootstrap and its release trust step.
4. Before conversion, prepare the records the importer needs in the new app: configure model storage; adopt and verify each retained model file, sidecar and speech-weight file needed by the old profiles; create the physical card record; and install the required speech executables under Runtime. The converter cannot name files without complete acquisition records, a card ID and the speech executable IDs for converted speech profiles.
5. Run `python3 cutover/matuto-convert --card CARD_ID --out "$HOME/matuto-import.json" --report "$HOME/matuto-conversion-report.txt"`, replacing `CARD_ID` with the card record ID. Supply `--synthesis-executable ID` and `--recognition-executable ID` when converting speech profiles, and `--evidence PATH` if importing run records from the private evidence archive. The script reads old files as data, creates/imports the profile document, and imports old results as browsable legacy evidence; it never runs old files or makes old results qualified. Run `python3 cutover/matuto-convert --help` for custom old-layout paths and options such as `--map` when matching complete acquisitions is ambiguous.
6. After import, finish first-use setup: confirm model storage, register old builds, choose a runtime, then publish desired profiles and requalify them before serving. The pre-conversion records are only the converter's required inputs; the app setup and serving steps still follow the import.
7. Keep the old-file list printed by the cutover. It is a list of individual paths, not folders; use it to review and remove old files by hand only after conversion and import are complete.

The cutover's printed list contains individual files found in these old locations; entries depend on what exists on the host. This is the guide's copy of the locations and known file names the cutover reports:

- `${XDG_CONFIG_HOME:-$HOME/.config}/ai-agents-sandbox/cortex-manager/`: old manager state such as `build-job.json`, `build-job.log`, `build-job.lock`, `integration.json`, `model-root`, `published`, `presets.ini` and `voice-presets.ini`. The old login and store paths are excluded; generated presets are also excluded when they belong to the new install.
- `~/.config/ai-agents-sandbox/cortex-runtime/`: `selection.json`, `builds.json` and `run.lock` when present.
- `${XDG_CONFIG_HOME:-$HOME/.config}/ai-agents-sandbox/cortex-lab/`: old profile and speech-profile files, bundles, defaults and other lab configuration files.
- `${XDG_STATE_HOME:-$HOME/.local/state}/ai-agents-sandbox/cortex-lab/`: old lab result files and their supporting artifacts.

Use the cutover's actual printed paths as the authoritative list; it omits absent files and never prints folders.

The router API key at `~/.config/ai-agents-sandbox/pi-local/api-key` is reused by the new install. The old model-root setting does not configure new model storage; confirm storage in Settings. Re-register old builds and requalify imported candidates. Consumer configuration is owned by the consumer and is never changed by Matuto. Keep this list separate from any cleanup of model weights, builds or consumer files; those are not cutover targets.

## Troubleshooting

- If the manager does not answer, use the **Matuto** app-menu entry first; it performs the address-owner check and can open the error page.
- If the app reports a serving or work blocker, follow the action named on Overview or in Runs & jobs, then retry the requested operation.
- If **Repair** needs a network connection, reconnect and retry it. Repair does not change the installed version.
- If an update is interrupted, the updater or Repair finishes it forward. Do not restore the pre-update snapshot by hand.
- If you cannot tell whether a path is Matuto-owned, do not remove it manually.
