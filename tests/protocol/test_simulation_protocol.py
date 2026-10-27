"""Protocol test for the simulation and result tools (PLAN 10.2, 11 row M4).

The server runs as ``python -X utf8 -m vissim_mcp`` with the fake pywin32 modules: configure -> start (two
runs) -> wait/status until finished -> export the runs to CSV; stdout stays pure JSON-RPC.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from protocol_helpers import ALL_TOOLS, finish, handshake

from stdio_client import StdioServer

StartServer = Callable[..., StdioServer]
SAFE_ENV = {
    "VISSIM_EXE_PATTERN": r"^vissim_mcp_test_standin\d*\.exe$",
    "VISSIM_PID_DETECT_S": "0.3",
    "VISSIM_WAIT_MAX_S": "5",
}


def test_simulation_job_through_stdio(start_server: StartServer, isolated_env: dict[str, Path]) -> None:
    (isolated_env["projects"] / "results").mkdir()
    server = start_server(env=SAFE_ENV)
    tools = handshake(server)
    assert list(tools) == ALL_TOOLS
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True

    configured = server.call_tool("vissim_configure_simulation", {"period_s": 300, "seed": 5})["payload"]
    assert configured["ok"] is True and configured["period_s"] == 300 and configured["seed"] == 5

    started = server.call_tool(
        "vissim_start_simulation", {"runs": 2, "seed": 5, "seed_increment": 10, "chunk_sim_s": 60}
    )["payload"]
    assert started["ok"] is True, started
    assert started["seeds"] == [5, 15] and started["period"] == 300

    deadline = time.monotonic() + 30
    status = started
    while time.monotonic() < deadline:
        status = server.call_tool(
            "vissim_wait_simulation", {"job_id": started["job_id"], "max_wait_s": 60}, timeout=15
        )["payload"]
        assert status["ok"] is True and status["waited_s"] <= 5.5
        if status["state"] != "running":
            break
    assert status["state"] == "finished", status
    assert status["percent"] == 100.0 and status["run"] == 2

    final = server.call_tool("vissim_simulation_status")["payload"]
    assert final["state"] == "finished" and final["job_id"] == started["job_id"]
    health = server.call_tool("vissim_health")["payload"]
    assert health["job"]["state"] == "finished"

    exported = server.call_tool(
        "vissim_export_csv",
        {
            "collection": "Net.SimulationRuns",
            "attributes": ["No", "RandSeed"],
            "csv_path": "results/runs.csv",
        },
    )["payload"]
    assert exported["ok"] is True and exported["rows"] == 2
    csv_file = isolated_env["projects"] / "results" / "runs.csv"
    assert csv_file.read_text(encoding="utf-8-sig").splitlines() == ["No;RandSeed", "1;5", "2;15"]
    finish(server)
