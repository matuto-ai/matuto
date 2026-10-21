"""Voice serving lifecycle: one qualified bundle replaces agentic serving."""

import contextlib
import http.client
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from matuto import matuto_runtime
from matuto import (admission, arguments, router, runner, serving, serving_state, store)

HOST = serving_state.HOST
VOICE_ROUTER_UNIT = serving_state.VOICE_ROUTER_UNIT
PART_PREFIX = serving_state.VOICE_PART_PREFIX
VOICE_ROUTER_PORT = serving_state.VOICE_ROUTER_PORT
PART_PORTS = {"synthesis": 18081, "recognition": 18082}
READY_SECONDS = 30


class VoiceRefused(Exception):
    """Voice serving cannot safely be carried out."""


def bundle_options(config_root=None):
    """Every bundle and whether it is offered, with the reason it is not."""
    opened = store.read_store(config_root)
    if opened is None:
        return []
    try:
        connection = opened.connection()
        devices = matuto_runtime.pci_devices().get("devices", [])
        result = []
        for row in connection.execute("SELECT bundle_id, name FROM bundles ORDER BY name, bundle_id"):
            record = store._read_record(opened, "bundle", row["bundle_id"])
            qualification = store.qualification_state(opened, record)
            available, reason = store.record_availability(record, connection, detected_gpus=devices)
            qualified_state = (serving.CHANGED if qualification["state"] == serving.CANDIDATE
                               and qualification.get("changed_since_qualification")
                               else qualification["state"])
            bundle = serving.Bundle(row["name"], qualified_state, available,
                                    reason or qualification.get("reason", ""))
            choice = serving.bundle_choices((bundle,))[0]
            result.append({"id": row["bundle_id"], "name": row["name"],
                           "qualification": qualified_state, "available": available,
                           "offered": choice.offered, "reasons": list(choice.reasons)})
        return result
    finally:
        opened.close()


def choices(config_root=None):
    """Bundle choices for the pure serving transition rules."""
    return [serving.Bundle(item["name"], item["qualification"], item["available"],
                           "; ".join(item["reasons"]))
            for item in bundle_options(config_root)]


def _profile(opened, profile_id, kind):
    record = store._read_record(opened, kind, profile_id)
    available, reason = store.record_availability(
        record, opened.connection(), detected_gpus=matuto_runtime.pci_devices().get("devices", []))
    if not available:
        raise VoiceRefused(f"{kind.title()} profile {record['name']!r} is unavailable: {reason}.")
    return record


def _profile_file(opened, record, field):
    pick = record.get(field)
    if not pick:
        raise VoiceRefused(f"{record['name']!r} has no {field} file selected.")
    path = store._profile_file_path(opened, pick)
    store._check_profile_files(opened, pick, path)
    return str(path)


def _arguments(record, role, executable):
    path_option = "--voices-dir" if role == "synthesis" else "--public"
    default_path = Path(executable).parent / ("voices" if role == "synthesis"
                                              else "examples/server/public")
    rows = [row for row in record.get("arguments", []) if row["tool"] == "server"]
    extra, path_seen = [], False
    for row in rows:
        flag = row.get("option") or row.get("catalogue")
        value = row.get("value")
        if flag in ("--public", "--voices-dir"):
            if flag != path_option:
                raise VoiceRefused(f"{flag} is not valid for a {role} profile.")
            path = Path(value) if value else default_path
            if ".." in path.parts:
                raise VoiceRefused(f"{flag} must not traverse to a parent directory.")
            if not path.is_absolute():
                path = Path(executable).parent / path
            extra.extend((flag, str(path)))
            path_seen = True
            continue
        extra.append(flag)
        if value is not None:
            extra.append(value)
    if not path_seen:
        extra.extend((path_option, str(default_path)))
    return extra


