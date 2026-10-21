"""Validate and atomically save the active llama.cpp build and card."""

import contextlib
import datetime
import sqlite3

from matuto import admission, installation, registry, store


def select(connection, build_id, backend, card_id, *, serving, runtime_run,
           detected_cards, validate_device=None, hip_targets=None):
    """Explicitly choose a build, backend and detected card without starting work.

    `runtime_run` is True when a run using the runtime is active, False when none
    is, or None when unknown. `validate_device` must corroborate the build's device
    listing with the card's physical PCI identity.
    """
    _refuse_busy(serving, runtime_run)
    card = _card(connection, card_id, detected_cards)
    build = _build(connection, build_id, backend)
    _validate_build(connection, build)
    _validate_target(build, backend, card["gpu_identity"], hip_targets)
    validator = validate_device or (lambda selected, identity: _validate_registered_device(
        connection, selected, identity))
    _corroborate(validator, build, card["gpu_identity"])
    return _save(connection, build_id, card_id, backend)


def use(connection, build_id, *, serving, runtime_run, detected_cards,
        validate_device=None, hip_targets=None):
    """Switch only the build, preserving the selected backend and card."""
    selected = connection.execute(
        "SELECT build_id, card_id, backend FROM runtime_selection WHERE selection_id = 1"
    ).fetchone()
    if selected is None:
        raise store.StoreError("Select a runtime build and card first.")
    _refuse_busy(serving, runtime_run)
    card = _card(connection, selected["card_id"], detected_cards)
    build = _build(connection, build_id, selected["backend"])
    _validate_build(connection, build)
    _validate_target(build, selected["backend"], card["gpu_identity"], hip_targets)
    validator = validate_device or (lambda chosen, identity: _validate_registered_device(
        connection, chosen, identity))
    _corroborate(validator, build, card["gpu_identity"])
    return _save(connection, build_id, selected["card_id"], selected["backend"],
                 expected_selection=(selected["build_id"], selected["card_id"],
                                     selected["backend"]))


def _refuse_busy(serving, runtime_run):
    if serving is None or serving not in ("stopped", "agentic", "voice"):
        raise store.StoreError("Serving state is unknown; stop serving and retry runtime selection.")
    if serving != "stopped":
        raise store.StoreError(f"{serving.capitalize()} serving is running. Stop serving before changing the runtime selection.")
    if runtime_run is None:
        raise store.StoreError("Runtime work is unknown; stop any runtime run and retry.")
    if runtime_run:
        raise store.StoreError("A run using the runtime is running. Stop it before changing the runtime selection.")


def _card(connection, card_id, detected_cards):
    card = connection.execute(
        "SELECT card_id, gpu_identity FROM cards WHERE card_id = ?", (card_id,)).fetchone()
    if card is None:
        raise store.StoreError("No card record is selected. Add a card in Settings › Cards first.")
    if detected_cards is None:
        raise store.StoreError("GPU detection is unavailable; the selected card cannot be verified.")
    if card["gpu_identity"] not in detected_cards:
        raise store.StoreError(f"Card {card_id!r} is not currently detected. Select a detected card in Settings › Cards.")
    return card


def _build(connection, build_id, backend):
    row = connection.execute("SELECT * FROM builds WHERE build_id = ?", (build_id,)).fetchone()
    if row is None:
        raise store.StoreError(f"No build is registered as {build_id!r}.")
    if row["backend"] != backend:
        raise store.StoreError(f"Build {build_id!r} uses the {row['backend']} backend, not {backend}.")
    return row


def _validate_build(connection, build):
    available, reason = registry.availability(connection, build["build_id"])
    if not available:
        raise store.StoreError(reason or f"Build {build['build_id']!r} is unavailable.")


def _validate_target(build, backend, gpu_identity, hip_targets):
    if backend != "hip":
        return
    if hip_targets is None:
        raise store.StoreError("HIP targets are unavailable; the selected card cannot be verified.")
    target = hip_targets.get(gpu_identity)
    if not target:
        raise store.StoreError(f"No HIP target was read for card {gpu_identity}.")
    if build["target"] != target:
        raise store.StoreError(
            f"Build {build['build_id']!r} targets {build['target'] or 'no HIP target'}, "
            f"but card {gpu_identity} uses {target}.")


