#!/usr/bin/env python3
"""The measured phases of `matuto-lab <profile> bench`.

The pinned 4,096/512 llama-bench invocation runs here first, in the dimensions
and artifacts it has always had. Two phases follow it for the numbers it cannot
express: how long a fresh owned server takes to become able to serve a request,
and generation throughput at a shallow prefill depth and at a depth that nearly
fills the profile's declared context.

One invocation owns the whole run: it validates the plan, holds one shared
runtime lease from before the selection is read until the last owned process is
reaped, refuses a GPU another server already holds, and then runs the pinned row
and both new phases in sequence. Byte-valued VRAM for the selected physical GPU
is sampled across all three; a reading attributes memory to the device, not to
the owned process, and an observed breach of the declared ceiling stops the run.
"""
import contextlib
import http.client
import json
import os
import shlex
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matuto import matuto_runtime as runtime

# Health polling resolution, and therefore the quantisation of a readiness sample.
# A warm page cache loads this model in about two seconds, so a coarse grid would
# report the grid rather than the load.
POLL_SECONDS = .02
VRAM_SAMPLE_SECONDS = .5
# Bounded startup, execution and shutdown deadlines. A depth row prefills most of
# a context before it generates, so the throughput bound is hours, not minutes.
READY_SECONDS = 300
THROUGHPUT_SECONDS = 7200
STOP_SECONDS = 15
# llama.cpp allocates a context in whole blocks of this many tokens.
CONTEXT_BLOCK = 256
# Dimensions, repetitions, output format, model and context sizing belong to this
# module; a profile that also sets one would silently measure something else.
OWNED_OPTIONS = (
    "-p", "--n-prompt", "--prompt", "-n", "--n-gen", "--n-predict",
    "-d", "--n-depth", "-r", "--repetitions", "-o", "--output", "-oe",
    "--output-err", "-m", "--model", "-ngl", "--n-gpu-layers", "--gpu-layers",
    "-c", "--ctx-size", "--n-ctx",
)


class BenchError(Exception):
    """Developer-authored reason for a failed phase, never command or model output."""


def positive(environment, name):
    value = environment.get(name, "")
    if not value.isdigit() or int(value) < 1:
        raise BenchError(f"{name} must be a positive integer for the readiness and depth measurements.")
    return int(value)


def summarize(unit, samples):
    """Mean and sample spread over the configured repetitions, with the samples kept."""
    return {
        "unit": unit,
        "samples": [round(value, 4) for value in samples],
        "sample_count": len(samples),
        "mean": round(statistics.fmean(samples), 4),
        "sample_standard_deviation": round(statistics.stdev(samples), 4),
    }


