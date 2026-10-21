#!/usr/bin/env python3
"""Host-only WQ observations. Test outcomes are not profile approval decisions."""
import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "agents"))
import matuto_runtime as runtime
from matuto import progress as matuto_progress
from matuto import store_lab

DEFAULTS = {
    "first_byte_seconds": 35, "short_total_seconds": 90,
    "generation_tokens_per_second": 20, "vram_mb": 30000,
    "rss_kib": 33554432, "cancel_recovery_seconds": 5,
    "restart_seconds": 300, "long_execution_seconds": 1800,
    "retrieval_samples": 3, "retrieval_generation_tokens": 8192,
}
# amdgpu hands a dead server's device memory back after the process is reaped,
# so the counter read straight after a stop can still be the previous server's
# allocation. The device has given the memory back when the counter returns to
# the baseline that server started from; the allowance is desktop drift between
# two starts, hundreds of MB against a model's tens of GB.
VRAM_DRIFT_ALLOWANCE_BYTES = 2_000_000_000
VRAM_RELEASE_LIMIT_SECONDS = 30
VRAM_RELEASE_INTERVAL_SECONDS = .2
# How often a long-context transfer's live slot counters are sampled. The last
# sample before the deadline is the record of what the request had achieved.
PROGRESS_SAMPLE_SECONDS = 1
# The one slot this run qualifies: the slot id is the report key, the context it
# serves and the long-context check bound to it. It is filled from the pinned
# store launch spec before anything runs, because a worker-qualification
# run takes only that spec and reads no profile file; there is no shipped table.
SLOTS = {}


def long_band(context):
    """The counted-input band a slot's long-context retrieval aims at.

    Smaller windows use about 1% granularity so rounding does not shift the
    target far below 88%; large windows retain readable 5,000-token steps.
    """
    quantum = 5000 if context >= 98304 else max(1, context // 100)
    low = max(1, context * 88 // 100 // quantum * quantum)
    width = 1000 if context >= 90000 else max(1, context // 100)
    return low, min(context, low + width)


def long_case(slot, context):
    low, high = long_band(context)
    return ("Context", f"Can the {slot.upper()} profile retrieve three values from a roughly {low // 1000}K-token input?",
            f"{low}–{high} server-counted input tokens per sample, each sample carrying its own three values, all three values returned in complete JSON, server recovers. "
            "PASS requires unanimity across the scored samples; a sample that produced no scorable answer, whether it hit the generation bound, exhausted the context, or reached the execution deadline, is unscored rather than a retrieval failure. "
            "This is sampled retrieval, not general long-document quality.")


def retrieval_sentinels(sample):
    """The three values one long-context sample extracts; a different set per sample.

    Each position keeps its own number and each sample shifts all three, so the
    three values are unrelated within a sample and no two samples share a prompt.
    Repeating one prompt would let the server's prefix cache answer later samples
    from the same computed keys, and one number for all three positions would let
    a model that read START fill in MIDDLE and END without retrieving them.
    """
    return {"start": f"cobalt-{17 + sample}", "middle": f"cedar-{29 + sample}", "end": f"ember-{43 + sample}"}


# These are the public questions and criteria, not generated model text. Each
# slot's long-context row is derived from its declared context, so a new slot
# states its band the same way the existing two do.
CASES = {
    "WQ-01": ("Tools", "Can it choose a lookup, fill its arguments, and avoid an unnecessary call?", "Two auto-choice lookups request the correct ticket and fields; a no-lookup task makes no tool call; API responses complete normally."),
    "WQ-02": ("Structured output", "Can it return the requested machine-readable record?", "HTTP 200, normal completion, exact JSON keys, types and requested values."),
    "WQ-03H": ("Template interoperability", "Does the template preserve earlier reasoning across turns and a tool result?", "Synthetic earlier and later reasoning plus the tool result survive /apply-template with preservation enabled; control behavior is recorded, not inferred from model recall."),
    "WQ-03": ("Tool results", "Does it use the supplied tool result rather than always answering the same state?", "Two different correlated results produce their respective exact typed final answers, with no extra call or truncation."),
    **{slot["long_check"]: long_case(name, slot["context"]) for name, slot in SLOTS.items()},
    "WQ-06": ("Performance", "Do two one-slot requests queue, finish correctly and meet the declared resource limits?", "Processing and deferral are observed; both requests complete, the task answer is correct, measured speed/HTTP first-byte/total time and sampled memory meet limits, and queue metrics return to zero."),
    "WQ-07": ("Tool errors", "Can it follow an explicit one-retry policy after invalid tool data?", "Exactly one correlated retry of the same read-only lookup, then an exact typed answer from corrected data. Natural-language explanations are not scored by word searches."),
    "WQ-08": ("HTTP resilience", "Does an incomplete upload time out instead of keeping the connection open?", "With an isolated two-second read timeout, the server closes/resets or returns 400/408 between 1.5 and 4 seconds; client timeout is not success; a normal-profile request succeeds afterward."),
    "WQ-09": ("Server resilience", "Does interrupting and restarting the owned server recover the same configuration?", "Work is active before interruption, no complete successful old response survives, the same model/build/template/context returns within the restart limit, and a new task succeeds."),
    "WQ-10": ("API boundary", "Do input overflow and generation at the context boundary stop safely?", "Oversized input receives the pinned structured HTTP 400; a near-full input generates until the server reports context exhaustion without shifting; the slot is idle and a new task succeeds."),
    "WQ-11": ("Cancellation", "Does cancelling an active stream release work promptly?", "HTTP 200 and a generated SSE delta are observed before cancellation, no terminal event has already arrived, the slot is released within the recovery limit, and a new task succeeds."),
}
LONG_CHECK = "WQ-LONG"


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Finding(Exception):
    """Only call with developer-authored reasons and bounded, non-content observations."""
    def __init__(self, status, reason, **observations):
        super().__init__(reason)
        self.status, self.reason, self.observations = status, reason, observations


def require(condition, reason, **observations):
    if not condition:
        raise Finding("FAIL", reason, **observations)


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


class InvalidLimitError(ValueError):
    def __init__(self, field, message):
        self.field = field
        super().__init__(message)


def validate_limits(overrides=None):
    """Freeze positive WQ limits and refuse malformed start-request values."""
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict) or any(key not in DEFAULTS for key in overrides):
        raise ValueError("WQ limits must name supported limits")
    limits = dict(DEFAULTS)
    for key, value in overrides.items():
        if not numeric(value) or value <= 0:
            raise InvalidLimitError(key, "WQ limits must be finite positive numbers")
        if key in ("retrieval_samples", "retrieval_generation_tokens") and int(value) != value:
            raise InvalidLimitError(key, f"{key.replace('_', '-')} must be a whole number")
        limits[key] = value
    return limits


def vram_fields(prefix, value):
    return {prefix + "_bytes": value, prefix + "_decimal_mb": round(value / 1e6, 3), prefix + "_mib": round(value / 1048576, 3)}


def finish_code(body):
    try:
        value = body["choices"][0]["finish_reason"]
    except (KeyError, IndexError, TypeError):
        return "missing"
    return value if value in ("stop", "length", "tool_calls") else "unknown"


def generation_observations(body):
    """Publish only bounded counters/flags, never verbose prompts or generated text."""
    result = {"finish_reason": finish_code(body)}
    if type(body) is not dict:
        return result
    usage = body.get("usage")
    if type(usage) is dict:
        for key in ("prompt_tokens", "completion_tokens"):
            value = usage.get(key)
            if type(value) is int and 0 <= value <= 2**31 - 1:
                result[key] = value
    detail = body.get("__verbose")
    if type(detail) is dict and type(detail.get("truncated")) is bool:
        result["context_exhausted"] = detail["truncated"]
    return result


def counter_value(text, name):
    """One non-negative Prometheus counter, or None when it is absent or ambiguous."""
    matches = re.findall(r"^llamacpp:" + name + r"(?:\{[^\n]*\})?\s+([0-9.eE+-]+)\s*$", text, re.M)
    if len(matches) != 1:
        return None
    value = float(matches[0])
    return value if numeric(value) and value >= 0 else None


def generation_split(body):
    """The server's own prefill/generation split, or None when it reported none.

    A scored answer arrives in one piece, so the transfer's first-byte and total
    times cannot separate prompt processing from generation; the response
    timings can. Both halves are reported the same way, so a row can say which
    half was slow at the depth it ran at.
    """
    timing = body.get("timings") if type(body) is dict else None
    if type(timing) is not dict:
        return None
    result = {}
    for half, prefix in (("prefill", "prompt"), ("generation", "predicted")):
        tokens, milliseconds, rate = timing.get(prefix + "_n"), timing.get(prefix + "_ms"), timing.get(prefix + "_per_second")
        if not (type(tokens) is int and tokens >= 0 and numeric(milliseconds) and milliseconds >= 0 and numeric(rate) and rate >= 0):
            return None
        result[half + "_tokens"] = tokens
        result[half + "_seconds"] = round(milliseconds / 1000, 3)
        result[half + "_tokens_per_second"] = rate
    return result


def progress_observations(samples):
    """What a long-context request had achieved when its deadline arrived.

    A transfer killed at its deadline leaves no answer body, so the only record
    of how far it got is live sampling. The token counts come from the server's
    slot table, which counts prompt tokens and decoded tokens while the request
    runs; the cumulative prediction counters used to read zero here because
    they only advance when a request finishes. Each sample carries the elapsed
    time it was taken, so the generation rate is the decoded tokens since the
    last sample that saw generation unstarted, over the time since that sample.
    """
    last = samples[-1]
    unstarted = [sample for sample in samples if sample["generation_tokens"] == 0]
    window_start = unstarted[-1]["elapsed_seconds"] if unstarted else 0.0
    result = {"progress_prefill_tokens": last["prefill_tokens"],
              "progress_generation_tokens": last["generation_tokens"]}
    if last["generation_tokens"] == 0:
        result["progress_generation_seconds"] = 0.0
        return result
    generation_seconds = round(last["elapsed_seconds"] - window_start, 3)
    result["progress_prefill_seconds"] = window_start
    result["progress_generation_seconds"] = generation_seconds
    if window_start > 0:
        result["progress_prefill_tokens_per_second"] = round(last["prefill_tokens"] / window_start, 3)
    if generation_seconds > 0:
        result["progress_generation_tokens_per_second"] = round(last["generation_tokens"] / generation_seconds, 3)
    return result


def deadline_observations(samples):
    """What one long-context request had achieved when its deadline arrived.

    Returns the progress observations, or the reason none are available. The
    rate, when there is one, is compared with the declared minimum by the caller.
    """
    if not samples:
        return {"deadline_progress_available": False,
                "deadline_progress_reason": "The server's live slot counters could not be read while the request ran, so no achieved progress is recorded."}
    progress = progress_observations(samples)
    result = {"deadline_progress_available": True, **progress}
    if progress.get("progress_generation_tokens_per_second") is None:
        result["generation_rate_evaluated"] = False
        result["generation_rate_evaluation_reason"] = (
            "Generation had not started before the deadline, so no rate was achieved to compare."
            if progress["progress_generation_tokens"] == 0 else
            "No generation time was observed before the deadline, so no rate could be compared.")
    else:
        result["generation_rate_evaluated"] = True
    return result


def message(body, finish="stop"):
    observed = generation_observations(body)
    code = observed["finish_reason"]
    if code == "length":
        exhausted = observed.get("context_exhausted") is True
        reason = "Context exhausted before completion." if exhausted else "Generation stopped at a limit; context exhaustion was not confirmed."
        raise Finding("FAIL", reason + " Answer correctness was not evaluated.", **observed, required_finish_reason=finish, failure_kind="context_exhausted" if exhausted else "generation_limit", correctness_evaluated=False)
    require(code == finish, "Response did not complete with the required finish reason.", **observed, required_finish_reason=finish)
    try:
        value = body["choices"][0]["message"]
        require(type(value) is dict, "Assistant message is not an object.")
        return value
    except (KeyError, IndexError, TypeError) as error:
        raise Finding("FAIL", "Response lacks an assistant message.") from error


def exact_object(value, expected):
    require(type(value) is dict, "Structured answer is not a JSON object.")
    require(value.keys() == expected.keys(), "Structured answer has missing or extra fields.", missing_fields=len(expected.keys() - value.keys()), extra_fields=len(value.keys() - expected.keys()))
    # Report field names from our fixed task, never the model's keys or values.
    wrong_types = [key for key in expected if type(value[key]) is not type(expected[key])]
    wrong_values = [key for key in expected if value[key] != expected[key]]
    require(not wrong_types and not wrong_values, "Structured answer differs from the supplied task or tool result.", wrong_type_fields=wrong_types, wrong_value_fields=wrong_values)


def answer(body, expected):
    msg = message(body)
    require(not msg.get("tool_calls"), "Final answer made an unexpected tool call.")
    try:
        value = json.loads(msg.get("content"))
    except (TypeError, ValueError) as error:
        raise Finding("FAIL", "Final answer is not parseable JSON.") from error
    exact_object(value, expected)


def tool_call(body, ticket, fields):
    msg = message(body, "tool_calls")
    calls = msg.get("tool_calls")
    require(type(calls) is list and len(calls) == 1, "Expected exactly one tool call.", tool_call_count=len(calls) if type(calls) is list else 0)
    call = calls[0]
    require(type(call) is dict and type(call.get("id")) is str and bool(call["id"]), "Tool call lacks a correlation ID.")
    function = call.get("function")
    require(type(function) is dict and function.get("name") == "lookup_ticket", "Wrong tool selected.")
    try:
        args = json.loads(function.get("arguments"))
    except (TypeError, ValueError) as error:
        raise Finding("FAIL", "Tool arguments are not parseable JSON.") from error
    require(type(args) is dict and args.keys() == {"ticket", "fields"}, "Tool arguments have missing or extra fields.")
    require(type(args["ticket"]) is int and args["ticket"] == ticket, "Tool arguments do not match the requested ticket.", ticket_matches=False)
    actual = args["fields"]
    require(type(actual) is list and all(type(x) is str for x in actual) and sorted(actual) == sorted(fields), "Tool arguments do not match the requested field set.", fields_match=False)
    return msg, call


def json_task(prompt, expected):
    types = {int: "integer", str: "string", bool: "boolean"}
    return {
        "model": "qwen3-coder", "temperature": 0, "max_tokens": -1,
        "verbose": True, "response_fields": ["truncated"],
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {"schema": {
            "type": "object", "properties": {k: {"type": types[type(v)]} for k, v in expected.items()},
            "required": list(expected), "additionalProperties": False,
        }}},
    }


def schema_task():
    expected = {"ticket": 18, "state": "open", "actionable": True}
    return json_task("Return ticket 18 with state open and actionable true as JSON.", expected), expected


def lookup_task(ticket=18, fields=("title", "state")):
    return {
        "model": "qwen3-coder", "temperature": 0, "max_tokens": -1,
        "verbose": True, "response_fields": ["truncated"],
        "tool_choice": "auto", "parallel_tool_calls": False,
        "messages": [{"role": "user", "content": f"Look up ticket {ticket} and request only these fields: {', '.join(fields)}."}],
        "tools": [
            {"type": "function", "function": {"name": "lookup_ticket", "description": "Look up selected ticket fields by its numeric ID.", "parameters": {
                "type": "object", "properties": {"ticket": {"type": "integer"}, "fields": {"type": "array", "items": {"enum": ["title", "state"]}, "minItems": 1, "maxItems": 2}},
                "required": ["ticket", "fields"], "additionalProperties": False,
            }}},
            {"type": "function", "function": {"name": "lookup_user", "description": "Look up a user by login.", "parameters": {
                "type": "object", "properties": {"login": {"type": "string"}}, "required": ["login"], "additionalProperties": False,
            }}},
        ],
    }


def load_task(stream=False, tokens=2048):
    return {"model": "qwen3-coder", "temperature": 0, "max_tokens": tokens, "ignore_eos": True, "stream": stream, "messages": [{"role": "user", "content": "Write the integers from 1 upward, one per line."}]}


def sse_state(raw):
    """Parse only complete SSE events; a partial final event is not proof of activity."""
    chunks = raw.decode("utf-8", "replace").replace("\r\n", "\n").split("\n\n")[:-1]
    generated = terminal = False
    for chunk in chunks:
        data = "\n".join(line[5:].lstrip() for line in chunk.splitlines() if line.startswith("data:"))
        if not data:
            continue
        if data == "[DONE]":
            terminal = True
            continue
        try:
            event = json.loads(data)
            choices = event.get("choices", [])
            for choice in choices:
                terminal |= choice.get("finish_reason") is not None
                delta = choice.get("delta", {})
                generated |= any(type(delta.get(k)) is str and bool(delta[k]) for k in ("content", "reasoning_content"))
        except (ValueError, AttributeError, TypeError) as error:
            raise Finding("FAIL", "Stream contains an invalid completed SSE event.") from error
    return generated, terminal


class Transfer:
    def __init__(self, transport, path, payload, timeout):
        self.owner = transport
        self.started = time.monotonic()
        self.timeout = timeout
        stem = transport.raw / f"http-{transport.counter}"
        transport.counter += 1
        self.body = stem.with_suffix(".body")
        self.stats = stem.with_suffix(".stats")
        self.headers = stem.with_suffix(".headers")
        cmd = ["curl", "--noproxy", "*", "--silent", "--show-error", "--no-buffer",
               "--connect-timeout", str(min(5, timeout)), "--max-time", str(timeout),
               "--max-filesize", "8388608", "--output", str(self.body), "--dump-header", str(self.headers),
               "--write-out", "%{http_code}\t%{time_starttransfer}\t%{time_total}"]
        if payload is not None:
            request = stem.with_suffix(".request")
            request.write_text(json.dumps(payload))
            cmd += ["--header", "Content-Type: application/json", "--data-binary", "@" + str(request)]
        cmd.append(transport.endpoint + path)
        with self.stats.open("wb") as out, stem.with_suffix(".error").open("wb") as err:
            self.proc = subprocess.Popen(cmd, stdout=out, stderr=err)
        transport.clients.append(self)

    def snapshot(self):
        return self.body.read_bytes() if self.body.exists() else b""

    def status_header(self):
        text = self.headers.read_text(errors="replace") if self.headers.exists() else ""
        codes = re.findall(r"^HTTP/\S+ (\d{3})", text, re.M)
        return int(codes[-1]) if codes else 0

    def result(self):
        try:
            self.proc.wait(timeout=max(.1, self.timeout + 1 - (time.monotonic() - self.started)))
        except subprocess.TimeoutExpired:
            self.close()
            raise Finding("FAIL", "HTTP execution deadline elapsed; no completed answer was available to score.", failure_kind="execution_timeout", correctness_evaluated=False, deadline_seconds=self.timeout)
        if self.proc.returncode == 28:
            raise Finding("FAIL", "HTTP execution deadline elapsed; no completed answer was available to score.", failure_kind="execution_timeout", correctness_evaluated=False, curl_exit=28, deadline_seconds=self.timeout)
        if self.proc.returncode:
            raise Finding("FAIL", "HTTP transfer failed.", curl_exit=self.proc.returncode, deadline_seconds=self.timeout)
        try:
            code, first, total = self.stats.read_text().strip().split("\t")
            self.code, self.first, self.total = int(code), float(first), float(total)
            if not all(numeric(v) and v >= 0 for v in (self.first, self.total)):
                raise ValueError
        except (OSError, ValueError) as error:
            raise Finding("ERROR", "HTTP timing/status instrumentation is unavailable.") from error
        return self

    def json(self):
        try:
            return json.loads(self.snapshot())
        except (ValueError, UnicodeError) as error:
            raise Finding("FAIL", "HTTP response is not valid JSON.", http_status=self.code) from error

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        if self in self.owner.clients:
            self.owner.clients.remove(self)


class Transport:
    def __init__(self, raw, port):
        self.raw, self.counter, self.clients = Path(raw), 0, []
        self.endpoint = f"http://127.0.0.1:{port}"

    def start(self, path, payload=None, timeout=5):
        return Transfer(self, path, payload, max(.05, timeout))

    def request(self, path, payload=None, timeout=5, text=False):
        transfer = self.start(path, payload, timeout)
        try:
            result = transfer.result()
            body = result.snapshot().decode("utf-8", "replace") if text else result.json()
            return result.code, body, {"http_first_byte_seconds": round(result.first, 3), "total_seconds": round(result.total, 3)}
        finally:
            transfer.close()

    def close(self):
        for transfer in self.clients[:]:
            transfer.close()


def procedure_identity():
    try:
        sha = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        sha, dirty = "unavailable", (ROOT / ".git").exists()
    revision = os.environ.get("MATUTO_WQ_PROCEDURE_REVISION") or sha
    return {"sha": revision, "dirty": dirty, "driver_sha256": hashlib.sha256((ROOT / "wq.py").read_bytes()).hexdigest()}


def host_identity():
    identity = {"system": platform.system(), "kernel": platform.release()}
    try:
        release = Path("/etc/os-release").read_text()
        match = re.search(r'^ID="?([a-zA-Z0-9._-]+)"?$', release, re.M)
        if match:
            identity["os_id"] = match[1]
    except OSError:
        pass
    for name, command, pattern in (
        ("hip_version", ["hipconfig", "--version"], r"\b(\d+\.\d+\.\d+(?:-\d+)?)\b"),
        ("amd_smi_tool_version", ["amd-smi", "version"], r"AMDSMI Tool:\s*([0-9.]+)"),
    ):
        identity[name] = "unavailable"
        if shutil.which(command[0]):
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                match = re.search(pattern, result.stdout)
                if result.returncode == 0 and match:
                    identity[name] = match[1]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return identity


class Reporter:
    def __init__(self, root, profiles, limits, role):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.data = {"started_utc": now(), "procedure": procedure_identity(), "limits": dict(limits),
                     "role": role, "host": host_identity(), "profiles": {}, "results": [],
                     "evidence_boundary": "Worker/API observations only: captured identity and owned-server results. Discovery is labeled as such, and serving/consumer evidence stays in the lab and consumer suites. No reported status publishes, promotes, or qualifies a configuration.",
                     "owner_decision": {"status": "pending", "accepted_scope": "", "exceptions_by_wq_id_and_reason": [], "date_and_name": "", "procedure_sha": ""}}
        long_checks = {slot["long_check"] for slot in SLOTS.values()}
        for profile in profiles:
            self.data["results"].append(self.row(profile, "SETUP", "Setup", "Can the pinned profile be verified and served without touching an existing server?", "Checksum verification, owned loopback server, expected model/build/context and one idle slot.", "discovery", "worker setup: verified checksum, effective run identity, and owned-server readiness"))
            for ident, (category, question, rule) in CASES.items():
                row = self.row(profile, ident, category, question, rule, "execution", "worker/API observation from the owned server")
                if ident in long_checks and ident != SLOTS[profile]["long_check"]:
                    row["applicable"] = False
                    row["reason"] = "Not applicable to this profile."
                self.data["results"].append(row)
        self.save()

    @staticmethod
    def row(profile, ident, category, question, rule, kind, evidence):
        return {"profile": profile, "id": ident, "category": category, "question": question, "pass_rule": rule, "kind": kind, "evidence": evidence, "status": "SKIP", "applicable": True, "reason": "Not run yet.", "observations": {}}

    def get(self, profile, ident):
        return next(r for r in self.data["results"] if r["profile"] == profile and r["id"] == ident)

    def result(self, profile, ident, status, reason, observations):
        row = self.get(profile, ident)
        row.update(status=status, reason=reason, observations=copy.deepcopy(observations), recorded_utc=now())
        self.save()
        print(f"{profile} {ident}: {status} — {reason}", flush=True)
        if observations:
            print("  " + json.dumps(observations, sort_keys=True), flush=True)

    def skip_pending(self, profile, reason):
        for row in self.data["results"]:
            if row["profile"] == profile and row["reason"] == "Not run yet.":
                row["reason"] = reason
        self.save()

    def save(self):
        self.data["updated_utc"] = now()
        rows = self.data["results"]
        self.data["counts"] = {s: sum(r["status"] == s for r in rows) for s in ("PASS", "FAIL", "ERROR", "SKIP")}
        lines = ["# WQ observation report", "", "Automated results are evidence, not permission to enable or reject a profile. Owner acceptance is separate.", "",
                 "## Run", "", "```json", json.dumps({k: self.data[k] for k in ("started_utc", "procedure", "role", "limits", "host", "profiles", "counts")}, indent=2), "```", "",
                 "## At a glance", "", "| Profile | Check | Kind | Result | Question |", "| --- | --- | --- | --- | --- |"]
        for row in rows:
            lines.append(f"| {row['profile']} | {row['id']} | {row['kind']} | {row['status']} | {row['question']} |")
        for row in rows:
            lines += ["", f"### {row['profile']} / {row['id']} — {row['status']}", "", f"**Kind:** {row['kind']} — {row['evidence']}", "", f"**Question:** {row['question']}", "", f"**Pass rule:** {row['pass_rule']}", "", f"**Result:** {row['reason']}", "", "```json", json.dumps(row["observations"], indent=2), "```"]
        lines += ["", "## Owner decision — pending", "", "Edit after the run. Do not change automated rows. Accepting a scope despite named failures does not change the runner's exit status or publish any profile.", "", "- Accepted role/scope:", "- WQ-ID exceptions and reasons:", "- Residual risks accepted:", "- Date/name:", f"- Procedure SHA: {self.data['procedure']['sha']}", ""]
        for name, content in (("report.json", json.dumps(self.data, indent=2) + "\n"), ("report.md", "\n".join(lines))):
            temporary = self.root / (name + ".tmp")
            temporary.write_text(content)
            temporary.chmod(0o600)
            temporary.replace(self.root / name)


def free_port(port):
    try:
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    except OSError as error:
        raise Finding("ERROR", "Loopback port is occupied. Stop your existing server yourself or choose --port; WQ stopped nothing.", port=port) from error


def other_servers(proc=Path("/proc")):
    """Whether some other llama-server already holds the GPU.

    A launch execve's a file descriptor, so the kernel takes `comm` from that
    path and not from the program name. The name the process was started under
    is in argv[0], which both a launched and a directly executed server carry.
    A zombie's cmdline is empty, so it cannot match.
    """
    for entry in proc.iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "cmdline").read_bytes().split(b"\0", 1)[0].rpartition(b"/")[2] == b"llama-server":
                    return True
            except OSError:
                pass
    return False


