"""The real-Vissim suite (tests/real) is wired so that it can never run by accident (PLAN 10.3, M6 prep).

* the default run deselects it (``addopts = -m "not vissim"`` + the marker added by tests/real/conftest.py);
* ``-m vissim --collect-only`` lists every PLAN 10.3 test;
* ``-m vissim`` without ``--vissim-com-license`` skips every test before any fixture runs;
* no module of tests/real imports pywin32 (the server and the probe run as subprocesses with real pywin32).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_DIR = REPO_ROOT / "tests" / "real"
EXPECTED = {
    "test_real_smoke.py::test_smoke_connect_load_simulate_close",
    "test_real_com_names.py::test_probe_com_names",
    "test_real_break_semantics.py::test_raw_break_at_and_break_disable_value",
    "test_real_break_semantics.py::test_stale_break_does_not_break_a_job",
    "test_real_break_semantics.py::test_pause_at_signal_state_and_resume",
    "test_real_stress.py::test_five_runs_of_an_hour",
    "test_real_stress.py::test_stop_takes_effect_within_one_chunk",
    "test_real_failures.py::test_killed_vissim_gives_a_clear_error_and_restart_recovers",
    "test_real_failures.py::test_modal_dialog_gives_timeout_hung_and_restart_recovers",
    "test_real_failures.py::test_killed_server_leaves_no_vissim",
    "test_real_failures.py::test_stdin_eof_exits_fast_and_closes_vissim",
    "test_real_encoding.py::test_non_ascii_path_round_trip",
}
PYWIN32_MODULES = {"pythoncom", "pywintypes", "win32com", "win32api", "win32gui", "win32process"}


def _pytest(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", "-p", "no:cacheprovider", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


def _ids(output: str) -> set[str]:
    return {
        line.strip().replace("tests/real/", "").replace("tests\\real\\", "")
        for line in output.splitlines()
        if "::" in line and "test_real_" in line
    }


def test_default_run_deselects_the_real_suite() -> None:
    result = _pytest("tests/real", "--collect-only", "-q")
    assert result.returncode == 5, result.stdout + result.stderr  # 5 = no tests collected
    assert _ids(result.stdout) == set()
    assert f"{len(EXPECTED)} deselected" in result.stdout


def test_marker_run_collects_every_plan_10_3_test() -> None:
    result = _pytest("tests/real", "-m", "vissim", "--collect-only", "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _ids(result.stdout) == EXPECTED


def test_without_the_license_option_everything_is_skipped() -> None:
    started = time.monotonic()
    result = _pytest("tests/real", "-m", "vissim", "-q", "-rs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{len(EXPECTED)} skipped" in result.stdout
    assert "--vissim-com-license" in result.stdout
    assert time.monotonic() - started < 60


def test_real_suite_never_imports_pywin32() -> None:
    for path in sorted(REAL_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in PYWIN32_MODULES, f"{path.name} imports {name}"
