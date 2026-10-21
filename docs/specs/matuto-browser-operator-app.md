---
status: active
---

# Matuto browser-first operator app

This specification consolidates the settled browser-app decisions. Decision links below navigate to their requirements in this document or to a local decision record; original discussions and prototype captures remain in the private archive. Where a later decision amends an earlier one, the amendment is stated here and wins.

## Problem Statement

Matuto is installed from a checkout and managed through a capability-URL page that covers ordinary serving and little else. The [action inventory][d138] found that profile authoring, host defaults, qualification runs and acceptance, model acquisition with provenance, llama.cpp build submission, speech and bundle work, settings, Matuto updates, recovery and removal all still need a terminal, hand-edited sourced Bash, or files copied into the repository. Publication does not enforce qualification, the shared runtime lease is not job admission, an empty host cannot reach the console it would use to download a first model, and host evidence lands in a repository that may become public. There is no release, update or rollback path, and no supported way to start, repair or remove Matuto after bootstrap.

The operator wants one coherent same-machine browser app in which every Matuto-owned task after bootstrap can be done without editing files, while the CLI remains available, first use and return both make sense, and changes that touch serving, credentials, evidence or the installation stay explicit and recoverable.

## Solution

Replace the checkout-installed manager with a versioned, signed Matuto release that serves one browser app at the fixed address `http://matuto.localhost:8890/` behind a local username/password login ([login][d139], [password-only amendment][d139a]). All Matuto-owned configuration, qualification records and run evidence move into one SQLite store read through one Python store module ([store][d140], [ADR 0008](../decisions/0008-keep-matuto-configuration-in-one-sqlite-store.md)). Model profiles, bundles and cards are authored in forms; qualification is computed from accepted evidence matching a served-configuration fingerprint ([profiles][d140], [qualification][d141]). One lab runner starts browser and CLI runs as transient systemd user units under one shared admission slot that also covers downloads and builds ([qualification][d141], [journeys][d143]). Matuto acquires verified model files from public Hugging Face repositories or adopts files on disk ([acquisition][d144]), builds official llama.cpp and speech runtimes from fixed recipes into side-by-side installations ([runtime][d145], [recipes][d152], [install probe][d153], [runtime page][d158]), and updates itself forward from public signed releases with an independent updater ([updates][d142], [publishing][d151], [startup and repair][d147]). A one-off cutover moves the operator from the checkout install, preserving model files, tuned profiles and legacy evidence ([migration][d150], [cutover][d147]).

The browser presentation follows the seven approved page decisions — [Overview and serving][d154], [Model files][d155], [Profiles][d156], [Runs & jobs][d157], [Runtime][d158], [Settings][d159], [Login][d160] — joined by the [cross-page navigation and visual decision][d161]: one Quiet studio layout with a light/dark switch, a seven-page list that folds into a menu in narrow windows, a status-only header, and app-wide blocker, confirmation and wording rules. The approved prototypes are throwaway planning fixtures, not code to reuse.

## User Stories

1. As the operator, I want to open Matuto at one fixed, bookmarkable local address and sign in with a username and password I chose, so that I do not depend on a secret URL.
2. As the operator, I want my session to survive manager restarts, updates and reboots for seven days, so that ordinary use does not keep asking me to sign in.
3. As the operator, I want to change my password in the app and reset a forgotten one from the terminal, so that I can recover without editing files.
4. As the operator, I want other local accounts and hostile web pages kept out of the manager and console, so that only I can drive my models.
5. As the operator, I want first use to show the few records Matuto needs — model storage, model files, a runtime and a language profile — and open each where its work is done, so that I can set up without reading a guide.
6. As the operator returning later, I want Overview to show what is serving and what is running and offer Start, so that nothing that uses the card starts behind my back.
7. As the operator, I want to create, edit, duplicate, import, export and remove language profiles, speech profiles, bundles and cards in forms, so that I never hand-edit sourced Bash.
8. As the operator, I want Matuto to tell me which records an edit affects and whether they lose qualification before I save, so that I do not invalidate evidence by accident.
9. As the operator, I want to run verify, smoke, bench, a temporary server, bundle runs, worker qualification and context searches from the browser or the CLI, so that qualification no longer needs terminal procedures.
10. As the operator, I want one job at a time with refusals that name the current work, so that measurements and downloads never contend unexpectedly.
11. As the operator, I want to accept or withdraw qualification in the browser by citing completed runs at the current configuration, so that "qualified" always means evidence I accepted for what is actually served.
12. As the operator, I want published candidates labelled everywhere, so that published never implies qualified.
13. As the operator, I want to start agentic serving, serve voice with a qualified bundle, switch between them and stop serving, each previewing what stops and starts, so that consumers are never interrupted silently.
14. As the operator, I want the native model console reachable only while healthy agentic serving runs, so that a bookmark cannot unload the voice model.
15. As the operator, I want to download verified model files from Hugging Face repositories I allowed, or adopt files already on disk, so that every profile points at files with recorded identity.
16. As the operator, I want downloads to survive closing the tab and to resume or be discarded safely after interruption, so that large transfers are not wasted or left half-visible.
17. As the operator, I want to build official llama.cpp (HIP or Vulkan), Breeze and whisper versions side by side and choose which llama.cpp build and card serve, so that I can try and roll back runtimes without breaking a working one.
18. As the operator, I want to register an existing external build without trusting it, and use it only after consented checks pass, so that unknown code is not run silently.
19. As the operator, I want storage locations, download origins, cards, the argument catalogue, the lab port and my account in one Settings page, so that configuration has one home.
20. As the operator, I want to check for, review and install Matuto updates with explicit consent and a verified database snapshot first, so that an update cannot surprise me or lose my store.
21. As the operator, I want Repair Matuto and Update Matuto app-menu entries that work when the manager does not, so that I can recover without a terminal.
22. As the operator, I want to remove Matuto while keeping my store, weights, builds and credentials, so that a later reinstall picks up where I left off.
23. As the operator, I want Matuto to start at login without using the GPU, so that the app is there when I need it and the card is idle until I start serving.
24. As the operator moving from the checkout install, I want a one-off cutover and conversion that keep my model files, tuned profiles and legacy results, so that requalification costs only a smoke, a bench and one bundle run.
25. As the operator, I want my run evidence kept in the store, downloadable, and out of the repository, so that private host details never reach a public repository.
26. As the operator, I want every page usable by keyboard alone, readable in light and dark mode, and workable in a narrow tiled window, so that the app fits how I actually work.
27. As the operator, I want page-wide blockers in one place, a reason beside each unavailable control, and popup errors that keep my input, so that I always know what to do next.
28. As the operator publishing a release, I want written guidance on when and how to release, a release workflow that builds and signs only after I approve it, a signed release list I can withdraw from, and an install-from-file test before publishing, so that releases stay safe without signing by hand.
29. As a consumer of served models, I want the published aliases and the server API key to keep working across Matuto updates and the cutover, so that my configuration does not break.
30. As the operator, I want the CLI to keep running procedures, listing runs, importing and exporting records, resetting the login, installing from a file and removing Matuto, so that scripts and terminal recovery remain possible.

