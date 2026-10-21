"""Store-backed speech entry; keep adaptations out of the legacy helper."""

import io
import json
import os
import re
import signal
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

AGENTS = Path(__file__).resolve().parents[1]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import matuto_voice as voice

_legacy_owned = voice.bench.owned
_legacy_confirm_model = voice.confirm_model
_state = {"model_pid_file": None, "audio": None, "recognition": None}


def _owned(command, environment, stdout, stderr):
    if environment.get("MATUTO_LAB_STORE_BACKED") == "1":
        if command and command[0] == environment.get("MATUTO_VOICE_LAB"):
            identity_path = environment.get("MATUTO_RUNTIME_IDENTITY_PATH")
            if identity_path:
                _state["model_pid_file"] = Path(identity_path).with_suffix(".pid")
        if len(command) > 6 and command[3] == "speech":
            try:
                parts = json.loads(environment["MATUTO_VOICE_PARTS"])
            except (KeyError, TypeError, ValueError):
                raise voice.VoiceError("The stored speech executable identities are unavailable.") from None
            part = next((item for item in parts if item.get("executable") == command[6]), None)
            if part is None or not part.get("executable_sha256"):
                raise voice.VoiceError("The stored speech executable has no recorded SHA-256.")
            environment = dict(environment, MATUTO_RUNTIME_EXPECTED_SHA256=part["executable_sha256"])
    return _legacy_owned(command, environment, stdout, stderr)


def _confirm_model(port, spec, process, identity):
    pid_file = _state.get("model_pid_file")
    if pid_file is not None:
        try:
            process = SimpleNamespace(pid=int(pid_file.read_text()))
        except (OSError, ValueError):
            raise voice.VoiceError("The stored model launch identity is unavailable.") from None
    return _legacy_confirm_model(port, spec, process, identity)


_legacy_request = voice.request
_legacy_synthesize = voice.synthesize
_legacy_decide_bundle = voice.decide_bundle


def _request(port, method, path, *args, **kwargs):
    response = _legacy_request(port, method, path, *args, **kwargs)
    if method == "POST" and path == "/v1/audio/speech":
        _state["audio"] = response
    return response


def _synthesize(port, text, timeout=voice.GENERATION_SECONDS):
    result = _legacy_synthesize(port, text, timeout)
    try:
        parts = json.loads(os.environ["MATUTO_VOICE_PARTS"])
        recognition = next(item for item in parts if item.get("role") == "recognition")
        status, audio = _state["audio"]
        if status != 200 or not audio:
            raise ValueError("synthesis returned no clip for recognition")
        clip = io.BytesIO()
        with wave.open(clip, "wb") as output:
            output.setnchannels(voice.AUDIO_CHANNELS)
            output.setsampwidth(voice.AUDIO_SAMPLE_BYTES)
            output.setframerate(voice.AUDIO_SAMPLE_RATE)
            output.writeframes(audio)
        from matuto import progress
        progress.report("recognition round trip", "sending synthesized clip to recognition")
        boundary = "matutorecognitionboundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + clip.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        started = time.monotonic_ns()
        answer_status, payload = _legacy_request(
            recognition["port"], "POST", "/inference", body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout)
        elapsed = time.monotonic_ns() - started
        if answer_status != 200:
            raise ValueError(f"recognition endpoint answered HTTP {answer_status}")
        answer = json.loads(payload)
        transcript = answer.get("text")
        if not isinstance(transcript, str):
            raise ValueError("recognition endpoint returned no text field")
        words = re.findall(r"\b[\w']+\b", text.lower())
        recognized = set(re.findall(r"\b[\w']+\b", transcript.lower()))
        duration = result["duration_nanoseconds"]
        _state["recognition"] = {
            "status": "measured", "text": transcript, "wall_nanoseconds": elapsed,
            "audio_duration_nanoseconds": duration,
            "word_count": sum(word in recognized for word in words),
            "passed": bool(transcript.strip()) and elapsed < duration,
        }
    except (KeyError, StopIteration, TypeError, ValueError, OSError, wave.Error) as error:
        _state["recognition"] = {"status": "error", "reason": str(error)}
    return result


def _decide_bundle(observations):
    decision = _legacy_decide_bundle(observations)
    recognition = _state.get("recognition")
    result = recognition or {"status": "error", "reason": "No recognition round trip was measured."}
    decision["recognition"] = result
    if os.environ.get("MATUTO_VOICE_BUNDLE"):
        try:
            (Path(os.environ["MATUTO_RUN_RESULTS_DIR"]) / "recognition.json").write_text(
                json.dumps(result, sort_keys=True))
        except OSError:
            pass
    if decision["outcome"] == voice.QUALIFIED:
        if recognition is None or recognition["status"] == "error":
            decision.update(outcome=voice.ERROR,
                            reason="The recognition round trip could not be measured.")
        elif not recognition["passed"]:
            decision.update(outcome=voice.REFUSED,
                            reason="Recognition returned empty text or took at least as long as the clip.")
    return decision


def _progress_wait_ready(port, process, path, what, status=None):
    bundle = os.environ.get("MATUTO_VOICE_BUNDLE")
    if bundle:
        from matuto import progress
        progress.report("loading bundle parts", f"waiting for {what}")
    answer = _legacy_wait_ready(port, process, path, what, status)
    if bundle:
        with open(Path(os.environ["MATUTO_RUN_RESULTS_DIR"]) / "parts-started.txt",
                  "a", encoding="utf-8") as report:
            report.write(what + "\n")
    return answer


def _progress_synthesize(port, text, timeout=voice.GENERATION_SECONDS):
    from matuto import progress
    progress.report("bundle synthesis under load", "synthesizing clip")
    return _synthesize(port, text, timeout)


_legacy_wait_ready = voice.wait_ready
voice.bench.owned = _owned
voice.confirm_model = _confirm_model
voice.request = _request
voice.synthesize = _progress_synthesize
voice.decide_bundle = _decide_bundle
voice.wait_ready = _progress_wait_ready


def main(argv):
    return voice.main(argv)


if __name__ == "__main__":
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, voice.interrupted)
    try:
        sys.exit(main(sys.argv[1:]))
    except voice.VoiceError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
