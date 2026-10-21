#!/usr/bin/env python3
"""Speech-runtime and voice-bundle qualification for `matuto-lab`.

A speech profile qualifies one runtime on its own. A bundle profile names one
model profile and the speech profiles that form the voice, and qualifies them
loaded together: the model server and every speech runtime stay resident while
occupancy is read, and synthesis is timed while model generation is in flight.

Two entry points share the one decision function:

  part RESULTS_DIR           qualify one speech runtime alone
  bundle RESULTS_DIR         qualify a bundle against the selected physical card

The decision itself lives in `decide_bundle`, which is the seam the fixture
numbers are fed through; no separate command publishes it.

The launcher, not this module, is what places synthesis on the selected card.
This module reads the card's own counters and never substitutes a zero for a
reading it could not take.
"""
import contextlib
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matuto import matuto_bench as bench
from matuto import matuto_runtime as runtime

READY_SECONDS = 300
GENERATION_SECONDS = 7200
POLL_SECONDS = 0.02
# Breeze-TTS-2 streams mono 24 kHz signed 16-bit little endian PCM, matching the
# reference server, so a duration is a byte count rather than a decoded header.
AUDIO_SAMPLE_RATE = 24000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_BYTES = 2
DEFAULT_LOAD_PROMPT = "Describe the sea in one short paragraph."
DEFAULT_LOAD_TEXT = "Matuto qualifies this voice bundle under the stated generation load."
QUALIFIED, REFUSED, ERROR = "qualified", "refused", "error"
# Qualifying a part never qualifies a bundle, and the record says so where a
# reader would otherwise look for the bundle verdict.
BUNDLE_NOT_ESTABLISHED = {
    "state": "not_established",
    "reason": "Qualifying a speech runtime alone never qualifies a bundle; a bundle is qualified by loading its parts together under model generation.",
}
TERMINATION_ERROR = "The qualification was interrupted; owned processes were stopped and this run is incomplete."


class VoiceError(Exception):
    """Developer-authored reason for a failed qualification, never command or model output."""


class PlacementRefusal(VoiceError):
    """The lab cannot put synthesis on the selected physical card, so it refuses to qualify.

    Nothing is measured in this case: a bundle qualified without its synthesis part
    held on the selected card would not describe the bundle an operator runs.
    """


def decide_bundle(observations):
    """Decide a bundle from the numbers a run supplied.

    Occupancy is compared in bytes and the real-time factor as the ratio of the
    generated audio duration to the synthesis request's own wall time, both
    whole integer nanoseconds, so neither comparison rounds. The stated
    generation load is a precondition rather than a divisor: the factor is only
    a factor while that load is in flight, which the run proves separately. A
    number the run could not supply is an instrumentation error, never a zero
    and never a pass.
    """
    required = ("combined_occupancy_bytes", "capacity_bytes", "audio_duration_nanoseconds", "synthesis_wall_nanoseconds")
    missing = [name for name in required if not isinstance(observations.get(name), int) or isinstance(observations.get(name), bool)]
    if missing:
        return {"outcome": ERROR, "reason": "Missing measurement telemetry: " + ", ".join(sorted(missing)) + "."}
    numbers = {name: observations[name] for name in required}
    if numbers["capacity_bytes"] <= 0 or numbers["synthesis_wall_nanoseconds"] <= 0:
        return {"outcome": ERROR, "numbers": numbers,
                "reason": "Unusable measurement telemetry; a card capacity and a synthesis duration must both be positive."}
    if numbers["combined_occupancy_bytes"] < 0 or numbers["audio_duration_nanoseconds"] <= 0:
        return {"outcome": ERROR, "numbers": numbers,
                "reason": "Unusable measurement telemetry; the parts hold a non-negative occupancy and synthesis returns audio."}
    numbers["real_time_factor"] = numbers["audio_duration_nanoseconds"] / numbers["synthesis_wall_nanoseconds"]
    if numbers["combined_occupancy_bytes"] > numbers["capacity_bytes"]:
        return {"outcome": REFUSED, "numbers": numbers,
                "reason": f"Combined occupancy {numbers['combined_occupancy_bytes']} bytes is above the card capacity of {numbers['capacity_bytes']} bytes."}
    if numbers["audio_duration_nanoseconds"] <= numbers["synthesis_wall_nanoseconds"]:
        return {"outcome": REFUSED, "numbers": numbers,
                "reason": f"Synthesis real-time factor {numbers['audio_duration_nanoseconds']} nanoseconds of audio per {numbers['synthesis_wall_nanoseconds']} nanoseconds of synthesis is at or below 1.0."}
    return {"outcome": QUALIFIED, "numbers": numbers,
            "reason": "Combined occupancy is within card capacity and synthesis produced audio faster than it plays."}