## Acceptance Criteria

Each criterion is observable behavior. Codes group criteria by area for slicing; the cited decision is authoritative where wording is compressed.

### Access, login and browser protections

- AUTH-1: The manager serves only on loopback at `http://matuto.localhost:8890/` and the console at `http://matuto.localhost:8889/`; neither address carries a secret, both can be bookmarked, and `matuto-manager open` and the desktop entry open the manager address.
- AUTH-2: With no stored login, the address shows Create login (username, password, confirm; only non-empty values required). Submitting saves `~/.config/ai-agents-sandbox/matuto-manager/login.json`, mode 600, holding the username and a salted scrypt hash from Python's standard library, and signs the operator in. There is no lockout or rate limiting.
- AUTH-3: A session lasts 7 days across manager restarts, Matuto updates and reboots. Sign out ends only that browser's session. Changing the password or resetting the login ends every session. Signed out, any address shows the sign-in form and returns to the requested page afterwards; unsaved edits and pending actions are not restored.
- AUTH-4: Account offers Change password only: current password, new password and confirmation; the username is preserved; there is no username-renaming control. A failed change keeps the form and changes nothing.
- AUTH-5: `matuto-manager reset-login` deletes the login and ends every session; the next visit shows Create login. Bootstrap, setup and Repair never touch an existing login.
- AUTH-6: Every mutation, login included, requires an exact Origin match and a CSRF token; requests with a Host outside the allowlist or marked `Sec-Fetch-Site: cross-site` are refused; the manager sends `frame-ancestors 'none'`; the console may be framed only by the manager origin and keeps its mutation allowlist; the router API key is injected server-side and never reaches the browser; the session cookie is HttpOnly, SameSite=Strict and host-only.
- AUTH-7: The capability file, capability-derived hostname, `/auth?cap=` route and capability cookie no longer exist.
- AUTH-8: An open tab notices a failed status check within two seconds, keeps last-known state marked as such, never reports a pending action as successful, and points to the Matuto app-menu entry; when a session is confirmed expired, protected content is removed from the page.

### Store, records and import/export

- STORE-1: All Matuto-owned configuration (profiles, bundles, cards, argument catalogue, artifact locations, download origins, lab port, runtime selection, build registry, publication), qualification records, acceptances, acquisition records, job history and run evidence live in one SQLite file, mode 600, accessed only through one Python store module by the manager, the lab and `wq.py`; the Bash lab asks that module and never opens the file or sources a profile.
- STORE-2: Exactly two kinds of thing remain files: the router's `presets.ini`, generated from the store; and `login.json`. Consumer exports are not Matuto state; ai-agents-sandbox owns them ([consumer settings][d146]).
- STORE-3: Sourced Bash profiles, `defaults.sh`, and the vLLM and legacy profile formats are removed; portable profiles use the JSON import format.
- STORE-4: One JSON document format holds one or more cards, model profiles and bundles referring to each other by ID within the document or against the store. Importing a new ID creates a record; importing an existing ID edits it under the form rules; import never carries qualification and new language profiles and bundles arrive as candidates. Export writes the same format. Page and CLI `import`/`export` share these rules; the CLI has no other authoring commands.
- STORE-5: Save and import refuse malformed records. A missing file, switched build or undetected card makes a record unavailable with a reason, never invalid or silently re-identified. There are no saved drafts.
- STORE-6: The store enforces references: removing a published or referenced record is refused; after an allowed removal its qualification records and runs stay browsable.

### Profiles, bundles, cards and qualification state

- PROF-1: A language profile has an alias, context, thinking, per-tool (server, smoke, bench) arguments, optional mmproj and MTP, and points to a card; a synthesis profile picks a speech executable and weights and points to a card; a recognition profile runs on the CPU with no card. Only language profiles are published. A bundle is one language profile plus synthesis and recognition profiles. A card is a detected physical GPU (never typed) with its VRAM ceiling, default GPU layers and smoke/bench sizes. Every record has a notes field.
- PROF-2: Forms pick model files only from complete acquisition records and executables only from the build registry or installed speech executables; they never accept a typed path or digest.
- PROF-3: An argument row is either an argument-catalogue entry or Custom (one flag and one value, stored as argv, never split or passed through a shell). Custom does not bypass validation: `--rpc`, `--api-key`, `--config`, `--preset`, `--override-tensor`, device conflicts, duplicates and launcher-owned host/port/model reject at save; an option the active build does not advertise warns at save and launch still refuses it. Removing a catalogue entry turns its uses into Custom rows.
- PROF-4: A language profile or bundle is qualified exactly when an accepted qualification record matches the fingerprint of its current served configuration; otherwise it is a candidate, shown as "changed since qualification" when an earlier fingerprint was accepted, and restored to qualified when an edit returns it to an accepted fingerprint. Nobody can type qualification. Synthesis and recognition profiles have no qualification state of their own.
- PROF-5: The served configuration excludes names, notes, aliases, smoke/bench measurement settings, a card's name and VRAM ceiling, and the llama.cpp build. A profile change reaches every bundle using it; a change to a card's physical GPU or default GPU layers reaches its profiles and their bundles; pointing a profile at another card always invalidates it. A different active llama.cpp build only warns that results may differ.
- PROF-6: Saving a change to a served value first shows each changed value old → new, the record's resulting state and every other affected record; cosmetic-only edits save directly.
- PROF-7: A change affecting a live record (published while the router runs, or a member of the running voice bundle) is refused and names Stop serving; Matuto stops nothing itself. A non-live published record saves, stays published, may become a candidate, and has its preset regenerated.
- PROF-8: Duplicate asks for a new stable ID, name and distinct alias before publication, starts without evidence and leaves the original published and unchanged. Two published profiles never share an alias. There is no same-alias swap.
- PROF-9: Publishing a candidate requires a confirmation that names it as not qualified; publishing starts and loads nothing; a published candidate is labelled a candidate everywhere.