def build_plan(environment, bench_args):
    """Derive both depths from the profile and prove the plan fits the declared context."""
    context = positive(environment, "CONTEXT_SIZE")
    generation = positive(environment, "BENCH_GENERATION_TOKENS")
    shallow = positive(environment, "BENCH_PROMPT_TOKENS")
    repetitions = positive(environment, "BENCH_REPETITIONS")
    ceiling_mb = positive(environment, "BENCH_VRAM_LIMIT_MB")
    if repetitions < 2:
        raise BenchError("BENCH_REPETITIONS must be at least 2; a mean and a spread cannot come from one sample.")
    for option in bench_args:
        if option.partition("=")[0] in OWNED_OPTIONS:
            raise BenchError(f"BENCH_ARGS sets an option this measurement owns: {option}.")
    near_full = context - generation
    if near_full <= shallow:
        raise BenchError("The profile's context leaves no depth above BENCH_PROMPT_TOKENS once BENCH_GENERATION_TOKENS is reserved.")
    depths = {"shallow": shallow, "near_full": near_full}
    for name, depth in depths.items():
        # llama-bench allocates n_prompt + n_gen + n_depth, and the backend rounds
        # that allocation up to whole blocks.
        allocated = generation + depth
        blocks = -(-allocated // CONTEXT_BLOCK) * CONTEXT_BLOCK
        if blocks > context:
            raise BenchError(f"The {name} depth row would allocate {blocks} tokens, above the profile's declared context of {context}.")
    return {
        "context_size": context,
        "generation_tokens": generation,
        "repetitions": repetitions,
        "planned_depths": depths,
        "vram_ceiling_decimal_mb": ceiling_mb,
        "allocation": "llama-bench allocates n_prompt + n_gen + n_depth, rounded up to whole blocks of 256 tokens",
    }


class Vram:
    """Samples the selected device's byte-valued counter across every phase."""

    def __init__(self, path, baseline, ceiling_mb):
        self.path, self.ceiling = Path(path), ceiling_mb * 1_000_000
        # The peak starts at the pre-run reading. A device already over its
        # ceiling has been observed over it, and no later workload undoes that.
        self.baseline = self.peak = baseline
        self.samples, self.failure = 1, None
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self.collect, daemon=True)

    @staticmethod
    def read_device(path):
        try:
            value = int(Path(path).read_text())
        except (OSError, ValueError) as error:
            raise BenchError("Byte-valued VRAM telemetry is missing or invalid; no zero measurement is assumed.") from error
        if value < 0:
            raise BenchError("Byte-valued VRAM telemetry reported a negative reading; no zero measurement is assumed.")
        return value

    def read(self):
        return self.read_device(self.path)

    def sample(self):
        value = self.read()
        with self.lock:
            self.samples += 1
            self.peak = max(self.peak, value)

    def collect(self):
        """Keep sampling where no phase is waiting: identity calls, reaping, parsing."""
        while not self.finished.wait(VRAM_SAMPLE_SECONDS):
            try:
                self.sample()
            except BenchError as error:
                self.failure = error
                return

    def check(self):
        """Read the device and raise on an observed breach or lost telemetry.

        Phases poll this between waits, so a workload shorter than the background
        interval is still measured against the ceiling.
        """
        if self.failure is not None:
            raise self.failure
        self.sample()
        if self.peak > self.ceiling:
            raise BenchError(f"Sampled VRAM reached {self.peak} bytes, above the declared ceiling of {self.ceiling} bytes.")

    def record(self, pci):
        return {
            "vram_device": str(self.path),
            "vram_pci": pci,
            "attribution": "selected_device_not_process_ownership",
            "sampling_interval_seconds": VRAM_SAMPLE_SECONDS,
            "samples": self.samples,
            "declared_ceiling_decimal_mb": self.ceiling // 1_000_000,
            "baseline_measurement": "read before this bench run started any workload, so the pinned invocation's memory is not recorded as idle",
            "baseline_bytes": self.baseline,
            "baseline_decimal_mb": round(self.baseline / 1e6, 3),
            "peak_measurement": "the highest reading this run observed, including the pre-run baseline; a run whose own allocation stays below that baseline reports the baseline",
            "sampled_peak_bytes": self.peak,
            "sampled_peak_decimal_mb": round(self.peak / 1e6, 3),
            "ceiling_enforcement": "sampled in the background at this interval and again on every wait poll; an observed breach stops the run, but a transient overshoot between samples is not excluded",
        }

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.finished.set()
        self.thread.join(timeout=VRAM_SAMPLE_SECONDS * 4)


def launch_command(environment, tool, arguments):
    """The runtime launcher invocation for one workload."""
    return [
        sys.executable, "-B", str(Path(__file__).resolve().parent / "matuto_runtime.py"),
        "launch", "bench" if tool == "llama-bench" else "serve",
        tool, environment["GPU_LAYERS"], "--", *arguments,
    ]


def record_command(results, name, command):
    path = results / name
    path.write_text(shlex.join(command) + "\n")
    path.chmod(0o600)


@contextlib.contextmanager
def owned(command, environment, stdout, stderr):
    """Start one workload in its own process group, with its cleanup already in scope.

    Callers record the command before they get here. Recording it after the
    launch left a window where an interruption returned failure while the
    process it had just started stayed up.
    """
    process = None
    try:
        process = subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr, start_new_session=True)
        yield process
    finally:
        if process is not None:
            stop(process)


