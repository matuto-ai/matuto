"""Discover the selected runtime and verify stored launch prerequisites without loading a model."""

from matuto import store, store_lab
from matuto.procedure_support import evidence, failed, report, setup


def run(snapshot, record):
    check = {"name": "runtime discovery", "status": "SKIP",
             "detail": "runtime verification did not reach device discovery"}
    evidence_record = dict(record, fingerprint=store.fingerprint(snapshot, record))
    evidence("verify", evidence_record, [check], check["detail"])
    spec, environment, home = setup(snapshot, record)
    try:
        report("verify", "checking stored launch arguments and runtime")
        if store_lab._run_language(spec, "verify", environment):
            failed("runtime verification did not complete")
        check = {"name": "runtime discovery", "status": "PASS",
                 "detail": "the selected runtime enumerated its devices without loading the model"}
        evidence("verify", spec["record"], [check],
                 "Runtime verification completed; no model was loaded.")
        report("verify", "runtime discovery complete")
        return 0
    finally:
        home.cleanup()