### Runs, admission and acceptance

- RUN-1: The browser and CLI start verify, smoke, bench, serve (a loopback temporary server shown with its address, running until Stop), bundle runs, worker qualification (`matuto-lab <profile> wq`) and context searches against one stored record at its current fingerprint. `speech qualify` is removed; `wq selftest` stays a developer command.
- RUN-2: One lab-owned runner starts each run as a transient systemd user unit recorded in the store. A run started in a terminal is visible and stoppable in the browser; the CLI follows output and Ctrl-C cancels. The runner supplies the pseudo-terminal smoke needs.
- RUN-3: One shared admission slot covers runs (including no-card Verify), builds and downloads: a second request is refused, names the current work and is never queued.
- RUN-4: A card run while agentic or voice serving is up is offered only as "Stop serving and run" behind a confirmation that consumers lose the model; serving stays stopped afterwards. Start and Serve voice refuse while a run holds the card; every card run refuses a card held by a llama-server Matuto does not own. These rules hold for browser and CLI alike, at the launchers, including router starts through systemd.
- RUN-5: Stop ends the whole unit gracefully, then forcibly after a bounded wait, and records the run cancelled; a completion that won the race is preserved. Closing the browser or restarting the manager leaves runs going and reconnectable. Logout or reboot ends them and they record interrupted; a run with no recorded end shows interrupted, never passed. Completed WQ rows survive a Stop.
- RUN-6: Progress shows phase, elapsed time, current step and a bounded, redacted log tail; WQ's raw requests and responses stay private during the run and are deleted after it.
- RUN-7: Every run ends completed, errored, cancelled or interrupted and carries checks as PASS, FAIL, ERROR or SKIP. Smoke checks the reply against the exact requested text. The bundle run passes a recognition round trip when non-empty text returns faster than the clip lasts; a bundle that does not fit or runs at or below real time completes with a failed check. Completion grants no qualification.
- RUN-8: The WQ form shows its defaults with the VRAM limit from the card ceiling; changed limits freeze at Start. WQ sizes long-context retrieval at about 88% of the profile's context and records the installed Matuto version as its procedure revision; a run from a dirty checkout is marked and cannot be cited.
- RUN-9: Context search offers only ladder rungs (512K, 384K, 256K, 192K, 128K, 96K, 64K, 48K, 32K) at or below the trained context, starting from the operator's chosen rung, runs temporary copies differing only in context, stops after the first working rung plus a consistency check of the rung below, and names no winner when that check fails. A language rung works when the server loads, whole-card peak VRAM stays under the card ceiling and WQ-06 and WQ-10 pass; a bundle rung when fit, real-time and recognition checks pass. "Set context to N" is an ordinary edit after which the matching rung run is citable without rerunning.
- RUN-10: Acceptance happens only in the browser and cites completed runs at the record's current fingerprint: at least a passed smoke and a completed bench for a language profile, or a passed bundle run for a bundle; these cannot be overridden. Every other cited failed check needs a named exception with a reason; automated results are read-only; runs on another build may be cited with a warning. The acceptance records cited runs, scope, exceptions and reasons, residual risks, date and login username. Errored, cancelled, interrupted and legacy runs are browsable but never citable.
- RUN-11: Withdraw returns the record to candidate, keeps the acceptance in history and stops nothing; a withdrawn voice bundle keeps serving until stopped and its next Serve voice refuses. A run referenced by any acceptance, withdrawn ones included, cannot be deleted.
- RUN-12: Run evidence (record, checks, summary, artifacts, capped logs marked when truncated) moves from a private temporary directory into the store when the run ends, is kept until the operator deletes it, and can be downloaded per run. Nothing is written to the repository.

### Serving, voice and console

- SERVE-1: Overview after setup shows a status headline, Serving (published records with candidate labels, loaded model read-only, runtime build/backend/card, same-machine addresses including voice parts) and Current work (the one running download, build or run with a link to it in Runs & jobs; absent when idle). There is no model-load controller and no consumer configuration.
- SERVE-2: Controls match the state: stopped (Start agentic serving, Serve voice…), healthy or unhealthy agentic (Stop serving…, Restart…, Serve voice…), voice (Stop serving…, Switch to agentic serving…). Refusals name the blocker beside the control and explain it on activation; nothing queues.
- SERVE-3: Start agentic serving acts without confirmation. Stop serving, Restart, Serve voice, Switch and Stop serving from voice confirm their Stops/Starts/Stays consequences with initial focus on the safe choice. Stop serving from voice stops every voice part and starts nothing; Switch stops voice and starts agentic serving with the published profiles; a remaining speech part leaves Stop retryable and Switch refused.
- SERVE-4: Serve voice offers only qualified, available bundles, requires a usable runtime and reachable storage, refuses during a build or a card-holding run, and may coexist with a download. A failed voice start cleans up started parts and restores only the prior workload.
- SERVE-5: Nothing that uses the card starts at login or sign-in; the router's crash restart only restarts serving the operator started.
- SERVE-6: The Model console stays in the page list with a specific unavailability reason and recovery action, and is usable only during healthy agentic serving, never during voice. The console address itself enforces the same gate. The page explains that console loads and consumer requests share one model slot. The console's download and delete operations are refused with pointers to Model files and external file management.