def read_int(path):
    try:
        value = int(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise VoiceError("Byte-valued VRAM telemetry is missing or invalid; no zero measurement is assumed.") from error
    if value < 0:
        raise VoiceError("Byte-valued VRAM telemetry reported a negative reading; no zero measurement is assumed.")
    return value


def require_free_port(host, port):
    try:
        with socket.socket() as probe:
            # A previous run's connections may still linger; only a live listener counts.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
    except OSError as error:
        raise VoiceError(f"Loopback port {port} is occupied. Free it yourself; this qualification stopped nothing.") from error


def request(port, method, path, body=None, headers=None, timeout=30, host="127.0.0.1"):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(1 << 30)
    finally:
        connection.close()


def wait_ready(port, process, path, what, status=None):
    """Wait for the owned runtime to answer, and for the named status when the
    endpoint's own semantics say which status means ready.

    A runtime that is up answers; which status it puts on a path it may not serve
    is its own business, so requiring one would spend the whole readiness deadline
    on a healthy runtime whose root is a 404 and then report it as never ready.
    The model server is the other case: llama.cpp defines /health, answering 503
    while the weights are still loading and 200 once they are resident, so taking
    the first answer there would read the identity endpoint and the occupancy
    counter against a server that had not loaded and then fail before measuring.
    """
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VoiceError(f"The owned {what} exited during startup with status {process.returncode}; no readiness is recorded. Its own output is retained in this run's log.")
        answered = None
        try:
            answered, _ = request(port, "GET", path, timeout=min(2, max(.1, deadline - time.monotonic())))
        except (OSError, http.client.HTTPException):
            pass
        if answered is not None and (status is None or answered == status):
            return
        time.sleep(POLL_SECONDS)
    if status is None:
        raise VoiceError(f"The owned {what} did not become ready within {READY_SECONDS} seconds.")
    raise VoiceError(f"The owned {what} did not answer HTTP {status} within {READY_SECONDS} seconds.")


def confirm_speech_identity(process, executable):
    try:
        matches = Path(f"/proc/{process.pid}/exe").samefile(executable)
    except OSError:
        matches = False
    if not matches:
        raise VoiceError("The ready speech endpoint is not backed by the expected owned speech executable.")


def confirm_model(port, spec, process, identity):
    """Prove the ready endpoint is the owned server, running the named model."""
    try:
        digest = hashlib.sha256(Path(f"/proc/{process.pid}/exe").read_bytes()).hexdigest()
    except OSError:
        raise VoiceError("The ready endpoint is not backed by a readable owned executable.") from None
    if identity.get("executable", {}).get("sha256") != digest:
        raise VoiceError("The ready model endpoint is not backed by the executable the launcher recorded.")
    try:
        status, body = request(port, "GET", "/props", timeout=10)
        props = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise VoiceError("The owned model server did not return a readable identity endpoint.") from error
    if status != 200 or type(props) is not dict:
        raise VoiceError("The owned model server's identity endpoint did not return an HTTP 200 object.")
    try:
        model_matches = Path(props.get("model_path", "")).samefile(spec["model_path"])
    except (OSError, TypeError):
        model_matches = False
    if not model_matches:
        raise VoiceError("The live model differs from the model profile the bundle names.")
    projector = spec.get("mmproj") or {}
    mtp = spec.get("mtp", {"enabled": False})
    draft = mtp.get("model") or {}
    expected = {"mmproj": projector.get("path", "unavailable"), "mmproj_sha256": projector.get("sha256", ""),
                "spec_type": "draft-mtp" if mtp["enabled"] else "unavailable",
                "draft_model": draft.get("path", "unavailable"), "draft_sha256": draft.get("sha256", "")}
    if any(identity.get("request", {}).get(key) != value for key, value in expected.items()):
        raise VoiceError("The launched projector or MTP settings differ from the bundle model profile.")
    if projector and props.get("modalities", {}).get("vision") is not True:
        raise VoiceError("The owned model server did not report vision support for the requested projector.")
    return runtime.build_revision(props.get("build_info")) or "unavailable"


def wait_processing(port, process, what):
    """Wait until the owned server reports generation in flight.

    A synthesis factor is only a factor under generation load, so the run waits
    for the server's own processing state rather than assuming the request it
    just sent has already been picked up.
    """
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VoiceError(f"The owned {what} exited before generation was observed in flight.")
        try:
            status, body = request(port, "GET", "/slots", timeout=2)
            slots = json.loads(body)
            if status == 200 and type(slots) is list and any(type(slot) is dict and slot.get("is_processing") for slot in slots):
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        time.sleep(POLL_SECONDS)
    raise VoiceError("The model server never reported generation in flight; the synthesis factor was not measured under load.")


def synthesize(port, text, timeout=GENERATION_SECONDS):
    """Time one synthesis request and derive its audio duration from the PCM.

    Nothing is rounded here: the audio duration is whole nanoseconds from a whole
    sample count, and the wall time is whole nanoseconds from the clock.
    """
    boundary = "matutolabqualificationboundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\n{text}\r\n--{boundary}--\r\n").encode()
    started = time.monotonic_ns()
    status, audio = request(port, "POST", "/v1/audio/speech", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout)
    finished = time.monotonic_ns()
    if status != 200:
        raise VoiceError(f"The synthesis endpoint answered HTTP {status} instead of returning audio.")
    samples = len(audio) // (AUDIO_SAMPLE_BYTES * AUDIO_CHANNELS)
    duration_nanoseconds = samples * 1_000_000_000 // AUDIO_SAMPLE_RATE
    if duration_nanoseconds <= 0:
        raise VoiceError("The synthesis endpoint returned no audio; no real-time factor is assumed.")
    return {
        "state": "measured",
        "audio_bytes": len(audio),
        "sample_rate_hz": AUDIO_SAMPLE_RATE,
        "channels": AUDIO_CHANNELS,
        "sample_bytes": AUDIO_SAMPLE_BYTES,
        "sample_count": samples,
        "duration_nanoseconds": duration_nanoseconds,
        "wall_nanoseconds": finished - started,
        "started_ns": started,
        "finished_ns": finished,
    }