def artifact(results, name, mode="wb"):
    """One private run artifact, opened for the phase that writes it."""
    handle = (results / name).open(mode)
    os.fchmod(handle.fileno(), 0o600)
    return handle


def stop(process):
    """Reap the owned process group, escalating, even while unwinding from a signal.

    A termination signal is delivered on whichever thread the runtime picks, so
    blocking it on this one defers nothing: the handler still raises here, out of
    a wait that has not finished. Escalation therefore has to survive that raise,
    or the process it exists for is exactly the one left running.
    """
    if process.poll() is not None:
        return
    interruption = None
    for number in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, number)
        try:
            process.wait(timeout=STOP_SECONDS)
            break
        except subprocess.TimeoutExpired:
            continue
        except BenchError as error:
            interruption = interruption or error
    if process.poll() is None:
        raise BenchError("The owned process survived termination and is still running; free the GPU yourself.")
    if interruption is not None:
        raise interruption


def endpoint(environment, path, timeout):
    connection = http.client.HTTPConnection(environment.get("SERVER_HOST") or "127.0.0.1", int(environment["SERVER_PORT"]), timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(1 << 20)
    finally:
        connection.close()


def require_free_port(environment):
    port = int(environment["SERVER_PORT"])
    try:
        with socket.socket() as probe:
            # A previous trial's connections may still linger; only a live
            # listener on the port counts as occupied.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((environment.get("SERVER_HOST") or "127.0.0.1", port))
    except OSError as error:
        raise BenchError(f"Loopback port {port} is occupied. Free it yourself; this measurement stopped nothing.") from error


def other_servers():
    """Whether some other llama-server already holds the GPU.

    A launch execve's a file descriptor, so the kernel takes `comm` from that
    path and not from the program name. The name the process was started under
    is in argv[0], which both a launched and a directly executed server carry.
    """
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "cmdline").read_bytes().split(b"\0", 1)[0].rpartition(b"/")[2] == b"llama-server":
                    return True
            except OSError:
                pass
    return False


def confirm_identity(environment, process):
    """Prove the endpoint that answered is the expected owned model and context.

    The live llama.cpp build is recorded, never compared: a rebuilt backend is a
    fact about the measurement, not a reason to refuse to take it.
    """
    try:
        executable_matches = Path(f"/proc/{process.pid}/exe").samefile(runtime.active_installation() / "llama-server")
    except (OSError, runtime.InspectionError):
        executable_matches = False
    if not executable_matches:
        raise BenchError("The ready endpoint is not backed by the expected owned server executable.")
    try:
        props_status, props_body = endpoint(environment, "/props", 10)
        slots_status, slots_body = endpoint(environment, "/slots", 10)
        props, slots = json.loads(props_body), json.loads(slots_body)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise BenchError("The owned server did not return readable identity endpoints.") from error
    if props_status != 200 or slots_status != 200 or type(props) is not dict or type(slots) is not list:
        raise BenchError("The owned server's identity endpoints did not return the expected HTTP 200 objects.")
    try:
        model_matches = Path(props.get("model_path", "")).samefile(environment["MODEL_PATH"])
    except (OSError, TypeError):
        model_matches = False
    if not model_matches:
        raise BenchError("The live model differs from the profile the readiness measurement was planned against.")
    declared_context = int(environment["CONTEXT_SIZE"])
    observed_context = slots[0].get("n_ctx") if len(slots) == 1 else None
    if observed_context != declared_context:
        # A model caps its slot at its training context, so an over-declared
        # profile serves happily and would then be measured under the wrong label.
        reported = observed_context if type(observed_context) is int else "unavailable"
        raise BenchError(f"The live slot context is {reported}, not the profile's declared {declared_context}.")
    return runtime.build_revision(props.get("build_info")) or "unavailable"


