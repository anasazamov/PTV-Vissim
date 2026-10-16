"""Deliberate stdout noise for the guard tests: Python level, fd level and native (below the C runtime)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parents[1])
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from fake_vissim import native_stdout_write  # noqa: E402

#: Label of every kind of noise ``make_noise`` produces (each line is ``f"{marker} {label}"``).
NOISE_LABELS = ("print()", "sys.stdout.write", "sys.__stdout__.write", "os.write(1)", "native write")


def make_noise(marker: str) -> None:
    """Write one line per ``NOISE_LABELS`` entry "to stdout" in every way a library or DLL could."""
    print(f"{marker} print()")
    sys.stdout.write(f"{marker} sys.stdout.write\n")
    sys.stdout.flush()
    if sys.__stdout__ is not None:
        sys.__stdout__.write(f"{marker} sys.__stdout__.write\n")
        sys.__stdout__.flush()
    os.write(1, f"{marker} os.write(1)\n".encode())
    native_stdout_write(f"{marker} native write\n".encode())
