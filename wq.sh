#!/usr/bin/env bash
# Compatibility wrapper for the readable worker-qualification runner.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/wq.py" "$@"
