"""The launch systemd's ExecStart runs: the lease, the launch checks, then the router."""

import sys

from matuto import admission, router


def run(argv):
    """Start the router, or with `--check` run every check without starting it."""
    if argv not in ([], ["--check"]):
        print("usage: matuto router [--check]", file=sys.stderr)
        return 2
    try:
        if not argv:
            router.launch()
            return 0
        with router.lease() as decision:
            if decision.outcome != admission.ADMIT:
                raise router.LauncherRefused(decision.reason)
            router.prepare().close()
    except router.LauncherRefused as refusal:
        print(f"matuto router: {refusal}", file=sys.stderr)
        return 1
    print("The router's lease and launch checks pass; the unit would start.")
    return 0