### Model acquisition and storage

- ACQ-1: Downloads are refused until the operator configures model storage in Settings; there is no shipped, inferred or fallback root, and an unconfigured or inaccessible root is named, never replaced. One root supplies the download destination and native discovery; additional artifact locations serve adoption only.
- ACQ-2: Downloads come only from public Hugging Face repositories admitted by configured download origins. The wizard (Prepare → Select files → Review → Acquire) resolves a branch or tag to the full immutable commit and reviews source, revision, every required file, summed size, destination and checksum status before consent. Selection survives Back/Continue and mode switching. Matuto does not guess quantization, compatibility or hardware fit, and never overwrites an existing file.
- ACQ-3: The download job uses `curl` under the shared runner and slot, works without a router, model or GPU selection, may run beside serving (with a contention warning) and voice, and survives closing the tab, signing out and manager restarts; logout or reboot interrupts it without recording success.
- ACQ-4: Progress shows phase, current file and transferred/total bytes. Cancel, failure or interruption keep isolated resumable partials; Resume reuses the pinned request and rechecks retained data; transfer retries are bounded. Space is checked before starting without claiming a reservation; disk exhaustion stops safely and preserves partials. Partial files never appear in discovery or profile selection.
- ACQ-5: Cancel download and Discard partial download are distinct and confirmed. Discard previews only the job's own staging files and size, refuses concurrent writers, rechecks at confirmation, and never removes completed, adopted or pre-existing weights. Moving or deleting completed weights and license handling stay outside Matuto.
- ACQ-6: An acquisition record keeps every required file's source-relative name, size, published checksum when available, computed SHA-256 and verification time; every shard and sidecar is checked. Incomplete sets, wrong sizes and known mismatches stay unusable; "locally hashed; no published checksum" requires explicit consent; a mismatch is never downgraded to unverified success. Accepting replacement bytes creates a new artifact identity.
- ACQ-7: Adoption hashes files in place from a configured artifact location without copying or moving them, needs no download root or origin, labels unverifiable provenance "locally hashed; publisher unverified" and leaves unknown source or revision unknown. An adopted model outside the root reaches serving through its published preset.
- ACQ-8: A full-set Verify action exists on every acquisition record, before any profile exists. Ordinary launches keep checking the first model file and selected sidecars only.
- ACQ-9: Changing a location moves no files, shows affected records, refuses while serving or a job uses it (naming the Stop action), and verifies every required file at the new location before marking artifacts available; identical bytes keep qualification. Model storage and its linked artifact location are visibly one shared directory and are previewed together.
- ACQ-10: A complete record's primary next action is Use in a new profile. Acquisition publishes, qualifies and starts nothing.

### Runtime builds and registry

- RT-1: Fixed recipes build llama.cpp HIP/ROCm, llama.cpp Vulkan, Breeze synthesis (Vulkan) and whisper recognition (CPU) from canonical official origins in clean source separate from any operator checkout. No fork URL, patch, arbitrary command or free-form CMake option is accepted.
- RT-2: Check versions lists only refs the official source returned and accepts an explicit official branch or commit; a failed check reports unknown, never "latest" or "up to date". The full commit is resolved and shown with the fixed recipe and physical target before Build; changing inputs clears the pin. Nothing updates on a schedule.
- RT-3: The HIP target is chosen from detected physical cards, defaulting to the active card, and the first build needs no existing selection; Vulkan has no gfx target. Missing detectable prerequisites are named; Matuto never installs drivers, SDKs, compilers or packages.
- RT-4: All recipes build with `-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON`; llama.cpp HIP, llama.cpp Vulkan and whisper.cpp also with `-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH=$ORIGIN`. An installation is the whole staged build tree renamed atomically to a unique final path; it never overwrites an existing installation, including a rebuild of the same commit.
- RT-5: The installation check, run from an unrelated working directory after the rename, verifies the source and submodule commits, expected executables and linked files, that every project library resolves inside the installation, and rejects any server or library whose RUNPATH/RPATH has an empty entry or an entry not starting with `$ORIGIN`. It runs `--version`/`--help`, reads whisper capabilities from help text rather than exit status, tolerates the RADV non-conformance warning, and never treats device enumeration or startup as GPU or model proof.
- RT-6: Builds use the shared slot and exclude voice; ordinary serving may continue under a CPU/RAM/disk contention warning; nothing stops or restarts automatically. Retry is a new attempt at the same commit; there is no automatic resume. Cancel stops the whole job gracefully then forcibly and preserves a completion that won. Only checked complete installations are discoverable; partial or unverified staging never appears installed. Job history (pinned request, phase, capped redacted log, outcome, provenance and output digests) is kept until deleted; provenance needed by installations or qualification survives log deletion.
- RT-7: A completed build registers, selects, qualifies and changes nothing. Build, Inspect, Register and Use are separate. Register records a stable ID and editable label; an ID never moves to another tree. Forget removes only the registration, refuses the active entry even when its files are missing, and leaves files and evidence for later rediscovery. There is no Clear selection.
- RT-8: First selection, or a change of backend or card, uses explicit runtime selection naming the build and the active card from Settings › Cards; Use switches the build keeping backend and card and refuses a mismatched HIP target. Both require stopped affected workloads, validate before saving, keep the old selection on failure, and neither starts serving nor falls back. Runtime selection links to Settings › Cards when no card exists.
- RT-9: An existing external tree may be registered as unavailable metadata that runs and certifies nothing, with unknown provenance kept unknown. Executable inspection needs explicit trust consent; known unsafe loader paths are reported before executable probes; incomplete or unsafe required checks refuse Use and selection with no warning-only path; a corrected external installation becomes eligible after successful checks without being relabelled official.
- RT-10: Installing or updating a speech runtime adds an executable to the Speech profiles picker without a second registry; it changes no profile and grants no qualification. Selecting another speech executable follows the served-configuration edit rules (live refusal naming Stop serving, old → new preview, affected bundles, restoration or new bundle evidence before voice).

