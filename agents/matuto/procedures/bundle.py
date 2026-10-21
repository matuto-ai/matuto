"""Run bundle qualification through the store-backed lab and record its checks."""

import json
import os
from pathlib import Path
import tempfile

from matuto import matuto_runtime
from matuto import progress, store, store_lab
from matuto.procedure_support import evidence as write_evidence

REVISION = "bundle-v1"


def _measurement(value):
    return type(value) is int and value >= 0


def _checks(summary, phase="", results_dir=None):
    performed = set(summary.get("performed", ()))
    parts = summary.get("parts", ())
    required = {"model server", *(f"{part['role']} runtime {part['profile']}" for part in parts)}
    readiness_path = Path(results_dir or "") / "parts-started.txt"
    try:
        started = set(readiness_path.read_text().splitlines())
    except OSError:
        started = set()
    all_started = ({"model_launch", "synthesis_launch", "recognition_launch"} <= performed
                   or bool(parts) and required <= started)
    decision = summary.get("decision", {})
    numbers = decision.get("numbers", {})
    occupancy = summary.get("combined_occupancy_bytes", numbers.get("combined_occupancy_bytes"))
    capacity = summary.get("card", {}).get("capacity_bytes", numbers.get("capacity_bytes"))
    duration = summary.get("synthesis", {}).get("duration_nanoseconds", numbers.get("audio_duration_nanoseconds"))
    wall = summary.get("synthesis", {}).get("wall_nanoseconds", numbers.get("synthesis_wall_nanoseconds"))

    def status(reached, measured, passed):
        if not reached:
            return "SKIP"
        if not measured:
            return "ERROR"
        return "PASS" if passed else "FAIL"

    fit_measured = _measurement(occupancy) and type(capacity) is int and capacity > 0
    realtime_measured = type(duration) is int and type(wall) is int and duration > 0 and wall > 0
    recognition_path = Path(results_dir or "") / "recognition.json"
    recognition = decision.get("recognition", {})
    if not recognition:
        try:
            recognition = json.loads(recognition_path.read_text())
        except (OSError, ValueError):
            recognition = {}
    recognition_measured = recognition.get("status") == "measured"
    synthesis_reached = all_started and (
        phase in ("bundle synthesis under load", "recognition round trip") or "decision" in summary)
    recognition_reached = all_started and (
        phase == "recognition round trip" or "recognition" in decision or recognition_path.exists())
    realtime_status = status(all_started and synthesis_reached, realtime_measured,
                             realtime_measured and duration > wall)
    if realtime_status == "PASS" and summary.get("generation", {}).get("spanned_synthesis") is not True:
        realtime_status = "ERROR"
    transcript = recognition.get("text")
    text_state = ("non-empty" if isinstance(transcript, str) and transcript.strip() else
                  "empty" if isinstance(transcript, str) else "unavailable")
    word_count = recognition.get("word_count", "unavailable")
    if type(word_count) is not int or word_count < 0:
        word_count = "unavailable"
    return [
        {"name": "fit", "status": status(all_started, fit_measured,
         fit_measured and occupancy <= capacity),
         "detail": f"combined occupancy {occupancy} bytes; card capacity {capacity} bytes"},
        {"name": "real-time", "status": realtime_status,
         "detail": f"audio duration {duration} ns; synthesis wall time {wall} ns"},
        {"name": "recognition", "status": status(recognition_reached,
         recognition_measured, recognition_measured and recognition.get("passed") is True),
         "detail": (f"text {text_state}; matching words {word_count}; "
                    f"recognition time {recognition.get('wall_nanoseconds', 'unmeasured')} ns; "
                    f"clip duration {recognition.get('audio_duration_nanoseconds', 'unmeasured')} ns")},
    ], all_started


def _phase():
    path = os.environ.get(progress.FILE_VARIABLE)
    if not path:
        return ""
    try:
        reported = progress.latest(path)
    except OSError:
        return ""
    return reported[0] if reported else ""


def run(opened, record):
    """Execute one stored bundle at the runner's admitted fingerprint."""
    evidence = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"])
    measurement = evidence / "bundle-measurement"
    measurement.mkdir(mode=0o700)
    initial = [{"name": name, "status": "SKIP", "detail": "bundle parts did not all start"}
               for name in ("fit", "real-time", "recognition")]
    admitted = dict(record, fingerprint=store.fingerprint(opened, record))
    write_evidence("bundle", admitted, initial, "bundle parts did not all start")
    progress.report("loading bundle parts", "starting model, synthesis and recognition")
    try:
        detected = matuto_runtime.pci_devices()
        if detected.get("state") != "completed":
            raise store.StoreError("GPU detection failed; the admitted bundle cannot be verified.")
        spec = store.launch_spec(opened, "bundle", record["bundle_id"],
                                 detected_gpus=detected.get("devices", ()))
        with tempfile.TemporaryDirectory(prefix="matuto-store-lab-") as home:
            environment = store_lab._environment(home)
            environment["MATUTO_RUN_RESULTS_DIR"] = str(measurement)
            store_lab._prepare_runtime(spec, environment)
            lab_status = store_lab._bundle_environment(spec, environment)
        summary = json.loads((measurement / "summary.json").read_text())
    except (OSError, ValueError, store.StoreError) as error:
        parts = [{"role": role, "profile": profile}
                 for role, profile in record.get("members", {}).items()
                 if role in ("synthesis", "recognition")]
        checks, all_started = _checks({"parts": parts}, _phase(), measurement)
        write_evidence("bundle", admitted, checks, str(error))
        return 0 if all_started else 1
    phase = _phase()
    checks, all_started = _checks(summary, phase, measurement)
    text = (f"Bundle {record['name']}: {summary.get('outcome', 'error')}. "
            f"{summary.get('reason', 'See the bundle measurement artifact for details.')}")
    write_evidence("bundle", spec["record"], checks, text)
    progress.report("bundle checks", "fit, real-time and recognition recorded")
    # A measured FAIL or ERROR is a completed run with a failed check; startup
    # failure is an errored run because the bundle was never fully assembled.
    return 0 if all_started else (lab_status or 1)
