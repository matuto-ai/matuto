# Releasing Matuto

Matuto distributes signed releases through [GitHub Releases](https://github.com/matuto-ai/matuto/releases). The release workflow builds a draft; publication requires maintainer approval. Signing-key custody, account configuration, and recovery procedures are maintained privately, not in this repository.

## Release contract

A release contains `agents/`, `wq.py`, `wq.sh`, `LICENSE`, and the operator guides from one source commit. `RELEASE.json` records each payload file's SHA-256. The release builder refuses `docs/evidence/`, which must never be published. Host records, credentials, and private operational notes do not belong in a release.

The release consists of three files: `matuto-<version>.release`, `release-list.json`, and `release-list.json.sig`. The signed list records each version's source commit, size, digest, notes, prerequisites, database-change flag, and withdrawal status. Signatures use OpenSSH's `matuto-release` namespace. The updater verifies the list against the key trusted at bootstrap, then checks the release bytes against their signed digest.

Use `MAJOR.MINOR.PATCH` without leading zeros. Release versions and signed-list numbers only increase, including after an unpublished candidate fails testing. Release notes describe user-visible changes, new prerequisites, and database changes; they contain no private operational details.

## Publication checklist

1. Select a green `main` commit, including passing updater safety tests, and prepare the version and release notes.
2. Run the release workflow on that commit and inspect the proposed signed list before approving signing.
3. Test the draft on a non-production installation. Verify signature and digest checks, startup, login, profiles, settings, and update recovery. A fresh installation must also complete bootstrap with the candidate's public key.
4. Inspect the release contents for private material. Publish the draft only after the checks pass, keeping it designated as the latest release. A failed candidate needs a new version.

Publishing installs nothing on an existing machine. Updates require the user's explicit consent and a verified pre-update snapshot. A release test establishes only the operating-system and hardware combinations actually exercised; record gaps rather than claiming broader coverage.

Never replace or delete a published release file, even after withdrawal. Repair needs the original bytes to reinstall that version.

## Withdrawal

A withdrawal publishes a newly signed list with a higher list number and the affected version marked `withdrawn: true`. It may be a list-only release without a new application archive. Keep it designated as the latest release so clients see the withdrawal.

Withdrawal blocks new installs of that version. It does not change machines already running it; recovery requires a newer release. The updater does not downgrade or restore a database snapshot automatically. Private maintainer procedures govern authorization and signing of withdrawals.

## Verify the trusted key

The public key is `release-key.pub`; its fingerprint is published in the [installation instructions](../README.md#install). Compare the bootstrap prompt with that published fingerprint before accepting it.

A release cannot change its own trust key. Replacing the trusted key requires an explicit bootstrap trust step on each installation, after checking the replacement fingerprint through a trusted channel. A machine that still trusts a compromised key remains at risk until that trust is replaced.