### Settings and maintenance

- SET-1: Settings has sections Storage & artifacts, Download origins, Cards, Argument catalogue, Lab port, Account and Maintenance. First configurations and additions (Save model storage, Add origin, Add card) act directly; buttons that open a consequence preview are labelled Review…, and final commits name their outcome.
- SET-2: Card edits preview only changed values (an unchanged form offers no Save), name affected profiles and bundles for served-value changes, and keep qualification for measurement-only changes; a card in use by live serving refuses edits naming Stop serving. Cards offers an Add card form picking a detected GPU when none exists.
- SET-3: The lab port keeps loopback and refuses changes while a lab lease holds it. Failed confirmations stay in the same popup with context and draft kept; a path saved as unavailable reports that partial outcome.
- SET-4: Remove Matuto lives in Maintenance and in the CLI. It refuses while serving, a run, build, download or update is running, naming each Stop action; previews removed versus kept state; removes everything except the manager and updater first, shows the kept list with paths and sizes, then disables login start and deletes the manager and updater; the result and final-step-failure views have no working Settings menu, and final-step failure directs terminal completion. It keeps the store, the pre-update snapshot, login and sessions, the router API key, partial downloads, build source and staging, weights, builds and speech executables, and never touches consumer configuration.
- SET-5: A later bootstrap reuses kept data; a newer reinstalled release snapshots and migrates the store; a kept store newer than the reinstalled release makes bootstrap refuse.

### Matuto releases and updates

- UPD-1: Check for updates and Install are separate consented actions. With no public release feed, offline, a failed check or a withdrawn offer, the app stays usable and shows an honest no-offer or failure state with the last successful check; an interrupted installation is never reported successful.
- UPD-2: An offer shows installed and offered versions, notes, download size, prerequisites and database migration. Install refuses until the operator has stopped serving and finished or cancelled every job, naming each blocker; it never stops work itself; missing OS prerequisites block it; new browser, CLI and service-start work is blocked during installation.
- UPD-3: Before changing anything the updater checks the release signature against the key trusted at bootstrap and the release file against its signed digest, checks compatibility and space, stops writers, saves a consistent SQLite snapshot with the backup API and verifies it, refusing the update if any check fails. It keeps one labelled pre-update snapshot (date, version, schema; size and location shown), replaces it only after its replacement verifies, and preserves the original snapshot across a retried update.
- UPD-4: A failed staged migration leaves the original database unchanged; before activation a failure leaves the old release and database intact; once activation starts, the updater finishes forward or stays in maintenance with an explicit error, reconciles interruption before admitting writers, and never runs old code against a new schema or restores the snapshot automatically.
- UPD-5: Success means the expected manager version opens its database and serves the fixed address with login and sessions intact; only the manager restarts and serving stays stopped. API credentials are preserved, not regenerated. There is no downgrade, automatic rollback or snapshot restore command, and no root service, lingering or remote listener.
- UPD-6: One release covers the manager, browser UI, lab, shared modules, voice tooling and the installed WQ procedure; commands and services never mix releases. An update never changes llama.cpp or speech runtimes, weights or OS packages.
- UPD-7: The signed release list carries every release's version, source commit, size, SHA-256, notes, prerequisites, database-change flag and withdrawn mark, plus a number that only rises; a lower number is treated as a stale copy (no news), so a replayed list cannot make a withdrawn release installable. Signatures are OpenSSH signatures checked with `ssh-keygen`.
- UPD-8: The bootstrap trust step records and displays the release key fingerprint; only rerunning it changes the trusted key; the updater never accepts a key from a release.
- UPD-9: A terminal command installs a release file and signed list from disk with the same signature and digest checks.

### Startup, repair and recovery

- START-1: Bootstrap enables the manager's user service so it starts at every login to the operator's account, SSH included; it uses no GPU, listens only on loopback, and Matuto never enables lingering. `systemctl --user disable matuto-manager.service` turns login start off and Repair and updates preserve that choice.
- START-2: The Matuto app-menu entry: during an update, opens the error page with update progress; otherwise clears the manager's failed state and starts it, restarts it once if it does not answer within a short wait, opens the error page if it still fails, and refuses to open an address held by another account's process. After several quick crashes systemd stops retrying until the entry is used.
- START-3: The error page is a private local file with no buttons, naming the cause, a bounded redacted log excerpt, and what Repair Matuto and Update Matuto would do; using the entry again rewrites it. A manager failure does not stop serving; a new tab or bookmark shows the browser's connection error; Matuto sends no desktop notification.
- START-4: Repair (menu entry, app action and CLI) never changes the installed version and, in order: finishes an interrupted update forward; otherwise checks installed files against the release's signed per-file checksums and reinstalls the same version if any are missing or altered; regenerates service files (preserving login start), menu entries and router presets from the store, restores Matuto folder permissions and creates the router API key only if missing; restarts only the manager and checks it opens its database and serves the address with login intact. Outside the first step it never edits the store, and it never touches login, sessions, runtime selection, registry, weights, builds, runs or consumer files, stops serving or cancels jobs. It refuses unsafe files it does not own and says when a reinstall needs the network.
- START-5: When the installed version cannot start and a newer release is published, the error page names it with its notes, size, prerequisites, migration and exactly what must stop; Update Matuto then installs it, snapshot first, refusing and rewriting the page if running work changed; with nothing on offer it opens the app's update screen.
- START-6: The updater lives outside the release folders and all three menu entries run it; a newer updater from a release replaces it only after that release's manager has started and served the address with login intact; a broken updater is fixed only by rerunning bootstrap.

### Cutover from the checkout install

