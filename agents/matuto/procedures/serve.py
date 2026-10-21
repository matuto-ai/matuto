"""Run the store-backed temporary loopback server until the runner stops it."""

import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from matuto import matuto_runtime
from matuto import progress, store, store_lab

LAB = Path(__file__).resolve().parents[2] / "matuto-lab"
START_TIMEOUT = 30
POLL_INTERVAL = 0.1


def _port_is_free(port):
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _server_answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _stop_process(process):
    """End the whole store-backed launcher if startup never reaches loopback."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _launch_spec(opened, record):
    devices = matuto_runtime.pci_devices()
    if devices.get("state") != "completed":
        raise store.StoreError("GPU detection failed; the stored runtime selection cannot be verified.")
    try:
        spec = store.launch_spec(opened, "language", record["profile_id"],
                                 detected_gpus=devices.get("devices", ()))
    except store.StoreError as error:
        if "no lab port" in str(error).lower():
            raise store.StoreError("No lab port is configured; set it in Settings › Lab port.") from None
        raise
    if spec["record"]["fingerprint"] != store.fingerprint(opened, record):
        raise store.StoreError("The launch spec does not match the admitted profile fingerprint.")
    return spec


def run(opened, record):
    """Serve the admitted language record from its immutable launch snapshot."""
    if record["kind"] != "language":
        raise store.StoreError("serve requires a stored language profile.")

    spec = _launch_spec(opened, record)
    profile = spec["record"]
    port = spec["lab_port"]
    if not _port_is_free(port):
        raise store.StoreError(f"127.0.0.1:{port} is already in use.")

    with tempfile.TemporaryDirectory(prefix="matuto-serve-") as home:
        environment = store_lab._environment(home)
        store_lab._prepare_runtime(spec, environment)
        environment["MATUTO_LAB_STORE_BACKED"] = "1"
        environment["MATUTO_STORE_LAUNCH_RECORD"] = json.dumps(profile, sort_keys=True)
        identity = Path(environment["XDG_STATE_HOME"]) / "serve-identity.json"
        environment["MATUTO_RUNTIME_IDENTITY_PATH"] = str(identity)
        process = subprocess.Popen([str(LAB), profile["id"], "serve"], env=environment,
                                   start_new_session=True)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            status = process.poll()
            if status is not None:
                _stop_process(process)
                raise store.StoreError(
                    f"temporary server exited before it was ready (status {status}).")
            if _server_answers(port):
                address = f"http://127.0.0.1:{port}"
                progress.report("Serving", address)
                print(f"Temporary server: {address}", flush=True)
                break
            time.sleep(POLL_INTERVAL)
        else:
            _stop_process(process)
            raise store.StoreError(f"temporary server did not answer on 127.0.0.1:{port}.")

        # A serve run has no natural success case. Stop owns its lifecycle in the runner.
        status = process.wait()
        _stop_process(process)
        raise store.StoreError(f"temporary server exited before Stop (status {status}).")