# The publication copy of a retained launch identity. The complete record stays
# in the private runtime artifact the lab wrote; a report carries the digests and
# configuration facts an operator needs to associate a result with a
# configuration, never a filesystem location.
SERVING_IDENTITY_FIELDS = ("check", "backend", "pci", "uuid", "selector", "executable", "build", "gpuLayers", "host", "driver", "offload")
SERVING_REQUEST_FIELDS = ("model_sha256", "context")


def published_identity(identity):
    """Project a launch identity onto the fields a publication report may carry."""
    published = {field: identity.get(field, "unavailable") for field in SERVING_IDENTITY_FIELDS}
    request = identity.get("request") or {}
    published["request"] = {field: request.get(field, "unavailable") for field in SERVING_REQUEST_FIELDS}
    return published


class Server:
    """One worker-qualification slot, driven by the store's pinned launch spec.

    There is no profile file anywhere in the run: the store's emitted spec is the
    only source of the record, and the slot is served by launching
    `matuto_runtime.py` with the argv that record resolves to.
    """

    def __init__(self, raw, profile, port, limits, http, gpu_card=None):
        self.raw, self.profile, self.port = Path(raw), profile, port
        self.limits, self.http, self.proc = limits, http, None
        self.runtime_lease = None
        self.runtime_identity = None
        self.gpu_card, self.vram_device, self.vram_baseline = gpu_card, None, {}
        # What the device still owes: the reading the most recent owned server
        # loaded on top of. It outlives a refused baseline, because a server
        # that died still has to give its memory back before the next reading
        # describes free memory.
        self.vram_owed = None
        self.cfg = self.raw / "config"
        try:
            self.spec = json.loads(os.environ["MATUTO_WQ_LAUNCH_SPEC"])
            self.record = self.spec["record"]
        except (KeyError, TypeError, ValueError):
            raise Finding("ERROR", "WQ requires the store's launch spec for a stored language profile.") from None
        # Freeze the pinned spec once so a mid-run edit cannot change what is
        # served or what the report identifies this run as having qualified.
        pdir = self.cfg / "ai-agents-sandbox/matuto-lab/profiles"
        pdir.mkdir(parents=True)
        self.target = pdir / f"wq-{profile}.json"
        self.target.write_text(json.dumps(self.spec, sort_keys=True) + "\n")
        self.target.chmod(0o600)
        self.profile_source_label = "stored launch spec"
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.cfg), XDG_STATE_HOME=str(self.raw / "state"), MATUTO_RUNTIME_IDENTITY_PATH=str(self.raw / "runtime.json"))
        self.install_runtime_selection()
        model = self.record["files"]["model"]
        self.expected = {
            "MODEL_PATH": model["path"], "MODEL_FILE": Path(model["path"]).name,
            "MODEL_SHA256": model["sha256"], "BACKEND_VERSION": "unavailable",
            "MODEL_ALIAS": self.record["alias"], "CONTEXT_SIZE": str(self.record["context_size"]),
            "GPU_LAYERS": str(99 if self.record["gpu_layers"] == -1 else self.record["gpu_layers"]),
            "MODEL_SOURCE": "stored", "MODEL_REVISION": "",
            "SERVER_OUTPUT_TOKENS": str(self.record["server_output_tokens"]),
        }
        # The store's launch spec serves the record's context as its output
        # ceiling; a run must not qualify a configuration that states two.
        require(self.expected["SERVER_OUTPUT_TOKENS"] == self.expected["CONTEXT_SIZE"], "WQ requires the server output ceiling to equal the context size, not an independent answer budget.")
        self.resolve_locations()

    def resolve_locations(self):
        """Where the pinned backend is, from the launch spec's own selection."""
        self.expected["INSTALLATION"] = str(runtime.installation_bin(self.spec["runtime"]["location"]))

    def install_runtime_selection(self):
        """Install the pinned runtime selection in the home this run serves under.

        `matuto_runtime.py` reads the selection from its own home, so a run the
        manager launched serves the pinned build, backend and card under that
        private home. An account that already has a selection keeps it: the run
        serves it and reports any difference from the pinned record.
        """
        try:
            selected = self.spec["runtime"]
            if runtime.read_selection() is None:
                backend = {"hip": "HIP/ROCm", "vulkan": "Vulkan"}[selected["backend"]]
                runtime.select_runtime(selected["location"], backend, selected["gpu_identity"],
                                       environment=dict(os.environ, XDG_CONFIG_HOME=str(self.cfg)))
        except (KeyError, TypeError, ValueError, runtime.InspectionError) as error:
            raise Finding("ERROR", f"The pinned runtime selection is unavailable: {error}") from None

    def runtime_command(self, action, tool, layers, arguments):
        return [sys.executable, "-B", str(ROOT / "agents/matuto_runtime.py"), "launch",
                action, tool, str(layers), "--", *arguments]

    def server_arguments(self, upload_timeout):
        """The argv this run serves, from the stored record plus the run's own flags."""
        arguments = store_lab._server_arguments(dict(self.record, server_port=self.port))
        arguments += ["--metrics", "--reasoning-format", "deepseek", "--reasoning-preserve"]
        if "--no-context-shift" not in self.record["arguments"]["server"]:
            arguments.append("--no-context-shift")
        if upload_timeout:
            arguments += ["--timeout", "2"]
        return arguments

    def verify_files(self):
        """Hash every file the record picked and state the digests it observed.

        `start()` calls this on every start, including a restart, so weights
        replaced at a pinned path after verification are refused rather than
        served under the identity this run verified. The runtime records the
        digests it was launched with, so they are passed on here; a sidecar the
        record does not pick must inherit none from the environment.
        """
        digests = {}
        for role, picked in sorted(self.record["files"].items()):
            digest = hashlib.sha256()
            try:
                with open(picked["path"], "rb") as stream:
                    for block in iter(lambda: stream.read(1 << 20), b""):
                        digest.update(block)
            except OSError as error:
                raise Finding("ERROR", f"The recorded {role} file could not be read: {error}") from None
            if digest.hexdigest() != picked["sha256"]:
                raise Finding("ERROR", f"The recorded {role} file does not match its recorded SHA-256.", role=role)
            digests[role] = picked["sha256"]
        self.env["MATUTO_RUNTIME_MODEL_SHA256"] = digests["model"]
        for role, name in (("mmproj", "MATUTO_RUNTIME_MMPROJ_SHA256"),
                           ("mtp", "MATUTO_RUNTIME_MTP_SHA256")):
            if role in digests:
                self.env[name] = digests[role]
            else:
                self.env.pop(name, None)

    def acquire_runtime(self):
        if self.runtime_lease is None:
            try:
                self.runtime_lease = runtime.runtime_lease()
            except runtime.InspectionError as error:
                raise Finding("ERROR", str(error)) from None

    def close(self):
        self.stop()
        if self.runtime_lease is not None:
            os.close(self.runtime_lease)
            self.runtime_lease = None

    def verify(self):
        self.acquire_runtime()
        if other_servers():
            raise Finding("ERROR", "Another llama-server is running. Free the GPU yourself before rerunning; WQ stopped nothing.")
        free_port(self.port)
        self.verify_files()
        with (self.raw / "verify.log").open("wb") as output:
            result = subprocess.run(self.runtime_command("verify", "llama-server", "-", ["--list-devices"]), env=self.env, stdout=output, stderr=subprocess.STDOUT, timeout=self.limits["restart_seconds"])
        if result.returncode:
            raise Finding("ERROR", "Stored profile verification failed. Check the recorded model, build and sidecars.", verify_exit=result.returncode)
        self.runtime_identity = json.loads((self.raw / "runtime.json").read_text())
        self.env["MATUTO_RUNTIME_EXPECTED_SHA256"] = self.runtime_identity["executable"]["sha256"]
        return {"runtime": self.runtime_identity, **self.profile_record()}

    def profile_record(self):
        """Identity fields the report pins for the profile this slot is qualifying."""
        return {"checksum_verified": True, "profile_source": self.profile_source_label, "expected_model_sha256": self.expected["MODEL_SHA256"], "expected_model_revision": self.expected["MODEL_REVISION"], "expected_backend_commit": self.expected["BACKEND_VERSION"], "temporary_profile_sha256": hashlib.sha256(self.target.read_bytes()).hexdigest(), "generation_policy": "remaining_context_no_shift", "server_output_tokens": int(self.expected["SERVER_OUTPUT_TOKENS"]), "task_max_tokens": -1}

    def serving(self):
        """The identity the owned server actually launched with, compared with the profile request.

        The identity is read back from the worker artifact the lab wrote before it
        exec'd the model, so it is the executed configuration rather than the
        selection-time expectation. The profile's model digest is the one verify
        already checked against the file, so equal paths with different weights
        still compare as changed. Only `published_identity`'s allowlist reaches a
        report; the executed model location stays in the private artifact.
        """
        try:
            identity = json.loads((self.raw / "runtime.json").read_text())
            selection = runtime.read_selection()
            current = runtime.current_configuration(selection, "llama-server")
        except (OSError, ValueError, KeyError, TypeError, runtime.InspectionError):
            return {"state": "unavailable", "reason": "The executed serving identity was not retained."}
        current["request"] = {"model": self.expected["MODEL_PATH"], "model_sha256": self.expected["MODEL_SHA256"], "context": str(self.expected["CONTEXT_SIZE"])}
        current["offload"] = {"requested_gpu_layers": str(self.expected["GPU_LAYERS"])}
        return {"identity": published_identity(identity), "comparison": runtime.configuration_comparison(identity, current, runtime.RUNTIME_CONFIGURATION_FIELDS)}

    def healthy(self, timeout=2):
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            return self.http.request("/health", timeout=timeout)[0] == 200
        except (Finding, OSError):
            return False

    def telemetry_device(self, require_open=True):
        candidates = []
        for card in Path("/sys/class/drm").glob("card[0-9]*"):
            if not re.fullmatch(r"card\d+", card.name) or self.gpu_card and card.name != self.gpu_card:
                continue
            try:
                device = card / "device"
                identity = self.runtime_identity
                if identity and device.resolve().name == identity["pci"] and (device / "vendor").read_text().strip() == "0x1002":
                    candidates.append(device)
            except (OSError, ValueError):
                pass
        if len(candidates) != 1:
            raise Finding("ERROR", "VRAM telemetry is unavailable for the selected physical GPU; --gpu-card must refer to that same device. No zero measurement is assumed.", candidate_devices=len(candidates))
        device = candidates[0]
        if require_open:
            opened = set()
            for fd in Path(f"/proc/{self.proc.pid}/fd").iterdir():
                with contextlib.suppress(OSError):
                    target = os.readlink(fd)
                    if re.fullmatch(r"/dev/dri/renderD\d+", target):
                        opened.add((Path("/sys/class/drm") / Path(target).name / "device").resolve())
            if device.resolve() not in opened:
                raise Finding("ERROR", "Chosen VRAM device is not demonstrably opened by the owned server.")
        return device / "mem_info_vram_used"

    def vram_reading(self, device):
        try:
            value = int(device.read_text())
            if value < 0:
                raise ValueError
        except (OSError, ValueError) as error:
            raise Finding("ERROR", "Pre-start byte-valued VRAM telemetry is missing or invalid; no zero baseline is assumed.") from error
        return value

    def settled_baseline(self, device, released):
        """The counter once the stopped server's memory is actually back.

        `released` is the baseline the previous server started from, so the
        device owes that much before a fresh reading describes free memory
        rather than an allocation still on its way out. Waiting is what keeps a
        restart from recording the dead server as the memory the next one had.
        """
        reading = self.vram_reading(device)
        if released is None:
            return reading, 0.0, "no_previous_server_allocation_to_release"
        ceiling, first, started, reads = released + VRAM_DRIFT_ALLOWANCE_BYTES, reading, time.monotonic(), 1
        while reading > ceiling:
            if time.monotonic() - started >= VRAM_RELEASE_LIMIT_SECONDS:
                raise Finding("ERROR", "The device still held the stopped server's memory when the release wait expired; no pre-start baseline is recorded.", release_limit_seconds=VRAM_RELEASE_LIMIT_SECONDS, released_baseline_vram_bytes=released, expected_vram_bytes_at_most=ceiling, first_observed_vram_bytes=first, last_observed_vram_bytes=reading, telemetry_reads=reads)
            time.sleep(VRAM_RELEASE_INTERVAL_SECONDS)
            reading, reads = self.vram_reading(device), reads + 1
        return reading, round(time.monotonic() - started, 3), "previous_server_allocation_released"

    def start(self, upload_timeout=False):
        self.acquire_runtime()
        # Every start re-observes the record's files, so a restart cannot serve
        # weights that changed after verification, and states their digests.
        self.verify_files()
        released = self.vram_owed
        self.stop()
        self.vram_device, self.vram_baseline = None, {}
        free_port(self.port)
        device = self.telemetry_device(require_open=False)
        baseline, settle_seconds, settlement = self.settled_baseline(device, released)
        self.vram_device, self.vram_owed = device.resolve(), baseline
        self.vram_baseline = {"vram_device": str(device), "vram_pci": self.runtime_identity["pci"], "baseline_attribution": "selected_device_before_start_not_process_ownership", "baseline_settled": settlement, "baseline_settle_seconds": settle_seconds, **vram_fields("pre_start_vram", baseline)}
        started = time.monotonic()
        with (self.raw / "server.log").open("ab") as output:
            self.proc = subprocess.Popen(self.runtime_command("serve", "llama-server", self.expected["GPU_LAYERS"], self.server_arguments(upload_timeout)), env=self.env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        end = started + self.limits["restart_seconds"]
        while time.monotonic() < end:
            if self.proc.poll() is not None:
                raise Finding("ERROR", "Owned server exited while loading. A successful profile load was not observed.")
            if self.healthy(min(2, end - time.monotonic())):
                identity = self.identity()
                elapsed = time.monotonic() - started
                require(elapsed <= self.limits["restart_seconds"], "Server readiness exceeded the declared startup/restart limit.", readiness_seconds=round(elapsed, 3))
                return identity, elapsed
            time.sleep(.2)
        raise Finding("FAIL", "Owned server did not become ready within the declared startup/restart limit.", readiness_limit_seconds=self.limits["restart_seconds"])

    def identity(self):
        if self.proc is None or self.proc.poll() is not None:
            raise Finding("ERROR", "Owned server is not running.")
        try:
            binary_matches = Path(f"/proc/{self.proc.pid}/exe").samefile(Path(self.expected["INSTALLATION"] or "/nonexistent") / "llama-server")
        except OSError:
            binary_matches = False
        require(binary_matches, "Endpoint startup is not backed by the expected owned server executable.")
        code, props, _ = self.http.request("/props")
        code2, slots, _ = self.http.request("/slots")
        require(code == code2 == 200 and type(props) is dict and type(slots) is list, "Identity endpoints did not provide the expected HTTP 200 objects.")
        model, build, template = props.get("model_path"), props.get("build_info"), props.get("chat_template")
        try:
            model_matches = type(model) is str and Path(model).samefile(self.expected["MODEL_PATH"])
        except OSError:
            model_matches = False
        require(model_matches, "Live model identity differs from the verified profile.", model_matches=model_matches)
        # The stored record declares the context this run qualifies. Comparing
        # the served window against it, rather than against the profile that was
        # just loaded, is what keeps a run from qualifying a configuration it
        # does not actually serve.
        declared = int(self.expected["CONTEXT_SIZE"])
        served = slots[0].get("n_ctx") if len(slots) == 1 else None
        require(props.get("total_slots") == 1 and len(slots) == 1 and served == declared,
                f"Record {self.profile} declares context {declared} but the server serves {served}.",
                slot=self.profile, declared_context=declared, served_context=served)
        require(type(template) is str and bool(template), "Live chat-template identity is unavailable.")
        preserve = props.get("chat_template_caps", {}).get("supports_preserve_reasoning") is True
        require(preserve, "Pinned template does not report supports_preserve_reasoning; WQ-03H would not test what it claims.")
        return {"n_ctx": served, "model_sha256": self.expected["MODEL_SHA256"], "build_info_sha256": hashlib.sha256(build.encode()).hexdigest(), "observed_build_revision": runtime.build_revision(build) or "unavailable", "template_sha256": hashlib.sha256(template.encode()).hexdigest(), "preserve_capability": preserve}

    def stop(self, crash=False):
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL if crash else signal.SIGTERM)
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=5)
            except ProcessLookupError:
                self.proc.wait(timeout=5)