- CUT-1: A one-off cutover script and a one-off conversion script ship together outside the permanent bootstrap and are deleted from the repository after the operator's cutover. The new install contains no knowledge of the old layout.
- CUT-2: The cutover refuses while the old router or voice serving runs, an old build is in progress or a lab run holds the old runtime lease, naming the old page's Stop actions; otherwise it stops the old manager, deletes the old manager, router, voice router and voice-part units, the five old commands in `~/.local/bin` and the old menu entry, reloads systemd, and prints every old file it left in place (files, never folders). Rerunning it, or bootstrap, finishes whatever is not done.
- CUT-3: The conversion reads old profile files as data, never runs old code, and emits import JSON; imported language profiles and bundles arrive as candidates with their context sizes and other settings kept. Old results are imported once as legacy runs: browsable, without a fingerprint, never citable. Run records remain in the private archive, outside the public repository; guides and decisions keep sanitized research findings without private evidence links.
- CUT-4: The new install reuses `~/.config/ai-agents-sandbox/pi-local/api-key` so consumers keep working; nothing else new reads old files; the old model-root setting never becomes the confirmed model storage; the old runtime selection and registry are not carried over, and build folders stay discoverable for re-registration; old consumer exports are untouched.
- CUT-5: After the cutover, Overview shows only the remaining setup tasks (set model storage; register and select a build) and requalification needs only a passed smoke and a completed bench per language profile and one passed bundle run, followed by browser acceptance.

### Navigation, shared status and accessibility

- UI-1: One Quiet studio layout throughout: neutral/oxblood light mode, original Quiet studio dark mode, and a sun/moon switch (role switch, accessible name "Dark mode") whose choice is remembered in that browser only, not the account; a first visit starts light.
- UI-2: The page list is Overview, Model files, Profiles, Runs & jobs, Runtime, Settings, Model console, with "needs agentic serving" under Model console when unusable. Below about 720 px it folds into a menu button (☰, ✕ while open, accessible name "Pages", expanded state exposed); choosing a page closes it and focuses the page heading; Escape closes it and returns focus to the button.
- UI-3: Profiles, Runtime and Settings show their sections as a tab row under the page heading beside the page list (Profiles: Language profiles, Speech profiles, Bundles, Import / export; Runtime: llama.cpp builds, Install a version, Speech runtimes, Build activity; Settings as in SET-1).
- UI-4: Every signed-in page's header shows the serving state (or "Card held by a run"), the one running job or run (or "Nothing running"), and at most one repair item, each linking to where it is handled, and no control that stops or starts work. Without manager contact every item is marked "last known" and stops being a link.
- UI-5: Page-wide blockers, warnings and repair notices share one region under the heading and section tabs; a single unavailable control states its reason beside it; popup failures appear as one focused alert below the popup title with input kept and never leak into another popup.
- UI-6: Only actions that stop or replace something preview Stops/Starts/Stays with initial focus on the safe choice; first-time saves, additions, Register and Start agentic serving act directly; candidate publication, record-edit, Discard partial download and Remove Matuto keep their own approved reviews.
- UI-7: Wording: other pages refer to Overview's voice control as Stop serving (there is no Stop voice control); "Nothing running" replaces "No job running"; Review… labels only preview-opening controls; Sign out (in Settings › Account) everywhere; Change password, never Change login; controls that leave a page name their destination except the approved labels Use in a new profile and Qualify….
- UI-8: The four setup tasks — Set model storage, Get model files, Prepare the runtime, Create or import a language profile — are unnumbered, open Settings › Storage & artifacts, the Model files wizard, Runtime's install form (or build list when builds were found) and Profiles › Language profiles, move under Already configured when done, and leave Overview with it once all four records exist; a later failure shows a repair notice, never setup again. Publication lives in Profiles and Serving, not setup.
- UI-9: Use in a new profile before a runtime or card exists opens Profiles with a notice naming each missing prerequisite with its link before any input.
- UI-10: The first Tab reaches a skip link; changing page focuses the page heading; every Tab stop shows a visible focus outline of at least 3:1 contrast; popups open on the safe choice and Escape returns focus to their opener; text meets 4.5:1 in both modes; no page or popup overflows horizontally at 390, 800, 1024 or 1280 px; coarse pointers get 44 px targets.

### Release guidance

- REL-1: `docs/RELEASING.md` ships with the updater and documents the public release contents, version format, verification and trust rules, pre-publish testing, draft publication, and withdrawal behavior. Installation-specific results and maintainer key-custody, account-setup, and recovery procedures stay private. A manually started GitHub workflow builds the release file, extends the published signed list and signs it after approval, then creates a draft; it never publishes. Withdrawal remains a separately authorized maintainer action ([CI releases][r432]).
- REL-2: Published release files, withdrawn ones included, are never replaced or deleted, and each release file carries its signed per-file checksum list for Repair.

## Implementation Decisions

