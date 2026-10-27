"""tools/signals.py (PLAN 7.5): vissim_set_signal_state in process against FakeVissim.

Covers the state write (ContrByCOM switched on by Vissim), the release, the JOB_RUNNING refusal while a job
runs (allowed while paused and without a job), unknown controller/group, argument errors and the audit
records.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from fake_vissim import SIG_STATES
from service_harness import Harness, eventually
from vissim_mcp.server import build_server
from vissim_mcp.tools import signals
from vissim_mcp.tools import simulation as simulation_tools

MakeHarness = Callable[..., Harness]
SGS = "SignalControllers[1].SGs"


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    payload: dict[str, Any] = json.loads(result.content[0].text)
    assert payload["ok"] is (not result.isError)
    return payload


def audit_records(audit_dir: Path, tool: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file in sorted(audit_dir.glob("audit-*.jsonl")):
        records.extend(json.loads(line) for line in file.read_text(encoding="utf-8").splitlines())
    return [record for record in records if record["tool"] == tool]


@pytest.fixture(autouse=True)
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simulation_tools, "WAIT_POLL_S", 0.02)


async def connected(harness: MakeHarness, **options: Any) -> tuple[Harness, FastMCP]:
    h = harness(None, **options)
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def test_schema_and_annotations(harness: MakeHarness) -> None:
    h = harness()
    tools = {tool.name: tool for tool in await build_server(h.service, h.settings).list_tools()}
    tool = tools["vissim_set_signal_state"]
    assert list(tool.inputSchema["properties"]) == ["sc_no", "sg_no", "state", "release_to_controller"]
    assert sorted(tool.inputSchema["required"]) == ["sc_no", "sg_no"]
    assert tool.annotations is not None and tool.annotations.readOnlyHint is False
    assert tool.description and len(tool.description) < 4096


async def test_set_state_without_job(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    assert h.vissim.attribute(SGS, 2, "ContrByCOM") is False
    result = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 2, "state": " green "})
    assert result["ok"] is True, result
    assert result["SigState"] == "GREEN" and result["ContrByCOM"] is True
    assert result["old"] == {"SigState": "RED", "ContrByCOM": False}
    assert result["path"] == "Net.SignalControllers[1].SGs[2]" and not result.get("warnings")
    assert h.vissim.attribute(SGS, 2, "SigState") == "GREEN"
    assert h.vissim.attribute(SGS, 2, "ContrByCOM") is True
    assert h.vissim.attribute(SGS, 1, "SigState") == "RED"  # other groups untouched
    (record,) = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert record["ok"] is True and record["path"] == "Net.SignalControllers[1].SGs[2]"
    assert record["args"]["state"] == " green "
    assert record["old"] == {"SigState": "RED", "ContrByCOM": False}
    assert record["new"] == {"SigState": "GREEN", "ContrByCOM": True}
    assert record["written"] == ["SigState"]


async def test_integer_state_is_passed_through(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    result = await call(mcp, "vissim_set_signal_state", {"sc_no": 2, "sg_no": 4, "state": 3})
    assert result["ok"] is True and result["ContrByCOM"] is True
    assert (
        result["SigState"] == SIG_STATES[2] == h.vissim.attribute("SignalControllers[2].SGs", 4, "SigState")
    )


async def test_release_to_controller(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "state": "AMBER"})
    released = await call(
        mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "release_to_controller": True}
    )
    assert released["ok"] is True, released
    assert released["ContrByCOM"] is False and released["SigState"] == "AMBER"
    assert released["old"] == {"SigState": "AMBER", "ContrByCOM": True}
    assert h.vissim.attribute(SGS, 1, "ContrByCOM") is False
    records = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert [r["written"] for r in records] == [["SigState"], ["ContrByCOM"]]
    assert records[1]["new"] == {"SigState": "AMBER", "ContrByCOM": False}


async def test_argument_errors_are_audited(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = await connected(harness)
    cases: list[dict[str, Any]] = [
        {"sc_no": 1, "sg_no": 1},  # neither state nor release
        {"sc_no": 1, "sg_no": 1, "state": "RED", "release_to_controller": True},
        {"sc_no": 0, "sg_no": 1, "state": "RED"},
        {"sc_no": True, "sg_no": 1, "state": "RED"},
        {"sc_no": "1", "sg_no": 1, "state": "RED"},
        {"sc_no": 1, "sg_no": 1.5, "state": "RED"},
        {"sc_no": 1, "sg_no": 1, "state": "  "},
        {"sc_no": 1, "sg_no": 1, "state": False},
        {"sc_no": 1, "sg_no": 1, "state": [1]},
        {"sc_no": 1, "sg_no": 1, "state": "RED", "release_to_controller": "maybe"},
    ]
    for arguments in cases:
        result = await call(mcp, "vissim_set_signal_state", arguments)
        assert result["error"]["code"] == "INVALID_ARGUMENT", (arguments, result)
    records = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert len(records) == len(cases)
    assert all(r["ok"] is False and r["error_code"] == "INVALID_ARGUMENT" for r in records)
    assert h.vissim.attribute(SGS, 1, "ContrByCOM") is False


async def test_unknown_controller_or_group(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    _h, mcp = await connected(harness)
    for sc_no, sg_no in ((9, 1), (1, 9)):
        result = await call(mcp, "vissim_set_signal_state", {"sc_no": sc_no, "sg_no": sg_no, "state": "RED"})
        assert result["error"]["code"] == "RESOLVE_FAILED", result
    records = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert [r["error_code"] for r in records] == ["RESOLVE_FAILED", "RESOLVE_FAILED"]
    assert records[0]["old"] is None and records[0]["written"] == []


async def test_invalid_state_is_a_com_error_with_old_values_audited(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(harness)
    result = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 3, "state": "PURPLE"})
    assert result["error"]["code"] == "COM_ERROR", result
    assert "SigState" in result["error"]["message"] and "GREEN" in result["error"]["hint"]
    assert h.vissim.attribute(SGS, 3, "SigState") == "RED"
    assert h.vissim.attribute(SGS, 3, "ContrByCOM") is False
    (record,) = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert record["ok"] is False and record["error_code"] == "COM_ERROR"
    assert record["old"] == {"SigState": "RED", "ContrByCOM": False} and record["written"] == []


async def test_not_connected(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h = harness()
    mcp = build_server(h.service, h.settings)
    result = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "state": "RED"})
    assert result["error"]["code"] == "NOT_CONNECTED"
    (record,) = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert record["error_code"] == "NOT_CONNECTED"


async def test_refused_while_running_allowed_while_paused(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(harness, wall_s_per_sim_s=0.002)
    h.vissim.set_simulation_attribute("SimPeriod", 3600)
    started = await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 60, "pause_at": [1800]})
    assert started["ok"] is True
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    refused = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "state": "GREEN"})
    assert refused["error"]["code"] == "JOB_RUNNING", refused
    assert refused["error"]["details"]["job_id"] == started["job_id"]
    assert h.vissim.attribute(SGS, 1, "SigState") == "RED"

    status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 30})
    assert status["state"] == "paused" and status["paused_at"] == 1800
    allowed = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "state": "GREEN"})
    assert allowed["ok"] is True and allowed["SigState"] == "GREEN" and allowed["ContrByCOM"] is True
    assert h.vissim.sim_active and h.vissim.sim_sec == 1800

    await call(mcp, "vissim_stop_simulation")
    after = await call(
        mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "release_to_controller": True}
    )
    assert after["ok"] is True and after["ContrByCOM"] is False
    codes = [r["error_code"] for r in audit_records(isolated_env["audit"], "vissim_set_signal_state")]
    assert codes == ["JOB_RUNNING", None, None]


async def test_worker_side_check_catches_a_resumed_job(
    harness: MakeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job resumed between the tool's check and the COM task is still refused (on the worker)."""
    h, mcp = await connected(harness)
    calls = {"n": 0}

    class Running:
        id = "sim-fake"
        state = "running"

    def flaky(service: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else Running()

    monkeypatch.setattr(signals, "running_job", flaky)
    result = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, "state": "GREEN"})
    assert result["error"]["code"] == "JOB_RUNNING" and calls["n"] == 2
    assert h.vissim.attribute(SGS, 1, "SigState") == "RED"