def _speech_spec(opened, record, role):
    row = opened.connection().execute(
        "SELECT s.kind, s.path, s.sha256, s.notes, l.path AS root FROM speech_executables AS s"
        " JOIN artifact_locations AS l USING (location_id)"
        " WHERE s.executable_id = ?", (record["speech_executable_id"],)).fetchone()
    if row is None or row["kind"] != role:
        raise VoiceRefused(f"{role.title()} executable for {record['name']!r} is unavailable.")
    executable = Path(row["path"])
    if not executable.is_absolute():
        executable = Path(row["root"]) / executable
    if not executable.is_absolute() or ".." in executable.parts:
        raise VoiceRefused(f"The installed executable for {record['name']!r} has an unsafe path.")
    model_path = _profile_file(opened, record, "weights")
    backend = "CPU" if role == "recognition" else _speech_backend(row["notes"])
    return {"role": role, "backend": backend, "executable": str(executable), "sha256": row["sha256"],
            "model": model_path, "model_sha256": _file_digest(opened, record["weights"]),
            "port": PART_PORTS[role], "args": _arguments(record, role, executable)}


def _speech_backend(notes):
    try:
        provenance = json.loads(notes or "{}")
    except ValueError:
        raise VoiceRefused("The installed speech executable has invalid provenance.") from None
    backend = provenance.get("backend")
    if backend == "vulkan":
        return "Vulkan"
    raise VoiceRefused(f"The installed synthesis executable has unsupported backend {backend!r}.")


def _file_digest(opened, pick):
    row = opened.connection().execute(
        "SELECT computed_sha256 FROM acquisition_files WHERE acquisition_id = ? AND file_name = ?",
        (pick["acquisition_id"], pick["file_name"])).fetchone()
    return row["computed_sha256"] if row else ""


def _systemd_quote(value):
    text = str(value)
    if "\n" in text or "\r" in text:
        raise VoiceRefused("A voice unit argument contains a line break.")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("%", "%%") + '"'


def _systemd_command(arguments):
    return " ".join(_systemd_quote(argument) for argument in arguments)


def part_unit(profile):
    return serving_state.part_unit(profile)


def _part_unit_text(spec):
    command = [*router.launcher_command()[:-1], "voice-part", spec["role"], spec["backend"],
               spec["executable"], spec["model"], HOST, str(spec["port"]), "--", *spec["args"]]
    return f"""[Unit]
Description=Matuto voice {spec['role']} serving

[Service]
Type=simple
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=control-group
TimeoutStopSec=20
Environment=MATUTO_RUNTIME_EXPECTED_SHA256={_systemd_quote(spec['sha256'])}
Environment=MATUTO_RUNTIME_MODEL_SHA256={_systemd_quote(spec['model_sha256'])}
ExecStart={_systemd_command(command)}
Restart=on-failure
RestartSec=2

"""


def _voice_router_text(bundle):
    command = [*router.launcher_command()[:-1], "voice-router", bundle]
    return f"""[Unit]
Description=Matuto voice router

[Service]
Type=simple
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
KillMode=control-group
TimeoutStopSec=20
ExecStart={_systemd_command(command)}
Restart=on-failure
RestartSec=2

"""