- **Store module.** One Python module owns the SQLite schema, migrations, reference enforcement, fingerprint computation, qualification state, import/export validation and generated router files; the manager, lab and `wq.py` call it, and the Bash lab reaches it through a Python entry point. Only Python's standard library is used ([store][d140], [ADR 0008](../decisions/0008-keep-matuto-configuration-in-one-sqlite-store.md)).
- **Fingerprint.** A digest over the served configuration as defined in PROF-5, using required files' digest identity rather than paths or record IDs, so relocation of identical bytes keeps qualification ([profiles][d140], [acquisition][d144]).
- **Runner and slot.** One lab-owned runner launches every run, build and download as a transient systemd user unit, records it in the store, and owns the single admission slot plus the card-holding lease checked by every launcher, including the router's systemd start path ([qualification][d141], [runtime][d145], [journeys][d143]).
- **Router outputs.** `presets.ini` is regenerated from the store on publication changes and record edits; adopted models outside the root are served through their preset ([acquisition][d144]).
- **HTTP surface.** The manager serves the app, its JSON API and the console proxy behind the protections in AUTH-6; the console address applies the serving gate itself ([login][d139], [Overview][d154]).
- **CLI surface.** Kept: run procedures, follow and cancel runs, list runs, `import`, `export`, `reset-login`, Repair, remove, install-from-file, `open`. Not offered: acceptance and any other authoring command ([qualification][d141], [store][d140], [startup][d147], [publishing][d151]).
- **Updater.** A same-user program installed outside release folders, run by the Matuto, Repair Matuto and Update Matuto menu entries and by the app's Install action; it records progress durably and survives the manager stopping ([updates][d142], [startup][d147]).
- **Release artifacts.** A release file built from an explicit list plus a signed release list per UPD-7; GitHub releases of the Matuto repository once it is public; the latest-release address is fixed by the trust step ([publishing][d151]).
- **Recipes.** The four fixed recipes, examined pins and prerequisites in the [recipe research][d152], with the RPATH options and installation-check rule from the [install probe][d153]. Launch contracts pass absolute paths (or disable) for whisper `--public` and Breeze `--voices-dir`, and always pass an explicit device because both backends list the integrated GPU ([install probe][d153]).
- **Decision records to write during delivery.** Amend [0005](../decisions/0005-embed-model-console-with-manager-only-framing.md): its primary protection moves from the secret hostname to the login plus Host, Origin and Sec-Fetch-Site guards ([login][d139]). Record that the browser app supersedes the narrower controller in [0002](../decisions/0002-manage-the-native-router-in-a-browser.md) and [0004](../decisions/0004-author-manager-owned-config-in-the-page.md) — form authoring, browser runs, acquisition ownership with console download/delete refused, Custom arguments — while 0002's rule that phone or network access needs its own authentication stays ([store][d140], [acquisition][d144], [login][d139]). Retire 0006's "profile one / profile two" wording ([profiles][d140]). Amend [0008](../decisions/0008-keep-matuto-configuration-in-one-sqlite-store.md) for the consumer-settings decision: no integration settings among the store's contents and no consumer exports as Matuto state ([consumer settings][d146]).
- **Glossary.** Add job, setup task, repair notice, current work, runtime selection, repair, update, error page, updater and login start to `CONTEXT.md`, alongside the existing profile, qualification and run terms ([navigation][d161], [startup][d147]).
- **Guides.** `docs/MATUTO_MANAGER.md` and `docs/MATUTO_LAB.md` are rewritten for the app: the Setup / Repair description, the no-autostart statement and the terminal removal recipe are replaced ([startup][d147]); the lab Promotion section, WQ's report-editing decision step, `speech qualify` and the speech-only summary schema go ([qualification][d141]); the migration section keeps the cutover's printed old-file list ([cutover][d147]).
- **Presentation.** Build to the approved page decisions and the [navigation decision][d161]; the approved shared-shell prototype and page captures remain private reference fixtures, not implementation.

## Testing Decisions

- Test public behavior at the highest practical seam: the store module's Python API for records, fingerprints, qualification state, references and import/export; the manager's HTTP interface for authentication, protections, admission, serving transitions and page data, extending the existing `agents/tests/matuto-manager-regression.sh` pattern of fake `systemctl`, fake router and fixture runtimes on `PATH`; the page's own script for browser-side state and rendering, extending `agents/tests/page-behaviour.cjs`.
- Runner, slot and lease rules are tested through the CLI and HTTP entry points with fake units, including Stop races, interruption and refusals naming current work, rather than through internal functions.
- The updater's safety tests run on every pull request once the updater exists, as the [publishing decision][d151] requires: pre-update snapshot, writer exclusion, interrupted update followed by forward repair, bad signature, wrong digest and insufficient disk space; plus stale-list and withdrawn-release handling.
- Cutover and conversion are one-off: verify them with disposable, commit-pinned evidence on a copy of the old layout and on the operator's host, not with retained tests, since the scripts are deleted afterwards.
- Rendered-browser checks (keyboard-only walks, focus outline, contrast, overflow at 390/800/1024/1280 px in both modes) are disposable headless-browser probes at the candidate, following the [navigation requirements][d161]; `validate.sh` gains no browser dependency unless CI provisions one.
- Recipe builds and installation checks are host-bound: unit-test recipe argv and RUNPATH rejection with fixture trees, and leave real builds to target-host validation.
- Prior art: `agents/tests/runtime_fixture.py`, `runtime_abi_fixture.c` and `speech_fixture.c` for fake runtimes; `wq.sh selftest` for the WQ procedure; [docs/testing/physical-host-acceptance.md](../testing/physical-host-acceptance.md) for host evidence.

## Validation

- `./validate.sh` passes at every candidate, including new store, HTTP, page and updater tests; CI's `validate` workflow is green at the pull-request head.
- Disposable headless-browser walks of the four setup tasks, a qualification and acceptance, voice, an update with blockers, storage repair and lost contact pass at 390 and 1280 px in both modes, with no overflow, unnamed control, sub-4.5:1 text or invisible focus outline.
- Target-host evidence for the installation under qualification, recorded under the physical-host acceptance procedure and kept out of the repository: login start at graphical and SSH login; systemd retry limit and reset; the three menu entries in the launcher; the error page opening in the operator's browser (Flatpak/Snap browsers may not); the address-owner check; all four recipes built and checked in place, including HIP rebuilt with the RPATH fix; a model-backed agentic start and a voice start; a download, resume and discard; cutover and conversion end to end with requalification.
- Release validation: build, sign and install a release from disk on a test machine separate from the serving machine before publishing; once the repository is public, Check for updates finds it and an update succeeds with the snapshot verified.
- Human browser check of the real app's keyboard-only, narrow-window and light/dark behavior on the operator's browser and fonts.

## Assumptions

- One operator on one machine; the Linux account is the trust boundary ([login][d139]). Impact: no multi-user or remote design.
- Target platforms are the documented Linux distributions with drivers, SDKs and compilers installed by bootstrap or the operator ([runtime][d145], [install probe][d153]). Impact: Ubuntu-only defects may surface after a release, as [publishing][d151] accepts.
- Current Chromium- and Firefox-based browsers send `Sec-Fetch-Site`; browsers that omit it lose that layer only ([login][d139]).
- The Matuto repository becomes public on GitHub before the first public release; until then Check for updates finds nothing ([publishing][d151]).
- The upstream llama.cpp console renders Matuto's refusal of its download and delete operations readably; delivery verifies this ([Overview][d154], [acquisition][d144]).

