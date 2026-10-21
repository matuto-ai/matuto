"""Measure the stored profile with Matuto's existing benchmark and no new pass threshold."""

import json
import os
from pathlib import Path

from matuto import store_lab
from matuto.procedure_support import evidence, failed, report, setup


def run(snapshot, record):
    spec, environment, home = setup(snapshot, record)
    try:
        report("bench", "running benchmark measurements")
        destination = Path(os.environ["MATUTO_RUN_EVIDENCE_DIR"]) / "bench"
        status = store_lab._run_language(spec, "bench", environment, results_dir=destination)
        summary_path = destination / "summary.json"
        summary = summary_path.read_text("utf-8") if summary_path.exists() else ""
        if not summary:
            failed("benchmark did not produce its measurement summary")
        document = json.loads(summary)
        evidence("bench", spec["record"], [],
                 "Benchmark measurements recorded.\n" + json.dumps(document, indent=2, sort_keys=True),
                 **{"bench/summary.json": summary})
        report("bench", "benchmark measurements recorded")
        if status:
            failed(document.get("reason") or "benchmark did not complete")
        return 0
    finally:
        home.cleanup()