def _write_unit(unit, text):
    path = serving_state.unit_directory() / unit
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        return path
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}")
        temporary.write_text(text, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        router._systemctl("daemon-reload")
    except OSError as error:
        raise VoiceRefused(f"Could not write voice unit {path}: {error}") from None
    return path


@contextlib.contextmanager
def lease(config_root=None, *, state=None):
    if state is None:
        state = serving_state.read(config_root=config_root).state
    opened = store.open_store(config_root)
    try:
        connection = opened.connection()
        connection.execute("PRAGMA busy_timeout = 60000")
        connection.execute("BEGIN IMMEDIATE")
        try:
            decision = admission.decide(admission.SERVE_VOICE, "to serve voice",
                                        runner._current_work(connection), state,
                                        router.card_holder(config_root, connection))
            yield decision
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
    finally:
        opened.close()


def prepare_part(role, config_root=None):
    opened = store.read_store(config_root)
    if opened is None:
        raise VoiceRefused("The Matuto store cannot be read.")
    try:
        running = opened.connection().execute(
            "SELECT bundle_id FROM voice_serving WHERE voice_serving_id = 1").fetchone()
        if not running:
            raise VoiceRefused("No voice-serving record names a bundle.")
        row = opened.connection().execute(
            "SELECT profile_id FROM bundle_members WHERE bundle_id = ? AND role = ?",
            (running["bundle_id"], role)).fetchone()
        if row is None:
            raise VoiceRefused(f"The running bundle has no {role} profile.")
        record = _profile(opened, row["profile_id"], role)
        spec = _speech_spec(opened, record, role)
        return spec
    finally:
        opened.close()


def launch_part(role, backend, executable, model, host, port, args):
    with lease() as decision:
        if decision.outcome != admission.ADMIT:
            raise VoiceRefused(decision.reason)
    expected = prepare_part(role)
    supplied = (backend, executable, model, host, port, args)
    recorded = (expected["backend"], expected["executable"], expected["model"], HOST,
                expected["port"], expected["args"])
    if supplied != recorded:
        raise VoiceRefused("The speech launcher does not match the active voice-serving record.")
    _exec_speech(role, backend, executable, model, host, port, args)


def _exec_speech(role, backend, executable, model, host, port, args):
    directory = descriptor = None
    try:
        request = matuto_runtime._speech_request(role, backend, executable, model, host,
                                                 str(port), list(args))
        environment = matuto_runtime.execution_environment()
        if role == "synthesis":
            opened = store.read_store()
            if opened is None:
                raise VoiceRefused("The Matuto store cannot be read.")
            try:
                selection = router._selection(opened)
                pci = selection["gpu_identity"]
                hosts = [item for item in matuto_runtime.pci_devices().get("devices", [])
                         if item.get("pciAddress") == pci]
                identities = [item for item in matuto_runtime.bounded_identity_probe(
                    selection["runtime_backend"], environment)
                    if item.get("pciAddress") == pci and isinstance(item.get("uuid"), str)]
                if len(hosts) != 1 or len(identities) != 1:
                    raise VoiceRefused("The selected physical GPU could not be corroborated for synthesis.")
                selected = {"backend": selection["runtime_backend"], "pci": pci,
                            "corroborator": {"uuid": identities[0]["uuid"], "host": hosts[0]}}
                environment, _device = matuto_runtime.resolve_device(selected, environment, backend)
            finally:
                opened.close()
        directory = matuto_runtime.protected_open(Path(executable).parent, directory=True)
        descriptor = matuto_runtime.protected_open(executable)
        identity = matuto_runtime.fingerprint(descriptor)
        expected = os.environ.get("MATUTO_RUNTIME_EXPECTED_SHA256")
        if expected and expected != identity["sha256"]:
            raise VoiceRefused("The installed speech executable changed; serve it again after verification.")
        model_sha256 = os.environ.get("MATUTO_RUNTIME_MODEL_SHA256", "")
        if model_sha256:
            environment["MATUTO_RUNTIME_MODEL_SHA256"] = model_sha256
        os.set_inheritable(directory, True)
        os.set_inheritable(descriptor, True)
        os.chdir(f"/proc/self/fd/{directory}")
        os.execve(f"/proc/self/fd/{descriptor}", [Path(executable).name, *request], environment)
    except (matuto_runtime.InspectionError, OSError, router.LauncherRefused) as error:
        raise VoiceRefused(str(error)) from None
    finally:
        for held in (descriptor, directory):
            if held is not None:
                with contextlib.suppress(OSError):
                    os.close(held)


def _launch_check_profile(opened, profile, selector, help_text):
    connection = opened.connection()
    rows = [{"tool": row["tool"], "option": row["flag"], "value": row["value"]}
            for row in connection.execute(
                 "SELECT tool, flag, value FROM profile_arguments"
                 " WHERE profile_id = ? AND tool = 'server' ORDER BY position",
                 (profile["profile_id"],))]
    try:
        arguments.launch_check(rows, selector=selector,
                               help_text={"server": help_text})
    except arguments.Refusal as error:
        raise VoiceRefused(
            f"Voice language profile {profile['name']!r} ({profile['profile_id']}) {error};"
            " nothing is started.") from None


def launch_router(bundle_id_value, config_root=None):
    """Launch the voice router through the same exclusive card lease as speech parts."""
    with lease(config_root) as decision:
        if decision.outcome != admission.ADMIT:
            raise VoiceRefused(decision.reason)
    opened = store.read_store(config_root)
    if opened is None:
        raise VoiceRefused("The Matuto store cannot be read.")
    try:
        running = opened.connection().execute(
            "SELECT bundle_id FROM voice_serving WHERE voice_serving_id = 1").fetchone()
        if running is None or running["bundle_id"] != bundle_id_value:
            raise VoiceRefused("Voice serving was stopped or its bundle changed; refusing restart.")
        selection = router._selection(opened)
        row = opened.connection().execute(
            "SELECT profile_id FROM bundle_members WHERE bundle_id = ? AND role = 'language'",
            (bundle_id_value,)).fetchone()
        if row is None:
            raise VoiceRefused(f"Bundle {bundle_id_value!r} has no language profile.")
        profile = _profile(opened, row["profile_id"], "language")
        files = store.router_files(opened)
        for reason in (files["key_reason"], files["model_directory_reason"]):
            if reason:
                raise VoiceRefused(f"{reason}, so voice serving cannot start.")
        preset = store._router_preset_section(opened, profile)
        voice_path = opened.path.parent / "voice-presets.ini"
        store._replace_router_preset(voice_path, preset + "\n")
        environment = matuto_runtime.execution_environment()
        directory, descriptor = matuto_runtime._tool_descriptor(selection["location"], router.ROUTER_PROCESS)
        coordination = None
        try:
            environment = router._card_environment(selection, environment)
            help_text = matuto_runtime.probe(descriptor, directory, "--help", environment)
            selector = router._selector(descriptor, directory, environment, selection)
            _launch_check_profile(opened, profile, selector, help_text)
            argv = ["--host", HOST, "--port", str(VOICE_ROUTER_PORT), "--api-key-file", files["key_path"],
                    "--models-dir", files["model_directory"], "--models-preset", str(voice_path),
                    "--models-max", "1", "--device", selector]
            matuto_runtime.validate_arguments(argv, help_text, selector, router=True)
            coordination = matuto_runtime.runtime_lease()
            os.set_inheritable(directory, True)
            os.set_inheritable(descriptor, True)
            os.set_inheritable(coordination, True)
            matuto_runtime._landlock_confine(descriptor)
            os.chdir(files["model_directory"])
            os.execve(descriptor, [router.ROUTER_PROCESS, *argv], environment)
        finally:
            if coordination is not None:
                with contextlib.suppress(OSError):
                    os.close(coordination)
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                os.close(directory)
    except (matuto_runtime.InspectionError, store.StoreError, router.LauncherRefused) as error:
        raise VoiceRefused(str(error)) from None
    finally:
        opened.close()


def activate(control, *, confirmed, config_root=None, bundle=None, timeout=READY_SECONDS,
             sleep=time.sleep):
    """Serve, stop, or switch only when its state rule offers that control."""
    state = serving_state.read(config_root=config_root)
    work = _current_work(config_root)
    runtime = _runtime(config_root)
    storage = _storage(config_root)
    bundles = choices(config_root)
    offered = next((item for item in serving.controls(_view(state), work, runtime, storage, bundles)
                    if item.name == control), None)
    if offered is None or not offered.offered:
        raise VoiceRefused(" ".join(offered.reasons) if offered else
                           f"{serving.LABELS.get(control, control)} is not offered in this state.")
    if control == serving.SERVE_VOICE:
        selected = _bundle_record(config_root, bundle)
        choice = next((item for item in serving.bundle_choices(bundles)
                       if item.name == selected["name"]), None)
        if choice is None or not choice.offered:
            raise VoiceRefused("; ".join(choice.reasons) if choice else "The selected bundle is not offered.")
        if not confirmed:
            return {"control": control, "acted": False,
                    "preview": _consequence(serving.consequence(control, _view(state), work,
                                                                  selected["name"]))}
        return start(config_root, selected["bundle_id"], timeout=timeout, sleep=sleep)
    consequence = serving.consequence(control, _view(state), work)
    if not confirmed:
        return {"control": control, "acted": False, "preview": _consequence(consequence)}
    if control == serving.STOP_SERVING:
        return stop(config_root, state=state)
    if control == serving.SWITCH_TO_AGENTIC:
        return switch(config_root)
    raise VoiceRefused(f"{control!r} is not a voice transition.")


def start(config_root, selected_bundle, *, timeout=READY_SECONDS, sleep=time.sleep):
    """Start all bundle parts, rolling back to precisely the previous workload."""
    state = serving_state.read(config_root=config_root)
    work = _current_work(config_root)
    runtime = _runtime(config_root)
    storage = _storage(config_root)
    bundles = choices(config_root)
    controls = serving.controls(_view(state), work, runtime, storage, bundles)
    control = next((item for item in controls if item.name == serving.SERVE_VOICE), None)
    if control is None or not control.offered:
        raise VoiceRefused(" ".join(control.reasons) if control else "Serve voice is not offered in this state.")
    bundle = _bundle_record(config_root, selected_bundle)
    choice = next((item for item in serving.bundle_choices(bundles) if item.name == bundle["name"]), None)
    if choice is None or not choice.offered:
        raise VoiceRefused("; ".join(choice.reasons) if choice else "The selected bundle is not offered.")
    consequence = serving.consequence(serving.SERVE_VOICE, _view(state), work, bundle["name"])
    if state.state == admission.AGENTIC:
        stopped, reason = router.stop_unit()
        if not stopped:
            raise VoiceRefused(reason)
    opened = None
    started = []
    parts = []
    readiness_failures = []
    target_units = [VOICE_ROUTER_UNIT]
    try:
        opened = store.open_store(config_root)
        connection = opened.connection()
        members = {row["role"]: row["profile_id"] for row in connection.execute(
            "SELECT role, profile_id FROM bundle_members WHERE bundle_id = ?", (selected_bundle,))}
        target_units.extend(part_unit(members[role]) for role in PART_PORTS if role in members)
        for role in ("synthesis", "recognition"):
            member = members.get(role)
            if member is None:
                raise VoiceRefused(f"Bundle {bundle['name']!r} has no {role} profile.")
            record = _profile(opened, member, role)
            spec = _speech_spec(opened, record, role)
            parts.append((record, spec))
        for record, spec in parts:
            _write_unit(part_unit(record["profile_id"]), _part_unit_text(spec))
        _write_unit(VOICE_ROUTER_UNIT, _voice_router_text(selected_bundle))
        _commit_record(connection, selected_bundle, ())
        _start_unit(VOICE_ROUTER_UNIT)
        started.append(VOICE_ROUTER_UNIT)
        for record, _spec in parts:
            unit = part_unit(record["profile_id"])
            _start_unit(unit)
            started.append(unit)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            readiness_failures = []
            for unit in started:
                unit_state = serving_state.unit_state(unit)
                reason = (f"systemd reports {unit_state}" if unit_state != "active"
                          else _readiness_reason(unit, parts))
                if reason:
                    readiness_failures.append(f"{unit}: {reason}")
            if not readiness_failures:
                _commit_record(connection, selected_bundle, parts)
                return {"control": serving.SERVE_VOICE, "acted": True, "state": admission.VOICE,
                        "bundle": bundle["name"], "units": started,
                        "addresses": [f"http://{HOST}:{spec['port']}" for _record, spec in parts],
                        "preview": _consequence(consequence)}
            sleep(0.2)
        detail = "; ".join(readiness_failures) or "readiness deadline expired before the first check"
        raise VoiceRefused(f"Voice serving did not become ready: {detail}.")
    except BaseException as error:
        remaining = []
        for unit in reversed(target_units):
            state_now = serving_state.unit_state(unit)
            if state_now not in ("inactive", "failed"):
                ok, _detail = _stop_unit(unit)
                if not ok or serving_state.unit_state(unit) in (*serving_state.LIVE,
                                                                  serving_state.UNKNOWN):
                    remaining.append(unit)
        recovery_errors = (["Voice parts remain running: " + ", ".join(remaining)] if remaining else [])
        if opened is not None:
            try:
                if remaining:
                    _store_failed_parts(opened.connection(), selected_bundle, remaining)
                else:
                    opened.connection().execute("DELETE FROM voice_serving_parts")
                    opened.connection().execute("DELETE FROM voice_serving WHERE voice_serving_id = 1")
            except (store.StoreError, OSError, sqlite3.Error) as record_error:
                recovery_errors.append(f"Could not record remaining voice parts: {record_error}")
        if state.state == admission.AGENTIC and not remaining:
            try:
                router.start_unit(config_root)
            except router.LauncherRefused as restore_error:
                recovery_errors.append(f"Could not restore prior agentic serving: {restore_error}")
        if isinstance(error, VoiceRefused):
            if recovery_errors:
                raise VoiceRefused(f"{error} {'; '.join(recovery_errors)}") from None
            raise
        if isinstance(error, (store.StoreError, OSError, sqlite3.Error, matuto_runtime.InspectionError)):
            message = str(error)
            if recovery_errors:
                message += "; " + "; ".join(recovery_errors)
            raise VoiceRefused(message) from None
        raise
    finally:
        if opened is not None:
            opened.close()


def stop(config_root, *, state=None):
    state = state or serving_state.read(config_root=config_root)
    if state.state != admission.VOICE and not any(part.state in serving_state.LIVE for part in state.parts):
        raise VoiceRefused("Stop serving from voice is not offered while Matuto is not voice serving.")
    targets = {VOICE_ROUTER_UNIT, *(part_unit(part.profile) for part in state.parts)}
    stray, _complete = serving_state.stray_units(serving_state.voice_record(config_root))
    targets.update(stray)
    _clear_voice_record(config_root)
    remaining = []
    failures = []
    for unit in sorted(targets):
        ok, detail = _stop_unit(unit)
        if not ok:
            failures.append(detail)
        if serving_state.unit_state(unit) in (*serving_state.LIVE, serving_state.UNKNOWN) or not ok:
            remaining.append(unit)
    opened = store.open_store(config_root)
    try:
        connection = opened.connection()
        if remaining:
            _store_failed_units(connection, state.bundle, remaining)
        else:
            connection.execute("DELETE FROM voice_serving_parts")
            connection.execute("DELETE FROM voice_serving WHERE voice_serving_id = 1")
        after = serving_state.read(config_root=config_root)
        reason = "; ".join(failures)
        if remaining:
            reason = "; ".join(filter(None, (reason, "still running: " + ", ".join(remaining))))
        return {"control": serving.STOP_SERVING, "acted": True, "stopped": not remaining,
                "state": after.state, "remaining": remaining, "reason": reason}
    finally:
        opened.close()


def switch(config_root):
    result = stop(config_root)
    if result["remaining"]:
        raise VoiceRefused("Switch refused; voice parts remain running: " + ", ".join(result["remaining"]))
    router.start_unit(config_root)
    state = router.wait_ready(config_root)
    return {"control": serving.SWITCH_TO_AGENTIC, "acted": True, "state": state.state,
            "healthy": state.healthy, "address": state.address}


def _readiness_reason(unit, parts):
    if unit == VOICE_ROUTER_UNIT:
        key = serving_state.router_key(serving_state.router_key_path())
        if not key:
            return "router API key is unavailable"
        headers = {"authorization": f"Bearer {key}", "accept": "application/json"}
        path, port = "/health", VOICE_ROUTER_PORT
    else:
        profile = unit.removeprefix(PART_PREFIX).removesuffix(".service")
        match = next((spec for record, spec in parts if record["profile_id"] == profile), None)
        if match is None:
            return "no serving record names this part"
        headers, path, port = {}, "/", match["port"]
    connection = None
    try:
        connection = http.client.HTTPConnection(HOST, port, timeout=1)
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(4096)
        if unit != VOICE_ROUTER_UNIT:
            return "" if 100 <= response.status < 500 else f"HTTP endpoint returned {response.status}"
        if response.status != 200:
            return f"health endpoint returned HTTP {response.status}"
        answer = json.loads(body)
        return "" if isinstance(answer, dict) and bool(answer.get("status")) else "health response was invalid"
    except (OSError, ValueError, http.client.HTTPException) as error:
        return f"endpoint did not answer: {error}"
    finally:
        if connection is not None:
            connection.close()


def _start_unit(unit):
    answer = router._systemctl("start", unit)
    if not answer.answered or answer.returncode:
        raise VoiceRefused(f"{unit} did not start: {router._detail(answer) or 'systemctl failed'}.")


def _stop_unit(unit):
    answer = router._systemctl("stop", unit)
    return (answer.answered and answer.returncode == 0,
            "" if answer.returncode == 0 else f"{unit} could not be stopped: {router._detail(answer)}")


def _commit_record(connection, bundle_id_value, parts):
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM voice_serving_parts")
        connection.execute("DELETE FROM voice_serving WHERE voice_serving_id = 1")
        connection.execute("INSERT INTO voice_serving VALUES (1, ?)", (bundle_id_value,))
        for record, spec in parts:
            connection.execute("INSERT INTO voice_serving_parts VALUES (?, ?, ?)",
                               (record["kind"], bundle_id_value, spec["port"]))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _store_failed_parts(connection, bundle_id_value, remaining):
    _store_failed_record(connection, bundle_id_value, remaining)


def _clear_voice_record(config_root):
    opened = store.open_store(config_root)
    try:
        connection = opened.connection()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM voice_serving_parts")
        connection.execute("DELETE FROM voice_serving WHERE voice_serving_id = 1")
        connection.execute("COMMIT")
    except BaseException as error:
        if opened.connection().in_transaction:
            opened.connection().execute("ROLLBACK")
        if isinstance(error, (sqlite3.Error, store.StoreError, OSError)):
            raise VoiceRefused(f"Could not record that voice serving was stopped: {error}") from None
        raise
    finally:
        opened.close()


def _store_failed_units(connection, bundle_id_value, remaining):
    if bundle_id_value:
        _store_failed_record(connection, bundle_id_value, remaining)


def _store_failed_record(connection, bundle_id_value, remaining):
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DELETE FROM voice_serving_parts")
        connection.execute("DELETE FROM voice_serving WHERE voice_serving_id = 1")
        connection.execute("INSERT INTO voice_serving VALUES (1, ?)", (bundle_id_value,))
        speech_units = sorted(unit for unit in remaining if unit.startswith(PART_PREFIX))
        if len(speech_units) > 1:
            # The schema has no failed-operation flag; keep the record incomplete and report other live units as stray.
            speech_units = speech_units[:1]
        for unit in speech_units:
            profile = unit.removeprefix(PART_PREFIX).removesuffix(".service")
            row = connection.execute("SELECT role FROM bundle_members WHERE bundle_id = ? AND profile_id = ?",
                                     (bundle_id_value, profile)).fetchone()
            if row and row["role"] in PART_PORTS:
                connection.execute("INSERT OR REPLACE INTO voice_serving_parts VALUES (?, ?, ?)",
                                   (row["role"], bundle_id_value, PART_PORTS[row["role"]]))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _bundle_record(config_root, selected):
    opened = store.read_store(config_root)
    if opened is None:
        raise VoiceRefused("The Matuto store cannot be read.")
    try:
        if not isinstance(selected, str) or not selected:
            raise VoiceRefused("Select one offered bundle.")
        row = opened.connection().execute("SELECT bundle_id, name FROM bundles WHERE bundle_id=?",
                                          (selected,)).fetchone()
        if row is None:
            row = opened.connection().execute("SELECT bundle_id, name FROM bundles WHERE name=?",
                                              (selected,)).fetchone()
        if row is None:
            raise VoiceRefused(f"No bundle is stored as {selected!r}.")
        record = store._read_record(opened, "bundle", row["bundle_id"])
        qualification = store.qualification_state(opened, record)
        if qualification["state"] != serving.QUALIFIED:
            raise VoiceRefused(f"Bundle {row['name']!r} is not qualified: {qualification.get('reason', 'candidate')}.")
        available, reason = store.record_availability(
            record, opened.connection(), detected_gpus=matuto_runtime.pci_devices().get("devices", []))
        if not available:
            raise VoiceRefused(f"Bundle {row['name']!r} is unavailable: {reason}.")
        return dict(record, name=row["name"])
    finally:
        opened.close()


def _view(state):
    parts = () if state.state == admission.VOICE and state.healthy else tuple(
        part.profile for part in state.parts)
    return serving.Serving(state.state, state.healthy, parts, state.bundle)


def _current_work(config_root):
    from matuto.runner import _current_work
    opened = store.read_store(config_root)
    if opened is None:
        return None
    try:
        return _current_work(opened.connection())
    finally:
        opened.close()


def _runtime(config_root):
    opened = store.read_store(config_root)
    if opened is None:
        return serving.Runtime(False, "the store cannot be read")
    try:
        try:
            router._selection(opened)
            return serving.Runtime(True)
        except router.LauncherRefused as error:
            return serving.Runtime(False, str(error))
    finally:
        opened.close()


def _storage(config_root):
    opened = store.read_store(config_root)
    if opened is None:
        return serving.Storage(False, "the store cannot be read")
    try:
        files = store.router_files(opened)
        detail = files["model_directory_reason"]
        return serving.Storage(not bool(detail), detail)
    finally:
        opened.close()


def _consequence(consequence):
    return {"stops": list(consequence.stops), "starts": list(consequence.starts),
            "stays": list(consequence.stays), "safe_choice": consequence.safe_choice}