def measure_readiness(environment, plan, results, vram):
    """Time a fresh owned server from launch to its first ready health response."""
    server_args = [
        *json.loads(environment["MATUTO_BENCH_SERVER_ARGS"]),
        "-m", environment["MODEL_PATH"], "-ngl", environment["GPU_LAYERS"],
        "-c", environment["CONTEXT_SIZE"], "-n", environment["SERVER_OUTPUT_TOKENS"],
        "--alias", environment["MODEL_ALIAS"],
        "--host", environment.get("SERVER_HOST") or "127.0.0.1",
        "--port", environment["SERVER_PORT"],
    ]
    child = dict(environment, MATUTO_RUNTIME_IDENTITY_PATH=str(results / "runtime-identity-readiness.json"))
    command = launch_command(child, "llama-server", server_args)
    # Every trial launches the same command, so it is recorded once, before the
    # first clock starts and before there is any process to leave behind.
    record_command(results, "readiness-command.txt", command)
    durations, observed_build = [], "unavailable"
    with artifact(results, "readiness-server.log", "ab") as log:
        for _ in range(plan["repetitions"]):
            require_free_port(environment)
            started = time.monotonic()
            with owned(command, child, log, subprocess.STDOUT) as process:
                elapsed = wait_ready(environment, process, started, vram)
                observed_build = confirm_identity(environment, process)
            durations.append(elapsed)
    return durations, observed_build


def wait_ready(environment, process, started, vram):
    deadline = started + READY_SECONDS
    while time.monotonic() < deadline:
        vram.check()
        if process.poll() is not None:
            raise BenchError("The owned server exited while loading; no readiness time is recorded.")
        try:
            if endpoint(environment, "/health", min(2, max(.1, deadline - time.monotonic())))[0] == 200:
                return time.monotonic() - started
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(POLL_SECONDS)
    raise BenchError(f"The owned server did not become ready within {READY_SECONDS} seconds.")


def wait_bench(process, vram, what, log):
    """Poll one owned llama-bench to completion against the deadline and the ceiling.

    Polled at the sampling interval, not the readiness clock's resolution: nothing
    here needs 20 ms, and the counter this reads belongs to the device whose
    throughput the run is measuring.
    """
    deadline = time.monotonic() + THROUGHPUT_SECONDS
    while process.poll() is None:
        if time.monotonic() > deadline:
            raise BenchError(f"The {what} exceeded {THROUGHPUT_SECONDS} seconds.")
        vram.check()
        time.sleep(VRAM_SAMPLE_SECONDS)
    if process.returncode != 0:
        raise BenchError(f"The {what} failed with exit status {process.returncode}; it reported why in {log}.")


def measure_pinned(environment, plan, results, vram):
    """The pinned 4,096/512 row, in the dimensions and artifacts it has always had.

    Only where it runs has changed. It used to sit between two measurement
    passes, where nothing sampled the device, nothing had checked whether
    another server already held the GPU, and no lease held the runtime
    selection still.
    """
    arguments = [
        "-m", environment["MODEL_PATH"], "-ngl", environment["GPU_LAYERS"],
        *json.loads(environment["MATUTO_BENCH_BENCH_ARGS"]),
        "-p", str(plan["planned_depths"]["shallow"]),
        "-n", str(plan["generation_tokens"]),
        "-r", str(plan["repetitions"]), "-o", "json",
    ]
    command = launch_command(environment, "llama-bench", arguments)
    record_command(results, "command.txt", command)
    with artifact(results, "bench.json") as output, artifact(results, "bench.stderr.log") as errors:
        with owned(command, environment, output, errors) as process:
            wait_bench(process, vram, "pinned benchmark row", "bench.stderr.log")