## Unresolved Material Decisions

None. Two items stay conditional and outside this specification: voice-client discovery of the speech addresses becomes a separate provider-neutral Matuto API decision only if the ai-agents-sandbox consumer work finds the information missing ([consumer settings][d146], [navigation][d161]); making the repository public, including any history rewrite of committed host evidence, is a separate repository decision ([scope][map], [publishing][d151]).

## Relevant Constraints

- Security: loopback only; login plus Origin, CSRF, Host and Sec-Fetch-Site guards; the manager is unframeable and the console framable only by it; the router API key never reaches the browser; no root service, lingering or remote listener; third-party build and inspection code runs as the operator only after explicit consent where the decisions require it; the release key never enters agent-mounted paths or unapproved CI jobs, and on GitHub only the approved signing job reads it ([login][d139], [runtime page][d158], [publishing][d151], [CI releases][r432]).
- Accepted risks: the first visitor to an empty Create login form sets the login; other local accounts can guess passwords without a rate limit; processes running as the operator can drive the manager; while the manager is down another account could bind its address, which the Matuto entry detects and a bookmark cannot ([login][d139], [startup][d147]).
- Privacy: run evidence and WQ data stay in the private store or temporary directories, logs are redacted and capped, and nothing is written to the repository ([qualification][d141]).
- Compatibility: Python standard library only for the store; `ssh-keygen` for signatures; consumers keep the router API key path and published aliases ([store][d140], [publishing][d151], [cutover][d147]).

## Risks and Mitigations

- A schema migration or activation failure could leave Matuto unavailable: snapshot first, staged migration, forward repair through the independent updater, and the updater safety tests on every pull request ([updates][d142], [startup][d147]).
- A stolen release key can sign releases until the trust step is rerun everywhere: restrict signing authority to approved operations, keep custody and recovery procedures private, and require explicit trust replacement ([publishing][d151], [CI releases][r432]).
- Launch checks verify only the first model file and sidecars, so a later shard alteration can escape them: the full-set Verify action and recorded verification time ([acquisition][d144]).
- Contention from downloads or builds can slow serving: visible warnings, and runs and voice excluded from builds ([acquisition][d144], [runtime][d145]).
- Older external builds may fail the RUNPATH and consent gates: they stay registered but unavailable until repaired or rebuilt ([runtime page][d158]).
- Cross-page journeys 2–8 were walked only by automated probes during planning: the validation section's browser walks and the human browser check cover the real app ([navigation][d161]).
- Withdrawn or stale releases could be replayed: the monotonic signed list number and withdrawn marks ([publishing][d151]).

## Out of Scope

- Phone or network access, which needs its own authentication design ([scope][map], [login][d139]).
- One-time bootstrap and installation of system packages, GPU drivers and ROCm/Vulkan runtimes ([scope][map]).
- Serving on several cards at once ([profiles][d140]).
- Matuto downgrades, automatic rollback and snapshot restore ([updates][d142]).
- License handling and moving or deleting completed model weights ([acquisition][d144]).
- Pi-specific configuration, consumer model selection and thinking preferences, owned by ai-agents-sandbox ([consumer settings][d146]).
- Rewriting git history to remove committed host evidence, and making the repository public ([scope][map]).
- Voice-client discovery of speech addresses, conditional as stated above ([consumer settings][d146]).
- Release schedules and automatic publishing ([publishing][d151], [CI releases][r432]).

## Further Notes

- Decision index, in map order: [action inventory][d138]; [login][d139] and [password-only amendment][d139a]; [profile authoring and invalidation][d140]; [browser runs and evidence][d141]; [updates and rollback][d142]; [journey prototype][d143]; [acquisition and storage][d144]; [runtime builds and registry][d145]; [recipe research][d152]; [migration][d150]; [startup, repair and removal][d147]; [release publishing][d151]; [install probe][d153]; [Login page][d160]; [Overview page][d154]; [Model files page][d155]; [Profiles page][d156]; [Runs & jobs page][d157]; [Runtime page][d158]; [Settings page][d159]; [navigation and visual direction][d161].
- Earlier resolutions superseded in part: the profile decision's same-alias duplicate swap is replaced by the distinct-alias rule ([Profiles][d156]); the login decision's Change login and Log out wording by Change password and Sign out ([amendment][d139a], [navigation][d161]); the rough journey storyboard's Stop voice/automatic return by the Overview transitions ([Overview][d154]); the publishing decision's manual releases by approval-gated GitHub CI signing ([CI releases][r432]).
- Approved prototype captures and throwaway prototype sources remain in the private archive and must not be merged.

[map]: #out-of-scope
[d138]: #problem-statement
[d139]: #access-login-and-browser-protections
[d139a]: #access-login-and-browser-protections
[d140]: #profiles-bundles-cards-and-qualification-state
[d141]: #runs-admission-and-acceptance
[d142]: #matuto-releases-and-updates
[d143]: #navigation-shared-status-and-accessibility
[d144]: #model-acquisition-and-storage
[d145]: #runtime-builds-and-registry
[d146]: ../decisions/0008-keep-matuto-configuration-in-one-sqlite-store.md
[d147]: #startup-repair-and-recovery
[d150]: #cutover-from-the-checkout-install
[d151]: #release-guidance
[d152]: #runtime-builds-and-registry
[d153]: #runtime-builds-and-registry
[d154]: #serving-voice-and-console
[d155]: #model-acquisition-and-storage
[d156]: #profiles-bundles-cards-and-qualification-state
[d157]: #runs-admission-and-acceptance
[d158]: #runtime-builds-and-registry
[d159]: #settings-and-maintenance
[d160]: #access-login-and-browser-protections
[d161]: #navigation-shared-status-and-accessibility
[vis]: #navigation-shared-status-and-accessibility
[r432]: ../RELEASING.md