def _validate_registered_device(connection, build, gpu_identity):
    """Probe only the checked server and require it to resolve to this physical GPU."""
    import json
    import os
    from matuto import matuto_runtime

    backend = {"hip": "HIP/ROCm", "vulkan": "Vulkan"}[build["backend"]]
    relative_server = installation.SERVER_PATHS[build["recipe"]]
    row = connection.execute(
        "SELECT check_digests FROM installations WHERE location = ?", (build["location"],)
    ).fetchone()
    try:
        expected = json.loads(row["check_digests"]) if row else {}
    except (TypeError, ValueError):
        expected = {}
    if not expected.get(relative_server):
        relative_server = "llama-server"
    if not expected.get(relative_server):
        raise store.StoreError("The checked installation does not identify its server executable.")

    environment = matuto_runtime.execution_environment()
    host = next((item for item in matuto_runtime.pci_devices()["devices"]
                 if item["pciAddress"] == gpu_identity), None)
    identities = [item for item in matuto_runtime.bounded_identity_probe(backend, environment)
                  if item.get("pciAddress") == gpu_identity and isinstance(item.get("uuid"), str)]
    if host is None or len(identities) != 1:
        raise store.StoreError("The build backend cannot uniquely corroborate the selected physical GPU.")
    selection = {"installation": build["location"], "backend": backend, "pci": gpu_identity,
                 "corroborator": {"uuid": identities[0]["uuid"], "host": host}}
    environment, _identity = matuto_runtime.resolve_device(selection, environment)
    directory, descriptor = matuto_runtime._tool_descriptor(build["location"], "llama-server")
    try:
        actual = matuto_runtime.fingerprint(descriptor)
        if actual != expected[relative_server]:
            raise store.StoreError("The checked server changed; inspect the build again before selecting it.")
        help_text = matuto_runtime.probe(descriptor, directory, "--help", environment)
        if not all(flag in help_text for flag in ("--device", "--list-devices")):
            raise store.StoreError("The checked server has no device-selection capabilities.")
        listed = [item for item in matuto_runtime._device_listing(
            matuto_runtime.probe(descriptor, directory, "--list-devices", environment))
            if item[1] == backend]
        if len(listed) != 1:
            raise store.StoreError("The selected build cannot execute on exactly the chosen GPU.")
        if matuto_runtime.fingerprint(descriptor) != actual:
            raise store.StoreError("The checked server changed during device validation; retry selection.")
    finally:
        os.close(descriptor)
        os.close(directory)
    return True


def _corroborate(validate_device, build, gpu_identity):
    if validate_device is None:
        raise store.StoreError("The selected build cannot verify the card's physical GPU.")
    try:
        result = validate_device(build, gpu_identity)
    except Exception as error:
        raise store.StoreError(f"The selected build could not verify card {gpu_identity}: {error}") from None
    if result is not True:
        raise store.StoreError(f"The selected build does not corroborate card {gpu_identity}.")


def _save(connection, build_id, card_id, backend, *, expected_selection=None):
    connection.execute("BEGIN IMMEDIATE")
    try:
        if expected_selection is not None:
            current = connection.execute(
                "SELECT build_id, card_id, backend FROM runtime_selection WHERE selection_id = 1"
            ).fetchone()
            if current is None or tuple(current) != expected_selection:
                raise store.StoreError("The runtime selection changed; review it and try Use again.")
        run = next((row for row in connection.execute(
            "SELECT procedure, record_label FROM runs WHERE outcome IS NULL")
                    if row["procedure"] not in admission.NO_CARD_RUNS), None)
        if run is not None:
            raise store.StoreError(
                f"A run using the runtime is running ({run['record_label']}). "
                "Stop it before changing the runtime selection.")
        connection.execute(
            "INSERT INTO runtime_selection (selection_id, build_id, card_id, backend, saved_at)"
            " VALUES (1, ?, ?, ?, ?) ON CONFLICT(selection_id) DO UPDATE SET"
            " build_id = excluded.build_id, card_id = excluded.card_id,"
            " backend = excluded.backend, saved_at = excluded.saved_at",
            (build_id, card_id, backend,
             datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
        result = dict(connection.execute(
            "SELECT * FROM runtime_selection WHERE selection_id = 1").fetchone())
        connection.execute("COMMIT")
        return result
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise
