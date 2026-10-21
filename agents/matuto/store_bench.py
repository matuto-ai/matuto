"""Store-backed benchmark entry; keep adaptations out of the legacy helper."""

import signal
import sys
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[1]
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

from matuto import matuto_bench as bench

_legacy_launch_command = bench.launch_command


def _launch_command(environment, tool, arguments):
    if environment.get("MATUTO_STORE_BACKED_LAB") != "1" or tool != "llama-server":
        return _legacy_launch_command(environment, tool, arguments)
    has_gpu_layers = environment.get("MATUTO_BENCH_SERVER_HAS_GPU_LAYERS")
    if has_gpu_layers not in ("0", "1"):
        raise bench.BenchError("The stored server GPU-layer override state is unavailable to the benchmark.")
    has_gpu_layers = has_gpu_layers == "1"
    if has_gpu_layers:
        arguments = list(arguments)
        model = environment["MODEL_PATH"]
        model_index = next((index for index in range(len(arguments) - 1)
                            if arguments[index:index + 2] == ["-m", model]), None)
        if model_index is None:
            raise bench.BenchError("The stored model is missing from the benchmark readiness command.")
        generated = model_index + 2
        if arguments[generated:generated + 2] != ["-ngl", environment["GPU_LAYERS"]]:
            raise bench.BenchError("The generated GPU-layer default is missing from the benchmark readiness command.")
        del arguments[generated:generated + 2]
    return _legacy_launch_command(environment, tool, arguments)


bench.launch_command = _launch_command


def main(argv):
    return bench.main(argv)


if __name__ == "__main__":
    try:
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, bench.interrupted)
        sys.exit(main(sys.argv[1:]))
    except bench.BenchError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