def measure_throughput(environment, plan, results, vram):
    """One generation-only llama-bench run covering both planned depths."""
    depths = plan["planned_depths"]
    arguments = [
        "-m", environment["MODEL_PATH"], "-ngl", environment["GPU_LAYERS"],
        *json.loads(environment["MATUTO_BENCH_BENCH_ARGS"]),
        "-p", "0", "-n", str(plan["generation_tokens"]),
        "-d", f"{depths['shallow']},{depths['near_full']}",
        "-r", str(plan["repetitions"]), "-o", "json",
    ]
    child = dict(environment, MATUTO_RUNTIME_IDENTITY_PATH=str(results / "runtime-identity-depth.json"))
    command = launch_command(child, "llama-bench", arguments)
    record_command(results, "bench-depth-command.txt", command)
    with artifact(results, "bench-depth.json") as output, artifact(results, "bench-depth.stderr.log") as errors:
        with owned(command, child, output, errors) as process:
            wait_bench(process, vram, "depth throughput run", "bench-depth.stderr.log")
    return read_rows(results / "bench-depth.json", plan)


def read_rows(path, plan):
    """Validate the emitted rows against the plan and keep their observed KV types."""
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise BenchError("The depth throughput run did not emit readable JSON.") from error
    if type(rows) is not list:
        raise BenchError("The depth throughput run did not emit a JSON list of rows.")
    measured = {}
    for name, depth in plan["planned_depths"].items():
        matching = [row for row in rows if type(row) is dict and row.get("n_depth") == depth]
        if len(matching) != 1:
            raise BenchError(f"The depth throughput run returned {len(matching)} rows at the planned {name} depth of {depth}; exactly one is required.")
        row = matching[0]
        samples = row.get("samples_ts")
        if type(samples) is not list or len(samples) != plan["repetitions"] or not all(type(value) in (int, float) for value in samples):
            raise BenchError(f"The {name} depth row does not carry one throughput sample per configured repetition.")
        if row.get("n_prompt") != 0 or row.get("n_gen") != plan["generation_tokens"]:
            raise BenchError(f"The {name} depth row was not measured at the planned generation-only dimensions.")
        if not (type(row.get("type_k")) is str and type(row.get("type_v")) is str):
            raise BenchError(f"The {name} depth row does not state the KV cache types it was measured with.")
        measured[name] = {
            "n_depth": row["n_depth"],
            "n_gen": row["n_gen"],
            "type_k": row["type_k"],
            "type_v": row["type_v"],
            "generation": summarize("tokens_per_second", samples),
        }
    return measured


def report(document):
    """The three numbers on the terminal, beside the telemetry summary bench prints.

    An operator who waited out five cold loads and a near-full prefill should not
    have to open a file to learn what the run measured.
    """
    readiness = document["readiness"]
    lines = [f"Load to ready: {readiness['mean']} s mean, {readiness['sample_standard_deviation']} s spread"
             f" over {readiness['sample_count']} trials, build {readiness['observed_build_revision']}"]
    for name, row in document["throughput"].items():
        generation = row["generation"]
        lines.append(f"Generation at depth {row['n_depth']} ({name}): {generation['mean']} tokens/s mean,"
                     f" {generation['sample_standard_deviation']} spread, KV {row['type_k']}/{row['type_v']}")
    vram = document["telemetry"]
    lines.append(f"Peak VRAM: {vram['sampled_peak_decimal_mb']} MB against a declared {vram['declared_ceiling_decimal_mb']} MB,"
                 f" from a {vram['baseline_decimal_mb']} MB baseline")
    print("\n".join(lines), file=sys.stderr)


def write_summary(results, document):
    path = results / "summary.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def interrupted(*_):
    """Turn a termination signal into an unwind, so owned processes are reaped."""
    raise BenchError("The measurement was interrupted; owned processes were stopped and this run is incomplete.")


