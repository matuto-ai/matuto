#!/usr/bin/env python3
"""Compatibility entry for voice support owned by the Matuto app."""
import runpy
import sys

from matuto import matuto_voice as voice

if __name__ == "__main__":
    runpy.run_module("matuto.matuto_voice", run_name="__main__")
else:
    sys.modules[__name__] = voice
