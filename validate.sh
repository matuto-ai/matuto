#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Tests that launch persistent llama-server fixtures run after the process scan.
APP_TEST_WORKERS=6
VALIDATE_BUDGET_SECONDS=150
# -B does not cover Python subprocesses spawned by the checks.
export PYTHONDONTWRITEBYTECODE=1

# Elapsed seconds per top-level command, plus an inclusive total. Only the
# stable name is printed: the command text and its environment are never echoed
# because they carry credentials.
now_ms() { printf '%s' "$(( ${EPOCHREALTIME/./} / 1000 ))"; }
elapsed_seconds() { printf '%d.%03d' "$(( $1 / 1000 ))" "$(( $1 % 1000 ))"; }
run_timed() {
    local name="$1"; shift
    local started; started="$(now_ms)"
    local log; log="$(mktemp)"
    local status=0
    "$@" >"$log" 2>&1 || status=$?
    if (( status )); then
        cat "$log" >&2
    else
        # Keep nested suite summaries and timings, not successful fixture diagnostics.
        grep -E '^(APP TEST |TIMING )' "$log" || true
    fi
    rm -f "$log"
    printf 'TIMING validate.%s %ss status=%s\n' "$name" "$(elapsed_seconds "$(( $(now_ms) - started ))")" "$status"
    return "$status"
}
VALIDATE_STARTED="$(now_ms)"
wq_test_dir=""
app_pid=""
app_log=""
finish_app_tests() {
    [[ -n "$app_pid" ]] || return 0
    local status=0
    wait "$app_pid" || status=$?
    app_pid=""
    python3 -B -c 'import sys; from pathlib import Path; sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())' "$app_log" || return "$?"
    return "$status"
}
cleanup() {
    local status="$1"
    # Even an unexpected validation exit must let background suites finish.
    finish_app_tests || true
    if [[ -n "$app_log" ]]; then rm -f "$app_log" || true; fi
    if [[ -n "$wq_test_dir" ]]; then rm -rf "$wq_test_dir" || true; fi
    local elapsed_ms; elapsed_ms="$(( $(now_ms) - VALIDATE_STARTED ))"
    local elapsed; elapsed="$(elapsed_seconds "$elapsed_ms")"
    if (( status == 0 && elapsed_ms > VALIDATE_BUDGET_SECONDS * 1000 )); then
        echo "Validation took ${elapsed}s, exceeding the ${VALIDATE_BUDGET_SECONDS}s budget." >&2
        status=1
    fi
    printf 'TIMING validate.total %ss status=%s\n' "$elapsed" "$status"
    exit "$status"
}
trap 'cleanup "$?"' EXIT

if (( $# > 1 )); then
    echo "Usage: ./validate.sh [--wq-only]" >&2
    exit 2
fi
case "${1:-}" in
    "") ;;
    --wq-only) ;;
    *) echo "Usage: ./validate.sh [--wq-only]" >&2; exit 2 ;;
esac
shellcheck_files=(
    "$ROOT/validate.sh"
    "$ROOT/wq.sh"
    "$ROOT/agents/ci-docs-only"
    "$ROOT/agents/ci-wq-only"
    "$ROOT/agents/matuto-lab"
)
run_timed shellcheck-errors python3 -c '
import json
import subprocess
import sys

result = subprocess.run(
    ["shellcheck", "-s", "bash", "-S", "style", "--format=json", *sys.argv[1:]],
    capture_output=True,
    text=True,
)
if result.returncode not in (0, 1):
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
findings = json.loads(result.stdout)
selected = [finding for finding in findings if finding["level"] == "error" or finding["code"] == 2155]
for finding in selected:
    print("{}:{}:{}: {}: {} [SC{}]".format(
        finding["file"], finding["line"], finding["column"],
        finding["level"], finding["message"], finding["code"],
    ))
raise SystemExit(bool(selected))
' "${shellcheck_files[@]}"
run_timed ci-routing python3 -B "$ROOT/agents/tests/test_ci_routing.py"
# The app package's tests are found by glob: a new test file under agents/tests/app/
# is picked up by being placed there, and an empty or absent match is not a failure.
run_app_tests() {
    python3 -B - "$ROOT" "$APP_TEST_WORKERS" <<'PY'
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess
import sys
import time

root = Path(sys.argv[1])
tests = root / "agents/tests/app"

def run_file(path):
    started = time.monotonic()
    command = [sys.executable, "-B", "-m", "unittest", "discover",
               "-s", str(tests), "-t", str(tests), "-p", path.name]
    try:
        result = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as error:
        result = subprocess.CompletedProcess(command, 1, f"{error}\n".encode())
    return path.name, time.monotonic() - started, result

failed = []
with ThreadPoolExecutor(max_workers=int(sys.argv[2])) as workers:
    # File size is a cheap duration estimate: start large suites before the tail goes idle.
    process_fixtures = {"test_procedures.py", "test_serve.py", "test_store_launch.py"}
    paths = sorted((path for path in tests.glob("test_*.py") if path.name not in process_fixtures),
                   key=lambda path: path.stat().st_size, reverse=True)
    pending = [workers.submit(run_file, path) for path in paths]
    for future in as_completed(pending):
        name, elapsed, result = future.result()
        if result.returncode:
            sys.stdout.buffer.write(result.stdout)
            if result.stdout and not result.stdout.endswith(b"\n"):
                sys.stdout.buffer.write(b"\n")
            sys.stdout.flush()
        print(f"APP TEST {name} {elapsed:.3f}s status={result.returncode}", flush=True)
        if result.returncode:
            failed.append(name)
if failed:
    print("Failed app test files: " + ", ".join(sorted(failed)), flush=True)
raise SystemExit(bool(failed))
PY
}
run_process_fixture_tests() {
    for test_file in test_procedures.py test_serve.py test_store_launch.py; do
        python3 -B -m unittest discover -s "$ROOT/agents/tests/app" \
            -t "$ROOT/agents/tests/app" -p "$test_file" || return $?
    done
}
app_log="$(mktemp)"
run_timed app-unittests run_app_tests >"$app_log" 2>&1 &
app_pid=$!
run_timed app-process-fixtures run_process_fixture_tests
finish_app_tests
run_page_tests() {
    for page_test in "$ROOT"/agents/tests/app/*.test.cjs; do
        [ -e "$page_test" ] || continue
        node "$page_test" || return $?
    done
}
run_timed page-behaviour run_page_tests
wq_test_dir="$(mktemp -d)"
run_timed wq-compile python3 -B -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$ROOT/wq.py"
run_timed wq-selftest env XDG_STATE_HOME="$wq_test_dir" "$ROOT/wq.sh" selftest
if run_timed wq-invalid-profile env WQ_PROFILE=invalid "$ROOT/wq.sh" run 2>/dev/null; then
    echo "wq.sh accepted an invalid profile" >&2
    exit 1
fi
