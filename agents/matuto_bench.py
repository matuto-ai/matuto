#!/usr/bin/env python3
"""Compatibility entry for benchmark support owned by the Matuto app."""
import runpy
import sys

from matuto import matuto_bench as bench

if __name__ == "__main__":
    runpy.run_module("matuto.matuto_bench", run_name="__main__")
else:
    sys.modules[__name__] = bench
