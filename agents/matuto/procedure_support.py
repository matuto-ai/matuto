"""Shared setup and evidence writing for the name-discovered run procedures."""

import json
import os
import tempfile
from pathlib import Path

from matuto import progress, store
from matuto import store_lab


def setup(snapshot, record, pinned_spec=None):
    """Get the current or admitted launch spec and private run environment."""
    if record["kind"] != "language":
        raise store.StoreError("verify, smoke, bench, and WQ require a stored language profile.")
    identifier = record.get("profile_id") or record.get("bundle_id")
    record = dict(record, id=identifier, fingerprint=store.fingerprint(snapshot, record))
    spec = (store_lab._read_spec(record["kind"], identifier) if pinned_spec is None
            else pinned_spec)
    if (not isinstance(spec, dict) or not isinstance(spec.get("record"), dict)
            or spec["record"].get("id") != identifier
            or spec["record"].get("fingerprint") != record["fingerprint"]):
        raise store.StoreError("the stored record changed after runner admission; refusing to execute")
    home = tempfile.TemporaryDirectory(prefix="matuto-run-")
    environment = store_lab._environment(home.name)
    try:
        store_lab._prepare_runtime(spec, environment)
    except BaseException:
        home.cleanup()
        raise
    return spec, environment, home


def evidence(name, record, checks, summary, **artifacts):
    """Write the runner's evidence contract only inside its private directory."""
    directory = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    (directory / "record.json").write_text(json.dumps({
        "schema": "matuto-run-record/1", "procedure": name,
        "record": record.get("id") or record.get("profile_id") or record.get("bundle_id"),
        "fingerprint": record["fingerprint"],
        "performed": [name],
    }, indent=2, sort_keys=True) + "\n")
    (directory / "checks.json").write_text(json.dumps(checks, indent=2, sort_keys=True) + "\n")
    (directory / "summary.txt").write_text(summary.rstrip() + "\n")
    for filename, body in artifacts.items():
        target = directory / filename
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else str(body).encode())


def report(phase, step):
    progress.report(phase, step)


def failed(message):
    raise store.StoreError(message)
