# Matuto

Host-side tooling that qualifies local models against the GPU they run on and serves the qualified ones to consumers. These terms name what the operator configures, qualifies and serves.

## Profiles and serving

**Model profile**:
The served configuration of one model, of kind language, synthesis or recognition.
_Avoid_: lab profile, preset

**Language profile**:
A model profile for a language model, and the only kind published to the router.
_Avoid_: agentic profile

**Synthesis profile**:
A model profile for a text-to-speech runtime, placed on a card.
_Avoid_: speech part

**Recognition profile**:
A model profile for a speech-recognition runtime, which runs on the CPU and has no card.
_Avoid_: speech part

**Bundle**:
One language profile plus the synthesis and recognition profiles that serve voice with it, qualified as a unit.
_Avoid_: profile two

**Card**:
One physical GPU on the host and the limits Matuto applies to it.
_Avoid_: card profile, device profile, GPU profile

**Runtime selection**:
The saved choice of the llama.cpp build and the active card that serve, with its backend. It changes only by explicit runtime selection or Use, and a completed build never changes it.

**Active card**:
The card the runtime selection serves on; one card serves at a time.

**Published**:
A language profile present in the router's catalogue, so consumers can select it.

**Live**:
A record running services depend on: published while the router runs, or part of the running bundle.

**Agentic serving**:
The router serving published language profiles to consumers.
_Avoid_: profile one

**Voice serving**:
The services of one bundle running in place of the router.
_Avoid_: profile two

## Qualification

**Served configuration**:
The values that decide what a profile, bundle or card launches, excluding names, notes, aliases, measurement settings and the llama.cpp build.

**Fingerprint**:
The digest of a served configuration.

**Qualification record**:
A fingerprint, the runs gathered against it and the operator's acceptance.

**Qualified**:
A language profile or bundle whose current fingerprint matches an accepted qualification record. Synthesis and recognition profiles are qualified only as bundle members.
_Avoid_: qualification state, qualification label

**Candidate**:
Any language profile or bundle that is not qualified.
_Avoid_: experimental, unqualified

**Run**:
One execution of a qualification procedure against one record at one fingerprint. It ends completed, errored, cancelled or interrupted.
_Avoid_: job, attempt

**Check**:
One rule a run evaluates, reported as PASS, FAIL, ERROR or SKIP.
_Avoid_: test, row

**Acceptance**:
The operator's decision that cited completed runs qualify a record at its fingerprint, with the scope accepted and any named exceptions.
_Avoid_: owner decision, approval, promotion

**Named exception**:
A failed check the operator accepts, with its reason, as part of an acceptance.

**Context search**:
A run that tries a record at decreasing context sizes from a chosen starting size to find the largest that holds.
_Avoid_: context sweep, fit search

**Legacy run**:
Evidence recorded before the store existed: browsable, without a fingerprint, and never cited by an acceptance.

## Overview, jobs and setup

**Job**:
A download or a build, admitted under the same runner and admission slot as runs. A run is not a job.

**Current work**:
The one running download, build or run, shown on Overview with a link to it in Runs & jobs and absent when idle. The header shows it or "Nothing running".

**Setup task**:
One of the four first-use tasks on Overview — Set model storage, Get model files, Prepare the runtime, Create or import a language profile — each opening where its work is done. Finished tasks move under Already configured and the list leaves Overview once all four records exist. Publication is not a setup task.

**Repair notice**:
What a later failure of something already configured shows, never the setup tasks again. It appears in the shared notice region of the affected page and as the header's one repair item, linking to where it is repaired.

## Startup, repair and updates

**Login start**:
The manager's user service starting at every login to the operator's account, SSH included, using no GPU and without lingering. bootstrap enables it, `systemctl --user disable matuto-manager.service` turns it off, and Repair and updates preserve that choice.

**Repair**:
Version-preserving and forward-only. The Repair Matuto menu entry, the app's Repair action and the CLI finish an interrupted update forward, otherwise reinstall the same version when installed files are missing or altered, regenerate generated files and restart only the manager. It never changes the installed version.
_Avoid_: Setup / Repair

**Update**:
Installing a newer Matuto release with explicit consent and a verified pre-update snapshot first, through Check for updates and Install, or through the Update Matuto menu entry when the installed version cannot start. It is distinct from repair.

**Error page**:
The private local file with no buttons that the Matuto app-menu entry opens when the manager does not start or answer, or with an update's progress during an update. It names the cause, shows a bounded redacted log excerpt, says what Repair Matuto and Update Matuto would do, and is rewritten each time the entry is used.

**Updater**:
The same-user program installed outside the release folders that the Matuto, Repair Matuto and Update Matuto menu entries and the app's Install action run. A newer one from a release replaces it only after that release's manager has started and served the address with login intact, and a broken one is fixed only by rerunning bootstrap.

## Settings

**Argument catalogue**:
The operator-maintained list of launch arguments offered in profile forms.
_Avoid_: allowlist

**Custom argument**:
A launch argument typed freely instead of chosen from the argument catalogue.

**Artifact location**:
A directory Matuto may pick model files and speech runtimes from.
_Avoid_: source

**Download origin**:
An upstream repository or organisation that acquisition may fetch from.
_Avoid_: source
