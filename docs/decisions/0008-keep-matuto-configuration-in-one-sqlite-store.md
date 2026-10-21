# Keep Matuto configuration in one SQLite store

- Status: Accepted
- Amends: [0002](0002-manage-the-native-router-in-a-browser.md)

> The browser-app consumer-integration decision amends this one: Matuto owns serving and server authentication and ai-agents-sandbox owns Pi configuration, model selection and thinking preferences, so integration settings are no longer store contents and consumer exports are no longer Matuto state. The store and evidence decisions here stand.

## Question

The browser app is to author model profiles, bundles, cards and its own settings, and to record which configuration the operator qualified. Record 0002 ruled out a database. Should that configuration stay in files, as sourced Bash or as JSON, or move into a database?

## Decision

Matuto keeps all Matuto-owned operator configuration and every qualification record in one SQLite file, mode 600, beside the other Matuto state. That covers model profiles, bundles, cards, the argument catalogue, artifact locations, download origins, the runtime selection, the build registry and publication. The lab, the manager and `wq.py` all read through one Python store module. The Bash lab asks that module and never queries the file, and nothing sources a profile. Python's built-in `sqlite3` module is enough, so the host needs no new package; the `sqlite3` command is optional, for inspecting the file by hand.

Run evidence lives in the store too: each run's record, checks, summary and artifacts, including logs, which are capped and marked when truncated. A run writes to a private temporary directory while it runs and its artifacts move into the store when it ends. Run artifacts and installation-specific findings stay out of the public repository. Legacy runs can be imported for reference but cannot qualify a current configuration.

Two kinds of thing stay files, because another program reads them or another decision fixed them: the router's `presets.ini`, generated from the store; and `login.json`, which `reset-login` deletes. Consumer exports are not Matuto state.

Hand-editing ends. The import and export format is one JSON document holding one or more records, available in the page and as CLI `import` and `export`. Importing an existing ID edits that record under the same rules as the form, and import never carries qualification. `defaults.sh` and the sourced Bash, vLLM and legacy profile formats are removed. A release that changes the schema migrates the store during its update, after copying the file.

This decision replaces sourced Bash profiles with stored records.

## Reasoning

0002 rejected a database because it would duplicate the router's discovery and presets and drift from actual model state. This store does neither. The router still owns discovery, loading, unloading and its console, and `presets.ini` becomes an output of the store rather than a second source beside it.

What changed is that the configuration now has relations. A profile points at a card, a bundle is composed of profiles, and a qualification holds only while its fingerprint matches the served configuration it names. Files would keep those references by convention and check them on every read. The store enforces them, so a card in use cannot vanish and a referenced profile cannot be removed, and it answers "which profiles use this card" without scanning a directory.

The cost was named and accepted. Hand-editing is gone, and import and export replace it for the CLI and for moving individual records. Every release that changes the schema carries a migration. Portable records use the import format rather than executable profile files. One JSON file per record would keep hand-editing and avoid schema migrations, but one store enforces relationships consistently as the app grows.

Once no reader executes a profile, the reason [0004](0004-author-manager-owned-config-in-the-page.md) gave for keeping profile authoring out of the page, that a profile is sourced Bash, no longer holds. That is the revisit condition 0004 named.

## Rejected alternatives

- Keep sourced Bash and have the page write it: a web form would author code the lab later executes, which 0004 already rejected.
- One JSON file per record: keeps hand-editing and needs no migrations, but leaves references and cross-record consistency to checks in every reader.
- Require the `sqlite3` command so the Bash lab can query the store itself: a second reader repeating the validation, and a new host package for no gain.
- A database server: one operator on one machine needs no service to run, secure and back up.

## What would change the answer

Revisit if Matuto stops being one operator on one machine, because a shared or remote store is a different design. Revisit if text editing of configuration becomes a requirement again, because import and export are its only text path.
