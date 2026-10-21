"""Store-backed input for the side-by-side Matuto lab."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

AGENTS = Path(__file__).resolve().parents[1]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import arguments, store

STORE = Path(__file__).with_name("store.py").resolve()
RUNTIME = AGENTS / "matuto" / "matuto_runtime.py"
LAB = AGENTS / "matuto-lab"
BENCH = Path(__file__).with_name("store_bench.py")
VOICE = Path(__file__).with_name("store_voice.py")


def _environment(home):
    home = Path(home)
    config, data, state = home / ".config", home / ".local/share", home / ".local/state"
    for path in (config, data, state):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    store_config = os.environ.get("MATUTO_STORE_CONFIG_ROOT") or os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(config),
               XDG_DATA_HOME=str(data), XDG_STATE_HOME=str(state),
               MATUTO_STORE_CONFIG_ROOT=store_config, MATUTO_STORE_BACKED_LAB="1")
    return env


def _prepare_runtime(spec, env):
    runtime = spec["runtime"]
    backends = {"hip": "HIP/ROCm", "vulkan": "Vulkan"}
    backend = backends.get(runtime["backend"])
    if backend is None:
        raise store.StoreError(f"Unsupported stored runtime backend {runtime['backend']!r}.")
    completed = subprocess.run(
        [sys.executable, "-B", str(RUNTIME), "select", runtime["location"], backend,
         runtime["gpu_identity"]], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False)
    if completed.returncode:
        raise store.StoreError(completed.stderr.strip() or "The stored runtime selection could not be verified.")


def _launch(env, action, tool, layers, arguments, pid_file=None):
    command = [sys.executable, "-B", str(RUNTIME), "launch", action, tool,
               str(layers), "--", *arguments]
    if pid_file is None:
        return subprocess.call(command, env=env)
    process = subprocess.Popen(command, env=env)
    path = Path(pid_file)
    try:
        path.write_text(f"{process.pid}\n")
        return process.wait()
    finally:
        path.unlink(missing_ok=True)


def _profile_arguments(record, tool):
    result = []
    for row in record["argument_rows"]:
        if row["tool"] != tool:
            continue
        result.append(row["option"])
        if row["value"] is not None:
            result.append(row["value"])
    return result


def _has_server_gpu_layers(record):
    return any(row["tool"] == "server" and row["option"].partition("=")[0]
               in ("-ngl", "--n-gpu-layers", "--gpu-layers")
               for row in record["argument_rows"])


def _runtime_gpu_layers(record):
    return 99 if record["gpu_layers"] == -1 else record["gpu_layers"]


def _server_arguments(record):
    args = _profile_arguments(record, "server")
    if not _has_server_gpu_layers(record):
        args += ["-ngl", str(_runtime_gpu_layers(record))]
    args += ["-m", record["files"]["model"]["path"],
            "-c", str(record["context_size"]),
            "-n", str(record["server_output_tokens"]), "--alias", record["alias"],
            "--host", record["server_host"], "--port", str(record["server_port"])]
    if "mmproj" in record["files"]:
        args += ["--mmproj", record["files"]["mmproj"]["path"]]
    if "mtp" in record["files"]:
        args += ["--spec-type", "draft-mtp", "--spec-draft-model", record["files"]["mtp"]["path"]]
    return args


def _sidecar_arguments(record):
    args = []
    if "mmproj" in record["files"]:
        args += ["--mmproj", record["files"]["mmproj"]["path"]]
    if "mtp" in record["files"]:
        args += ["--spec-type", "draft-mtp", "--spec-draft-model", record["files"]["mtp"]["path"]]
    return args


def _smoke_arguments(record):
    if "mmproj" in record["files"] or "mtp" in record["files"]:
        raise store.StoreError("Smoke is text-only; use bundle qualification to measure model features.")
    return [*_profile_arguments(record, "smoke"), "-m", record["files"]["model"]["path"],
            "-ngl", str(_runtime_gpu_layers(record)), "-c", str(record["context_size"]),
            "-n", str(record["smoke_output_tokens"]), "--seed", "1234", "--temp", "0",
            "-p", "Reply with exactly: ROCm smoke test OK"]


def _launch_argument_check(rows):
    from matuto import matuto_runtime

    try:
        selection = matuto_runtime.read_selection()
        if selection is None:
            raise store.StoreError("No runtime selection is configured in the store.")
        environment = matuto_runtime.execution_environment()
        environment, _identity = matuto_runtime.resolve_device(selection, environment)
    except matuto_runtime.InspectionError as error:
        raise store.StoreError(str(error)) from None
    tools = {"server": "llama-server", "smoke": "llama-cli", "bench": "llama-bench"}
    help_text = {}
    selector = None
    for tool, executable in tools.items():
        if tool != "server" and not any(row["tool"] == tool for row in rows):
            continue
        try:
            directory, descriptor = matuto_runtime._tool_descriptor(selection["installation"], executable)
            try:
                help_text[tool] = matuto_runtime.probe(descriptor, directory, "--help", environment)
                if tool == "server":
                    if not all(option in help_text[tool] for option in ("--device", "--list-devices")):
                        raise store.StoreError("The active build does not advertise device selection.")
                    listing = matuto_runtime.probe(descriptor, directory, "--list-devices", environment)
                    selectors = [name for name, backend in matuto_runtime._device_listing(listing)
                                 if backend == selection["backend"]]
                    if len(selectors) != 1:
                        raise store.StoreError("The active build did not expose exactly one selected backend device.")
                    selector = selectors[0]
            finally:
                os.close(descriptor)
                os.close(directory)
        except matuto_runtime.InspectionError as error:
            raise store.StoreError(str(error)) from None
    try:
        arguments.launch_check(rows, selector=selector, help_text=help_text)
    except arguments.Refusal as error:
        raise store.StoreError(str(error)) from None


def _check_launch_arguments(rows, env):
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(RUNTIME).resolve().parent / "store_lab.py"),
         "check-arguments",
         json.dumps(rows, separators=(",", ":"))], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode:
        print(completed.stderr.strip() or "Stored launch arguments could not be checked.", file=sys.stderr)
    return completed.returncode


def _run_language(spec, action, env, results_dir=None):
    record = spec["record"]
    if record["kind"] != "language":
        raise store.StoreError(f"{action} requires a stored language profile.")
    env["MATUTO_RUNTIME_MODEL_SHA256"] = record["files"]["model"]["sha256"]
    for name in ("MODEL_MMPROJ", "MODEL_MTP", "MATUTO_RUNTIME_MMPROJ_SHA256",
                 "MATUTO_RUNTIME_MTP_SHA256"):
        env.pop(name, None)
    if "mmproj" in record["files"]:
        env["MATUTO_RUNTIME_MMPROJ_SHA256"] = record["files"]["mmproj"]["sha256"]
    if "mtp" in record["files"]:
        env["MATUTO_RUNTIME_MTP_SHA256"] = record["files"]["mtp"]["sha256"]
    if _check_launch_arguments(record["argument_rows"], env):
        return 1
    layers = _runtime_gpu_layers(record)
    if action == "verify":
        return _launch(env, action, "llama-server", "-", ["--list-devices"])
    if action == "smoke":
        return _launch(env, action, "llama-cli", layers, _smoke_arguments(record))
    if action == "serve":
        identity_path = env.get("MATUTO_RUNTIME_IDENTITY_PATH")
        pid_file = str(Path(identity_path).with_suffix(".pid")) if identity_path else None
        return _launch(env, action, "llama-server", layers, _server_arguments(record), pid_file)
    if action == "bench":
        results = Path(results_dir) if results_dir else (
            Path(env["XDG_STATE_HOME"]) / "ai-agents-sandbox/matuto-lab/results" /
            record["id"] / str(time.time_ns()))
        results.mkdir(mode=0o700, parents=True)
        env.update({"CONTEXT_SIZE": str(record["context_size"]), "GPU_LAYERS": str(layers),
                    "BENCH_GENERATION_TOKENS": str(record["bench_generation_tokens"]),
                    "BENCH_PROMPT_TOKENS": str(record["bench_prompt_tokens"]),
                    "BENCH_REPETITIONS": str(record["bench_repetitions"]),
                    "BENCH_VRAM_LIMIT_MB": str(max(1, record["vram_ceiling_bytes"] // 1_000_000)),
                    "SERVER_OUTPUT_TOKENS": str(record["server_output_tokens"]),
                    "SERVER_HOST": record["server_host"],
                    "SERVER_PORT": str(record["server_port"]),
                    "MODEL_PATH": record["files"]["model"]["path"],
                    "MODEL_ALIAS": record["alias"],
                    "MATUTO_BENCH_SERVER_HAS_GPU_LAYERS": "1" if _has_server_gpu_layers(record) else "0",
                    "MATUTO_RUNTIME_MODEL_SHA256": record["files"]["model"]["sha256"]})
        if "mmproj" in record["files"]:
            env["MATUTO_RUNTIME_MMPROJ_SHA256"] = record["files"]["mmproj"]["sha256"]
        if "mtp" in record["files"]:
            env["MATUTO_RUNTIME_MTP_SHA256"] = record["files"]["mtp"]["sha256"]
        sidecars = _sidecar_arguments(record)
        return subprocess.call([sys.executable, "-B", str(BENCH), str(results), "--bench",
                                *_profile_arguments(record, "bench"), *sidecars, "--server",
                                *_profile_arguments(record, "server"), *sidecars], env=env)
    raise store.StoreError(f"Unsupported store-backed lab procedure {action!r}.")


def _run_speech(spec, action, env):
    record = spec["record"]
    if record["kind"] not in ("synthesis", "recognition") or action != "qualify":
        raise store.StoreError(f"{action} is not supported for stored {record['kind']} records.")
    executable = record["executable"]
    part = {"profile": record["id"], "display_name": record["name"],
            "role": record["kind"], "backend": executable["backend"],
            "executable": executable["path"], "executable_sha256": executable["sha256"],
            "model_path": record["model_path"], "model_sha256": record["model_sha256"],
            "host": record["host"], "port": record["port"],
            "args": record["arguments"]["server"]}
    results = Path(env["XDG_STATE_HOME"]) / "ai-agents-sandbox/matuto-lab/results" / record["id"] / str(time.time_ns())
    results.mkdir(mode=0o700, parents=True)
    child = dict(env, MATUTO_VOICE_PART=json.dumps(part, sort_keys=True),
                 MATUTO_RUNTIME_EXPECTED_SHA256=executable["sha256"])
    return subprocess.call([sys.executable, "-B", str(VOICE), "part", str(results)], env=child)


def _bundle_environment(spec, env):
    bundle = spec["record"]
    if bundle["kind"] != "bundle":
        raise store.StoreError("bundle requires a stored bundle record.")
    members = bundle["members"]
    model = members["language"]
    model["profile"] = model["id"]
    model["display_name"] = model["name"]
    model["model_path"] = model["files"]["model"]["path"]
    model["model_sha256"] = model["files"]["model"]["sha256"]
    model["host"] = model["server_host"]
    model["port"] = spec["lab_port"]
    model["mmproj"] = model["files"].get("mmproj")
    model["mtp"] = {"enabled": "mtp" in model["files"], "model": model["files"].get("mtp")}
    parts = []
    for role in ("synthesis", "recognition"):
        part = members.get(role)
        if part is None:
            continue
        parts.append({"profile": part["id"], "display_name": part["name"], "role": role,
                      "backend": part["executable"]["backend"], "executable": part["executable"]["path"],
                      "executable_sha256": part["executable"]["sha256"],
                      "model_path": part["model_path"], "model_sha256": part["model_sha256"],
                      "host": "127.0.0.1", "port": part["port"],
                      "args": part["arguments"]["server"]})
    results = Path(env.get("MATUTO_RUN_RESULTS_DIR") or env.get("MATUTO_RUN_EVIDENCE_DIR") or
                   (Path(env["XDG_STATE_HOME"]) / "ai-agents-sandbox/matuto-lab/results/bundles" /
                    bundle["id"] / str(time.time_ns())))
    results.mkdir(mode=0o700, parents=True, exist_ok=True)
    env.update({"MATUTO_VOICE_MODEL": json.dumps(model, sort_keys=True),
                "MATUTO_STORE_LAUNCH_RECORD": json.dumps(model, sort_keys=True),
                "MATUTO_VOICE_PARTS": json.dumps(parts, sort_keys=True),
                "MATUTO_RUN_RESULTS_DIR": str(results),
                "MATUTO_VOICE_BUNDLE": bundle["id"],
                "MATUTO_VOICE_BUNDLE_DISPLAY": bundle["name"],
                "MATUTO_VOICE_LOAD_TOKENS": str(bundle["load_tokens"]),
                "MATUTO_VOICE_LOAD_PROMPT": bundle["load_prompt"] or "",
                "MATUTO_VOICE_LAB": str(LAB), "MATUTO_LAB_STORE_BACKED": "1",
                "MATUTO_RUNTIME_MODEL_SHA256": model["model_sha256"]})
    return subprocess.call([sys.executable, "-B", str(VOICE), "bundle", str(results)], env=env)


def _read_spec(kind, identifier):
    completed = subprocess.run([sys.executable, "-B", str(STORE), "launch-spec", kind, identifier],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode:
        raise store.StoreError(completed.stderr.strip() or "The store refused to emit a launch spec.")
    try:
        return json.loads(completed.stdout)
    except ValueError:
        raise store.StoreError("The store returned an unreadable launch spec.") from None


def main(argv):
    if len(argv) == 2 and argv[0] == "check-arguments":
        try:
            rows = json.loads(argv[1])
            _launch_argument_check(rows)
            return 0
        except (OSError, ValueError, store.StoreError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
    if len(argv) == 3 and argv[0] == "member":
        try:
            record = json.loads(os.environ["MATUTO_STORE_LAUNCH_RECORD"])
        except (KeyError, TypeError, ValueError):
            raise store.StoreError("The bundle did not supply its language member spec.") from None
        if record.get("kind") != "language" or record.get("id") != argv[1]:
            raise store.StoreError("The bundle language member does not match its supplied spec.")
        if argv[2] != "serve":
            raise store.StoreError("A supplied bundle member supports only serve.")
        return _run_language({"record": record}, "serve", dict(os.environ))
    if len(argv) == 3 and argv[0] == "spec":
        try:
            print(json.dumps(_read_spec(argv[1], argv[2]), sort_keys=True))
            return 0
        except store.StoreError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
    if len(argv) == 4 and argv[0] == "run":
        _mode, kind, identifier, action = argv
        spec = _read_spec(kind, identifier)
        with tempfile.TemporaryDirectory(prefix="matuto-store-lab-") as home:
            env = _environment(home)
            _prepare_runtime(spec, env)
            if action == "bundle":
                return _bundle_environment(spec, env)
            if kind in ("synthesis", "recognition"):
                return _run_speech(spec, action, env)
            return _run_language(spec, action, env)
    print("Usage: matuto-lab --store spec KIND ID | --store run KIND ID ACTION", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, store.StoreError, store.RecordRefusal) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
