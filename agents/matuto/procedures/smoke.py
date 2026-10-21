"""Run a bounded smoke and check its reply against the requested text."""

import errno
import os
import pty
import select
import subprocess
import sys
from pathlib import Path

from matuto import store, store_lab
from matuto.procedure_support import evidence, report, setup

PTY = True
OUTPUT_LIMIT = 1024 * 1024


def _run_smoke(spec, environment):
    record = spec["record"]
    environment["MATUTO_RUNTIME_MODEL_SHA256"] = record["files"]["model"]["sha256"]
    for name in ("MODEL_MMPROJ", "MODEL_MTP", "MATUTO_RUNTIME_MMPROJ_SHA256",
                 "MATUTO_RUNTIME_MTP_SHA256"):
        environment.pop(name, None)
    if "mmproj" in record["files"] or "mtp" in record["files"]:
        raise store.StoreError("Smoke is text-only; use a bundle run for model features.")
    if store_lab._check_launch_arguments(record["argument_rows"], environment):
        raise store.StoreError("stored launch arguments could not be verified")
    arguments = store_lab._smoke_arguments(record)
    prompt = arguments[arguments.index("-p") + 1]
    _, marker, expected = prompt.partition("Reply with exactly: ")
    if not marker or not expected:
        raise store.StoreError("the smoke prompt does not specify the exact reply to check")
    conversation = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"]) / "smoke-conversation.txt"
    stored_options = {row["option"] for row in record["argument_rows"] if row["tool"] == "smoke"}
    for required in ("--simple-io", "--single-turn"):
        if required not in stored_options:
            arguments.append(required)
    command = [sys.executable, "-B", str(store_lab.RUNTIME), "launch", "smoke", "llama-cli",
               str(store_lab._runtime_gpu_layers(record)), "--", *arguments,
               "-o", str(conversation)]

    master, slave = pty.openpty()
    output, diagnostics = bytearray(), bytearray()
    truncated = diagnostics_truncated = False
    process = None
    try:
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=slave, stderr=subprocess.PIPE, close_fds=True)
        os.close(slave)
        slave = -1
        streams = {master: output, process.stderr.fileno(): diagnostics}
        while streams:
            ready, _, _ = select.select(list(streams), [], [])
            for descriptor in ready:
                try:
                    chunk = os.read(descriptor, 4096)
                except OSError as error:
                    if descriptor == master and error.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                if not chunk:
                    streams.pop(descriptor)
                    if descriptor != master:
                        process.stderr.close()
                    continue
                target = streams[descriptor]
                if descriptor == master:
                    truncated |= len(target) + len(chunk) > OUTPUT_LIMIT
                else:
                    diagnostics_truncated |= len(target) + len(chunk) > OUTPUT_LIMIT
                if len(target) < OUTPUT_LIMIT:
                    target.extend(chunk[:OUTPUT_LIMIT - len(target)])
        status = process.wait()
    except BaseException:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        if slave >= 0:
            os.close(slave)
        os.close(master)
    if diagnostics:
        sys.stderr.buffer.write(diagnostics)
        sys.stderr.flush()
    if diagnostics_truncated:
        print("[runtime diagnostics exceeded the capture limit]", file=sys.stderr)
    text = output.decode("utf-8", "replace")
    transcript_size = conversation.stat().st_size if conversation.exists() else 0
    if conversation.exists():
        with conversation.open("rb") as stream:
            transcript = stream.read(OUTPUT_LIMIT)
    else:
        transcript = b""
    transcript_text = transcript.decode("utf-8", "replace")
    header = f"User:\n{prompt}\n\nAssistant:\n"
    envelope = (transcript_text.startswith(header)
                and transcript_text.count("Assistant:\n") == 1
                and transcript_text.endswith("\n\n"))
    reply = transcript_text[len(header):-2] if envelope else None
    exact = transcript_size <= OUTPUT_LIMIT and reply == expected
    if status:
        outcome, detail = "ERROR", f"runtime exited with status {status}; smoke did not pass"
    elif truncated or transcript_size > OUTPUT_LIMIT:
        outcome, detail = "ERROR", "runtime output exceeded the capture limit; exact reply could not be verified"
    elif not envelope or not reply:
        outcome, detail = "ERROR", "runtime returned no reply"
    elif exact:
        outcome, detail = "PASS", "runtime reply exactly matched the requested text"
    else:
        outcome, detail = "FAIL", "runtime reply differed from the requested text"
    return (prompt, expected, text, diagnostics, transcript,
            {"name": "exact reply", "status": outcome, "detail": detail},
            truncated, diagnostics_truncated)


def run(snapshot, record):
    check = {"name": "exact reply", "status": "SKIP",
             "detail": "smoke execution did not reach the reply check"}
    evidence_record = dict(record, fingerprint=store.fingerprint(snapshot, record))
    evidence("smoke", evidence_record, [check], check["detail"])
    spec, environment, home = setup(snapshot, record)
    try:
        report("smoke", "running the exact-reply check")
        prompt, expected, output, diagnostics, transcript, check, truncated, diagnostics_truncated = (
            _run_smoke(spec, environment))
        evidence("smoke", spec["record"], [check],
                 f"Prompt: {prompt}\nExpected reply: {expected}\n{check['detail']}",
                 **{"smoke-output.txt": output,
                    "smoke-conversation.txt": transcript,
                    "smoke-output-truncated.txt": str(bool(truncated)).lower(),
                    "smoke-stderr.txt": diagnostics,
                    "smoke-stderr-truncated.txt": str(bool(diagnostics_truncated)).lower()})
        report("smoke", "exact-reply check complete")
        return 0
    finally:
        home.cleanup()