def generate(port, prompt, tokens, observed):
    """Send the stated generation load, recording when it started and finished."""
    observed["started_ns"] = time.monotonic_ns()
    try:
        # A model stops at its own end-of-text long before a large ceiling, so
        # the stated load is only the load that runs when the server ignores it.
        body = json.dumps({"prompt": prompt, "n_predict": tokens, "ignore_eos": True, "stream": False}).encode()
        status, payload = request(port, "POST", "/completion", body, {"Content-Type": "application/json"}, GENERATION_SECONDS)
        observed["status"] = status
        observed["response"] = json.loads(payload)
    except (OSError, ValueError, http.client.HTTPException) as error:
        observed["error"] = f"The generation request failed with {type(error).__name__}."
    finally:
        observed["finished_ns"] = time.monotonic_ns()


def read_identity(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {"state": "unavailable", "reason": "No effective run identity was captured for this qualification."}


def speech_command(spec):
    """The protected speech launcher invocation for one part, built once."""
    return [
        sys.executable, "-B", str(Path(__file__).resolve().parent / "matuto_runtime.py"),
        "speech", spec["role"], spec["backend"], spec["executable"], spec["model_path"],
        spec["host"], str(spec["port"]), "--", *spec.get("args", []),
    ]


def require_placement(selection, spec):
    """Refuse unless the card the selection names can be this part's device.

    The selection names the card by PCI address and by the UUID of whichever API
    the model runs on. A part may use the other API on that same card, so the
    placement is resolved from the part's own backend against that PCI address
    instead of comparing the two backend names, which would refuse a healthy
    runtime: a Vulkan synthesizer beside a HIP/ROCm router is one card, not a
    conflict.
    """
    try:
        runtime.resolve_device(selection, runtime.execution_environment(), spec["backend"])
    except runtime.InspectionError as error:
        raise PlacementRefusal(f"Synthesis profile {spec['profile']} has no place on the card the saved selection names: {error}") from None


def part(results, environment):
    document = {
        "schema": "matuto-lab-speech-part-summary/1",
        "outcome": ERROR,
        "performed": [],
        "bundle_qualification": dict(BUNDLE_NOT_ESTABLISHED),
        "definitions": {
            "occupancy_bytes": "The selected device's whole used-VRAM counter read after this runtime loaded, less the reading taken before it started. The counter belongs to the device, not to the process.",
            "readiness_seconds": "Wall time from spawning the owned speech runtime to its first HTTP response from the loopback root.",
        },
    }
    lease, device, baseline = None, None, None
    try:
        # Parsed inside the error path: a malformed part is a recorded error, not
        # a traceback raised before the module can write its own summary.
        try:
            spec = json.loads(environment["MATUTO_VOICE_PART"])
        except (KeyError, ValueError) as error:
            raise VoiceError(f"The lab did not supply the speech part as JSON: {error}") from None
        document["part"] = {name: spec[name] for name in ("profile", "display_name", "role", "backend", "executable", "host", "port")}
        if spec["role"] == "synthesis":
            lease = runtime.runtime_lease(False)
            selection = runtime.read_selection()
            if selection is None:
                raise PlacementRefusal("No runtime selection is configured, so synthesis has no physical card to be placed on; the lab refuses rather than measure a placement it cannot establish.")
            require_placement(selection, spec)
            device = runtime.vram_telemetry(selection["pci"])
            document["pci"] = selection["pci"]
            baseline = read_int(device)
            document["baseline_bytes"] = baseline
        command = speech_command(spec)
        bench.record_command(results, "command.txt", command)
        child = dict(environment, MATUTO_RUNTIME_IDENTITY_PATH=str(results / "runtime-identity.json"),
                     MATUTO_RUNTIME_MODEL_SHA256=spec["model_sha256"])
        require_free_port(spec["host"], spec["port"])
        started = time.monotonic()
        with contextlib.ExitStack() as stack:
            log = stack.enter_context(bench.artifact(results, "speech-server.log", "ab"))
            process = stack.enter_context(bench.owned(command, child, log, subprocess.STDOUT))
            wait_ready(spec["port"], process, "/", "speech runtime")
            document["readiness_seconds"] = round(time.monotonic() - started, 4)
            confirm_speech_identity(process, spec["executable"])
            document["performed"] += ["runtime_launch", "readiness", "identity"]
            if device is None:
                document["occupancy"] = {"state": "not_applicable", "reason": "Recognition runs on the CPU and holds no device memory."}
            else:
                held = read_int(device) - baseline
                document["occupancy"] = {"state": "measured", "bytes": held, "decimal_mb": round(held / 1e6, 3),
                                         "pci": document["pci"], "attribution": "selected_device_not_process_ownership"}
                document["performed"].append("occupancy")
            document["identity"] = read_identity(child["MATUTO_RUNTIME_IDENTITY_PATH"])
        document["outcome"] = QUALIFIED
        return 0
    except PlacementRefusal as error:
        document["outcome"] = REFUSED
        document["reason"] = str(error)
        print(f"Refused speech runtime {spec['profile']}: {error}", file=sys.stderr)
        return 1
    except (VoiceError, bench.BenchError, runtime.InspectionError) as error:
        document["reason"] = str(error)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception:
        document["reason"] = "Unexpected qualification error; no exception text is published in this artifact."
        print("Error: unexpected qualification error; its traceback follows on stderr.", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if lease is not None:
            os.close(lease)
        bench.write_summary(results, document)
        if document["outcome"] == QUALIFIED:
            print(f"Qualified speech runtime {spec['profile']}: {document['part']['role']} on {document['part']['backend']},"
                  f" occupancy {document['occupancy'].get('decimal_mb', 'not applicable')} MB.", file=sys.stderr)


def bundle(results, environment):
    document = {
        "schema": "matuto-lab-bundle-summary/1",
        "outcome": ERROR,
        "performed": [],
        "definitions": {
            "per_part_occupancy_bytes": "Reading the whole used-VRAM counter of the selected device after each part became ready, less the reading taken before it started. The counter belongs to the device, not to the process.",
            "combined_occupancy_bytes": "The highest reading taken while every part was resident, less the reading taken before any part started. This is the bundle's own footprint, not the card's total usage.",
            "capacity_bytes": "The selected card's own total VRAM counter.",
            "remaining_free_bytes": "Card capacity less the highest observed used-VRAM reading, including the pre-run baseline; not capacity less the bundle footprint.",
            "model_features": "Projector and MTP identities are verified launch requests. Their separate occupancy costs require matched baseline, projector-only, MTP-only and combined runs; model occupancy includes every enabled model feature.",
            "model_backend": "The API the saved runtime selection names for the model. It is not necessarily the API a speech runtime was placed through: every part's own placement constraint is recorded in part_identities.",
            "real_time_factor": "Generated audio duration divided by the synthesis request's own wall time, both in whole nanoseconds, measured while the model server reported generation in flight.",
            "placement_refusal": "When the lab cannot put the bundle's synthesis part on the selected physical card, it refuses rather than measure a placement it cannot establish.",
            "load": "The stated generation load is the load the measurement is taken under: synthesis must begin while it is in flight, and a factor above 1.0 counts only when that load spanned the whole synthesis, so a pass is never earned on an idle card.",
            "refusal": "Combined occupancy above card capacity, or a real-time factor at or below 1.0, refuses the bundle. Both measured numbers are recorded; nothing is rounded and nothing is retried silently.",
            "missing_telemetry": "A reading the run could not take is an instrumentation error. No zero is assumed, and no bundle passes without its numbers.",
            "part_qualification": "Qualifying a speech runtime alone never qualifies a bundle.",
        },
    }
    lease = None
    document["bundle"] = {"name": environment["MATUTO_VOICE_BUNDLE"], "display_name": environment["MATUTO_VOICE_BUNDLE_DISPLAY"]}
    try:
        load = int(environment["MATUTO_VOICE_LOAD_TOKENS"])
    except (KeyError, ValueError):
        load = 0
    document["generation_load"] = {
        "tokens": load,
        "prompt": environment.get("MATUTO_VOICE_LOAD_PROMPT") or DEFAULT_LOAD_PROMPT,
        "synthesis_text": DEFAULT_LOAD_TEXT,
    }
    try:
        # Parsed inside the error path: a malformed bundle is a recorded error,
        # not a traceback raised before the module can write its own summary.
        try:
            specs = json.loads(environment["MATUTO_VOICE_PARTS"])
            model = json.loads(environment["MATUTO_VOICE_MODEL"])
        except (KeyError, ValueError) as error:
            raise VoiceError(f"The lab did not supply the bundle's parts and model as JSON: {error}") from None
        document["parts"] = [{name: spec[name] for name in ("profile", "display_name", "role", "backend", "executable", "model_path", "model_sha256", "host", "port")} for spec in specs]
        document["model"] = {name: model[name] for name in ("profile", "display_name", "model_path", "model_sha256", "alias", "context_size", "mmproj", "mtp")}
        load = document["generation_load"]["tokens"]
        if load < 1:
            raise VoiceError("BUNDLE_LOAD_TOKENS must be a positive integer; the stated generation load is what synthesis is measured against.")
        if not any(spec["role"] == "synthesis" for spec in specs):
            raise VoiceError("A bundle requires at least one synthesis speech profile; a bundle with no synthesis part has no factor to measure.")
        ports = [model["port"], *(spec["port"] for spec in specs)]
        if len(set(ports)) != len(ports):
            raise VoiceError("The bundle's model and speech ports must be distinct: " + ", ".join(str(port) for port in ports) + ".")
        for port in ports:
            require_free_port("127.0.0.1", port)
        # One lease for the whole bundle, so a selection change mid-run cannot
        # spread its occupancy across two physical cards.
        lease = runtime.runtime_lease(False)
        selection = runtime.read_selection()
        if selection is None:
            raise PlacementRefusal("No runtime selection is configured, so the bundle's synthesis part has no physical card to be placed on; the lab refuses rather than measure a placement it cannot establish.")
        for spec in specs:
            if spec["role"] == "synthesis":
                require_placement(selection, spec)
        device = runtime.vram_telemetry(selection["pci"])
        capacity = read_int(runtime.vram_capacity(selection["pci"]))
        document["card"] = {"pci": selection["pci"], "model_backend": selection["backend"], "uuid": selection["corroborator"]["uuid"],
                            "capacity_bytes": capacity, "capacity_decimal_mb": round(capacity / 1e6, 3)}
        baseline = read_int(device)
        document["baseline_bytes"] = baseline
        document["performed"].append("baseline_occupancy")
        with contextlib.ExitStack() as stack:
            model_identity_path = results / "runtime-identity-model.json"
            bench.record_command(results, "model-command.txt", [environment["MATUTO_VOICE_LAB"], model["profile"], "serve"])
            model_child = dict(environment, MATUTO_RUNTIME_IDENTITY_PATH=str(model_identity_path))
            model_process = stack.enter_context(bench.owned([environment["MATUTO_VOICE_LAB"], model["profile"], "serve"], model_child,
                                                            stack.enter_context(bench.artifact(results, "model-server.log", "ab")), subprocess.STDOUT))
            wait_ready(model["port"], model_process, "/health", "model server", 200)
            model_identity = read_identity(str(model_identity_path))
            document["observed_build_revision"] = confirm_model(model["port"], model, model_process, model_identity)
            document["model"]["identity"] = model_identity
            document["performed"] += ["model_launch", "model_readiness", "model_identity"]
            synthesis_spec = next(spec for spec in specs if spec["role"] == "synthesis")
            previous = read_int(device)
            document["per_part_occupancy_bytes"] = {"model": previous - baseline}
            document["part_identities"] = {}
            for spec in specs:
                command = speech_command(spec)
                bench.record_command(results, f"part-{spec['profile']}-command.txt", command)
                child = dict(environment, MATUTO_RUNTIME_IDENTITY_PATH=str(results / f"runtime-identity-{spec['profile']}.json"),
                             MATUTO_RUNTIME_MODEL_SHA256=spec["model_sha256"])
                process = stack.enter_context(bench.owned(command, child, stack.enter_context(bench.artifact(results, f"part-{spec['profile']}.log", "ab")), subprocess.STDOUT))
                wait_ready(spec["port"], process, "/", f"{spec['role']} runtime {spec['profile']}")
                confirm_speech_identity(process, spec["executable"])
                held = read_int(device)
                # A recognition part runs on the CPU, so its own delta is zero by
                # construction; it is recorded as such rather than left out.
                document["per_part_occupancy_bytes"][spec["profile"]] = held - previous
                previous = held
                document["part_identities"][spec["profile"]] = read_identity(child["MATUTO_RUNTIME_IDENTITY_PATH"])
                document["performed"].append(f"{spec['role']}_launch")
            combined = read_int(device)
            document["performed"].append("combined_occupancy")
            observed = {}
            thread = threading.Thread(target=generate, args=(model["port"], document["generation_load"]["prompt"], load, observed))
            thread.start()
            wait_processing(model["port"], model_process, "model server")
            if "finished_ns" in observed:
                raise VoiceError("Generation finished before synthesis began; raise BUNDLE_LOAD_TOKENS so the factor is measured under load.")
            synthesis = synthesize(synthesis_spec["port"], document["generation_load"]["synthesis_text"])
            combined = max(combined, read_int(device))
            thread.join(timeout=GENERATION_SECONDS)
            if thread.is_alive():
                raise VoiceError("The generation request did not finish within its deadline.")
            document["performed"].append("synthesis_under_generation")
            if observed.get("error"):
                raise VoiceError(observed["error"])
            if observed.get("status") != 200:
                raise VoiceError(f"The generation request answered HTTP {observed.get('status')} instead of generating.")
            tokens_predicted = observed["response"].get("tokens_predicted")
            if not isinstance(tokens_predicted, int) or tokens_predicted < 1:
                raise VoiceError("The generation request produced no tokens; the factor was not measured under load.")
            generation_wall = observed["finished_ns"] - observed["started_ns"]
            # The factor is only a factor under the stated load, so the run proves
            # the load was in flight for the synthesis it timed. A synthesis that
            # outlives the load still has honest numbers: it is slow, and a slow
            # synthesis is refused. Only a factor above 1.0 needs the load to have
            # spanned the whole measurement, because otherwise the pass is not
            # earned under load at all.
            if not observed["started_ns"] <= synthesis["started_ns"]:
                raise VoiceError("Synthesis did not begin while the stated generation load was in flight; the factor was not measured under load.")
            spanned = synthesis["finished_ns"] <= observed["finished_ns"]
            document["remaining_free_bytes"] = capacity - combined
            document["combined_occupancy_bytes"] = combined - baseline
            document["combined_occupancy_decimal_mb"] = round(document["combined_occupancy_bytes"] / 1e6, 3)
            document["generation"] = {"requested_tokens": load, "tokens_predicted": tokens_predicted,
                                      "wall_nanoseconds": generation_wall, "wall_seconds": round(generation_wall / 1e9, 4),
                                      "spanned_synthesis": spanned}
            document["synthesis"] = synthesis
            if model["mtp"]["enabled"]:
                timings = observed["response"].get("timings", {})
                drafted, accepted = timings.get("draft_n"), timings.get("draft_n_accepted")
                document["generation"]["mtp"] = {"draft_n": drafted, "draft_n_accepted": accepted}
                if type(drafted) is not int or type(accepted) is not int or not 0 <= accepted <= drafted or drafted <= 0:
                    raise VoiceError("MTP was requested but generation did not report usable draft-token counters; no active MTP qualification is established.")
        decision = decide_bundle({
            "combined_occupancy_bytes": document["combined_occupancy_bytes"],
            "capacity_bytes": capacity,
            "audio_duration_nanoseconds": document["synthesis"]["duration_nanoseconds"],
            "synthesis_wall_nanoseconds": document["synthesis"]["wall_nanoseconds"],
        })
        if decision["outcome"] == QUALIFIED and not document["generation"]["spanned_synthesis"]:
            raise VoiceError("Synthesis outlived the stated generation load, so a factor above 1.0 would not have been measured under load; raise BUNDLE_LOAD_TOKENS and qualify again.")
        document["decision"] = decision
        document["outcome"] = decision["outcome"]
        if decision["outcome"] != QUALIFIED:
            # The refusal's own reason travels with the artifact, so a reader of
            # summary.json sees why without the terminal that ran it.
            document["reason"] = decision["reason"]
            print(f"Error: {decision['reason']}", file=sys.stderr)
        return 0 if decision["outcome"] == QUALIFIED else 1
    except PlacementRefusal as error:
        document["outcome"] = REFUSED
        document["reason"] = str(error)
        print(f"Refused bundle {document['bundle']['name']}: {error}", file=sys.stderr)
        return 1
    except (VoiceError, bench.BenchError, runtime.InspectionError) as error:
        document["outcome"] = ERROR
        document["reason"] = str(error)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception:
        document["outcome"] = ERROR
        document["reason"] = "Unexpected qualification error; no exception text is published in this artifact."
        print("Error: unexpected qualification error; its traceback follows on stderr.", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if lease is not None:
            os.close(lease)
        bench.write_summary(results, document)
        if document.get("decision", {}).get("outcome") == QUALIFIED:
            print(f"Qualified bundle {document['bundle']['name']}: {document['combined_occupancy_decimal_mb']} MB occupied of "
                  f"{document['card']['capacity_decimal_mb']} MB, real-time factor {document['decision']['numbers']['real_time_factor']:.3f}.", file=sys.stderr)


def interrupted(*_):
    raise VoiceError(TERMINATION_ERROR)


def main(argv):
    if argv and argv[0] in ("part", "bundle") and len(argv) == 2:
        return (part if argv[0] == "part" else bundle)(Path(argv[1]), dict(os.environ))
    raise VoiceError("Usage: matuto_voice.py part RESULTS_DIR | bundle RESULTS_DIR")


if __name__ == "__main__":
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, interrupted)
    try:
        sys.exit(main(sys.argv[1:]))
    except VoiceError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