class Runner:
    def __init__(self, profile, server, http, report, limits):
        self.profile, self.server, self.http, self.report, self.limits = profile, server, http, report, limits
        self.last_tool, self.obs, self.baseline = None, {}, None

    def task(self, request):
        self.obs["execution_bound_seconds"] = self.limits["long_execution_seconds"]
        code, body, timing = self.http.request("/v1/chat/completions", request, self.limits["long_execution_seconds"])
        self.obs.update(http_status=code, **timing)
        require(code == 200, "Task endpoint did not return HTTP 200.", http_status=code)
        self.obs.update(generation_observations(body))
        return body

    def idle(self, seconds=None):
        end = time.monotonic() + (self.limits["cancel_recovery_seconds"] if seconds is None else seconds)
        while time.monotonic() < end:
            try:
                code, slots, _ = self.http.request("/slots", timeout=min(2, end - time.monotonic()))
                if code == 200 and type(slots) is list and len(slots) == 1 and slots[0].get("is_processing") is False:
                    return time.monotonic() <= end
            except Finding:
                pass
            time.sleep(.1)
        return False

    def recovery(self):
        require(self.idle(), "Slot did not return idle within the recovery limit.")
        request, expected = schema_task()
        answer(self.task(request), expected)
        self.obs["recovery_task_passed"] = True

    def run(self):
        matuto_progress.report("worker qualification", f"{self.profile}: setup")
        try:
            verified = self.server.verify()
            self.baseline, elapsed = self.server.start()
            require(self.idle(), "Server did not start with an idle slot.")
            self.report.data["profiles"][self.profile] = {**verified, "observed": self.baseline, "serving": self.server.serving(), "startup_seconds": round(elapsed, 3)}
            self.report.result(self.profile, "SETUP", "PASS", "Verified profile and owned server are ready.", {"startup_seconds": round(elapsed, 3), "slot": self.profile, "declared_context": SLOTS[self.profile]["context"], "n_ctx": self.baseline["n_ctx"], **self.server.vram_baseline})
        except Exception as error:
            status, reason, observations = self.describe(error)
            self.report.result(self.profile, "SETUP", status, reason, observations)
            self.report.skip_pending(self.profile, "Setup did not succeed; no model probe was sent.")
            return
        for ident in CASES:
            self.execute(ident)

    @staticmethod
    def describe(error):
        if isinstance(error, Finding):
            return error.status, error.reason, error.observations
        if isinstance(error, subprocess.TimeoutExpired):
            return "ERROR", "A setup or subprocess operation exceeded its deadline.", {}
        return "ERROR", "Unexpected runner error; no response or exception text is published.", {"runner_error": True}

    def execute(self, ident):
        row = self.report.get(self.profile, ident)
        if not row["applicable"]:
            return
        if ident in ("WQ-03", "WQ-07") and self.last_tool is None:
            self.report.result(self.profile, ident, "SKIP", "No valid WQ-01 assistant call exists for this dependent check.", {})
            return
        matuto_progress.report("worker qualification", f"{self.profile}: {ident}")
        print(f"\n{self.profile} {ident}: {row['question']}", flush=True)
        print("Pass rule: " + row["pass_rule"], flush=True)
        self.obs = {}
        status, reason = "PASS", "All stated conditions were observed."
        try:
            valid_identity = False
            if self.server.healthy() and self.idle():
                try:
                    valid_identity = self.server.identity() == self.baseline
                except Finding:
                    pass
            if not valid_identity:
                identity, _ = self.server.start()
                if identity != self.baseline:
                    raise Finding("ERROR", "The original server identity could not be restored; this check was not dispatched.", identity_restored=False)
                self.obs.update(restarted_before_case=True, baseline_settle_seconds=self.server.vram_baseline["baseline_settle_seconds"])
            if ident == SLOTS[self.profile]["long_check"]:
                # The long-context row reports the rate it observed rather than a
                # generic pass, so the sample counts are what the result names.
                reason = self.long_context() or reason
            else:
                getattr(self, ident.lower().replace("-", ""))()
        except Exception as error:
            status, reason, details = self.describe(error)
            self.obs.update(details)
        finally:
            self.http.close()
        self.report.result(self.profile, ident, status, reason, self.obs)

    def wq01(self):
        failures = []
        for index, (ticket, fields) in enumerate(((18, ("title", "state")), (29, ("state",)))):
            try:
                request = lookup_task(ticket, fields)
                msg, call = tool_call(self.task(request), ticket, fields)
                if index == 0:
                    self.last_tool = (request, msg, call)
                self.obs[f"lookup_case_{index + 1}_correct"] = True
            except Finding as error:
                failures.append(error)
                self.obs[f"lookup_case_{index + 1}_correct"] = False
                self.obs[f"lookup_case_{index + 1}_reason"] = error.reason
                self.obs[f"lookup_case_{index + 1}_observations"] = error.observations
        request = lookup_task()
        request["messages"] = [{"role": "user", "content": "No lookup is needed. Reply with exactly OK without using any tool."}]
        try:
            msg = message(self.task(request))
            require(not msg.get("tool_calls"), "No-lookup task made an unnecessary tool call.")
            require(type(msg.get("content")) is str and msg["content"].strip() == "OK", "No-lookup task did not give the requested short answer.")
            self.obs["no_tool_case_correct"] = True
        except Finding as error:
            failures.append(error)
            self.obs["no_tool_case_correct"] = False
            self.obs["no_tool_case_observations"] = error.observations
        if failures:
            raise Finding(failures[0].status, "One or more declared auto-tool cases failed.", failed_cases=len(failures), first_failure=failures[0].reason)

    def wq02(self):
        request, expected = schema_task()
        answer(self.task(request), expected)
        self.obs["typed_record_matches"] = True

    def wq03h(self):
        request = lookup_task()
        call = {"id": "synthetic-call", "type": "function", "function": {"name": "lookup_ticket", "arguments": '{"ticket":18,"fields":["state"]}'}}
        request["messages"] = [
            {"role": "user", "content": "Inspect a record."},
            {"role": "assistant", "content": None, "reasoning_content": "WQ_OLDER_SYNTHETIC_TRACE_9281", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": call["id"], "name": "lookup_ticket", "content": "WQ_SYNTHETIC_TOOL_RESULT_3482"},
            {"role": "assistant", "content": "Noted.", "reasoning_content": "WQ_LATER_SYNTHETIC_TRACE_4826"},
            {"role": "user", "content": "Continue."},
        ]
        for enabled in (True, False):
            request["chat_template_kwargs"] = {"preserve_reasoning": enabled}
            code, body, _ = self.http.request("/apply-template", request, 10)
            require(code == 200 and type(body) is dict and type(body.get("prompt")) is str, "Template endpoint did not return a rendered prompt.", http_status=code)
            prompt = body["prompt"]
            if enabled:
                checks = {"earlier_reasoning_retained": "WQ_OLDER_SYNTHETIC_TRACE_9281" in prompt, "later_reasoning_retained": "WQ_LATER_SYNTHETIC_TRACE_4826" in prompt, "tool_result_retained": "WQ_SYNTHETIC_TOOL_RESULT_3482" in prompt}
                self.obs.update(checks)
                require(all(checks.values()), "Template did not preserve all supplied synthetic history components.")
            else:
                self.obs["earlier_reasoning_retained_with_control_disabled"] = "WQ_OLDER_SYNTHETIC_TRACE_9281" in prompt
        self.obs["model_reasoning_generation_required"] = False

    def followup(self, state):
        original, msg, call = self.last_tool
        request = copy.deepcopy(original)
        expected = {"ticket": 18, "state": state}
        request["messages"] += [msg, {"role": "tool", "tool_call_id": call["id"], "name": "lookup_ticket", "content": json.dumps({"title": "test record", "state": state})}, {"role": "user", "content": "Return only JSON with ticket 18 and the state supplied by the tool. Do not make another call."}]
        return request, expected

    def wq03(self):
        for index, state in enumerate(("open", "closed")):
            request, expected = self.followup(state)
            answer(self.task(request), expected)
            self.obs[f"result_case_{index + 1}_matches"] = True

    def long_request(self, repetitions, sentinels=None):
        expected = dict(sentinels or retrieval_sentinels(0))
        prompt = ("Extract the three sentinel values as JSON.\nSTART " + expected["start"] + "\n"
                  + " filler" * (repetitions // 2) + "\nMIDDLE " + expected["middle"] + "\n"
                  + " filler" * (repetitions // 2) + "\nEND " + expected["end"])
        return json_task(prompt, expected), expected

    def count_tokens(self, request):
        code, body, _ = self.http.request("/v1/chat/completions/input_tokens", request, 30)
        if code != 200 or type(body) is not dict or type(body.get("input_tokens")) is not int or body["input_tokens"] < 1:
            raise Finding("ERROR", "Input token counting did not return a positive integer.", http_status=code)
        return body["input_tokens"]

    def calibrated_request(self, low, high, *, boundary=False, sentinels=None):
        repetitions = max(2, low - 100)
        for attempt in range(10):
            request, expected = self.long_request(repetitions, sentinels)
            if boundary:
                # Resource probe, not a scored answer: force generation to fill the slot.
                del request["response_format"]
                request["ignore_eos"] = True
            else:
                # The retrieval request carries its own bound. Every scored answer
                # on record generated at most 1,318 tokens and both scored failures
                # at least 2,670, so the bound separates a real answer from a
                # runaway that spends the whole deadline without closing one.
                request["max_tokens"] = int(self.limits["retrieval_generation_tokens"])
            count = self.count_tokens(request)
            if low <= count <= high:
                break
            next_repetitions = max(2, round(repetitions * ((low + high) / 2) / count))
            if next_repetitions == repetitions or next_repetitions > high * 4:
                raise Finding("ERROR", "Token calibration cannot reach the target using bounded filler.")
            repetitions = next_repetitions
        else:
            raise Finding("ERROR", "Token calibration exhausted ten attempts; no model quality claim is made.")
        return request, expected, count, attempt + 1

    def long_context(self):
        low, high = long_band(SLOTS[self.profile]["context"])
        samples = int(self.limits["retrieval_samples"])
        scored = passed = 0
        results = []
        for index in range(samples):
            request, expected, count, attempts = self.calibrated_request(low, high, sentinels=retrieval_sentinels(index))
            if index == 0:
                # The band and the calibrated count are the check's shape; later
                # samples calibrate into the same band, so they are recorded once.
                self.obs.update(input_tokens=count, calibration_attempts=attempts, available_context_tokens=self.baseline["n_ctx"] - count)
            sample = self.retrieval_sample(index, request, expected, count)
            if sample.get("unscored_reason") == "execution_timeout" and index + 1 < samples:
                # The deadline path aborts the transfer, so the server is still
                # working on a request nobody is reading. Dispatch the next sample
                # into that slot and it is counted against the leftover work rather
                # than against retrieval. WQ-11 asserts the release is bounded, so
                # wait for it and keep the wait and its outcome in the record.
                started = time.monotonic()
                sample["slot_released_before_next_sample"] = self.idle()
                sample["slot_release_seconds"] = round(time.monotonic() - started, 3)
            results.append(sample)
            scored += sample["scored"]
            passed += sample["passed"]
            # Published per sample as it completes, so a later sample that raises
            # does not discard the record of the ones that already ran.
            self.obs.update(target_band=[low, high], retrieval_samples=samples, retrieval_samples_scored=scored,
                            retrieval_samples_passed=passed, retrieval_generation_bound_tokens=int(self.limits["retrieval_generation_tokens"]),
                            retrieval_sample_results=results)
            if sample.get("slot_released_before_next_sample") is False:
                # Published above first: the row fails with the wait in its record
                # rather than continuing to score samples against leftover work.
                require(False, "The slot did not release within the recovery limit after a deadline sample.",
                        slot_release_seconds=sample["slot_release_seconds"])
        if "generation_rate_meets_declared_minimum" not in self.obs:
            self.obs["generation_rate_evaluated"] = False
            self.obs["generation_rate_evaluation_reason"] = "No sample produced a measurable generation rate, so the declared minimum was not evaluated."
        if not scored:
            raise Finding("FAIL", f"None of the {samples} samples produced a scorable answer; retrieval was not scored.")
        if passed != scored:
            raise Finding("FAIL", f"{passed} of {scored} scored samples returned all three values; unanimity is required.")
        self.obs["sentinels_match"] = True
        require(not self.obs.get("limits_exceeded"), "Observed generation rate is below the declared minimum; see the named limit and measurement.")
        require(self.server.healthy() and self.idle(), "Server did not recover after the long-context task.")
        self.obs["server_recovered"] = True
        unscored = samples - scored
        return f"{passed} of {scored} scored samples returned all three values" + (f"; {unscored} sample(s) were unscored." if unscored else ".")

    def retrieval_sample(self, index, request, expected, count):
        """Run one long-context sample and classify what it returned.

        A sample that produced no answer at all — cut off by the generation
        bound, by the end of the context, or by the HTTP deadline — is unscored:
        there is no answer to apply the pass rule to. A completed response is
        scored, and either passes or fails the three-value rule.
        """
        sample = {"sample": index + 1, "input_tokens": count}
        body, observations = self.long_task(request)
        sample.update(observations)
        if body is None:
            sample.update(scored=False, passed=False, outcome="unscored", unscored_reason="execution_timeout")
            rate = observations.get("progress_generation_tokens_per_second")
            if rate is not None:
                sample["generation_rate_meets_declared_minimum"] = self.evaluate_generation_floor(rate)
            return sample
        split = self.record_generation(body)
        sample.update(split)
        sample["generation_rate_meets_declared_minimum"] = self.evaluate_generation_floor(split["generation_tokens_per_second"])
        try:
            answer(body, expected)
        except Finding as error:
            kind = error.observations.get("failure_kind")
            if kind in ("context_exhausted", "generation_limit"):
                sample.update(scored=False, passed=False, outcome="unscored", unscored_reason=kind)
            else:
                sample.update(scored=True, passed=False, outcome="wrong_or_missing", failure=error.reason)
            return sample
        sample.update(scored=True, passed=True, outcome="passed")
        return sample

    def long_task(self, request):
        """Run one long-context request, keeping what a deadline would discard.

        The answer arrives in one piece, so a killed transfer leaves no body to
        measure at the depth the row ran at. The server's live slot table is
        sampled while the request is in flight for exactly that case: its
        cumulative /metrics prediction counters only advance when a request
        finishes. A sample that cannot be read is skipped rather than killing
        the request. Returns the completed body and its transfer observations,
        or None and the deadline progress when the execution deadline arrived.
        """
        self.obs["execution_bound_seconds"] = self.limits["long_execution_seconds"]
        started = time.monotonic()
        transfer = self.http.start("/v1/chat/completions", request, self.limits["long_execution_seconds"])
        samples = []
        try:
            while transfer.proc.poll() is None:
                sample = self.progress_sample(started)
                if sample is not None:
                    samples.append(sample)
                time.sleep(PROGRESS_SAMPLE_SECONDS)
            result = transfer.result()
            body = result.json()
            observations = {"http_status": result.code, "http_first_byte_seconds": round(result.first, 3), "total_seconds": round(result.total, 3)}
            if result.code != 200:
                raise Finding("FAIL", "Task endpoint did not return HTTP 200.", **observations)
            observations.update(generation_observations(body))
            return body, observations
        except Finding as error:
            if error.observations.get("failure_kind") == "execution_timeout":
                # Keep what the completed transfer's own record carried, such as
                # curl_exit and deadline_seconds, beside the live progress.
                return None, {**deadline_observations(samples), **error.observations}
            raise
        finally:
            transfer.close()

    def progress_sample(self, started):
        """One live slot reading and how long the request had run, or None.

        The slot table counts prompt tokens and decoded tokens as they happen,
        which the cumulative counters do not. A single slot is expected; an
        unreadable or unexpected answer is a missing measurement, not a zero.
        """
        try:
            code, slots, _ = self.http.request("/slots", timeout=2)
        except Finding:
            return None
        if code != 200 or type(slots) is not list or len(slots) != 1 or type(slots[0]) is not dict:
            return None
        slot = slots[0]
        next_token = slot.get("next_token")
        # The pinned build wraps this in a one-element array; older builds send the
        # object directly. Accept either and treat anything else as missing.
        if type(next_token) is list and len(next_token) == 1:
            next_token = next_token[0]
        processed = slot.get("n_prompt_tokens_processed")
        decoded = next_token.get("n_decoded") if type(next_token) is dict else None
        if not (type(processed) is int and processed >= 0 and type(decoded) is int and decoded >= 0):
            return None
        return {"prefill_tokens": processed, "generation_tokens": decoded,
                "elapsed_seconds": round(time.monotonic() - started, 3)}

    def record_generation(self, body):
        """One completed sample's measured prefill/generation split, or ERROR."""
        split = generation_split(body)
        if split is None:
            raise Finding("ERROR", "Measured generation timing is missing or invalid; no speed is assumed.")
        return split

    def evaluate_generation_floor(self, rate):
        """Compare one measured rate with the declared minimum and name a shortfall.

        Called once per sample, so a shortfall in any sample is named rather
        than the last sample's rate deciding the row.
        """
        floor = self.limits["generation_tokens_per_second"]
        self.obs["declared_generation_tokens_per_second"] = floor
        self.obs["generation_rate_evaluated"] = True
        met = rate >= floor
        self.obs["generation_rate_meets_declared_minimum"] = self.obs.get("generation_rate_meets_declared_minimum", True) and met
        self.obs.setdefault("limits_exceeded", [])
        if not met and "generation_tokens_per_second" not in self.obs["limits_exceeded"]:
            self.obs["limits_exceeded"].append("generation_tokens_per_second")
        return met

    def metrics(self):
        code, text, _ = self.http.request("/metrics", timeout=2, text=True)
        if code != 200:
            raise Finding("ERROR", "Metrics endpoint did not return HTTP 200.", http_status=code)
        result = {}
        for name, key in (("requests_processing", "processing"), ("requests_deferred", "deferred")):
            value = counter_value(text, name)
            if value is None:
                raise Finding("ERROR", "Request metrics are missing or ambiguous.")
            result[key] = value
        return result

    def active(self, transfer, deadline=10):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if transfer.proc.poll() is not None:
                raise Finding("ERROR", "Load completed or failed before active work could be observed; exercise invalid.")
            code, slots, _ = self.http.request("/slots", timeout=min(2, end - time.monotonic()))
            if code == 200 and type(slots) is list and len(slots) == 1 and slots[0].get("is_processing") is True:
                return
            time.sleep(.1)
        raise Finding("ERROR", "Active work was not observed before the exercise deadline.")

    def memory_sample(self, device):
        try:
            text = Path(f"/proc/{self.server.proc.pid}/status").read_text()
            rss = re.search(r"^VmRSS:\s+(\d+)\s+kB$", text, re.M)
            vram = int(device.read_text())
            if rss is None or int(rss[1]) <= 0 or vram <= 0:
                raise ValueError
            return int(rss[1]), vram
        except (OSError, ValueError) as error:
            raise Finding("ERROR", "RSS or byte-valued VRAM telemetry is missing or invalid.") from error

    def wq06(self):
        device = self.server.telemetry_device()
        if not self.server.vram_baseline or device.resolve() != self.server.vram_device:
            raise Finding("ERROR", "Peak VRAM device does not match a recorded pre-start baseline.")
        self.obs.update(self.server.vram_baseline, peak_attribution="device_opened_by_owned_server")
        peak_rss = peak_vram = samples = 0
        seen_processing = seen_deferred = False
        load = self.http.start("/v1/chat/completions", load_task(tokens=512), self.limits["short_total_seconds"])
        self.active(load)
        request, expected = schema_task()
        competitor = self.http.start("/v1/chat/completions", request, self.limits["long_execution_seconds"])
        while True:
            rss, vram = self.memory_sample(device)
            samples += 1
            peak_rss, peak_vram = max(peak_rss, rss), max(peak_vram, vram)
            metrics = self.metrics()
            seen_processing |= metrics["processing"] > 0
            seen_deferred |= metrics["deferred"] > 0
            if load.proc.poll() is not None and competitor.proc.poll() is not None:
                break
            time.sleep(.5)
        results = {}
        final = self.metrics()
        self.obs.update(results=results, execution_bound_seconds=self.limits["long_execution_seconds"], processing_observed=seen_processing, deferred_observed=seen_deferred, final_queue_idle=final == {"processing": 0, "deferred": 0}, telemetry_samples=samples, sampled_peak_rss_kib=peak_rss, **vram_fields("sampled_peak_vram", peak_vram), **vram_fields("derived_server_share_vram", peak_vram - self.server.vram_baseline["pre_start_vram_bytes"]))
        for label, transfer in (("load", load), ("task", competitor)):
            result = transfer.result()
            body = result.json()
            self.obs[label + "_http_status"] = result.code
            require(result.code == 200, "A contending request failed HTTP status.", http_status=result.code)
            results[label] = {"http_first_byte_seconds": result.first, "total_seconds": result.total, **generation_observations(body)}
            timing = body.get("timings", {}) if type(body) is dict else {}
            speed, count = timing.get("predicted_per_second"), timing.get("predicted_n")
            if not numeric(speed) or speed <= 0 or type(count) is not int or count <= 0:
                raise Finding("ERROR", "Measured generation timing is missing or invalid; no speed is assumed.")
            results[label].update(generation_tokens_per_second=speed, generation_tokens=count)
            if label == "task":
                answer(body, expected)
            else:
                require(finish_code(body) in ("stop", "length"), "Synthetic load did not complete; its deliberate token-budget finish is allowed.")
        require(seen_processing and seen_deferred, "Contention/deferral was not observed; timing alone is not evidence of a queue.")
        require(final == {"processing": 0, "deferred": 0}, "Request queue metrics did not return to zero.")
        failed = []
        for label, measured in results.items():
            for key, limit_key, lower in (("http_first_byte_seconds", "first_byte_seconds", False), ("total_seconds", "short_total_seconds", False), ("generation_tokens_per_second", "generation_tokens_per_second", True)):
                if (measured[key] < self.limits[limit_key]) if lower else (measured[key] > self.limits[limit_key]):
                    failed.append(label + "." + key)
        if peak_rss > self.limits["rss_kib"]:
            failed.append("rss_kib")
        if peak_vram > self.limits["vram_mb"] * 1_000_000:
            failed.append("vram_decimal_mb")
        self.obs["limits_exceeded"] = failed
        require(not failed, "Observed operating limits were exceeded; see the named limits and measurements.")

    def wq07(self):
        original, msg, call = self.last_tool
        request = copy.deepcopy(original)
        request["messages"] += [msg, {"role": "tool", "tool_call_id": call["id"], "name": "lookup_ticket", "content": "not valid JSON for lookup_ticket"}, {"role": "user", "content": "The tool data is invalid. Make exactly one retry of the same read-only ticket lookup with the same fields. Do not infer the ticket state from invalid data."}]
        retry_msg, retry = tool_call(self.task(request), 18, ("title", "state"))
        self.obs["retry_call_valid"] = True
        self.obs["natural_language_explanation_scored"] = False
        request["messages"] += [retry_msg, {"role": "tool", "tool_call_id": retry["id"], "name": "lookup_ticket", "content": '{"title":"test record","state":"closed"}'}, {"role": "user", "content": "Return only JSON containing ticket 18 and the state from the corrected tool result, without another call."}]
        expected = {"ticket": 18, "state": "closed"}
        answer(self.task(request), expected)
        self.obs["corrected_result_matches"] = True
        self.recovery()

    def wq08(self):
        try:
            self.server.start(upload_timeout=True)
            with socket.create_connection(("127.0.0.1", self.server.port), timeout=2) as sock:
                body = b'{"model":"qwen3-coder","messages":['
                headers = f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {len(body) + 4096}\r\n\r\n".encode()
                started = time.monotonic()
                sock.sendall(headers + body)
                sock.settimeout(4)
                try:
                    raw = sock.recv(1024)
                except socket.timeout as error:
                    # On self.obs, not on the Finding: a failing restore in the finally clause would discard exception-carried observations.
                    self.obs.update(server_timeout_seconds=2, observation_deadline_seconds=4)
                    raise Finding("FAIL", "Client observation deadline elapsed while the server left the upload open.") from error
                except ConnectionResetError:
                    raw = b""
                elapsed = time.monotonic() - started
            match = re.match(rb"HTTP/\S+ (\d{3})", raw)
            status = int(match[1]) if match else 0
            self.obs.update(server_timeout_seconds=2, observed_close_seconds=round(elapsed, 3), upload_http_status=status, connection_closed_without_response=not raw)
            if elapsed < 1.5 or raw and status not in (400, 408):
                raise Finding("ERROR", "Server rejected the upload for an unrelated/immediate condition; the timeout was not exercised.")
            require(elapsed <= 4, "Upload closure exceeded the observation deadline.")
        finally:
            identity, _ = self.server.start()
            require(identity == self.baseline, "Restoring the normal profile changed its identity.")
        self.recovery()

    def wq09(self):
        transfer = self.http.start("/v1/chat/completions", load_task(), self.limits["short_total_seconds"])
        self.active(transfer)
        if transfer.proc.poll() is not None:
            raise Finding("ERROR", "Request ended before controlled interruption; restart exercise invalid.")
        old_pid = self.server.proc.pid
        self.server.stop(crash=True)
        interrupted = False
        try:
            old = transfer.result()
            # Headers alone are not a complete response; incomplete JSON is an interrupted request.
            try:
                successful = old.code == 200 and finish_code(old.json()) in ("stop", "length", "tool_calls")
            except Finding:
                successful = False
            interrupted = not successful
        except Finding:
            interrupted = True
        finally:
            transfer.close()
        if not interrupted:
            raise Finding("ERROR", "Old request completed before interruption; restart exercise invalid, not a model failure.")
        identity, elapsed = self.server.start()
        # Readiness is the server's own number and excludes the wait for the
        # crashed server's VRAM, which is where the downtime a reader cares
        # about actually goes. Publish both halves on the row that restarted.
        self.obs.update(old_request_interrupted=True, new_process=self.server.proc.pid != old_pid, restart_readiness_seconds=round(elapsed, 3), baseline_settle_seconds=self.server.vram_baseline["baseline_settle_seconds"], identity_matches=identity == self.baseline)
        require(identity == self.baseline, "Model/build/template/context identity changed after restart.")
        self.recovery()

    def wq10(self):
        n_ctx = self.baseline["n_ctx"]
        request, _ = self.long_request(n_ctx + 3000)
        count = self.count_tokens(request)
        if count <= n_ctx:
            raise Finding("ERROR", "Generated boundary input is not actually over context; no model failure is inferred.", input_tokens=count, n_ctx=n_ctx)
        code, body, _ = self.http.request("/v1/chat/completions", request, self.limits["short_total_seconds"])
        error = body.get("error", {}) if type(body) is dict else {}
        self.obs.update(http_status=code, input_tokens=count, n_ctx=n_ctx)
        require(code == 400 and type(error) is dict and error.get("type") == "exceed_context_size_error" and type(error.get("n_ctx")) is int and error["n_ctx"] == n_ctx and type(error.get("n_prompt_tokens")) is int and error["n_prompt_tokens"] > n_ctx, "Oversized input did not receive the pinned structured context rejection.")
        self.obs["oversized_input_http_status"] = code
        request, _, count, attempts = self.calibrated_request(n_ctx - 128, n_ctx - 64, boundary=True)
        code, body, timing = self.http.request("/v1/chat/completions", request, self.limits["long_execution_seconds"])
        observed = generation_observations(body)
        self.obs["generation_boundary"] = {"http_status": code, "input_tokens": count, "available_context_tokens": n_ctx - count, "calibration_attempts": attempts, **timing, **observed}
        generated = observed.get("completion_tokens")
        require(code == 200 and observed["finish_reason"] == "length" and observed.get("context_exhausted") is True and observed.get("prompt_tokens") == count and type(generated) is int and n_ctx - 1 <= count + generated <= n_ctx, "Generation did not stop at the context boundary as required.")
        self.recovery()

    def wq11(self):
        transfer = self.http.start("/v1/chat/completions", load_task(stream=True), self.limits["short_total_seconds"])
        end = time.monotonic() + self.limits["first_byte_seconds"]
        while time.monotonic() < end:
            generated, terminal = sse_state(transfer.snapshot())
            if terminal or transfer.proc.poll() is not None:
                raise Finding("ERROR", "Stream completed/failed before cancellation of active work; exercise invalid.")
            if generated:
                self.active(transfer, deadline=2)
                break
            time.sleep(.05)
        else:
            raise Finding("ERROR", "No generated stream delta was observed before the activity deadline.")
        self.obs["stream_http_status"] = transfer.status_header()
        require(self.obs["stream_http_status"] == 200, "Streaming endpoint did not return HTTP 200.")
        _, terminal = sse_state(transfer.snapshot())
        if terminal:
            raise Finding("ERROR", "A terminal stream event arrived before cancellation; exercise invalid.")
        started = time.monotonic()
        transfer.close()
        remaining = self.limits["cancel_recovery_seconds"] - (time.monotonic() - started)
        released = remaining > 0 and self.idle(remaining)
        elapsed = time.monotonic() - started
        self.obs.update(generated_delta_observed=True, terminal_before_cancel=False, slot_released=released, cancellation_recovery_seconds=round(elapsed, 3))
        require(released and elapsed <= self.limits["cancel_recovery_seconds"], "Active cancellation did not release the slot within the declared recovery limit.")
        self.recovery()


def apply_launch_spec(spec):
    record = spec["record"]
    if (not isinstance(record, dict) or record.get("kind") != "language"
            or not isinstance(record.get("id"), str) or not record["id"]
            or type(record.get("context_size")) is not int or record["context_size"] <= 0
            or type(record.get("vram_ceiling_bytes")) is not int or record["vram_ceiling_bytes"] <= 0):
        raise ValueError("WQ requires a valid stored language launch spec")
    profile = record["id"]
    SLOTS[profile] = {"context": record["context_size"], "long_check": LONG_CHECK}
    CASES[LONG_CHECK] = long_case(profile, record["context_size"])
    return profile


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Observe WQ cases; report failures without deciding profile approval.")
    parser.add_argument("command", nargs="?", default="run", choices=("run", "selftest", "cases"))
    parser.add_argument("--profile", default=os.getenv("WQ_PROFILE", "all"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--run-dir", type=Path, help="New output directory; existing directories are never overwritten.")
    parser.add_argument("--gpu-card", type=lambda s: s if re.fullmatch(r"card\d+", s) else parser.error("gpu-card must be cardN"))
    parser.add_argument("--role", default="Single-consumer read-only lookup and structured extraction; no editing, delegation, cloud-isolation or unattended-service approval.")
    for key, default in DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), type=float, default=default)
    args = parser.parse_args(argv)
    # A run serves the one record its launch spec pins, so a name is checked
    # against the spec (in main) rather than against a slot table.
    if (args.profile != "all" and args.profile not in SLOTS
            and not os.environ.get("MATUTO_WQ_LAUNCH_SPEC")):
        parser.error(f"invalid choice: {args.profile!r}")
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    for key in DEFAULTS:
        if not numeric(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error("limits must be finite positive numbers")
    for key in ("retrieval_samples", "retrieval_generation_tokens"):
        if int(getattr(args, key)) != getattr(args, key):
            parser.error(key.replace("_", "-") + " must be a whole number")
    # Role is owner-authored public prose. Do not accept paths/URLs/control characters as report metadata.
    if len(args.role) > 400 or any(c in args.role for c in ("\n", "\r", "\t", "|", "<", ">", "/", "\\")):
        parser.error("role must be short public prose without paths, URLs or markup")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.command == "selftest":
        return selftest()
    if args.command == "cases":
        for ident, (category, question, rule) in CASES.items():
            print(f"{ident} ({category})\n  Question:  {question}\n  Pass rule: {rule}\n")
        return 0
    os.umask(0o077)
    if not os.environ.get("MATUTO_WQ_LAUNCH_SPEC"):
        print("WQ requires the store's launch spec for a stored language profile.", file=sys.stderr)
        return 2
    try:
        launch_spec = json.loads(os.environ["MATUTO_WQ_LAUNCH_SPEC"])
        profile = apply_launch_spec(launch_spec)
        if args.profile not in ("all", profile):
            raise ValueError
        args.profile = profile
    except (KeyError, TypeError, ValueError):
        print("WQ launch spec is invalid or is not a stored language profile.", file=sys.stderr)
        return 2
    profiles = [profile]
    limits = validate_limits(launch_spec.get("wq_limits"))
    if "vram_mb" not in (launch_spec.get("wq_limits") or {}):
        limits["vram_mb"] = launch_spec["record"]["vram_ceiling_bytes"] / 1_000_000
    parent = Path(os.getenv("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ai-agents-sandbox/matuto-lab/worker-runs"
    output = args.run_dir or parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(4).hex())
    if output.resolve().is_relative_to(ROOT):
        print("WQ output must stay outside the repository. Keep run evidence in private storage.", file=sys.stderr)
        return 2
    report = Reporter(output, profiles, limits, args.role)
    def terminate(signum, frame):
        # A second SIGTERM must not interrupt teardown of the separately owned server.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        report.data["interruption_signal"] = "SIGTERM"
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        for profile in profiles:
            report.skip_pending(profile, "Not run yet.")
            with tempfile.TemporaryDirectory(prefix="private-", dir=report.root) as private:
                http = server = None
                try:
                    if not shutil.which("curl"):
                        raise Finding("ERROR", "curl is required for bounded HTTP operations.")
                    http = Transport(private, args.port)
                    server = Server(private, profile, args.port, limits, http, args.gpu_card)
                    runner = Runner(profile, server, http, report, limits)
                    runner.run()
                except Exception as error:
                    status, reason, observations = Runner.describe(error)
                    report.result(profile, "SETUP", status, reason, observations)
                    report.skip_pending(profile, "Profile setup/run could not continue.")
                finally:
                    if http is not None:
                        http.close()
                    if server is not None:
                        server.close()
    except KeyboardInterrupt:
        report.data["interrupted"] = True
        for profile in profiles:
            report.skip_pending(profile, "Run interrupted by Ctrl-C or SIGTERM; check was not completed.")
    finally:
        try:
            report.data["finished_utc"] = now()
            report.save()
        finally:
            signal.signal(signal.SIGTERM, previous_term)
    if args.run_dir:
        print("\nReport files: report.md and report.json in the requested --run-dir.")
    else:
        print("\nReport files: <state root>/ai-agents-sandbox/matuto-lab/worker-runs/" + output.name + "/report.md and report.json")
        print("State root is XDG_STATE_HOME, or ~/.local/state when unset.")
    print("Owner decision remains pending; this command changes no serving profile.")
    return 1 if report.data.get("interrupted") or any(r["status"] in ("FAIL", "ERROR") for r in report.data["results"]) else 0


# Offline tests live at the existing selftest entry point, not in a second test runner.
def selftest():
    return run_selftests()


def run_selftests():
    sys.path.insert(0, str(ROOT / "agents/tests"))
    from runtime_fixture import create as create_runtime_fixture, configure as configure_runtime_fixture

    def host_model(root, source="unsloth/Qwen3.6-35B-A3B-GGUF", name="Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"):
        """A model collection on this host, and the setting that points at it."""
        directory = root / "models" / source
        directory.mkdir(parents=True)
        model = directory / name
        model.touch()
        setting = root / "model-root"
        setting.write_text(f"{root / 'models'}\n")
        setting.chmod(0o600)
        return model, setting

    class Tests(unittest.TestCase):
        def body(self, content, finish="stop", calls=None):
            msg = {"role": "assistant", "content": content}
            if calls is not None:
                msg["tool_calls"] = calls
            return {"choices": [{"finish_reason": finish, "message": msg}]}

        def timed_body(self, content, rate=40.0, predicted=800):
            body = self.body(content)
            body["timings"] = {"prompt_n": 115500, "prompt_ms": 261000.0, "prompt_per_second": 442.529,
                               "predicted_n": predicted, "predicted_ms": round(predicted / rate * 1000, 3), "predicted_per_second": rate}
            return body

        def call_body(self, ticket=18, fields=None, text=None):
            call = {"id": "test-call", "type": "function", "function": {"name": "lookup_ticket", "arguments": json.dumps({"ticket": ticket, "fields": fields or ["title", "state"]})}}
            return self.body(text, "tool_calls", [call])

        def setUp(self):
            # A run has exactly one slot: the stored record its launch spec names.
            # Tests that never build a Server still report against that slot.
            SLOTS["128k"] = {"context": 131072, "long_check": LONG_CHECK}
            CASES[LONG_CHECK] = long_case("128k", 131072)
            self.addCleanup(SLOTS.clear)
            self.addCleanup(CASES.pop, LONG_CHECK, None)

        def stored_spec(self, root, model, context=131072, profile="128k", gpu_layers=-1,
                        server_arguments=None, files=None, port=8080):
            """The launch spec the store emits for a stored record pointing at this fixture's model file."""
            root = Path(root)
            arguments = ["--parallel", "1", "--no-context-shift"] if server_arguments is None else list(server_arguments)
            picked = {"model": {"path": str(Path(model)),
                                "sha256": hashlib.sha256(Path(model).read_bytes()).hexdigest()}}
            picked.update(files or {})
            rows, index = [], 0
            while index < len(arguments):
                option = arguments[index]
                value = None
                if index + 1 < len(arguments) and not arguments[index + 1].startswith("-"):
                    value = arguments[index + 1]
                    index += 1
                rows.append({"tool": "server", "option": option, "value": value})
                index += 1
            return {
                "record": {"kind": "language", "id": profile, "name": f"Stored {profile}",
                           "alias": profile, "context_size": context,
                           "vram_ceiling_bytes": 30_000_000_000, "server_host": "127.0.0.1",
                           "server_port": port, "files": picked, "gpu_layers": gpu_layers,
                           "server_output_tokens": context, "smoke_output_tokens": 16,
                           "bench_prompt_tokens": 512, "bench_generation_tokens": 128,
                           "bench_repetitions": 3,
                           "arguments": {"server": arguments, "smoke": [], "bench": []},
                           "argument_rows": rows},
                "runtime": {"build_id": "fixture", "card_id": "card1", "backend": "hip",
                            "location": str(root / "build"), "recipe": "fixture",
                            "gpu_identity": "GPU-1234567890abcdef"},
                "lab_port": port,
            }

        @contextlib.contextmanager
        def pinned(self, spec):
            """Build a Server under a pinned launch spec without touching a real selection."""
            with patch.dict(os.environ, {"MATUTO_WQ_LAUNCH_SPEC": json.dumps(spec)}), \
                    patch.object(Server, "install_runtime_selection"):
                yield

        def test_strict_structured_values(self):
            _, expected = schema_task()
            answer(self.body(json.dumps(expected)), expected)
            for bad in ({**expected, "actionable": 1}, {**expected, "ticket": True}, {**expected, "state": "not open"}, {**expected, "extra": 1}, []):
                with self.assertRaises(Finding):
                    answer(self.body(json.dumps(bad)), expected)
            with self.assertRaises(Finding):
                answer(self.body(json.dumps(expected), "length"), expected)

        def test_tool_exact_request_not_allowed_ticket_set(self):
            tool_call(self.call_body(), 18, ("state", "title"))
            tool_call(self.call_body(29, ["state"]), 29, ("state",))
            for bad in (self.call_body(29), self.call_body(True), self.call_body(18, ["state", "state"]), self.call_body(18, ["state"])):
                with self.assertRaises(Finding):
                    tool_call(bad, 18, ("title", "state"))
            tool_call(self.call_body(text="Cannot tell whether open or closed; retrying."), 18, ("title", "state"))

        def test_requests_state_actual_task(self):
            req, expected = schema_task()
            for value in ("18", "open", "true"):
                self.assertIn(value, req["messages"][0]["content"])
            self.assertNotIn("const", req["response_format"]["json_schema"]["schema"]["properties"]["ticket"])
            self.assertEqual(lookup_task()["tool_choice"], "auto")
            self.assertNotIn("SELECTED_MARKER", json.dumps(lookup_task()))
            for request in (req, lookup_task()):
                self.assertEqual(request["max_tokens"], -1)
                self.assertTrue(request["verbose"])
                self.assertEqual(request["response_fields"], ["truncated"])
            self.assertEqual(load_task(tokens=512)["max_tokens"], 512)
            self.assertEqual(load_task()["max_tokens"], 2048)

        def test_context_stop_is_not_an_incorrect_answer(self):
            _, expected = schema_task()
            body = self.body(json.dumps(expected), "length")
            body["usage"] = {"prompt_tokens": 115500, "completion_tokens": 15572}
            for detail, kind in ((True, "context_exhausted"), (False, "generation_limit"), (None, "generation_limit")):
                body["__verbose"] = {"truncated": detail, "prompt": "PRIVATE_INPUT", "content": "PRIVATE_OUTPUT"}
                with self.assertRaises(Finding) as caught:
                    answer(body, expected)
                observed = caught.exception.observations
                self.assertEqual(observed["failure_kind"], kind)
                self.assertFalse(observed["correctness_evaluated"])
                self.assertEqual(observed["completion_tokens"], 15572)
                self.assertNotIn("PRIVATE", json.dumps(observed))
            body["usage"] = {"prompt_tokens": True, "completion_tokens": "PRIVATE_COUNT"}
            body["__verbose"]["truncated"] = "PRIVATE_FLAG"
            self.assertEqual(generation_observations(body), {"finish_reason": "length"})
            body["usage"] = {"prompt_tokens": -1, "completion_tokens": 2**31}
            self.assertEqual(generation_observations(body), {"finish_reason": "length"})

        def test_config_directory_rule_is_shared_by_both_programs(self):
            def status(mode, uid=None):
                return os.stat_result((mode, 0, 0, 1, os.getuid() if uid is None else uid, 0, 0, 0, 0, 0))
            # Neither mode nor owner is the rule any more. Being a directory is.
            for mode in (0o700, 0o750, 0o770, 0o707, 0o777):
                self.assertTrue(runtime.directory_is_safe(status(stat.S_IFDIR | mode)))
            self.assertTrue(runtime.directory_is_safe(status(stat.S_IFDIR | 0o700, os.getuid() + 1)))
            self.assertFalse(runtime.directory_is_safe(status(stat.S_IFREG | 0o700)))

        def test_a_links_own_owner_decides_whether_this_account_follows_it(self):
            # Runtime support owns the link rule used by the app, lab and WQ.
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                settings = root / "settings"
                settings.write_text("/srv/models\n")
                settings.chmod(0o600)
                link = root / "linked"
                link.symlink_to(settings)
                self.assertEqual(runtime.read_private_text(link, 4_096), "/srv/models\n")
                real_lstat = os.lstat
                real_stat = os.stat

                def foreign(info):
                    return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                                           os.getuid() + 1, info.st_gid, info.st_size,
                                           info.st_atime, info.st_mtime, info.st_ctime))

                def foreign_lstat(path, *args, **kwargs):
                    info = real_lstat(path, *args, **kwargs)
                    return foreign(info) if stat.S_ISLNK(info.st_mode) else info

                def foreign_stat(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    return foreign(info) if stat.S_ISLNK(info.st_mode) else info

                with patch("os.lstat", side_effect=foreign_lstat), patch("os.stat", side_effect=foreign_stat):
                    with self.assertRaises(runtime.InspectionError):
                        runtime.read_private_text(link, 4_096)

        def test_runtime_directory_walks_a_stow_folded_config_root(self):
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                home = root / "home"
                home.mkdir(mode=0o700)
                (home / ".config").mkdir(mode=0o700)
                # What GNU Stow produces when one package owns the whole directory.
                target = root / "dotfiles/agents/.config/ai-agents-sandbox"
                target.mkdir(mode=0o700, parents=True)
                (home / ".config/ai-agents-sandbox").symlink_to(target)
                with patch.dict(os.environ, {"HOME": str(home)}):
                    os.close(runtime._runtime_directory())
                    self.assertTrue((target / "matuto-runtime").is_dir())
                    # The link is followed and its target walked like any other
                    # component. A mode that drifted afterwards is walked, not
                    # refused; this used to stop every host action until someone
                    # chmodded the directory back by hand.
                    target.chmod(0o777)
                    os.close(runtime._runtime_directory())
                    target.chmod(0o700)
                    (target / "matuto-runtime").chmod(0o750)
                    os.close(runtime._runtime_directory())
                    (target / "matuto-runtime").chmod(0o700)
                    os.close(runtime._runtime_directory())
                # A supplied installation names a binary to execute, so its walk
                # still refuses the link that the configuration walk follows.
                linked = home / ".config/ai-agents-sandbox"
                with self.assertRaises(runtime.InspectionError):
                    runtime.protected_open(linked, directory=True)
                os.close(runtime.protected_open(linked, directory=True, follow_links=True))

        def test_runtime_cli_reads_a_selection_under_a_folded_config_root(self):
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                home = root / "home"
                home.mkdir(mode=0o700)
                (home / ".config").mkdir(mode=0o700)
                target = root / "dotfiles/agents/.config/ai-agents-sandbox"
                target.mkdir(mode=0o700, parents=True)
                (home / ".config/ai-agents-sandbox").symlink_to(target)
                # The record is read through the protected walk rather than the
                # directory descriptor, so the whole command has to be exercised.
                def show():
                    return subprocess.run([str(ROOT / "agents/matuto-lab"), "runtime", "show"],
                                          env=dict(os.environ, HOME=str(home)), capture_output=True, text=True)
                result = show()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("unconfigured", result.stdout)
                # Following a link is for directories only: the record at the end
                # of the walk keeps its no-follow open however it was reached.
                (target / "matuto-runtime").mkdir(mode=0o700)
                elsewhere = root / "elsewhere.json"
                # A record the command would accept if it read the link: the only
                # reason this refuses is the walk's no-follow open at its end.
                elsewhere.write_text(json.dumps({
                    "installation": "/tmp/installed/bin",
                    "backend": "HIP/ROCm",
                    "pci": "0000:03:00.0",
                    "corroborator": {"uuid": "GPU-1234567890abcdef", "host": {"pciAddress": "0000:03:00.0"}},
                    "executable": {},
                }))
                elsewhere.chmod(0o600)
                (target / "matuto-runtime/selection.json").symlink_to(elsewhere)
                result = show()
                # Refused at the walk, not read and then rejected for its content:
                # reading the link would report a configured runtime and exit 0.
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")

        def test_llama_cpp_cli_and_server_build_spellings_are_read(self):
            old = "version: 10218 (de699957b)\nbuilt with GNU 16.1.1"
            new = "version: 0.4.0-dev (build 10917, commit 8ea290247)\nbuilt with GNU 16.1.1"
            self.assertEqual(runtime.build_identity(old), {"number": 10218, "revision": "de699957b"})
            self.assertEqual(runtime.build_identity(new), {"number": 10917, "revision": "8ea290247"})
            self.assertEqual(runtime.build_revision("10218 (de699957b)"), "de699957b")
            self.assertEqual(runtime.build_revision("0.4.0-dev (build 10917, commit 8ea290247)"), "8ea290247")
            self.assertEqual(runtime.build_identity("b10917-8ea290247"), {"number": 10917, "revision": "8ea290247"})
            for absent in ("0.4.0-dev (build 10917)", "(unreleased)", "b10917-8ea290", "b10917-8ea290247garbage", "", None, 10218):
                self.assertIsNone(runtime.build_identity(absent))

        def test_build_revision_is_read_from_either_spelling(self):
            self.assertEqual(runtime.build_revision("10218 (de699957b)"), "de699957b")
            self.assertEqual(runtime.build_revision("version: 10218 (de699957b)\nbuilt with GNU 16.1.1"), "de699957b")
            for absent in ("10218 (unreleased)", "10218 de699957b", "", None, 10218):
                self.assertIsNone(runtime.build_revision(absent))


        def test_the_stored_record_reaches_the_runtime_without_a_profile_file(self):
            # Moving llama.cpp must not take a stored record with it: the run
            # serves the pinned model through the runtime, and reads no profile
            # file anywhere.
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                model = root / "models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
                model.parent.mkdir()
                model.write_bytes(b"weights")
                agents, build = create_runtime_fixture(root / "runtime")
                (root / "home").mkdir(mode=0o700)
                spec = self.stored_spec(root, model)
                spec["runtime"]["location"] = str(build.parent)
                with self.pinned(spec):
                    server = Server(root / "private", "128k", 8080, DEFAULTS, None)
                self.assertEqual(json.loads(server.target.read_text()), spec)
                self.assertFalse(list(server.cfg.glob("ai-agents-sandbox/matuto-lab/*.sh")))
                self.assertFalse((server.cfg / "ai-agents-sandbox/matuto-lab/defaults.sh").exists())
                server.env.update(HOME=str(root / "home"))
                configure_runtime_fixture(agents, build, server.env)
                with patch.object(sys.modules[__name__], "ROOT", root / "runtime"):
                    result = subprocess.run(server.runtime_command("serve", "llama-server", server.expected["GPU_LAYERS"], server.server_arguments(False)), env=server.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                received = json.loads((root / "runtime/received.json").read_text())
                self.assertEqual(received["hip"], "GPU-1234567890abcdef")
                args = received["args"]
                self.assertEqual(args[args.index("-m") + 1], str(model))
                self.assertEqual(args[args.index("-n") + 1], "131072")
                self.assertEqual(args[args.index("-c") + 1], "131072")
                self.assertIn("--no-context-shift", args)
                self.assertNotIn("--context-shift", args)

        def test_the_pinned_record_serves_its_own_context(self):
            # The record states the context this run qualifies, so the served
            # window is the stored one rather than a slot table's.
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                model = root / "models/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
                model.parent.mkdir()
                model.write_bytes(b"weights")
                agents, build = create_runtime_fixture(root / "runtime")
                (root / "home").mkdir(mode=0o700)
                spec = self.stored_spec(root, model, 98304, profile="stored-96k")
                spec["runtime"]["location"] = str(build.parent)
                with self.pinned(spec):
                    server = Server(root / "private", "stored-96k", 8080, DEFAULTS, None)
                self.assertEqual(server.expected["MODEL_PATH"], str(model))
                self.assertEqual(server.expected["CONTEXT_SIZE"], "98304")
                server.env.update(HOME=str(root / "home"))
                configure_runtime_fixture(agents, build, server.env)
                with patch.object(sys.modules[__name__], "ROOT", root / "runtime"):
                    result = subprocess.run(server.runtime_command("serve", "llama-server", server.expected["GPU_LAYERS"], server.server_arguments(False)), env=server.env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                args = json.loads((root / "runtime/received.json").read_text())["args"]
                self.assertEqual(args[args.index("-c") + 1], "98304")
                self.assertEqual(args[args.index("-n") + 1], "98304")

        def test_cli_selects_the_pinned_slot(self):
            self.assertEqual(parse_args(["run", "--profile", "all"]).profile, "all")
            self.assertEqual(parse_args(["run", "--profile", "128k"]).profile, "128k")
            # The table-borne profile source is gone: a run takes the store's spec.
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(["run", "--profile-source", "128k=/tmp/alternate.sh"])

        def test_stored_launch_spec_adds_any_context_without_the_slot_table(self):
            profile = "stored-16384"
            spec = {"record": {"kind": "language", "id": profile,
                               "context_size": 16384, "vram_ceiling_bytes": 24_000_000_000}}
            original_case = CASES.get("WQ-LONG")
            try:
                self.assertEqual(apply_launch_spec(spec), profile)
                self.assertEqual(SLOTS[profile]["context"], 16384)
                self.assertEqual(CASES["WQ-LONG"], long_case(profile, 16384))
                low, high = long_band(16384)
                self.assertAlmostEqual((low + high) / 2 / 16384, 0.88, delta=0.01)
            finally:
                SLOTS.pop(profile, None)
                if original_case is None:
                    CASES.pop("WQ-LONG", None)
                else:
                    CASES["WQ-LONG"] = original_case

        def test_stored_server_takes_only_the_launch_spec(self):
            with tempfile.TemporaryDirectory() as temp:
                temp = Path(temp)
                model = temp / "models/private model.gguf"
                model.parent.mkdir()
                model.write_bytes(b"weights")
                spec = self.stored_spec(temp, model, 16384, profile="stored-server", gpu_layers=42,
                                        server_arguments=["--temperature", "0.2", "--gpu-layers=42"])
                with self.pinned(spec):
                    server = Server(temp / "private", "stored-server", 8080, DEFAULTS, None)
                self.assertEqual(server.expected["MODEL_PATH"], str(model))
                self.assertEqual(server.profile_source_label, "stored launch spec")
                self.assertEqual(json.loads(server.target.read_text()), spec)
                args = server.server_arguments(False)
                self.assertEqual(args[:2], ["--temperature", "0.2"])
                # The record's own gpu-layers argument is the one served, so the
                # runner must not append a second one.
                self.assertNotIn("-ngl", args)
                self.assertEqual(args[args.index("-m") + 1], str(model))
                self.assertEqual(server.expected["SERVER_OUTPUT_TOKENS"], "16384")
                self.assertFalse((temp / "private/config/ai-agents-sandbox/matuto-lab/defaults.sh").exists())

        def test_every_start_verifies_the_record_files_and_states_their_digests(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                projector = Path(root) / "projector.gguf"
                projector.write_bytes(b"projector")
                files = {"mmproj": {"path": str(projector),
                                    "sha256": hashlib.sha256(b"projector").hexdigest()}}
                with patch.dict(os.environ, {"MATUTO_RUNTIME_MTP_SHA256": "b" * 64}), \
                     self.telemetry_server(root, files=files) as (server, _used):
                    model = Path(server.record["files"]["model"]["path"])
                    server.start()
                    # The digests the start observed are the ones the runtime records.
                    self.assertEqual(server.env["MATUTO_RUNTIME_MODEL_SHA256"],
                                     hashlib.sha256(model.read_bytes()).hexdigest())
                    self.assertEqual(server.env["MATUTO_RUNTIME_MMPROJ_SHA256"],
                                     files["mmproj"]["sha256"])
                    # A sidecar the record does not pick inherits no digest.
                    self.assertNotIn("MATUTO_RUNTIME_MTP_SHA256", server.env)
                    model.write_bytes(b"replaced weights")
                    with self.assertRaises(Finding) as caught:
                        server.start()
                    self.assertEqual(caught.exception.status, "ERROR")

        def test_an_unknown_profile_is_named_before_any_case(self):
            for argv, environment in ((["run", "--profile", "512k"], {}),
                                      (["run", "--run-dir", "/tmp/unused"],
                                       {"WQ_PROFILE": "512k"})):
                with patch.dict(os.environ, environment), \
                     contextlib.redirect_stderr(io.StringIO()) as error, \
                     self.assertRaises(SystemExit):
                    parse_args(argv)
                self.assertIn("invalid choice: '512k'", error.getvalue())

        def stored_identity_server(self, root, context=131072, served_model=None):
            # A real Server built from a stored record, with the HTTP identity
            # endpoints the identity check reads stubbed as in identity_server.
            root = Path(root)
            model = root / "stored.gguf"
            model.write_bytes(b"weights")
            spec = self.stored_spec(root, model, context)
            with self.pinned(spec):
                server = Server(root / "private", "128k", 8080, DEFAULTS, None)
            # The expected executable is the one this fixture provides, as in
            # identity_server: /proc/<pid>/exe resolves to this interpreter.
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "llama-server").symlink_to(sys.executable)
            server.expected["INSTALLATION"] = str(bindir)
            server.proc = type("P", (), {"pid": os.getpid(), "poll": lambda self: None})()
            props = {"model_path": str(served_model or model), "build_info": "10218 (de699957b)", "chat_template": "template", "total_slots": 1, "chat_template_caps": {"supports_preserve_reasoning": True}}
            server.http = type("H", (), {"request": lambda self, path, *a, **k: (200, props if path == "/props" else [{"n_ctx": context}], None), "close": lambda self: None})()
            return server

        def test_stored_record_records_its_identity(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                root = Path(root)
                server = self.stored_identity_server(root)
                report = Reporter(root / "report", ["128k"], DEFAULTS, "test role")
                runner = Runner("128k", server, server.http, report, DEFAULTS)
                runner.idle = lambda seconds=None: True
                server.verify = server.profile_record
                server.start = lambda upload_timeout=False: (server.identity(), .01)
                for ident in CASES:
                    setattr(runner, ident.lower().replace("-", ""), lambda ident=ident: None)
                # The long-context row is dispatched by name, not by method, so
                # stubbing the wqNN methods alone would leave it doing real work.
                runner.long_context = lambda: None
                runner.run()
                recorded = report.data["profiles"]["128k"]
                self.assertIn("serving", recorded)
                self.assertEqual(recorded["profile_source"], "stored launch spec")
                self.assertEqual(recorded["expected_model_sha256"], hashlib.sha256(b"weights").hexdigest())
                self.assertEqual(recorded["expected_model_revision"], "")
                self.assertEqual(recorded["expected_backend_commit"], "unavailable")
                self.assertEqual(recorded["server_output_tokens"], 131072)
                self.assertEqual(recorded["observed"]["n_ctx"], 131072)
                self.assertEqual(recorded["observed"]["template_sha256"], hashlib.sha256(b"template").hexdigest())
                # Every applicable case ran as a stub; the other slots' long checks
                # are SKIP. An ERROR means a case reached the network instead.
                self.assertEqual({r["status"] for r in report.data["results"]}, {"PASS", "SKIP"})

        def test_served_identity_mismatch_stops_before_any_case(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                root = Path(root)
                other = root / "other.gguf"
                other.touch()
                server = self.stored_identity_server(root, served_model=other)
                with self.assertRaises(Finding) as caught:
                    server.identity()
                self.assertFalse(caught.exception.observations["model_matches"])
                report = Reporter(root / "report", ["128k"], DEFAULTS, "test role")
                runner = Runner("128k", server, server.http, report, DEFAULTS)
                dispatched = []
                for ident in CASES:
                    setattr(runner, ident.lower().replace("-", ""), lambda ident=ident: dispatched.append(ident))
                server.verify = server.profile_record
                server.start = lambda upload_timeout=False: (server.identity(), .01)
                runner.run()
                self.assertEqual(dispatched, [])
                self.assertEqual(report.get("128k", "SETUP")["status"], "FAIL")
                self.assertTrue(all(report.get("128k", ident)["status"] == "SKIP" for ident in CASES))

        def test_serving_comparison_flags_replaced_weights_at_the_same_path(self):
            # Retained and current agree on the model path, so only the verified
            # digest can tell that the weights behind it were replaced.
            with tempfile.TemporaryDirectory() as root:
                server = self.stored_identity_server(root)
                (server.raw / "runtime.json").write_text(json.dumps({
                    "check": "serve", "installation": "/install", "backend": "HIP/ROCm", "pci": "0000:03:00.0",
                    "uuid": "GPU-1234567890abcdef", "selector": "0", "executable": {"sha256": "a" * 64},
                    "build": None, "gpuLayers": "99",
                    "request": {"model": server.expected["MODEL_PATH"], "model_sha256": "0" * 64, "context": "131072"},
                    "host": {}, "driver": {}, "offload": {"requested_gpu_layers": "99"}}))
                current = {"backend": "HIP/ROCm", "pci": "0000:03:00.0", "uuid": "GPU-1234567890abcdef",
                           "executable": {"sha256": "a" * 64}, "host": {}, "driver": {}}
                with patch.object(runtime, "read_selection", return_value={}), \
                     patch.object(runtime, "current_configuration", return_value=current):
                    serving = server.serving()
                self.assertEqual(serving["comparison"]["changed"], ["request.model_sha256"])
                self.assertEqual(serving["comparison"]["state"], "changed")
                self.assertEqual(serving["identity"]["request"]["model_sha256"], "0" * 64)
                # The publication copy names no location: the model path stays in
                # the private runtime artifact, not in either report file.
                self.assertNotIn(server.expected["MODEL_PATH"], json.dumps(serving))
                report = Reporter(Path(root) / "report", ["128k"], DEFAULTS, "test role")
                report.data["profiles"]["128k"] = {"serving": serving}
                report.save()
                for name in ("report.json", "report.md"):
                    self.assertNotIn(server.expected["MODEL_PATH"], (report.root / name).read_text())

        def test_long_context_band_follows_the_declared_context(self):
            # The 96K and 128K bands are recorded qualification evidence. The
            # derivation has to reproduce them exactly, or the shipped rows now
            # describe a check the recorded runs did not perform.
            rule = ("server-counted input tokens per sample, each sample carrying its own three values, all three values returned in complete JSON, server recovers. "
                    "PASS requires unanimity across the scored samples; a sample that produced no scorable answer, whether it hit the generation bound, exhausted the context, or reached the execution deadline, is unscored rather than a retrieval failure. "
                    "This is sampled retrieval, not general long-document quality.")
            self.assertEqual(long_case("128k", 131072), ("Context", "Can the 128K profile retrieve three values from a roughly 115K-token input?", "115000\u2013116000 " + rule))
            self.assertEqual(long_case("96k", 98304), ("Context", "Can the 96K profile retrieve three values from a roughly 85K-token input?", "85000\u201386000 " + rule))
            self.assertEqual(CASES[LONG_CHECK], long_case("128k", 131072))
            self.assertEqual(long_band(196608), (170000, 171000))
            self.assertEqual(long_band(262144), (230000, 231000))
            for name, context in (("128k", 131072), ("96k", 98304), ("192k", 196608), ("256k", 262144)):
                low, high = long_band(context)
                self.assertLess(high, context, f"{name} leaves no answer headroom")
                self.assertGreater(low / context, 0.85, f"{name} band is not a long-context input")

        def test_retrieval_request_carries_its_bound_and_the_boundary_probe_does_not(self):
            # The bound keeps a runaway generation from spending the whole deadline
            # without closing an answer. WQ-10 reaches the same builder to fill the
            # context, so its probe must stay unbounded.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.count_tokens = lambda request: 115500
                request, _, _, _ = runner.calibrated_request(115000, 116000)
                self.assertEqual(request["max_tokens"], DEFAULTS["retrieval_generation_tokens"])
                self.assertIn("response_format", request)
                probe, _, _, _ = runner.calibrated_request(115000, 116000, boundary=True)
                self.assertEqual(probe["max_tokens"], -1)
                self.assertTrue(probe["ignore_eos"])
                self.assertNotIn("response_format", probe)

        def test_retrieval_count_and_bound_are_limits_and_flags(self):
            # The host run varies the sample count without editing the runner, and
            # the report publishes both values in its limits block.
            args = parse_args(["run", "--retrieval-samples", "5", "--retrieval-generation-tokens", "4096"])
            self.assertEqual(int(args.retrieval_samples), 5)
            self.assertEqual(int(args.retrieval_generation_tokens), 4096)
            self.assertEqual(DEFAULTS["retrieval_samples"], 3)
            self.assertEqual(DEFAULTS["retrieval_generation_tokens"], 8192)
            # A fractional count or bound would be silently truncated by int().
            for flag, value in (("--retrieval-samples", "2.5"), ("--retrieval-generation-tokens", "1024.5")):
                with self.assertRaises(SystemExit):
                    parse_args(["run", flag, value])

        def test_long_context_takes_three_samples_each_with_its_own_sentinels(self):
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                seen = []
                def calibrated(low, high, **kwargs):
                    seen.append(kwargs.get("sentinels"))
                    return "request", expected, low + 500, 2
                runner.calibrated_request = calibrated
                runner.long_task = lambda request: (self.timed_body(json.dumps(expected)), {})
                runner.long_context()
                self.assertEqual(runner.obs["target_band"], [115000, 116000])
                self.assertEqual(runner.obs["input_tokens"], 115500)
                # Three samples ran, all three were scorable, and all three passed.
                self.assertEqual(runner.obs["retrieval_samples"], 3)
                self.assertEqual(runner.obs["retrieval_samples_scored"], 3)
                self.assertEqual(runner.obs["retrieval_samples_passed"], 3)
                self.assertEqual(runner.obs["retrieval_generation_bound_tokens"], DEFAULTS["retrieval_generation_tokens"])
                self.assertEqual([s["sample"] for s in runner.obs["retrieval_sample_results"]], [1, 2, 3])
                self.assertEqual([s["outcome"] for s in runner.obs["retrieval_sample_results"]], ["passed"] * 3)
                # No two samples reuse a prompt, so the draws are independent.
                self.assertEqual(len(seen), 3)
                self.assertEqual(len({json.dumps(s, sort_keys=True) for s in seen}), 3)
                self.assertTrue(all(seen))

        def test_a_sample_whose_numbers_are_copied_from_start_does_not_pass(self):
            # The three values have to be unrelated. With one number shared by all
            # three positions, a model that read START could fill in MIDDLE and END
            # from it and pass without retrieving either.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                for sample in range(3):
                    _, expected = runner.long_request(20, retrieval_sentinels(sample))
                    self.assertEqual(len(set(expected.values())), 3)
                    self.assertEqual(set(expected), {"start", "middle", "end"})
                    number = expected["start"].split("-")[1]
                    copied = {position: word + "-" + number for position, word in (("start", "cobalt"), ("middle", "cedar"), ("end", "ember"))}
                    with self.assertRaises(Finding):
                        answer(self.body(json.dumps(copied)), expected)
                    answer(self.body(json.dumps(expected)), expected)

        def test_long_context_reports_the_rate_it_observed(self):
            # One sample answered wrong and two answered right: the row fails, and
            # what it reports is the rate, not a single sample read as settled.
            with tempfile.TemporaryDirectory() as root:
                runner, report = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                missing = json.dumps({"ticket": 18, "state": "open"})
                answers = iter([self.timed_body(json.dumps(expected)), self.timed_body(missing), self.timed_body(json.dumps(expected))])
                runner.long_task = lambda request: (next(answers), {})
                with self.assertRaises(Finding) as caught:
                    runner.long_context()
                self.assertEqual(caught.exception.status, "FAIL")
                self.assertIn("2 of 3", caught.exception.reason)
                self.assertEqual(runner.obs["retrieval_samples"], 3)
                self.assertEqual(runner.obs["retrieval_samples_scored"], 3)
                self.assertEqual(runner.obs["retrieval_samples_passed"], 2)
                self.assertEqual([s["outcome"] for s in runner.obs["retrieval_sample_results"]], ["passed", "wrong_or_missing", "passed"])
                report.result("128k", LONG_CHECK, caught.exception.status, caught.exception.reason, runner.obs)
                published = (report.root / "report.md").read_text() + (report.root / "report.json").read_text()
                self.assertIn("2 of 3", published)
                self.assertIn("unanimity across the scored samples", published)
                self.assertIn(json.dumps("retrieval_samples") + ": 3", published)

        def test_long_context_does_not_count_an_unscored_sample_as_a_retrieval_failure(self):
            # A sample cut off by the generation bound or by the end of the context
            # has no answer to score, so it is unscored rather than a retrieval
            # failure, and the row reports the rate over the samples that answered.
            for kind, truncated in (("generation_limit", False), ("context_exhausted", True)):
                with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                    runner, _ = self.runner(root)
                    runner.obs = {}
                    _, expected = schema_task()
                    runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                    good = self.timed_body(json.dumps(expected))
                    cut = self.timed_body("", rate=40.0)
                    cut["choices"][0]["finish_reason"] = "length"
                    cut["__verbose"] = {"truncated": truncated}
                    answers = iter([good, cut, good])
                    runner.long_task = lambda request: (next(answers), {})
                    reason = runner.long_context()
                    self.assertEqual(runner.obs["retrieval_samples"], 3)
                    self.assertEqual(runner.obs["retrieval_samples_scored"], 2)
                    self.assertEqual(runner.obs["retrieval_samples_passed"], 2)
                    self.assertEqual([s["outcome"] for s in runner.obs["retrieval_sample_results"]], ["passed", "unscored", "passed"])
                    self.assertEqual(runner.obs["retrieval_sample_results"][1]["unscored_reason"], kind)
                    # A completed response leaves the slot free, so only the deadline path waits.
                    self.assertNotIn("slot_released_before_next_sample", runner.obs["retrieval_sample_results"][1])
                    self.assertIn("2 of 2", reason)
                    self.assertIn("1 sample(s) were unscored", reason)

        def test_long_context_fails_when_nothing_was_scored(self):
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                cut = self.timed_body("", rate=40.0)
                cut["choices"][0]["finish_reason"] = "length"
                cut["__verbose"] = {"truncated": True}
                runner.long_task = lambda request: (cut, {})
                with self.assertRaises(Finding) as caught:
                    runner.long_context()
                self.assertEqual(caught.exception.status, "FAIL")
                self.assertIn("None of the 3 samples produced a scorable answer", caught.exception.reason)
                self.assertEqual(runner.obs["retrieval_samples_scored"], 0)
                self.assertEqual(runner.obs["retrieval_samples_passed"], 0)

        def test_deadline_observations_report_what_was_achieved(self):
            # The live slot counters are the only record of a killed transfer. The
            # first scenario is the 192K case: prefill done, generation running at
            # about 17 tokens/s when the deadline arrived. The second is a deadline
            # that arrived during prefill: there is no rate to compare, so the
            # record has to say why.
            running = [{"prefill_tokens": 170500, "generation_tokens": 0, "elapsed_seconds": 261.0},
                       {"prefill_tokens": 170500, "generation_tokens": 26108, "elapsed_seconds": 1800.0}]
            observed = deadline_observations(running)
            self.assertTrue(observed["deadline_progress_available"])
            self.assertEqual(observed["progress_prefill_tokens"], 170500)
            self.assertEqual(observed["progress_generation_tokens"], 26108)
            self.assertEqual(observed["progress_prefill_seconds"], 261.0)
            self.assertEqual(observed["progress_generation_seconds"], 1539.0)
            self.assertAlmostEqual(observed["progress_generation_tokens_per_second"], 16.964)
            self.assertTrue(observed["generation_rate_evaluated"])
            prefill_only = deadline_observations([{"prefill_tokens": 85000, "generation_tokens": 0, "elapsed_seconds": 500.0}])
            self.assertTrue(prefill_only["deadline_progress_available"])
            self.assertFalse(prefill_only["generation_rate_evaluated"])
            self.assertIn("had not started", prefill_only["generation_rate_evaluation_reason"])
            unreadable = deadline_observations([])
            self.assertFalse(unreadable["deadline_progress_available"])
            self.assertIn("could not be read", unreadable["deadline_progress_reason"])

        def test_long_context_keeps_the_samples_that_already_ran(self):
            # A later sample can fail as ERROR: unmeasurable timing, calibration,
            # or a non-timeout transfer failure. The row still has to carry what
            # the samples before it measured rather than only the exception.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                def long_task(request):
                    if runner.obs.get("retrieval_samples_scored"):
                        raise Finding("ERROR", "Measured generation timing is missing or invalid; no speed is assumed.")
                    return self.timed_body(json.dumps(expected)), {}
                runner.long_task = long_task
                with self.assertRaises(Finding) as caught:
                    runner.long_context()
                self.assertEqual(caught.exception.status, "ERROR")
                self.assertEqual(runner.obs["retrieval_samples_scored"], 1)
                self.assertEqual(runner.obs["retrieval_samples_passed"], 1)
                self.assertEqual(len(runner.obs["retrieval_sample_results"]), 1)
                sample = runner.obs["retrieval_sample_results"][0]
                self.assertEqual(sample["outcome"], "passed")
                self.assertEqual(sample["generation_tokens_per_second"], 40.0)

        def test_long_task_keeps_the_deadline_record_and_its_curl_exit(self):
            # The timeout Finding's own observations are the only record of why
            # the transfer ended, so a deadline sample must carry them beside the
            # live progress rather than only the progress.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                runner.progress_sample = lambda started: None
                class Proc:
                    def __init__(self):
                        self.checks = 0
                    def poll(self):
                        self.checks += 1
                        return None if self.checks == 1 else 0
                class Transfer:
                    proc = Proc()
                    def result(self):
                        raise Finding("FAIL", "HTTP execution deadline elapsed; no completed answer was available to score.",
                                      failure_kind="execution_timeout", correctness_evaluated=False, curl_exit=28, deadline_seconds=1800)
                    def close(self):
                        pass
                runner.http = type("LongHTTP", (), {"start": lambda self, *a: Transfer()})()
                body, observations = runner.long_task({"messages": []})
                self.assertIsNone(body)
                self.assertEqual(observations["curl_exit"], 28)
                self.assertEqual(observations["deadline_seconds"], 1800)
                self.assertEqual(observations["failure_kind"], "execution_timeout")
                self.assertFalse(observations["deadline_progress_available"])

        def test_long_context_records_a_deadline_sample_as_unscored(self):
            # A deadline sample has no answer to score. It must not count against
            # the retrieval verdict, and what it had achieved is recorded on that
            # sample rather than read as a retrieval failure or an assumed zero.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                good = self.timed_body(json.dumps(expected))
                # The 192K case with the bound applied: the sample generated its full
                # 8,192-token allowance at the declared 40 tokens/s before the deadline.
                progress = deadline_observations([{"prefill_tokens": 170500, "generation_tokens": 0, "elapsed_seconds": 261.0},
                                                  {"prefill_tokens": 170500, "generation_tokens": 8192, "elapsed_seconds": 465.8}])
                outcomes = iter([(good, {}), (None, progress), (good, {})])
                runner.long_task = lambda request: next(outcomes)
                reason = runner.long_context()
                self.assertEqual(runner.obs["retrieval_samples_scored"], 2)
                self.assertEqual(runner.obs["retrieval_samples_passed"], 2)
                sample = runner.obs["retrieval_sample_results"][1]
                self.assertEqual(sample["outcome"], "unscored")
                self.assertEqual(sample["unscored_reason"], "execution_timeout")
                self.assertEqual(sample["progress_generation_tokens_per_second"], 40.0)
                self.assertTrue(sample["generation_rate_meets_declared_minimum"])
                self.assertEqual(runner.obs["limits_exceeded"], [])
                self.assertTrue(runner.obs["generation_rate_meets_declared_minimum"])
                self.assertIn("2 of 2", reason)
                self.assertIn("1 sample(s) were unscored", reason)

        def test_long_context_waits_for_the_slot_a_deadline_sample_left_busy(self):
            # The deadline path aborts the transfer, so the next sample would be
            # counted against work the server has not released. It waits, and the
            # wait and its outcome are in the sample's own record.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                waits = []
                runner.idle = lambda seconds=None: waits.append(seconds) or True
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                progress = deadline_observations([{"prefill_tokens": 115500, "generation_tokens": 26108, "elapsed_seconds": 1300.0}])
                outcomes = iter([(None, progress), (self.timed_body(json.dumps(expected)), {}), (self.timed_body(json.dumps(expected)), {})])
                runner.long_task = lambda request: next(outcomes)
                reason = runner.long_context()
                # Once between the deadline sample and its successor, once after the last.
                self.assertEqual(waits, [None, None])
                first, second, third = runner.obs["retrieval_sample_results"]
                self.assertEqual(first["outcome"], "unscored")
                self.assertTrue(first["slot_released_before_next_sample"])
                self.assertIsInstance(first["slot_release_seconds"], float)
                self.assertNotIn("slot_released_before_next_sample", second)
                self.assertNotIn("slot_released_before_next_sample", third)
                self.assertIn("2 of 2", reason)

        def test_long_context_fails_when_the_slot_never_releases(self):
            # A successor dispatched into a slot that still holds the aborted request
            # would be scored against the leftover work, so the row fails with the
            # wait in its record instead of counting it as a retrieval outcome.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.idle = lambda seconds=None: False
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                progress = deadline_observations([{"prefill_tokens": 115500, "generation_tokens": 26108, "elapsed_seconds": 1300.0}])
                outcomes = iter([(None, progress), (self.timed_body(json.dumps(expected)), {}), (self.timed_body(json.dumps(expected)), {})])
                runner.long_task = lambda request: next(outcomes)
                with self.assertRaises(Finding) as caught:
                    runner.long_context()
                self.assertIn("did not release", caught.exception.reason)
                self.assertIsInstance(caught.exception.observations["slot_release_seconds"], float)
                first = runner.obs["retrieval_sample_results"][0]
                self.assertFalse(first["slot_released_before_next_sample"])
                self.assertEqual(runner.obs["retrieval_samples_scored"], 0)

        def test_long_context_records_achieved_generation_split_per_sample(self):
            with tempfile.TemporaryDirectory() as root:
                runner, report = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                runner.long_task = lambda request: (self.timed_body(json.dumps(expected), rate=40.0), {})
                runner.long_context()
                sample = runner.obs["retrieval_sample_results"][0]
                self.assertEqual(sample["prefill_tokens"], 115500)
                self.assertEqual(sample["prefill_seconds"], 261.0)
                self.assertEqual(sample["prefill_tokens_per_second"], 442.529)
                self.assertEqual(sample["generation_tokens"], 800)
                self.assertEqual(sample["generation_seconds"], 20.0)
                self.assertEqual(sample["generation_tokens_per_second"], 40.0)
                self.assertEqual(runner.obs["declared_generation_tokens_per_second"], 20)
                self.assertTrue(runner.obs["generation_rate_meets_declared_minimum"])
                self.assertEqual(runner.obs["limits_exceeded"], [])
                report.result("128k", LONG_CHECK, "PASS", "Fixture observations.", runner.obs)
                published = (report.root / "report.md").read_text() + (report.root / "report.json").read_text()
                for name in ("prefill_tokens_per_second", "generation_seconds", "generation_tokens_per_second"):
                    self.assertIn(json.dumps(name) + ": " + json.dumps(sample[name]), published)

        def test_long_context_flags_generation_below_the_declared_minimum(self):
            # A shortfall in any sample is named, not just the last sample's rate.
            with tempfile.TemporaryDirectory() as root:
                runner, _ = self.runner(root)
                runner.obs = {}
                _, expected = schema_task()
                runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                runner.long_task = lambda request: (self.timed_body(json.dumps(expected), rate=17.0), {})
                with self.assertRaises(Finding) as caught:
                    runner.long_context()
                self.assertEqual(caught.exception.status, "FAIL")
                self.assertFalse(runner.obs["generation_rate_meets_declared_minimum"])
                self.assertEqual(runner.obs["limits_exceeded"], ["generation_tokens_per_second"])
                self.assertTrue(all(not s["generation_rate_meets_declared_minimum"] for s in runner.obs["retrieval_sample_results"]))

        def test_long_context_samples_live_slot_progress(self):
            # The cumulative prediction counters only advance when a request
            # finishes, so the deadline record reads the slot table's live counts
            # instead. An unreadable or unexpected slot answer is a missing
            # measurement, never an assumed zero.
            # The pinned build wraps next_token in a one-element array (nlohmann
            # turns `{ { {...} } }` into an array holding one object); the older
            # documented shape is the object directly. Both must read.
            wrapped = {"n_prompt_tokens_processed": 170500, "next_token": [{"n_decoded": 26108}]}
            bare = {"n_prompt_tokens_processed": 170500, "next_token": {"n_decoded": 26108}}
            cases = (
                ((200, [wrapped], {}), (170500, 26108)),
                ((200, [bare], {}), (170500, 26108)),
                ((200, [], {}), None),
                ((200, [wrapped, wrapped], {}), None),
                ((500, [wrapped], {}), None),
                ((200, [{"next_token": [{"n_decoded": 1}]}], {}), None),
                ((200, [{"n_prompt_tokens_processed": 1}], {}), None),
                ((200, [{"n_prompt_tokens_processed": 1, "next_token": []}], {}), None),
                ((200, [{"n_prompt_tokens_processed": 1, "next_token": [{"n_decoded": 1}, {"n_decoded": 2}]}], {}), None),
                ((200, [{"n_prompt_tokens_processed": 1, "next_token": [1]}], {}), None),
                ((200, [{"n_prompt_tokens_processed": 1, "next_token": [{"n_decoded": -1}]}], {}), None),
                (Finding("ERROR", "Slots endpoint did not answer."), None),
            )
            for answer, expected in cases:
                with self.subTest(answer=answer), tempfile.TemporaryDirectory() as root:
                    runner, _ = self.runner(root)
                    def request(path, timeout=None, **kwargs):
                        if isinstance(answer, Exception):
                            raise answer
                        return answer
                    runner.http = type("SlotsHTTP", (), {"request": staticmethod(request)})()
                    sample = runner.progress_sample(0.0)
                    if expected is None:
                        self.assertIsNone(sample)
                    else:
                        self.assertEqual((sample["prefill_tokens"], sample["generation_tokens"]), expected)
                        self.assertIsInstance(sample["elapsed_seconds"], float)

        def test_long_context_records_when_the_counters_cannot_be_read(self):
            # The achieved rate comes from the answer's own timings, so an
            # unreadable slot table must not turn a completed row into an ERROR;
            # on a deadline it must say the progress is unknown rather than assume
            # a zero, and a row where no sample answered is the nothing-scored FAIL.
            for deadline in (False, True):
                with self.subTest(deadline=deadline), tempfile.TemporaryDirectory() as root:
                    runner, _ = self.runner(root)
                    runner.obs = {}
                    _, expected = schema_task()
                    runner.calibrated_request = lambda low, high, **kwargs: ("request", expected, low + 500, 2)
                    body = self.timed_body(json.dumps(expected), rate=40.0)
                    unknown = deadline_observations([])
                    outcomes = iter([(None, dict(unknown))] * 3 if deadline else [(body, {})] * 3)
                    runner.long_task = lambda request: next(outcomes)
                    if deadline:
                        with self.assertRaises(Finding) as caught:
                            runner.long_context()
                        self.assertEqual(caught.exception.status, "FAIL")
                        self.assertIn("None of the 3 samples produced a scorable answer", caught.exception.reason)
                        self.assertEqual(runner.obs["retrieval_samples_scored"], 0)
                        sample = runner.obs["retrieval_sample_results"][0]
                        self.assertFalse(sample["deadline_progress_available"])
                        self.assertIn("could not be read", sample["deadline_progress_reason"])
                        self.assertFalse(runner.obs["generation_rate_evaluated"])
                        self.assertNotIn("generation_rate_meets_declared_minimum", runner.obs)
                    else:
                        runner.long_context()
                        self.assertEqual(runner.obs["retrieval_sample_results"][0]["generation_tokens_per_second"], 40.0)
                        self.assertTrue(runner.obs["generation_rate_meets_declared_minimum"])

        def test_served_context_is_checked_against_the_stored_record(self):
            with tempfile.TemporaryDirectory() as root:
                # The record declares 196608 and the server serves 131072, so the
                # server is not backing the record it was launched for.
                server = self.identity_server(root, {"supports_preserve_reasoning": True}, profile="192k")
                server.expected["CONTEXT_SIZE"] = "196608"
                with self.assertRaises(Finding) as caught:
                    server.identity()
                self.assertEqual(caught.exception.observations, {"slot": "192k", "declared_context": 196608, "served_context": 131072})
                for named in ("192k", "196608", "131072"):
                    self.assertIn(named, caught.exception.reason)

        def test_runtime_native_identity_abi(self):
            import ctypes
            with tempfile.TemporaryDirectory() as root:
                library = Path(root) / "identity.so"
                subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Werror", str(ROOT / "agents/tests/runtime_abi_fixture.c"), "-o", str(library)], check=True)
                def load(_names):
                    fd = runtime.protected_open(library, regular_data=True)
                    return ctypes.CDLL(f"/proc/self/fd/{fd}"), fd
                with patch.object(runtime, "_load_system_library", load):
                    self.assertEqual(runtime._hsa_identities(), [{"pciAddress": "0002:1a:00.3", "uuid": "GPU-0123456789abcdef"}])
                    self.assertEqual(runtime._vulkan_identities(), [{"pciAddress": "0002:1a:03.0", "uuid": "000102030405060708090a0b0c0d0e0f", "index": 0}])

        def test_runtime_selection_revalidation_and_action_constraints(self):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                agents, build = create_runtime_fixture(root / "fixture")
                (root / "home").mkdir(mode=0o700)
                env = dict(os.environ, HOME=str(root / "home"))
                script = [sys.executable, "-B", str(agents / "matuto_runtime.py")]
                command = script + ["launch", "serve", "llama-server", "7", "--", "--gpu-layers", "7", "--host", "127.0.0.1"]
                for backend in ("HIP/ROCm", "Vulkan"):
                    configure_runtime_fixture(agents, build, env, backend)
                    subprocess.run(command, env=env, check=True)
                    received = json.loads((root / "fixture/received.json").read_text())
                    identity = json.loads(received["identity"])
                    self.assertEqual(identity["backend"], backend)
                    self.assertEqual(identity["pci"], "0000:03:00.0")
                    self.assertEqual(identity["gpuLayers"], "7")
                # Reordering changes only the runtime selector, not physical identity.
                inventory_path = root / "fixture/inventory.json"
                inventory = json.loads(inventory_path.read_text())
                inventory["Vulkan"][0]["index"] = 3
                inventory_path.write_text(json.dumps(inventory))
                subprocess.run(command, env=env, check=True)
                self.assertEqual(json.loads((root / "fixture/received.json").read_text())["vulkan"], "3")
                # A refused invocation never reaches the server, so what the last
                # accepted one recorded is still exactly what is on disk.
                recorded = (root / "fixture/received.json").read_text()
                for arguments in (["--device", "none"], ["--host", "0.0.0.0"], ["--gpu-layers", "0"], ["--rpc", "untrusted"], ["--not-an-installed-option"]):
                    result = subprocess.run(command + arguments, env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual((root / "fixture/received.json").read_text(), recorded)
                result = subprocess.run(command, env=dict(env, LLAMA_ARG_DEVICE="none"), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("LLAMA_ARG_DEVICE", result.stderr)
                # An option operand that begins with a dash is data, not another
                # option; the selected runtime must receive the exact prompt value.
                configure_runtime_fixture(agents, build, env, "HIP/ROCm")
                smoke = script + ["launch", "smoke", "llama-cli", "7", "--", "-m", "model.gguf", "-ngl", "7", "-p", "--explain this text"]
                subprocess.run(smoke, env=env, check=True)
                received = json.loads((root / "fixture/received.json").read_text())["args"]
                self.assertEqual(received[received.index("-p") + 1], "--explain this text")
                # A prompt value equal to a recognized option stays a value: the
                # GPU-layer consistency check must not count it, and the real
                # device selector must still be appended at launch.
                for prompt in ("--gpu-layers", "--device"):
                    subprocess.run(script + ["launch", "smoke", "llama-cli", "7", "--", "-m", "model.gguf", "-ngl", "7", "-p", prompt], env=env, check=True)
                    received = json.loads((root / "fixture/received.json").read_text())["args"]
                    self.assertEqual(received[received.index("-p") + 1], prompt)
                    self.assertEqual(received[-2:], ["--device", "ROCm0"])
                # An auxiliary device option is validated but never substitutes
                # for the primary selector the main model must receive.
                for auxiliary in ("--device-draft", "--mmproj-device"):
                    subprocess.run(script + ["launch", "smoke", "llama-cli", "7", "--", "-m", "model.gguf", "-ngl", "7", auxiliary, "ROCm0"], env=env, check=True)
                    received = json.loads((root / "fixture/received.json").read_text())["args"]
                    self.assertEqual(received[received.index(auxiliary) + 1], "ROCm0")
                    self.assertEqual(received[-2:], ["--device", "ROCm0"])
                    refused = subprocess.run(script + ["launch", "smoke", "llama-cli", "7", "--", "-m", "model.gguf", "-ngl", "7", auxiliary, "ROCm1"], env=env, capture_output=True, text=True)
                    self.assertNotEqual(refused.returncode, 0, f"accepted {auxiliary} ROCm1")
                # A boolean switch before a conflicting selector must not swallow
                # it as an operand; the device conflict still refuses the launch.
                for arguments in (["--reasoning-preserve", "--device", "none"], ["--device", "0"], ["--device", "ROCm1"]):
                    result = subprocess.run(smoke + arguments, env=env, capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0, f"accepted {arguments}")
                positive = subprocess.run(smoke + ["--reasoning-preserve"], env=env, capture_output=True, text=True)
                self.assertEqual(positive.returncode, 0)
                configure_runtime_fixture(agents, build, env, "Vulkan")
                self.assertEqual(positive.returncode, 0)
                # Unrelated missing tools still do not prevent serving.
                (build / "llama-bench").unlink()
                (build / "llama-cli").unlink()
                subprocess.run(command, env=env, check=True)
                # In-place package replacement is re-observed at the next run.
                previous = json.loads((root / "fixture/received.json").read_text())["identity"]
                replacement = build / "replacement"
                replacement.write_bytes((build / "llama-server").read_bytes() + b"package update")
                replacement.chmod(0o700)
                replacement.replace(build / "llama-server")
                subprocess.run(command, env=env, check=True)
                current = json.loads(json.loads((root / "fixture/received.json").read_text())["identity"])
                self.assertNotEqual(current["executable"]["sha256"], json.loads(previous)["executable"]["sha256"])
                (build / "llama-server").chmod(0o777)
                self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)
                (build / "llama-server").chmod(0o700)
                for records in ([], [inventory["Vulkan"][0], inventory["Vulkan"][0]], [{**inventory["Vulkan"][0], "uuid": "f" * 32}]):
                    inventory["Vulkan"] = records
                    inventory_path.write_text(json.dumps(inventory))
                    self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0)

        def test_runtime_refuses_selection_while_workload_alive_and_recovers(self):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                agents, build = create_runtime_fixture(root / "fixture")
                (root / "home").mkdir(mode=0o700)
                env = dict(os.environ, HOME=str(root / "home"))
                configure_runtime_fixture(agents, build, env)
                (root / "fixture/hold").touch()
                script = [sys.executable, "-B", str(agents / "matuto_runtime.py")]
                process = subprocess.Popen(script + ["launch", "serve", "llama-server", "5", "--", "--gpu-layers", "5"], env=env)
                try:
                    deadline = time.monotonic() + 5
                    while not (root / "fixture/received.json").exists() and time.monotonic() < deadline and process.poll() is None:
                        time.sleep(.02)
                    self.assertIsNone(process.poll())
                    self.assertTrue((root / "fixture/received.json").exists())
                    change = script + ["select", str(build), "Vulkan", "0000:03:00.0"]
                    attempts = [subprocess.Popen(change, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(2)]
                    self.assertTrue(all(attempt.wait(timeout=5) != 0 for attempt in attempts))
                    state = json.loads(subprocess.check_output(script + ["show"], env=env))
                    self.assertEqual(state["selection"]["backend"], "HIP/ROCm")
                finally:
                    process.kill()
                    process.wait(timeout=5)
                configure_runtime_fixture(agents, build, env, "Vulkan")

        def test_runtime_interrupted_state_and_identity_destination_preserve_data(self):
            import runpy
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                agents, build = create_runtime_fixture(root / "fixture")
                (root / "home").mkdir(mode=0o700)
                with patch.dict(os.environ, {"HOME": str(root / "home")}, clear=True):
                    module = runpy.run_path(str(agents / "matuto" / "matuto_runtime.py"))["r"]
                    module.select_runtime(str(build), "HIP/ROCm", "0000:03:00.0")
                    selected = module.read_selection()
                    before = module.SELECTION_PATH.read_bytes()
                    replace = os.replace
                    def fail_commit(source, target, **options):
                        if target == "selection.json":
                            raise OSError("interrupted publication")
                        return replace(source, target, **options)
                    with patch.object(module.os, "replace", fail_commit), self.assertRaises(OSError):
                        module._write_selection(selected)
                    self.assertEqual(module.SELECTION_PATH.read_bytes(), before)
                    with self.assertRaises(module.InspectionError):
                        module.read_selection()
                    module.select_runtime(str(build), "HIP/ROCm", "0000:03:00.0")
                    self.assertEqual(module.read_selection(), selected)
                    original = root / "precious"
                    original.write_text("preserve me")
                    original.chmod(0o600)
                    destination = root / "identity.json"
                    os.link(original, destination)
                    with self.assertRaises(module.InspectionError):
                        module._record_identity(destination, selected)
                    self.assertEqual(original.read_text(), "preserve me")

        def _sleeping_driver_helper(self, root, pidfile):
            helper = root / "sleeping-driver.py"
            helper.write_text(f"import os, pathlib, time\npathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n")
            return helper

        def _driver_dead(self, child):
            stat = Path(f"/proc/{child}/stat")
            if not stat.exists():
                return True
            try:
                return stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z"
            except (OSError, IndexError):
                return False

        def test_runtime_interrupted_selection_reaps_identity_probe(self):
            # The real bounded_identity_probe owns a new-session driver child; an
            # interrupted CLI must unwind to its finally and reap that child.
            import runpy
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                agents, build = create_runtime_fixture(root / "fixture")
                (root / "home").mkdir(mode=0o700)
                env = dict(os.environ, HOME=str(root / "home"))
                pidfile = root / "driver.pid"
                helper = self._sleeping_driver_helper(root, pidfile)
                parent = subprocess.Popen([sys.executable, "-B", "-c",
                    f"import sys, runpy\nsys.path.insert(0, {str(ROOT / 'agents')!r})"
                    f"\nimport matuto_runtime as r\nr.__file__={str(helper)!r}"
                    f"\nfixture = runpy.run_path({str(agents / 'matuto' / 'matuto_runtime.py')!r})['r']"
                    f"\nfixture.bounded_identity_probe = r.bounded_identity_probe"
                    f"\nraise SystemExit(fixture.runtime_cli(['select', {str(build)!r}, 'HIP/ROCm', '0000:03:00.0']))"],
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                child = None
                try:
                    deadline = time.monotonic() + 5
                    while not pidfile.exists() and time.monotonic() < deadline:
                        time.sleep(.02)
                    if not pidfile.exists():
                        out, err = parent.communicate(timeout=2)
                        raise AssertionError(f"identity probe child never started; rc={parent.returncode} stderr={err[-800:]!r}")
                    child = int(pidfile.read_text())
                    parent.terminate()
                    self.assertEqual(parent.wait(timeout=5), 1)
                    deadline = time.monotonic() + 5
                    while not self._driver_dead(child) and time.monotonic() < deadline:
                        time.sleep(.1)
                    self.assertTrue(self._driver_dead(child), "identity probe child survived its interrupted parent")
                finally:
                    if parent.poll() is None:
                        parent.kill()
                        parent.wait()
                    if child:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child, signal.SIGKILL)

        def test_sse_requires_generated_complete_delta(self):
            self.assertEqual(sse_state(b'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'), (False, False))
            delta = b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": null}]}\r\n\r\n'
            self.assertEqual(sse_state(delta), (True, False))
            self.assertEqual(sse_state(delta[:-2]), (False, False))
            for finish in ("stop", "length", "tool_calls"):
                end = ('data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}) + '\n\n').encode()
                self.assertEqual(sse_state(delta + end), (True, True))
            self.assertEqual(sse_state(b'data: [DONE]\n\n'), (False, True))
            with self.assertRaises(Finding):
                sse_state(b'data: NOT JSON\n\n')

        def runner(self, root):
            class FakeServer:
                vram_baseline = {"baseline_settle_seconds": 0.0}
                def verify(self):
                    return {"checksum_verified": True}
                def start(self, upload_timeout=False):
                    return {"n_ctx": 131072}, .01
                def healthy(self):
                    return True
                def identity(self):
                    return {"n_ctx": 131072}
                # A synthetic runner is not a Server; the real Server/Runner handoff
                # is exercised by test_named_alternate_profile_records_its_identity.
                # Binding the production type keeps this double from masking a Server
                # that lost its serving report.
                def serving(self):
                    self.assert_serving_is_on_the_server()
                    return {"state": "unavailable", "reason": "synthetic runner"}
                @staticmethod
                def assert_serving_is_on_the_server():
                    assert callable(getattr(Server, "serving", None)), "production Server has no serving report"
            class FakeHTTP:
                def close(self):
                    pass
            report = Reporter(Path(root) / "report", ["128k"], DEFAULTS, "test role")
            runner = Runner("128k", FakeServer(), FakeHTTP(), report, DEFAULTS)
            runner.idle = lambda seconds=None: True
            runner.baseline = {"n_ctx": 131072}
            return runner, report

        def identity_server(self, root, caps, build="0.4.0-dev (build 10917, commit de699957b)", profile="128k"):
            # Server.__init__ reads a real profile and starts nothing, so build the
            # fields identity() actually reads and point the expected binary at this
            # interpreter, which /proc/<pid>/exe already resolves to.
            root = Path(root)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "llama-server").symlink_to(sys.executable)
            model = root / "model.gguf"
            model.write_text("")
            server = Server.__new__(Server)
            server.profile = profile
            server.proc = type("P", (), {"pid": os.getpid(), "poll": lambda self: None})()
            server.expected = {"INSTALLATION": str(bindir), "MODEL_PATH": str(model),
                               "BACKEND_VERSION": "de699957b92f490efebad149665b0dccf127eaff",
                               "CONTEXT_SIZE": "131072", "MODEL_SHA256": "abc"}
            props = {"model_path": str(model), "build_info": build,
                     "chat_template": "template", "total_slots": 1, "chat_template_caps": caps}
            server.http = type("H", (), {"request": lambda self, path, *a, **k:
                                         (200, props if path == "/props" else [{"n_ctx": 131072}], None)})()
            return server

        def test_identity_requires_the_preserve_reasoning_capability(self):
            with tempfile.TemporaryDirectory() as root:
                good = self.identity_server(root, {"supports_preserve_reasoning": True})
                self.assertIs(good.identity()["preserve_capability"], True)
            for caps in ({"supports_preserve_reasoning": False}, {}):
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaises(Finding):
                        self.identity_server(root, caps).identity()

        def test_live_identity_records_the_build_it_saw_without_gating_on_it(self):
            with tempfile.TemporaryDirectory() as root:
                server = self.identity_server(root, {"supports_preserve_reasoning": True}, build="b10917-8ea290247")
                server.expected["BACKEND_VERSION"] = "de699957b92f490efebad149665b0dccf127eaff"
                observed = server.identity()
                self.assertEqual(observed["n_ctx"], 131072)
                self.assertEqual(observed["observed_build_revision"], "8ea290247")

        def test_scored_tasks_use_execution_not_performance_deadline(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                req, expected = schema_task()
                body = self.body(json.dumps(expected))
                body["usage"] = {"prompt_tokens": 100, "completion_tokens": 3000}
                body["__verbose"] = {"truncated": False}
                def request(path, payload, timeout):
                    self.assertEqual(timeout, DEFAULTS["long_execution_seconds"])
                    self.assertEqual(payload["max_tokens"], -1)
                    return 200, body, {"total_seconds": 100}
                runner.http.request = request
                answer(runner.task(req), expected)
                self.assertEqual(runner.obs["completion_tokens"], 3000)
                self.assertEqual(runner.obs["total_seconds"], 100)

        def test_generation_boundary_requires_actual_context_exhaustion(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                n_ctx = runner.baseline["n_ctx"]
                recovered = []
                runner.recovery = lambda: recovered.append(True)
                for exhausted, generated, status in ((False, 128, "FAIL"), (True, 10, "FAIL"), (True, 128, "PASS")):
                    def request(path, payload, timeout):
                        count = payload["messages"][0]["content"].count(" filler") + 100
                        if path.endswith("input_tokens"):
                            return 200, {"input_tokens": count}, {}
                        if count > n_ctx:
                            return 400, {"error": {"type": "exceed_context_size_error", "n_ctx": n_ctx, "n_prompt_tokens": count}}, {}
                        self.assertEqual(timeout, DEFAULTS["long_execution_seconds"])
                        self.assertEqual(payload["max_tokens"], -1)
                        self.assertTrue(payload["ignore_eos"])
                        self.assertNotIn("response_format", payload)
                        body = self.body("private synthetic output", "length")
                        body.update(usage={"prompt_tokens": count, "completion_tokens": generated}, __verbose={"truncated": exhausted})
                        return 200, body, {}
                    runner.http.request = request
                    runner.execute("WQ-10")
                    self.assertEqual(report.get("128k", "WQ-10")["status"], status)
                self.assertEqual(recovered, [True])
                self.assertEqual(runner.obs["generation_boundary"]["available_context_tokens"], 128)

        def test_continues_and_skips_only_dependencies(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                visited = []
                def fail_tool():
                    raise Finding("FAIL", "Synthetic tool mismatch.", matched=False)
                runner.wq01 = fail_tool
                for ident in CASES:
                    if ident != "WQ-01":
                        setattr(runner, ident.lower().replace("-", ""), lambda ident=ident: visited.append(ident))
                runner.run()
                self.assertEqual(report.get("128k", "WQ-01")["status"], "FAIL")
                self.assertEqual(report.get("128k", "WQ-03")["status"], "SKIP")
                self.assertEqual(report.get("128k", "WQ-07")["status"], "SKIP")
                self.assertIn("WQ-03H", visited)
                self.assertIn("WQ-02", visited)
                self.assertIn("WQ-11", visited)

        def test_untrusted_failure_is_not_published(self):
            secret = 'PRIVATE_SECRET /tmp/private http://private.invalid/?cap=secret | injected\nrow'
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()) as console:
                runner, report = self.runner(root)
                def bad_finish():
                    message(self.body("", secret))
                runner.wq02 = bad_finish
                runner.execute("WQ-02")
                self.assertEqual(report.get("128k", "WQ-02")["status"], "FAIL")
                def bad_exception():
                    raise ValueError(secret)
                # The long-context row is dispatched by name rather than through
                # a wqNN method.
                runner.long_context = bad_exception
                runner.execute(LONG_CHECK)
                public = console.getvalue() + (report.root / "report.md").read_text() + (report.root / "report.json").read_text()
                for fragment in ("PRIVATE_SECRET", "/tmp/private", "private.invalid", "injected"):
                    self.assertNotIn(fragment, public)
                self.assertEqual(report.get("128k", LONG_CHECK)["status"], "ERROR")
                data = json.loads((report.root / "report.json").read_text())
                self.assertEqual(data["owner_decision"]["status"], "pending")
                self.assertEqual((report.root / "report.json").stat().st_mode & 0o777, 0o600)

        def test_template_retention_inspects_body(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                class TemplateHTTP:
                    def __init__(self, retain):
                        self.retain = retain
                    def close(self):
                        pass
                    def request(self, path, request, timeout):
                        self_path = path
                        if self_path != "/apply-template":
                            raise AssertionError("wrong endpoint")
                        text = ""
                        if self.retain:
                            text = " ".join(m.get("reasoning_content", "") + (m.get("content") or "") for m in request["messages"])
                        return 200, {"prompt": text}, {}
                runner.http = TemplateHTTP(False)
                runner.execute("WQ-03H")
                self.assertEqual(report.get("128k", "WQ-03H")["status"], "FAIL")
                runner.http = TemplateHTTP(True)
                runner.execute("WQ-03H")
                self.assertEqual(report.get("128k", "WQ-03H")["status"], "PASS")

        def test_result_forks_preserve_call_and_change_expected_state(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                msg, call = tool_call(self.call_body(), 18, ("title", "state"))
                runner.last_tool = lookup_task(), msg, call
                one, expected_one = runner.followup("open")
                two, expected_two = runner.followup("closed")
                self.assertEqual(one["messages"][-3], msg)
                self.assertEqual(one["max_tokens"], -1)
                self.assertEqual(two["max_tokens"], -1)
                self.assertEqual(two["messages"][-2]["tool_call_id"], call["id"])
                self.assertNotEqual(expected_one, expected_two)
                with self.assertRaises(Finding):
                    answer(self.body(json.dumps(expected_one)), expected_two)
                answer(self.body(json.dumps(expected_two)), expected_two)

        def test_tool_result_requests_keep_tools_without_schema(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                msg, call = tool_call(self.call_body(), 18, ("title", "state"))
                original = lookup_task()
                runner.last_tool = original, msg, call
                answered_states = []
                def request(path, payload, timeout):
                    if "tools" not in payload:  # Ordinary recovery keeps its schema.
                        self.assertIn("response_format", payload)
                        return 200, self.body(json.dumps(schema_task()[1])), {}
                    self.assertEqual(payload["tools"], original["tools"])
                    self.assertEqual(payload["tool_choice"], "auto")
                    self.assertNotIn("response_format", payload)
                    result = next(m for m in reversed(payload["messages"]) if m["role"] == "tool")
                    if result["content"] == "not valid JSON for lookup_ticket":
                        return 200, self.call_body(), {}
                    state = json.loads(result["content"])["state"]
                    answered_states.append(state)
                    return 200, self.body(json.dumps({"ticket": 18, "state": state})), {}
                runner.http.request = request
                runner.wq03()
                runner.wq07()
                self.assertCountEqual(answered_states, ["open", "closed", "closed"])
                self.assertTrue(runner.obs["retry_call_valid"])
                self.assertTrue(runner.obs["corrected_result_matches"])

        def test_metrics_are_text_and_require_both_gauges(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                def request(path, **kwargs):
                    self.assertTrue(kwargs["text"])
                    return 200, 'llamacpp:requests_processing 1\nllamacpp:requests_deferred 1\n', {}
                runner.http.request = request
                self.assertEqual(runner.metrics(), {"processing": 1, "deferred": 1})
                runner.http.request = lambda *a, **k: (200, 'llamacpp:requests_processing 0\n', {})
                with self.assertRaises(Finding):
                    runner.metrics()

        def test_owned_process_cleanup_does_not_kill_other_process(self):
            other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            own = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
            try:
                server = object.__new__(Server)
                server.proc = own
                server.stop()
                self.assertIsNotNone(own.poll())
                self.assertIsNone(other.poll())
                server.stop()
            finally:
                for proc in (other, own):
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=3)

        def test_other_servers_reads_argv0_not_comm(self):
            def proc_tree(root, entries):
                proc = Path(root) / "proc"
                proc.mkdir()
                for name, comm, cmdline in entries:
                    entry = proc / name
                    entry.mkdir()
                    (entry / "comm").write_text(comm + "\n")
                    (entry / "cmdline").write_bytes(cmdline)
                return proc

            # A server launched through the runtime's descriptor path: the
            # kernel takes comm from the descriptor, and argv[0] is the bare
            # tool name. A directly executed server can carry the name with any
            # comm. Neither may be missed.
            running = [
                ("101", "7", b"llama-server\0--host\000127.0.0.1\0"),
                ("102", "bash", b"/opt/llama.cpp/build/bin/llama-server\0--host\000127.0.0.1\0"),
            ]
            # A zombie keeps its comm and has no cmdline; an unrelated process
            # has neither and holds no GPU. Refusing for either blocks a run
            # that should proceed, so both must read as absent.
            idle = [
                ("201", "llama-server", b""),
                ("202", "python3", b"/usr/bin/python3\0-c\0pass\0"),
            ]

            for entries, expected in ((running, True), (idle, False)):
                for entry in entries:
                    with tempfile.TemporaryDirectory() as root:
                        self.assertEqual(other_servers(proc_tree(root, [entry])), expected, entry[0])
                with tempfile.TemporaryDirectory() as root:
                    self.assertEqual(other_servers(proc_tree(root, entries)), expected)

        @contextlib.contextmanager
        def telemetry_server(self, root, files=None):
            root = Path(root)
            drm = root / "drm"
            device = root / "0000:03:00.0"
            device.mkdir()
            (device / "vendor").write_text("0x1002\n")
            used = device / "mem_info_vram_used"
            used.write_text("5000000000\n")
            for node in ("card1", "renderD128"):
                (drm / node).mkdir(parents=True)
                (drm / node / "device").symlink_to(device)
            proc = root / "proc"
            (proc / "fd").mkdir(parents=True)
            (proc / "fd/3").symlink_to("/dev/dri/renderD128")
            (proc / "status").write_text("VmRSS:\t8192 kB\n")
            model = root / "models/server.gguf"
            model.parent.mkdir()
            model.write_bytes(b"weights")
            with self.pinned(self.stored_spec(root, model, files=files)):
                server = Server(root / "server", "128k", 0, DEFAULTS, None, gpu_card="card1")
            server.runtime_identity = {"pci": device.name}
            server.acquire_runtime = lambda: None
            server.stop = lambda: setattr(server, "proc", None)
            server.healthy = lambda timeout=2: True
            server.identity = lambda: {"n_ctx": 131072}
            path_type = Path
            def path(value):
                return path_type(str(value).replace("/sys/class/drm", str(drm)).replace("/proc/4242", str(proc)))
            def launch(*args, **kwargs):
                used.write_text("28000000000\n")
                return type("P", (), {"pid": 4242, "poll": lambda self: None})()
            with patch(__name__ + ".Path", side_effect=path), patch("subprocess.Popen", side_effect=launch):
                yield server, used

        def test_baseline_is_pre_start_refreshed_and_requires_valid_telemetry(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                with self.telemetry_server(root) as (server, used):
                    runner.server = server
                    server.verify = lambda: {}
                    server.serving = lambda: {}
                    for ident in CASES:
                        setattr(runner, ident.lower().replace("-", ""), lambda: None)
                    runner.run()
                    self.assertEqual(report.get("128k", "SETUP")["status"], "PASS")
                    self.assertEqual(report.get("128k", "SETUP")["observations"]["pre_start_vram_bytes"], 5_000_000_000)
                    for baseline, upload_timeout in ((6_000_000_000, True), (0, False)):
                        used.write_text(str(baseline))
                        server.start(upload_timeout=upload_timeout)
                        self.assertEqual(server.vram_baseline["pre_start_vram_bytes"], baseline)
                    for invalid in (None, "", "-1", "not a counter", "1.5", "\udcff"):
                        if invalid is None:
                            used.unlink()
                        else:
                            used.write_bytes(invalid.encode("utf-8", "surrogateescape"))
                        with self.assertRaises(Finding) as caught:
                            server.start()
                        self.assertEqual(caught.exception.status, "ERROR")
                        self.assertIsNone(server.proc)
                        self.assertEqual(server.vram_baseline, {})
                    used.write_text("5000000000")
                    for card, pci, vendor in (("card0", "0000:03:00.0", "0x1002"), ("card1", "0000:04:00.0", "0x1002"), ("card1", "0000:03:00.0", "0x10de")):
                        server.gpu_card = card
                        server.runtime_identity["pci"] = pci
                        (used.parent / "vendor").write_text(vendor)
                        with self.assertRaises(Finding) as caught:
                            server.start()
                        self.assertEqual(caught.exception.status, "ERROR")

        def test_restart_baseline_waits_for_the_stopped_server_to_release_memory(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                _, report = self.runner(root)
                with self.telemetry_server(root) as (server, used):
                    used.write_text("1902395000")
                    server.start()
                    self.assertEqual(server.vram_baseline["pre_start_vram_bytes"], 1_902_395_000)
                    self.assertEqual(server.vram_baseline["baseline_settled"], "no_previous_server_allocation_to_release")
                    self.assertEqual(server.vram_baseline["baseline_settle_seconds"], 0.0)
                    # The counter still reports the dead server for the first
                    # reads after the stop, as the WQ-06 row of the 2026-09-18
                    # 192K run recorded it. Each wait hands back the next value.
                    falling = iter(["28013117000"] * 3)
                    used.write_text("28013117000")
                    with patch("time.sleep", lambda seconds: used.write_text(next(falling, "1902395000"))):
                        server.start()
                    self.assertEqual(server.vram_baseline["pre_start_vram_bytes"], 1_902_395_000)
                    self.assertEqual(server.vram_baseline["baseline_settled"], "previous_server_allocation_released")
                    report.result("128k", "WQ-06", "PASS", "Fixture observations.", {**server.vram_baseline, **vram_fields("sampled_peak_vram", 28_181_295_000)})
                    row = next(r["observations"] for r in json.loads((report.root / "report.json").read_text())["results"] if r["id"] == "WQ-06")
                    self.assertEqual(row["baseline_settled"], "previous_server_allocation_released")
                    self.assertIn('"baseline_settled": "previous_server_allocation_released"', (report.root / "report.md").read_text())
                    # The share the row would publish is the loaded model, not
                    # the 168 MB the stale baseline produced.
                    self.assertEqual(row["sampled_peak_vram_bytes"] - row["pre_start_vram_bytes"], 26_278_900_000)
                    used.write_text("28013117000")
                    with patch(__name__ + ".VRAM_RELEASE_LIMIT_SECONDS", .01), self.assertRaises(Finding) as caught:
                        server.start()
                    self.assertEqual(caught.exception.status, "ERROR")
                    self.assertEqual(caught.exception.observations["first_observed_vram_bytes"], 28_013_117_000)
                    self.assertEqual(caught.exception.observations["last_observed_vram_bytes"], 28_013_117_000)
                    self.assertEqual(caught.exception.observations["expected_vram_bytes_at_most"], 3_902_395_000)
                    self.assertGreaterEqual(caught.exception.observations["telemetry_reads"], 2)
                    self.assertEqual(server.vram_baseline, {})
                    self.assertIsNone(server.proc)
                    # Refusing does not forget the debt: the next start on the
                    # same stale counter refuses again instead of recording it.
                    with patch(__name__ + ".VRAM_RELEASE_LIMIT_SECONDS", .01), self.assertRaises(Finding) as again:
                        server.start()
                    self.assertEqual(again.exception.observations["expected_vram_bytes_at_most"], 3_902_395_000)
                    self.assertEqual(server.vram_baseline, {})

        def test_contention_accepts_load_length_and_checks_first_byte(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()), contextlib.ExitStack() as stack:
                runner, report = self.runner(root)
                server, used = stack.enter_context(self.telemetry_server(root))
                server.start()
                runner.server = server
                runner.active = lambda transfer: None
                class DoneProc:
                    def poll(self):
                        return 0
                class Reply:
                    proc = DoneProc()
                    code, first, total = 200, 1, 20
                    def __init__(self, body):
                        self.body = body
                    def result(self):
                        return self
                    def json(self):
                        return self.body
                _, expected = schema_task()
                load = Reply(self.body("synthetic stress", "length"))
                task = Reply(self.body(json.dumps(expected)))
                for reply in (load, task):
                    reply.body["timings"] = {"predicted_per_second": 25.0, "predicted_n": 512}
                def exercise(first):
                    task.first = first
                    replies = iter((load, task))
                    gauges = iter(({"processing": 1, "deferred": 1}, {"processing": 0, "deferred": 0}))
                    runner.http.start = lambda *a: next(replies)
                    runner.metrics = lambda: next(gauges)
                    runner.wq06()
                exercise(1)
                self.assertTrue(runner.obs["deferred_observed"])
                report.result("128k", "WQ-06", "PASS", "Fixture observations.", runner.obs)
                data = json.loads((report.root / "report.json").read_text())
                observed = next(row["observations"] for row in data["results"] if row["id"] == "WQ-06")
                for prefix, values in (("pre_start_vram", (5_000_000_000, 5000, 4768.372)), ("sampled_peak_vram", (28_000_000_000, 28000, 26702.881)), ("derived_server_share_vram", (23_000_000_000, 23000, 21934.509))):
                    for unit, value in zip(("bytes", "decimal_mb", "mib"), values):
                        self.assertEqual(observed[prefix + "_" + unit], value)
                        self.assertIn(json.dumps(prefix + "_" + unit) + ": " + json.dumps(observed[prefix + "_" + unit]), (report.root / "report.md").read_text())
                self.assertEqual(Path(observed["vram_device"]).resolve(), used)
                self.assertEqual(observed["vram_pci"], "0000:03:00.0")
                self.assertNotEqual(observed["baseline_attribution"], observed["peak_attribution"])
                used.write_text("31000000000")
                with self.assertRaises(Finding):
                    exercise(1)
                self.assertEqual(runner.obs["derived_server_share_vram_bytes"], 26_000_000_000)
                self.assertIn("vram_decimal_mb", runner.obs["limits_exceeded"])
                used.write_text("4000000000")
                exercise(1)
                self.assertEqual(runner.obs["derived_server_share_vram_bytes"], -1_000_000_000)
                used.write_text("28000000000")
                runner.server.vram_device = used.parent / "different-device"
                with self.assertRaises(Finding) as caught:
                    exercise(1)
                self.assertEqual(caught.exception.status, "ERROR")
                runner.server.vram_device = used
                fd = used.parent.parent / "proc/fd/3"
                fd.unlink()
                with self.assertRaises(Finding) as caught:
                    exercise(1)
                self.assertEqual(caught.exception.status, "ERROR")
                fd.symlink_to("/dev/dri/renderD128")
                with self.assertRaises(Finding):
                    exercise(40)
                self.assertIn("task.http_first_byte_seconds", runner.obs["limits_exceeded"])
                task.body["choices"][0]["finish_reason"] = "length"
                task.body["__verbose"] = {"truncated": True}
                task.body["usage"] = {"prompt_tokens": 100, "completion_tokens": 130972}
                with self.assertRaises(Finding) as caught:
                    exercise(1)
                self.assertEqual(caught.exception.observations["failure_kind"], "context_exhausted")
                self.assertEqual(runner.obs["sampled_peak_vram_bytes"], 28_000_000_000)
                self.assertEqual(runner.obs["results"]["task"]["completion_tokens"], 130972)
                self.assertTrue(runner.obs["final_queue_idle"])

        def test_client_timeout_is_not_server_timeout_pass(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                runner.baseline = {"n_ctx": 131072}
                runner.server.port = 8080
                class OpenSocket:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def sendall(self, data):
                        pass
                    def settimeout(self, seconds):
                        pass
                    def recv(self, size):
                        raise socket.timeout()
                with patch("socket.create_connection", return_value=OpenSocket()), self.assertRaises(Finding) as caught:
                    runner.wq08()
                self.assertEqual(caught.exception.status, "FAIL")

        def test_upload_status_survives_successful_recovery_report(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                runner.server.port = 8080
                _, expected = schema_task()
                runner.http.request = lambda *a, **k: (200, self.body(json.dumps(expected)), {})
                with patch("socket.create_connection") as connect, patch("time.monotonic", side_effect=(0, 2)):
                    connect.return_value.__enter__.return_value.recv.return_value = b"HTTP/1.1 408 Request Timeout\r\n\r\n"
                    runner.execute("WQ-08")
                data = json.loads((report.root / "report.json").read_text())
                row = next(r for r in data["results"] if r["id"] == "WQ-08")
                self.assertEqual(row["status"], "PASS")
                self.assertEqual(row["observations"]["upload_http_status"], 408)
                self.assertEqual(row["observations"]["http_status"], 200)
                self.assertTrue(row["observations"]["recovery_task_passed"])
                self.assertIn('"upload_http_status": 408', (report.root / "report.md").read_text())

        def test_upload_status_survives_failing_profile_restore(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                runner.server.port = 8080
                runner.server.start = lambda upload_timeout=False: ({"n_ctx": 4096}, .01)
                class OpenSocket:
                    def __enter__(self):
                        return self
                    def __exit__(self, *args):
                        pass
                    def sendall(self, data):
                        pass
                    def settimeout(self, seconds):
                        pass
                    def recv(self, size):
                        raise socket.timeout()
                with patch("socket.create_connection", return_value=OpenSocket()):
                    runner.execute("WQ-08")
                data = json.loads((report.root / "report.json").read_text())
                row = next(r for r in data["results"] if r["id"] == "WQ-08")
                self.assertEqual(row["observations"]["server_timeout_seconds"], 2)
                self.assertEqual(row["observations"]["observation_deadline_seconds"], 4)

        def test_completed_stream_does_not_prove_cancellation(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                class Finished:
                    def poll(self):
                        return 0
                class TransferStub:
                    proc = Finished()
                    def snapshot(self):
                        return b'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
                runner.http.start = lambda *a: TransferStub()
                with self.assertRaises(Finding) as caught:
                    runner.wq11()
                self.assertEqual(caught.exception.status, "ERROR")

        def test_changed_restart_identity_is_not_accepted(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, _ = self.runner(root)
                runner.baseline = {"n_ctx": 131072, "build_info_sha256": "old"}
                class Running:
                    pid = 123
                    def poll(self):
                        return None
                class Interrupted:
                    proc = Running()
                    def result(self):
                        raise Finding("FAIL", "Synthetic interrupted transfer.")
                    def close(self):
                        pass
                runner.server.proc = Running()
                runner.server.stop = lambda **kw: None
                runner.server.start = lambda: ({"n_ctx": 131072, "build_info_sha256": "changed"}, .01)
                runner.http.start = lambda *a: Interrupted()
                runner.active = lambda transfer: None
                with self.assertRaises(Finding) as caught:
                    runner.wq09()
                self.assertEqual(caught.exception.status, "FAIL")
                self.assertFalse(runner.obs["identity_matches"])

        def test_a_crash_restart_row_records_how_long_the_device_took(self):
            # WQ-09 is the only row the owner reads for crash recovery, and the
            # wait for roughly 26 GB to come back is the part of the downtime
            # readiness cannot see.
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                class Crashed:
                    pid = 123
                    def poll(self):
                        return None
                class Restarted(Crashed):
                    pid = 456
                class Interrupted:
                    proc = Crashed()
                    def result(self):
                        raise Finding("FAIL", "Synthetic interrupted transfer.")
                    def close(self):
                        pass
                def restart():
                    runner.server.proc = Restarted()
                    runner.server.vram_baseline = {"baseline_settle_seconds": 7.5}
                    return runner.baseline, .01
                runner.server.proc = Crashed()
                runner.server.stop = lambda **kw: None
                runner.server.start = restart
                runner.http.start = lambda *a: Interrupted()
                runner.active = lambda transfer: None
                runner.recovery = lambda: None
                runner.execute("WQ-09")
                row = report.get("128k", "WQ-09")
                self.assertEqual(row["status"], "PASS")
                self.assertTrue(row["observations"]["new_process"])
                self.assertEqual(row["observations"]["restart_readiness_seconds"], .01)
                self.assertEqual(row["observations"]["baseline_settle_seconds"], 7.5)

        def test_healthy_changed_identity_never_dispatches_next_case(self):
            with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
                runner, report = self.runner(root)
                runner.server.identity = lambda: {"n_ctx": 131072, "template_sha256": "changed"}
                runner.server.start = lambda: (runner.server.identity(), .01)
                dispatched = []
                runner.wq10 = lambda: dispatched.append(True)
                runner.execute("WQ-10")
                self.assertEqual(dispatched, [])
                self.assertEqual(report.get("128k", "WQ-10")["status"], "ERROR")
                def restored():
                    runner.server.vram_baseline = {"baseline_settle_seconds": 3.25}
                    return runner.baseline, .01
                runner.server.start = restored
                runner.execute("WQ-10")
                self.assertEqual(dispatched, [True])
                self.assertEqual(report.get("128k", "WQ-10")["observations"]["baseline_settle_seconds"], 3.25)

        def test_sigterm_preserves_report_and_stops_owned_server(self):
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                script = root / "signal-probe.py"
                pid_file = root / "owned-pid"
                output = root / "report"
                script.write_text('''import json, os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import wq
os.environ["MATUTO_WQ_LAUNCH_SPEC"] = json.dumps({"record": {"kind": "language", "id": "128k", "context_size": 131072, "vram_ceiling_bytes": 24000000000}})
class Server:
    stop = wq.Server.stop
    close = wq.Server.stop
    def __init__(self, *args):
        self.vram_owed = None
        self.proc = None
    def verify(self):
        self.proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        pathlib.Path(sys.argv[2]).write_text(str(self.proc.pid))
        time.sleep(60)
wq.Server = Server
raise SystemExit(wq.main(['run', '--profile', '128k', '--run-dir', sys.argv[3]]))
''')
                child = subprocess.Popen([sys.executable, "-B", str(script), str(ROOT), str(pid_file), str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                owned_pid = None
                try:
                    end = time.monotonic() + 5
                    while not pid_file.exists() and time.monotonic() < end and child.poll() is None:
                        time.sleep(.02)
                    self.assertTrue(pid_file.exists())
                    owned_pid = int(pid_file.read_text())
                    child.terminate()
                    self.assertEqual(child.wait(timeout=5), 1)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(owned_pid, 0)
                    data = json.loads((output / "report.json").read_text())
                    self.assertTrue(data["interrupted"])
                    self.assertEqual(data["interruption_signal"], "SIGTERM")
                    self.assertFalse(list(output.glob("private-*")))
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=3)
                    if owned_pid:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(owned_pid, signal.SIGKILL)

        def test_output_rejects_repository_and_occupied_port(self):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["run", "--run-dir", str(ROOT / "wq-private-must-not-exist")]), 2)
            self.assertFalse((ROOT / "wq-private-must-not-exist").exists())
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                sock.listen()
                with self.assertRaises(Finding) as caught:
                    free_port(sock.getsockname()[1])
                self.assertEqual(caught.exception.status, "ERROR")

        def test_requested_argument_reads_the_operand_aware_option_view(self):
            help_text = "-p, --prompt PROMPT\n-c, --ctx-size N\n-m, --model FNAME\n--n-predict N\n"
            # A prompt value that looks like an option is an operand, so the later
            # real option still supplies the value the launch will run with.
            parsed = runtime._parsed_arguments(["-p", "--ctx-size=777", "-m", "/models/x.gguf", "--n-predict", "8", "-c", "131072"], help_text)
            self.assertEqual(runtime.requested_argument(parsed, "--ctx-size"), "131072")
            self.assertEqual(runtime.requested_argument(parsed, "--model"), "/models/x.gguf")
            # A repeated scalar option is applied in order, so the last one wins.
            repeated = runtime._parsed_arguments(["-c", "4096", "-c", "131072"], help_text)
            self.assertEqual(runtime.requested_argument(repeated, "--ctx-size"), "131072")
            self.assertEqual(runtime.requested_argument(runtime._parsed_arguments(["--ctx-size"], help_text), "--ctx-size"), "unavailable")
            self.assertEqual(runtime.requested_argument([], "--ctx-size"), "unavailable")
            self.assertEqual(runtime.requested_argument(runtime._parsed_arguments(["--ctx-size=131072"], help_text), "--ctx-size"), "131072")

        def test_report_labels_discovery_execution_and_configuration_association(self):
            with tempfile.TemporaryDirectory() as root:
                report = Reporter(Path(root) / "report", ["128k"], DEFAULTS, "test role")
                rows = {(row["profile"], row["id"]): row for row in report.data["results"]}
                self.assertEqual(rows[("128k", "SETUP")]["kind"], "discovery")
                self.assertEqual(rows[("128k", "WQ-01")]["kind"], "execution")
                self.assertIsInstance(report.data.get("evidence_boundary"), str)
                retained = {"backend": "HIP/ROCm", "pci": "0000:03:00.0", "uuid": "GPU-1234567890abcdef",
                            "executable": {"sha256": "a" * 64},
                            "host": {"kernel": "k1", "os_id": "fedora"}, "driver": {"amdgpu_driver": "6.8", "rocm_runtime": "6.2"},
                            "request": {"model": "/models/x.gguf", "model_sha256": "c" * 64, "context": "131072"}, "offload": {"requested_gpu_layers": "99"}}
                self.assertEqual(runtime.configuration_comparison(retained, copy.deepcopy(retained))["state"], "current")
                changed = copy.deepcopy(retained)
                changed["executable"]["sha256"] = "b" * 64
                comparison = runtime.configuration_comparison(retained, changed)
                self.assertEqual(comparison["state"], "changed")
                self.assertEqual(comparison["changed"], ["executable.sha256"])
                # The workload request is material to a single-model check but not to a router.
                workload = copy.deepcopy(retained)
                workload["request"]["context"] = "98304"
                workload["request"]["model_sha256"] = "d" * 64
                workload["offload"]["requested_gpu_layers"] = "0"
                self.assertEqual(runtime.configuration_comparison(retained, workload)["changed"], ["request.model_sha256", "request.context", "offload.requested_gpu_layers"])
                self.assertEqual(runtime.configuration_comparison(retained, workload, runtime.RUNTIME_INSTALLATION_FIELDS)["state"], "current")
                unknown = copy.deepcopy(retained)
                del unknown["driver"]
                self.assertEqual(runtime.configuration_comparison(retained, unknown)["state"], "unknown")
                self.assertEqual(runtime.configuration_comparison(retained, unknown)["unknown"], ["driver.amdgpu_driver", "driver.rocm_runtime"])
                unobserved = copy.deepcopy(retained)
                unobserved["driver"] = {"amdgpu_driver": "unavailable", "rocm_runtime": "unavailable"}
                self.assertEqual(runtime.configuration_comparison(retained, unobserved)["unknown"], ["driver.amdgpu_driver", "driver.rocm_runtime"])
                mixed = copy.deepcopy(retained)
                mixed["executable"]["sha256"] = "b" * 64
                del mixed["driver"]
                self.assertEqual(runtime.configuration_comparison(retained, mixed)["state"], "changed")
                self.assertEqual(runtime.configuration_comparison(None, retained)["state"], "unknown")

        def test_report_directory_never_overwritten(self):
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / "existing"
                path.mkdir()
                marker = path / "keep"
                marker.write_text("original")
                with self.assertRaises(FileExistsError):
                    Reporter(path, ["128k"], DEFAULTS, "test")
                self.assertEqual(marker.read_text(), "original")

        def test_invalid_limits_profile_role(self):
            for argv in (["run", "--profile", "invalid"], ["run", "--short-total-seconds", "nan"], ["run", "--restart-seconds", "0"], ["run", "--role", "/tmp/private"]):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse_args(argv)
            for overrides in ({"short_total_seconds": 0}, {"first_byte_seconds": math.inf},
                              {"retrieval_samples": 1.5}, {"unknown": 1}):
                with self.assertRaises(ValueError):
                    validate_limits(overrides)

        def test_setup_failure_is_reported_and_stops_the_run(self):
            class BadServer:
                stopped = 0
                def __init__(self, *args):
                    pass
                def verify(self):
                    raise Finding("ERROR", "Synthetic missing model.")
                def stop(self):
                    BadServer.stopped += 1
                close = stop
            with tempfile.TemporaryDirectory() as root, patch(__name__ + ".Server", BadServer), contextlib.redirect_stdout(io.StringIO()):
                output = Path(root) / "reports"
                spec = {"record": {"kind": "language", "id": "128k", "context_size": 131072, "vram_ceiling_bytes": 24_000_000_000}}
                with patch.dict(os.environ, {"MATUTO_WQ_LAUNCH_SPEC": json.dumps(spec)}):
                    code = main(["run", "--run-dir", str(output)])
                self.assertEqual(code, 1)
                data = json.loads((output / "report.json").read_text())
                self.assertEqual({r["profile"] for r in data["results"]}, set(SLOTS))
                self.assertEqual(sum(r["status"] == "ERROR" for r in data["results"]), len(SLOTS))
                self.assertEqual(BadServer.stopped, len(SLOTS))
                self.assertFalse(list(output.glob("private-*")))

        def test_transport_text_status_and_absolute_timeout(self):
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            import threading
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    if self.path == "/slow":
                        time.sleep(.5)
                    self.send_response(500 if self.path == "/error" else 200)
                    self.end_headers()
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.wfile.write(b'plain metrics' if self.path == "/metrics" else b'{"ok":true}')
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as root:
                    transport = Transport(root, server.server_port)
                    try:
                        self.assertEqual(transport.request("/metrics", text=True)[1], "plain metrics")
                        self.assertEqual(transport.request("/error")[0], 500)
                        started = time.monotonic()
                        with self.assertRaises(Finding) as caught:
                            transport.request("/slow", timeout=.15)
                        self.assertEqual(caught.exception.observations["failure_kind"], "execution_timeout")
                        self.assertFalse(caught.exception.observations["correctness_evaluated"])
                        self.assertLess(time.monotonic() - started, 2)
                        self.assertEqual(transport.clients, [])
                    finally:
                        transport.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, Finding, ValueError):
        print("WQ could not create or complete its report. Check output-directory permissions and run prerequisites; no private exception text is printed.", file=sys.stderr)
        sys.exit(2)