def main(argv):
    if len(argv) < 1:
        raise BenchError("Usage: matuto_bench.py RESULTS_DIR [--bench ARGS...] [--server ARGS...]")
    results = Path(argv[0])
    environment = dict(os.environ)
    section, collected = None, {"--bench": [], "--server": []}
    for argument in argv[1:]:
        if argument in collected:
            section = argument
        elif section is None:
            raise BenchError("Every measurement argument must follow --bench or --server.")
        else:
            collected[section].append(argument)
    environment["MATUTO_BENCH_BENCH_ARGS"] = json.dumps(collected["--bench"])
    environment["MATUTO_BENCH_SERVER_ARGS"] = json.dumps(collected["--server"])
    document = {
        "schema": "matuto-lab-bench-summary/1",
        "outcome": "error",
        "performed": [],
        "definitions": {
            "readiness_seconds": "Wall time from spawning a fresh owned server to its first HTTP 200 from /health, after which the live model, build and slot context are confirmed.",
            "readiness_includes": "Runtime launch preparation (device resolution and executable probes) and model load.",
            "readiness_excludes": "Model checksum verification, which the bench action performs once before these trials.",
            "readiness_cold": "Each trial starts a fresh process. The OS page cache is not dropped, so this is not cold-storage latency.",
            "observed_build_revision": "The llama.cpp revision the owned server actually reported. It is recorded for comparability and is never required to equal the profile's declared one.",
            "readiness_poll_interval_seconds": POLL_SECONDS,
            "generation_tokens_per_second": "llama-bench generation throughput with the KV cache prefilled to the stated depth, over the configured repetitions.",
            "not_established": "These are bench numbers for a single owned server with no concurrent load. They are not a served worker's throughput under an agent policy, and a row is comparable only with rows sharing its depth and KV types.",
            "retained_pinned_row": "The pinned 4,096/512 invocation runs first, in the dimensions it has always had, and its bench.json, stderr, identity and AMD-SMI artifacts are unchanged. Only the guards around it changed: it now runs under this run's lease, ownership check and sampled ceiling. command.txt records the same invocation, quoted by this module rather than by the shell, so its text differs from an earlier artifact while the command does not.",
        },
    }
    telemetry, pci, lease = None, "unavailable", None
    try:
        # The plan is proved before anything is launched, so a profile that
        # cannot be measured never reaches the GPU at all.
        plan = build_plan(environment, collected["--bench"])
        document["plan"] = plan
        # One lease for the whole run, not one per launched child. The children
        # leave gaps between them, and a selection changing in such a gap would
        # spread these numbers across backends, or read the peak from a counter
        # that belongs to a device no workload here ever touched.
        lease = runtime.runtime_lease(False)
        selection = runtime.read_selection()
        if selection is None:
            raise BenchError("No runtime selection is configured; the measured device cannot be identified.")
        pci = selection["pci"]
        device = runtime.vram_telemetry(pci)
        if other_servers():
            raise BenchError("Another llama-server is running. Free the GPU yourself; this measurement stopped nothing.")
        # Read before this run starts any workload, so memory an earlier
        # invocation has not released is never recorded as idle.
        baseline = Vram.read_device(device)
        with Vram(device, baseline, plan["vram_ceiling_decimal_mb"]) as vram:
            telemetry = vram
            vram.check()
            measure_pinned(environment, plan, results, vram)
            document["performed"].append("benchmark")
            durations, observed_build = measure_readiness(environment, plan, results, vram)
            document["readiness"] = {**summarize("seconds", durations),
                                     "observed_build_revision": observed_build,
                                     "declared_backend_version": environment.get("BACKEND_VERSION") or "unavailable"}
            document["performed"].append("readiness")
            document["throughput"] = measure_throughput(environment, plan, results, vram)
            document["performed"].append("depth_throughput")
            vram.check()
        document["outcome"] = "completed"
        return 0
    except (BenchError, runtime.InspectionError) as error:
        document["reason"] = str(error)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Partial evidence and a failure still reach the caller; no exception text
        # is published into an artifact an operator may share.
        document["reason"] = "Unexpected measurement error; no exception text is published in this artifact."
        print("Error: unexpected measurement error; its traceback follows on stderr.", file=sys.stderr)
        traceback.print_exc()
        return 1
    finally:
        if lease is not None:
            os.close(lease)
        if telemetry is not None:
            document["telemetry"] = telemetry.record(pci)
        write_summary(results, document)
        if document["outcome"] == "completed":
            report(document)


if __name__ == "__main__":
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, interrupted)
        sys.exit(main(sys.argv[1:]))
    except BenchError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
