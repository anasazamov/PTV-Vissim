"""PLAN 10.3 item 2 - COM names: scripts/probe_com_names.py against a real Vissim.

The probe runs as a subprocess with real pywin32 (never inside the pytest process, which keeps the fakes).
It does not rewrite the docs here; ``--update`` is a separate step of docs/m6-runbook.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import PROBE_SCRIPT, REPO_ROOT, real_server_env

pytestmark = pytest.mark.vissim

#: Names the server code relies on (not only reaches through the generic get/set tools).
REQUIRED = (
    "`Vissim.LoadNet(path, additive)`",
    "`Vissim.SaveNetAs(path)`",
    "`Vissim.Exit()`",
    "`Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics`",
    "`Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()`",
    "`<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)`",
    "`<collection>.GetMultipleAttributes(tuple)`",
    "`<obj>.AttValue(name)` / `.SetAttValue(name, value)`",
    "`SimPeriod`",
    "`SimRes`",
    "`SimSec`",
    "`SimBreakAt`",
    "`VISSIM_SEED_ATTR`",
    "`VISSIM_BREAK_DISABLE_VALUE`",
)


def test_probe_com_names(request: pytest.FixtureRequest, tmp_path: Path, record: Callable[..., None]) -> None:
    workdir = tmp_path / "probe"
    env = real_server_env(tmp_path, progid=request.config.getoption("vissim_progid"))
    env = {k: v for k, v in env.items() if not k.startswith("VISSIM_")}  # the probe sets its own
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(PROBE_SCRIPT),
            "--i-have-a-com-license",
            "--model",
            request.config.getoption("vissim_model"),
            "--progid",
            request.config.getoption("vissim_progid"),
            "--workdir",
            str(workdir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(REPO_ROOT),
        timeout=1800,
    )
    results_file = workdir / "probe-results.json"
    assert results_file.is_file(), f"no probe results (exit {result.returncode}):\n{result.stderr[-4000:]}"
    report = json.loads(results_file.read_text(encoding="utf-8"))
    statuses = {row["name"]: row["status"] for row in report["probes"]}
    record(
        probe_exit=result.returncode,
        aborted=report.get("aborted"),
        statuses=statuses,
        semantics=report.get("semantics"),
        close=report.get("close"),
        process=report.get("process_after_runs"),
        results_file=str(results_file),
    )
    print(result.stdout)
    assert not report.get("aborted"), report["aborted"]
    broken = {name: statuses.get(name) for name in REQUIRED if statuses.get(name) != "ok"}
    assert not broken, f"names the server relies on did not work: {broken}\n{result.stdout}"
    assert result.returncode in (0, 1), result.stderr[-4000:]
