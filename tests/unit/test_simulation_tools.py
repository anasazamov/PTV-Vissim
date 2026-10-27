"""tools/simulation.py (PLAN 7.4) in process: the job runs on the real COM worker against FakeVissim.

Covers start -> wait -> finished, the VISSIM_WAIT_MAX_S cap, calls served between chunks, resume/stop through
the tools, configure, restart/close aborting the job (close stops the simulation before Exit) and a lost
Vissim during a job.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness, eventually
from vissim_mcp.server import build_server
from vissim_mcp.tools import simulation as simulation_tools

MakeHarness = Callable[..., Harness]


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    payload: dict[str, Any] = json.loads(result.content[0].text)
    assert payload["ok"] is (not result.isError)
    return payload


def audit_records(audit_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file in sorted(audit_dir.glob("audit-*.jsonl")):
        records.extend(json.loads(line) for line in file.read_text(encoding="utf-8").splitlines())
    return records


@pytest.fixture(autouse=True)
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simulation_tools, "WAIT_POLL_S", 0.02)


async def connected(
    harness: MakeHarness, env: dict[str, str] | None = None, **options: Any
) -> tuple[Harness, FastMCP]:
    h = harness(env, **options)
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def wait_until_done(mcp: FastMCP, job_id: str | None = None, rounds: int = 50) -> dict[str, Any]:
    for _ in range(rounds):
        status = await call(mcp, "vissim_wait_simulation", {"job_id": job_id, "max_wait_s": 5})
        if status["state"] != "running":
            return status
    raise AssertionError("the job did not leave the running state")


async def test_tool_schemas(harness: MakeHarness) -> None:
    h = harness()
    tools = {tool.name: tool for tool in await build_server(h.service, h.settings).list_tools()}
    start = tools["vissim_start_simulation"]
    assert list(start.inputSchema["properties"]) == [
        "runs",
        "seed",
        "seed_increment",
        "chunk_sim_s",
        "pause_at",
    ]
    assert start.inputSchema.get("required", []) == []
    for name in ("vissim_simulation_status", "vissim_wait_simulation"):
        assert tools[name].annotations is not None and tools[name].annotations.readOnlyHint is True
    configure = tools["vissim_configure_simulation"]
    assert list(configure.inputSchema["properties"]) == [
        "period_s", "resolution", "seed", "max_speed", "quick_mode", "suspend_gui", "extra",
    ]  # fmt: skip


async def test_start_wait_finished(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 300)
    started = await call(mcp, "vissim_start_simulation", {"runs": 3, "seed": 21, "seed_increment": 2})
    assert started["ok"] is True and started["state"] == "running"
    assert started["seeds"] == [21, 23, 25] and started["period"] == 300 and started["runs"] == 3
    assert any(w.startswith("ensure evaluation keeps previous runs") for w in started["warnings"])
    status = await wait_until_done(mcp, started["job_id"])
    assert status["state"] == "finished" and status["percent"] == 100.0 and status["run"] == 3
    assert status["job_id"] == started["job_id"] and "waited_s" in status
    assert [record["RandSeed"] for record in h.vissim.run_records] == [21, 23, 25]
    again = await call(mcp, "vissim_simulation_status")
    assert again["state"] == "finished" and again["seeds"] == [21, 23, 25]
    health = h.service.health()["job"]
    assert health["job_id"] == started["job_id"] and health["state"] == "finished"
    (record,) = [r for r in audit_records(isolated_env["audit"]) if r["tool"] == "vissim_start_simulation"]
    assert record["ok"] is True and record["job_id"] == started["job_id"] and record["seeds"] == [21, 23, 25]


async def test_start_errors(harness: MakeHarness) -> None:
    h = harness()
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_start_simulation"))["error"]["code"] == "NOT_CONNECTED"
    assert (await call(mcp, "vissim_start_simulation", {"runs": 0}))["error"]["code"] == "INVALID_ARGUMENT"
    assert (await call(mcp, "vissim_simulation_status"))["error"]["code"] == "JOB_NOT_FOUND"
    assert (await call(mcp, "vissim_wait_simulation"))["error"]["code"] == "JOB_NOT_FOUND"
    await call(mcp, "vissim_connect")
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    bad_pause = await call(mcp, "vissim_start_simulation", {"pause_at": [600]})
    assert bad_pause["error"]["code"] == "INVALID_ARGUMENT"
    first = await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    await wait_until_done(mcp)
    second = await call(mcp, "vissim_start_simulation")
    assert second["error"]["code"] == "JOB_RUNNING" and first["job_id"] in second["error"]["message"]
    missing = await call(mcp, "vissim_simulation_status", {"job_id": "sim-nope"})
    assert missing["error"]["code"] == "JOB_NOT_FOUND"


async def test_seed_none_with_several_runs_through_the_tool(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, {"VISSIM_SEED_ATTR": "NoSuchSeed"})
    refused = await call(mcp, "vissim_start_simulation", {"runs": 2})
    assert refused["error"] == {
        "code": "INVALID_ARGUMENT",
        "message": "seed required for runs>1",
        "hint": refused["error"]["hint"],
    }
    assert h.service.jobs is not None and h.service.jobs.current is None


async def test_wait_never_exceeds_vissim_wait_max_s(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, {"VISSIM_WAIT_MAX_S": "0.4"}, wall_s_per_sim_s=0.002)
    h.vissim.set_simulation_attribute("SimPeriod", 3600)
    await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 10})
    started = time.monotonic()
    status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 60})
    elapsed = time.monotonic() - started
    assert status["state"] == "running"
    assert elapsed < 1.0 and status["waited_s"] <= 0.4 + 0.2
    assert "max_wait_s 60 was reduced to 0.4 (VISSIM_WAIT_MAX_S)" in status["warnings"]
    zero = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 0})
    assert zero["waited_s"] < 0.1
    assert (await call(mcp, "vissim_wait_simulation", {"max_wait_s": -1}))["error"][
        "code"
    ] == "INVALID_ARGUMENT"
    stop = await call(mcp, "vissim_stop_simulation")
    assert stop["stopping"] is True
    assert (await wait_until_done(mcp))["state"] == "stopped"


async def test_wait_returns_as_soon_as_the_job_pauses(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    await call(mcp, "vissim_start_simulation", {"pause_at": [120, 480]})
    status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 30})
    assert status["state"] == "paused" and status["paused_at"] == 120 and status["waited_s"] < 5
    assert h.vissim.sim_active and h.vissim.sim_sec == 120
    # attributes can be changed while paused (e.g. signal states in M5)
    values = await call(mcp, "vissim_get_attributes", {"path": "Simulation", "attributes": ["SimSec"]})
    assert values["values"] == {"SimSec": 120}
    resumed = await call(mcp, "vissim_resume_simulation")
    assert resumed["state"] in ("running", "paused")
    status = await wait_until_done(mcp)
    assert status["state"] == "paused" and status["paused_at"] == 480
    again = await call(mcp, "vissim_resume_simulation")
    assert again["ok"] is True
    assert (await wait_until_done(mcp))["state"] == "finished"
    not_paused = await call(mcp, "vissim_resume_simulation")
    assert not_paused["error"]["code"] == "INVALID_ARGUMENT"


async def test_stop_a_paused_job(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    await call(mcp, "vissim_start_simulation", {"pause_at": [100]})
    assert (await wait_until_done(mcp))["state"] == "paused"
    stopped = await call(mcp, "vissim_stop_simulation")
    assert stopped["stopping"] is True and stopped["state"] == "stopped"
    assert h.vissim.run_records[-1]["stopped"] is True and not h.vissim.sim_active
    nothing = await call(mcp, "vissim_stop_simulation")
    assert nothing["stopping"] is False and "already stopped" in nothing["warnings"][0]
    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] == "vissim_stop_simulation"]
    assert [r["state_after"] for r in records] == ["stopped", "stopped"]


async def test_a_tool_call_during_a_job_is_served_between_chunks(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, wall_s_per_sim_s=0.002)
    h.vissim.set_simulation_attribute("SimPeriod", 3600)  # about 7 s of wall time in 60 s chunks of 0.12 s
    await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 60})
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    started = time.monotonic()
    values = await call(mcp, "vissim_get_attributes", {"path": "Net.Links[1]", "attributes": ["Name"]})
    assert values["values"] == {"Name": "Link 1"}
    assert time.monotonic() - started < 1.5
    status = await call(mcp, "vissim_simulation_status")
    assert status["state"] == "running" and 0 < status["percent"] < 100
    await call(mcp, "vissim_stop_simulation")
    assert (await wait_until_done(mcp))["state"] == "stopped"


async def test_configure_simulation(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    result = await call(
        mcp,
        "vissim_configure_simulation",
        {"period_s": 900, "resolution": 5, "seed": 17, "extra": {"NumCores": 2}},
    )
    assert result["ok"] is True, result
    assert (result["period_s"], result["resolution"], result["seed"]) == (900, 5, 17)
    assert result["failed"] == [] and result.get("warnings", []) == []
    assert h.vissim.simulation_attribute("UseMaxSimSpeed") is True
    assert h.vissim.simulation_attribute("NumCores") == 2
    assert h.vissim._graphics._window._get("QuickMode") == 1
    assert h.vissim.gui_suspended is True

    resumed = await call(
        mcp, "vissim_configure_simulation", {"max_speed": False, "quick_mode": False, "suspend_gui": False}
    )
    assert resumed["period_s"] == 900 and "ResumeUpdateGUI" in resumed["applied"]
    assert h.vissim.gui_suspended is False and h.vissim.simulation_attribute("UseMaxSimSpeed") is False

    partial = await call(mcp, "vissim_configure_simulation", {"extra": {"NoSuchAttribute": 1}})
    assert partial["ok"] is True and partial["failed"][0]["attribute"] == "Simulation.NoSuchAttribute"
    assert partial["warnings"] == ["1 setting(s) failed; see failed"]

    denied = await call(mcp, "vissim_configure_simulation", {"extra": {"EvalOutputFile": "x"}})
    assert denied["error"]["code"] == "ATTRIBUTE_DENIED"
    for bad in ({"period_s": 0}, {"resolution": "fast"}, {"seed": 0}, {"extra": [1]}, {"max_speed": "maybe"}):
        assert (await call(mcp, "vissim_configure_simulation", bad))["error"]["code"] == "INVALID_ARGUMENT"

    await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    await wait_until_done(mcp)
    busy = await call(mcp, "vissim_configure_simulation", {"period_s": 100})
    assert busy["error"]["code"] == "JOB_RUNNING"
    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] == "vissim_configure_simulation"]
    assert len(records) == 10
    assert records[0]["ok"] is True and records[0]["new"]["SimPeriod"] == 900
    assert records[-1]["error_code"] == "JOB_RUNNING"


async def test_restart_aborts_the_job(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    started = await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    await wait_until_done(mcp)
    restarted = await call(mcp, "vissim_restart")
    assert restarted["aborted_job_id"] == started["job_id"]
    status = await call(mcp, "vissim_simulation_status")
    assert status["state"] == "aborted" and status["abort_reason"] == "Vissim was restarted"
    assert (await call(mcp, "vissim_resume_simulation"))["error"]["code"] == "INVALID_ARGUMENT"
    fresh = await call(mcp, "vissim_start_simulation")  # the new connection takes a new job
    assert fresh["ok"] is True and fresh["job_id"] != started["job_id"]
    await call(mcp, "vissim_stop_simulation")


async def test_close_aborts_the_job_and_stops_the_simulation_before_exit(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, wall_s_per_sim_s=0.001)
    vissim = h.vissim
    vissim.set_simulation_attribute("SimPeriod", 3600)
    started = await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 60})
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    closed = await call(mcp, "vissim_close")
    assert closed["ok"] is True and closed["aborted_job_id"] == started["job_id"]
    members = [member for member, _thread in vissim.calls]
    assert "Simulation.Stop" in members and "Vissim.Exit" in members
    assert members.index("Simulation.Stop") < members.index("Vissim.Exit")
    assert vissim.run_records[-1]["stopped"] is True
    status = await call(mcp, "vissim_simulation_status")
    assert status["state"] == "aborted" and status["abort_reason"] == "Vissim was closed"


async def test_a_lost_vissim_fails_the_job_and_the_connection(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, wall_s_per_sim_s=0.001)
    h.vissim.set_simulation_attribute("SimPeriod", 3600)
    await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 60})
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    h.vissim.crash()
    status = await wait_until_done(mcp)
    assert status["state"] == "error" and status["error"]["code"] == "COM_ERROR"
    assert status["error"]["details"]["disconnected"] is True
    await eventually(lambda: not h.service.connected)
    assert (await call(mcp, "vissim_health"))["connected"] is False


async def test_destructive_annotations(harness: MakeHarness) -> None:
    h = harness()
    tools = {tool.name: tool for tool in await build_server(h.service, h.settings).list_tools()}
    for name in ("vissim_start_simulation", "vissim_stop_simulation", "vissim_export_csv"):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.destructiveHint is True, name


async def test_stop_and_resume_of_an_unknown_job(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    for name in ("vissim_stop_simulation", "vissim_resume_simulation"):
        assert (await call(mcp, name))["error"]["code"] == "JOB_NOT_FOUND"
        assert (await call(mcp, name, {"job_id": "sim-nope"}))["error"]["code"] == "JOB_NOT_FOUND"


async def test_a_paused_job_after_the_connection_was_lost(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    started = await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    assert (await wait_until_done(mcp))["state"] == "paused"
    h.vissim.crash()
    lost = await call(mcp, "vissim_get_attributes", {"path": "Simulation", "attributes": ["SimSec"]})
    assert lost["ok"] is False and not h.service.connected
    status = await call(mcp, "vissim_simulation_status")
    assert status["state"] == "paused" and simulation_tools.LOST_CONNECTION_WARNING in status["warnings"]
    resumed = await call(mcp, "vissim_resume_simulation")
    assert resumed["error"]["code"] == "NOT_CONNECTED"
    stopped = await call(mcp, "vissim_stop_simulation", {"job_id": started["job_id"]})
    assert stopped["ok"] is True and stopped["stopping"] is True and stopped["state"] == "aborted"
    assert simulation_tools.LOST_CONNECTION_WARNING in stopped["warnings"]


async def test_resume_of_a_job_of_a_previous_connection(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    assert (await wait_until_done(mcp))["state"] == "paused"
    job = h.service.jobs.current  # type: ignore[union-attr]
    job.generation -= 1  # as if a restart had not reached this job
    refused = await call(mcp, "vissim_resume_simulation")
    assert refused["error"]["code"] == "NOT_CONNECTED"
    assert "previous Vissim connection" in refused["error"]["message"]
    assert job.state.value == "paused"
    job.generation += 1
    await call(mcp, "vissim_stop_simulation")


async def test_wait_returns_at_once_when_the_connection_is_lost(harness: MakeHarness) -> None:
    h, mcp = await connected(harness, wall_s_per_sim_s=0.002)
    h.vissim.set_simulation_attribute("SimPeriod", 3600)
    await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 10})
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    h.service.connected = False  # what a failed call does; the job is not ticked any more
    status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 5})
    assert status["state"] == "running" and status["waited_s"] < 1.0
    assert simulation_tools.LOST_CONNECTION_WARNING in status["warnings"]
    h.service.connected = True
    await call(mcp, "vissim_stop_simulation")
    assert (await wait_until_done(mcp))["state"] == "stopped"


async def test_status_never_calls_com(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    await call(mcp, "vissim_start_simulation", {"pause_at": [60]})
    await wait_until_done(mcp)
    before = h.vissim.call_count
    for _ in range(5):
        await call(mcp, "vissim_simulation_status")
        await call(mcp, "vissim_wait_simulation", {"max_wait_s": 0})
    await asyncio.sleep(0.05)
    assert h.vissim.call_count == before  # the paused job is not ticked either
    await call(mcp, "vissim_stop_simulation")
