"""tools/lifecycle.py (PLAN 7.1): the five life-cycle tools through FastMCP, in process - schemas and
annotations, a happy path and at least one error path for every tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from fake_vissim import CO_E_SERVER_EXEC_FAILURE, RPC_S_CALL_FAILED, make_com_error
from service_harness import Harness
from vissim_mcp.errors import RESTART_HINT, ErrorCode, VissimMcpError
from vissim_mcp.server import build_server
from vissim_mcp.tools.lifecycle import read_info

LIFECYCLE_TOOLS = ["vissim_connect", "vissim_health", "vissim_info", "vissim_restart", "vissim_close"]
DEMO_COUNTS = {
    "Links": 10,
    "Nodes": 4,
    "SignalControllers": 2,
    "SignalHeads": 4,
    "VehicleInputs": 3,
    "VehicleRoutingDecisionsStatic": 2,
    "DataCollectionMeasurements": 3,
    "VehicleTravelTimeMeasurements": 2,
    "QueueCounters": 2,
}


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    payload: dict[str, Any] = json.loads(result.content[0].text)
    assert payload["ok"] is (not result.isError)
    return payload


@pytest.fixture
def setup(harness: Callable[..., Harness]) -> tuple[Harness, FastMCP]:
    h = harness()
    return h, build_server(h.service, h.settings)


async def test_the_lifecycle_tools_are_registered(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    listed = await mcp.list_tools()
    assert [tool.name for tool in listed[: len(LIFECYCLE_TOOLS)]] == LIFECYCLE_TOOLS  # registered first
    tools = {tool.name: tool for tool in listed if tool.name in LIFECYCLE_TOOLS}
    expected = {  # name: (readOnlyHint, destructiveHint)
        "vissim_connect": (False, False),
        "vissim_health": (True, False),
        "vissim_info": (True, False),
        "vissim_restart": (False, True),
        "vissim_close": (False, True),
    }
    for name, tool in tools.items():
        assert tool.annotations is not None
        assert (tool.annotations.readOnlyHint, tool.annotations.destructiveHint) == expected[name], name
        assert tool.description and len(tool.description) < 4096
        assert "Example: " + name in tool.description
    connect_schema = tools["vissim_connect"].inputSchema
    assert list(connect_schema["properties"]) == ["progid"]
    assert connect_schema["properties"]["progid"]["default"] is None
    assert connect_schema.get("required", []) == []
    for name in LIFECYCLE_TOOLS[1:]:
        assert tools[name].inputSchema.get("properties", {}) == {}


async def test_vissim_connect(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    invalid = await call(mcp, "vissim_connect", {"progid": "not a progid"})
    assert invalid["error"]["code"] == "INVALID_ARGUMENT"

    payload = await call(mcp, "vissim_connect")
    assert payload["ok"] is True
    assert payload["pid"] == h.service.pid is not None
    assert payload["progid"] == "Vissim.Vissim" and payload["job_object"] is True
    assert payload["generation"] == 1 and "warnings" not in payload  # an empty list is dropped

    again = await call(mcp, "vissim_connect")
    assert again["error"]["code"] == "ALREADY_CONNECTED"
    assert "vissim_restart" in again["error"]["hint"]


async def test_vissim_connect_reports_a_failed_start(harness: Callable[..., Harness]) -> None:
    h = harness(dispatch_error=CO_E_SERVER_EXEC_FAILURE)
    payload = await call(build_server(h.service, h.settings), "vissim_connect")
    assert payload["error"]["code"] == "COM_ERROR"
    assert payload["error"]["details"]["hresult_hex"] == "0x80080005"
    assert "license" in payload["error"]["hint"].lower()


async def test_vissim_health(setup: tuple[Harness, FastMCP], monkeypatch: pytest.MonkeyPatch) -> None:
    h, mcp = setup
    before = await call(mcp, "vissim_health")
    assert before["connected"] is False and before["generation"] == 0 and before["pid"] is None
    await call(mcp, "vissim_connect")
    after = await call(mcp, "vissim_health")
    assert after["connected"] is True and after["pid"] == h.service.pid and after["generation"] == 1
    assert after["vissim_alive"] is True and after["busy_seconds"] == 0.0

    def broken() -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(h.service, "health", broken)
    failed = await call(mcp, "vissim_health")
    assert failed["error"]["code"] == "INTERNAL"
    assert failed["error"]["message"] == "internal error in vissim_health: RuntimeError: boom"


async def test_vissim_info(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    not_connected = await call(mcp, "vissim_info")
    assert not_connected["error"]["code"] == "NOT_CONNECTED"
    assert "vissim_connect" in not_connected["error"]["hint"]

    await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_info")
    assert payload["counts"] == DEMO_COUNTS
    assert payload["SimPeriod"] == 3600 and payload["SimRes"] == 10
    assert payload["loaded_file"] is None and payload["pid"] == h.service.pid
    assert payload["progid"] == "Vissim.Vissim" and "errors" not in payload


async def test_vissim_info_reports_unreadable_values_one_by_one(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    await call(mcp, "vissim_connect")
    del h.vissim._net._collections["QueueCounters"]  # this collection cannot be read
    del h.vissim._simulation._attrs["simres"]  # neither can this attribute
    payload = await call(mcp, "vissim_info")
    assert payload["ok"] is True
    assert payload["counts"]["QueueCounters"] is None and payload["counts"]["Links"] == 10
    assert payload["SimRes"] is None and payload["SimPeriod"] == 3600
    assert set(payload["errors"]) == {"QueueCounters", "Simulation.SimRes"}
    assert "unknown attribute 'SimRes'" in payload["errors"]["Simulation.SimRes"]


async def test_vissim_info_after_a_crash(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    await call(mcp, "vissim_connect")
    h.vissim.crash()
    payload = await call(mcp, "vissim_info")
    assert payload["error"]["code"] == "COM_ERROR" and payload["error"]["hint"] == RESTART_HINT
    assert (await call(mcp, "vissim_health"))["connected"] is False


@pytest.mark.parametrize(
    ("ok_calls", "failing_call"),
    # calls that still work: Net, Net.Links | Net, 9 x (Net.<collection>, <collection>.Count), Simulation
    [(2, "Links.Count"), (20, "Simulation.AttValue")],
    ids=["collection-loop", "attribute-loop"],
)
async def test_vissim_info_never_suppresses_a_lost_connection_inside_its_loops(
    setup: tuple[Harness, FastMCP], ok_calls: int, failing_call: str
) -> None:
    """PLAN 7.1: each value is read "with suppress" - but a disconnect is raised, not turned into null."""
    h, mcp = setup
    await call(mcp, "vissim_connect")
    h.vissim.crash_after_calls = h.vissim.call_count + ok_calls
    payload = await call(mcp, "vissim_info")
    assert h.vissim.calls[-1][0] == failing_call
    error = payload["error"]
    assert error["code"] == "COM_ERROR" and error["hint"] == RESTART_HINT
    assert error["details"]["disconnected"] is True
    assert (await call(mcp, "vissim_health"))["connected"] is False


async def test_vissim_info_raises_a_call_that_failed_in_transit(setup: tuple[Harness, FastMCP]) -> None:
    """RPC_S_CALL_FAILED may mean that Vissim is gone, so it is never reported as one unreadable count."""

    class LostInTransit:
        @property
        def Count(self) -> int:
            raise make_com_error(RPC_S_CALL_FAILED)

    h, mcp = setup
    await call(mcp, "vissim_connect")
    h.vissim._net._collections["Nodes"] = LostInTransit()  # type: ignore[assignment]
    payload = await call(mcp, "vissim_info")
    assert payload["error"]["code"] == "COM_ERROR"
    assert payload["error"]["details"]["hresult"] == RPC_S_CALL_FAILED
    assert (await call(mcp, "vissim_health"))["connected"] is True  # the Vissim process is still alive


def test_read_info_needs_the_com_root() -> None:
    with pytest.raises(VissimMcpError) as info:
        read_info(SimpleNamespace(vissim=None))  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.NOT_CONNECTED


async def test_vissim_restart(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    first = await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_restart")
    assert payload["generation"] == 2 and payload["previous_pid"] == first["pid"]
    assert payload["killed"] is True and payload["aborted_job_id"] is None
    assert payload["pid"] == h.service.pid != first["pid"]

    h.backends.backend_options["dispatch_error"] = CO_E_SERVER_EXEC_FAILURE  # the next start fails
    failed = await call(mcp, "vissim_restart")
    assert failed["error"]["code"] == "COM_ERROR"
    assert (await call(mcp, "vissim_health"))["connected"] is False

    h.service.shutdown_fast()
    shutting_down = await call(mcp, "vissim_restart")
    assert shutting_down["error"]["code"] == "SHUTTING_DOWN"


async def test_vissim_close(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    nothing = await call(mcp, "vissim_close")
    assert nothing["error"]["code"] == "NOT_CONNECTED"
    connected = await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_close")
    assert payload == {"ok": True, "closed": True, "method": "exit", "pid": connected["pid"]}
    health = await call(mcp, "vissim_health")
    assert health["connected"] is False and health["pid"] is None
