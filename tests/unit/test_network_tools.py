"""vissim_call_method (network editing) against the fake Vissim.

The fake offers AddLink / AddVehicleInput / DuplicateLink / RemoveLink on every collection and New on the
root; the names and parameters of real Vissim 2026 are recorded in docs/com-names.md and exercised by
tests/real/test_real_build_network.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness
from vissim_mcp.errors import ErrorCode, VissimMcpError
from vissim_mcp.server import build_server
from vissim_mcp.tools import network
from vissim_mcp.tools import simulation as simulation_tools

MakeHarness = Callable[..., Harness]
TOOL = "vissim_call_method"


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
    return [record for record in records if record["tool"] == TOOL]


@pytest.fixture(autouse=True)
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simulation_tools, "WAIT_POLL_S", 0.02)


@pytest.fixture
async def setup(harness: MakeHarness) -> tuple[Harness, FastMCP]:
    h = harness()
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def test_schema_and_annotations(harness: MakeHarness) -> None:
    h = harness()
    tools = {tool.name: tool for tool in await build_server(h.service, h.settings).list_tools()}
    tool = tools[TOOL]
    assert list(tool.inputSchema["properties"]) == ["path", "method", "args", "dry_run"]
    assert tool.inputSchema["required"] == ["path", "method"]
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True
    assert "AddLink" in (tool.description or "") and "ref" in (tool.description or "")


async def test_describe_lists_the_method_parameters(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    described = await call(mcp, "vissim_describe", {"path": "Net.Links"})
    members = {(m["name"], m["kind"]): m for m in described["members"]}
    assert members[("AddLink", "method")]["params"] == ["Key", "WktLinestring", "LaneWidths"]
    assert members[("RemoveLink", "method")]["params"] == ["Link"]
    assert "params" not in members[("Count", "property_get")]


async def test_add_set_duplicate_and_remove_objects(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    before = len(h.vissim.collection("Links")._all())
    added = await call(
        mcp,
        TOOL,
        {"path": "Net.Links", "method": "AddLink", "args": [0, "LINESTRING(0 0, 200 0)", [3.5, 3.5]]},
    )
    assert added["ok"] is True and added["called"] is True, added
    key = added["returned"]["key"]
    assert added["returned"] == {
        "object": True,
        "interface": added["returned"]["interface"],
        "key": key,
        "path": f"Net.Links[{key}]",
    }
    assert added["args"] == [0, "LINESTRING(0 0, 200 0)", [3.5, 3.5]]
    assert h.vissim.attribute("Links", key, "NumLanes") == 2
    assert len(h.vissim.collection("Links")._all()) == before + 1

    # the returned path is usable at once
    named = await call(
        mcp, "vissim_set_attributes", {"path": added["returned"]["path"], "values": {"Name": "A"}}
    )
    assert named["ok"] is True and h.vissim.attribute("Links", key, "Name") == "A"

    # an object argument is {"ref": path}
    vehicle_input = await call(
        mcp,
        TOOL,
        {"path": "Net.VehicleInputs", "method": "AddVehicleInput", "args": [0, {"ref": f"Net.Links[{key}]"}]},
    )
    assert vehicle_input["ok"] is True, vehicle_input
    assert vehicle_input["args"] == [0, {"ref": f"Net.Links[{key}]"}]
    assert h.vissim.attribute("VehicleInputs", vehicle_input["returned"]["key"], "Link") == str(key)

    copy = await call(
        mcp,
        TOOL,
        {"path": "Net.Links", "method": "DuplicateLink", "args": [{"ref": f"Net.Links[{key}]"}, 777]},
    )
    assert copy["returned"]["key"] == 777 and h.vissim.attribute("Links", 777, "Name") == "A"

    removed = await call(
        mcp, TOOL, {"path": "Net.Links", "method": "RemoveLink", "args": [{"ref": "Net.Links[777]"}]}
    )
    assert removed["ok"] is True and removed["returned"] == {"value": None}
    assert h.vissim.collection("Links")._find(777) is None

    records = audit_records(isolated_env["audit"])
    assert [r["method"] for r in records] == ["AddLink", "AddVehicleInput", "DuplicateLink", "RemoveLink"]
    assert all(r["ok"] is True and r["called"] is True for r in records)
    assert records[0]["returned"]["key"] == key and records[0]["path"] == "Net.Links"


async def test_dry_run_checks_without_calling(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    before = len(h.vissim.collection("Links")._all())
    checked = await call(
        mcp,
        TOOL,
        {"path": "Net.Links", "method": "RemoveLink", "args": [{"ref": "Net.Links[1]"}], "dry_run": True},
    )
    assert checked["ok"] is True and checked["called"] is False and checked["dry_run"] is True
    assert "returned" not in checked and len(h.vissim.collection("Links")._all()) == before
    missing = await call(
        mcp,
        TOOL,
        {"path": "Net.Links", "method": "RemoveLink", "args": [{"ref": "Net.Links[99999]"}], "dry_run": True},
    )
    assert missing["error"]["code"] == "RESOLVE_FAILED"
    (first, second) = audit_records(isolated_env["audit"])
    assert first["called"] is False and first["dry_run"] is True and second["error_code"] == "RESOLVE_FAILED"


async def test_new_network_needs_a_save_before_a_simulation(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    new = await call(mcp, TOOL, {"path": "Vissim", "method": "New"})
    assert new["ok"] is True and new["path"] == "Vissim" and new["returned"] == {"value": None}
    assert any("save it with vissim_save_network" in warning for warning in new["warnings"])
    assert h.service.loaded_file is None and len(h.vissim.collection("Links")._all()) == 0

    await call(
        mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": [0, "LINESTRING(0 0, 9 0)", [3.5]]}
    )
    refused = await call(mcp, "vissim_start_simulation", {"runs": 1})
    assert refused["error"]["code"] == "INVALID_ARGUMENT", refused
    assert "has no file" in refused["error"]["message"]
    assert "vissim_save_network" in refused["error"]["hint"] and "Save as" in refused["error"]["hint"]

    saved = await call(mcp, "vissim_save_network", {"save_as": "built/net1.inpx"})
    assert saved["ok"] is True and (isolated_env["projects"] / "built" / "net1.inpx").is_file()
    started = await call(mcp, "vissim_start_simulation", {"runs": 1})
    assert started["ok"] is True, started
    await call(mcp, "vissim_stop_simulation")


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("Vissim", "LoadNet"),
        ("Vissim", "SaveNetAs"),
        ("Vissim", "Exit"),
        ("", "ImportANM"),
        ("Simulation", "RunContinuous"),
        ("Simulation", "Stop"),
        ("Net.Links", "GetMultipleAttributes"),
        ("Net.Links", "SetAllAttValues"),
        ("Net.Links", "ItemByKey"),
        ("Net.Links", "AddFromFile"),
        ("Net.Links[1]", "SetAttValue"),
    ],
)
async def test_other_methods_are_denied(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path], path: str, method: str
) -> None:
    h, mcp = setup
    calls = h.vissim.call_count
    denied = await call(mcp, TOOL, {"path": path, "method": method, "args": ["x"]})
    assert denied["error"]["code"] == "ATTRIBUTE_DENIED", denied
    assert "vissim_save_network" in denied["error"]["hint"]
    assert h.vissim.call_count == calls  # refused before any COM call
    (record,) = audit_records(isolated_env["audit"])
    assert record["ok"] is False and record["error_code"] == "ATTRIBUTE_DENIED" and record["called"] is False


async def test_invalid_arguments(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    cases: list[dict[str, Any]] = [
        {"path": "Net.Links", "method": "Add Link"},
        {"path": "Net.Links", "method": "_AddLink"},
        {"path": "Net.Links", "method": 5},
        {"path": 5, "method": "AddLink"},
        {"path": "Net.Links", "method": "AddLink", "args": "0"},
        {"path": "Net.Links", "method": "AddLink", "args": [{"key": 1}]},
        {"path": "Net.Links", "method": "AddLink", "args": [{"ref": 5}]},
        {"path": "Net.Links", "method": "AddLink", "args": [{"ref": "Vissim"}]},
        {"path": "Net.Links", "method": "AddLink", "args": [float("nan")]},
        {"path": "Net.Links", "method": "AddLink", "args": [[[[[1]]]]]},
        {"path": "Net.Links", "method": "AddLink", "args": list(range(network.MAX_ARGS + 1))},
        {"path": "Net.Links", "method": "AddNoSuchThing"},
        {"path": "Net.Links", "method": "AddLink", "dry_run": "perhaps"},
    ]
    for arguments in cases:
        result = await call(mcp, TOOL, arguments)
        assert result["error"]["code"] == "INVALID_ARGUMENT", (arguments, result)
    unknown = await call(mcp, TOOL, {"path": "Net.Links", "method": "AddNoSuchThing"})
    assert "has no method 'AddNoSuchThing'" in unknown["error"]["message"]
    assert "vissim_describe" in unknown["error"]["hint"]


async def test_vissim_errors_and_missing_paths(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    bad_wkt = await call(
        mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": [0, "POINT(0 0)", [3.5]]}
    )
    assert bad_wkt["error"]["code"] == "COM_ERROR" and "Net.Links.AddLink" in bad_wkt["error"]["message"]
    assert "vissim_describe" in bad_wkt["error"]["hint"]
    duplicate = await call(
        mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": [1, "LINESTRING(0 0, 1 0)", [3.5]]}
    )
    assert duplicate["error"]["code"] == "COM_ERROR"
    wrong_count = await call(mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": [0]})
    assert wrong_count["ok"] is False
    missing = await call(mcp, TOOL, {"path": "Net.Nope", "method": "AddLink", "args": []})
    assert missing["error"]["code"] == "RESOLVE_FAILED"
    records = audit_records(isolated_env["audit"])
    assert records[0]["called"] is True and records[0]["error_code"] == "COM_ERROR"
    assert records[-1]["called"] is False
    assert h.service.health()["connected"] is True


async def test_refused_while_a_job_is_active(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    started = await call(mcp, "vissim_start_simulation", {"runs": 1, "pause_at": [100]})
    assert started["ok"] is True
    for _ in range(200):
        status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 1})
        if status["state"] == "paused":
            break
    assert status["state"] == "paused"
    refused = await call(
        mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": [0, "LINESTRING(0 0, 1 0)", [3]]}
    )
    assert refused["error"]["code"] == "JOB_RUNNING"
    new = await call(mcp, TOOL, {"path": "Vissim", "method": "New"})
    assert new["error"]["code"] == "JOB_RUNNING"
    await call(mcp, "vissim_stop_simulation")


async def test_not_connected(harness: MakeHarness) -> None:
    h = harness()
    mcp = build_server(h.service, h.settings)
    result = await call(mcp, TOOL, {"path": "Net.Links", "method": "AddLink", "args": []})
    assert result["error"]["code"] == "NOT_CONNECTED"


def test_method_policy() -> None:
    for name in ("AddLink", "addconnector", "RemoveLink", "DuplicateNode", "GenerateOppositeDirection"):
        network.check_method_allowed(name, on_root=False)
    for name in ("RecalculateSpline", "InvertDirection", "MoveLinkDuringSimulation"):
        network.check_method_allowed(name, on_root=False)
    network.check_method_allowed("New", on_root=True)
    network.check_method_allowed("new", on_root=True)
    for name, on_root in (
        ("AddLink", True),
        ("GenerateModelTransferFile", True),
        ("GenerateModelTransferFile", False),
        ("AddFromFile", False),
        ("RunContinuous", False),
        ("OptimizeAll", False),
        ("New", False),
    ):
        with pytest.raises(VissimMcpError) as raised:
            network.check_method_allowed(name, on_root=on_root)
        assert raised.value.code is ErrorCode.ATTRIBUTE_DENIED


def test_argument_conversion() -> None:
    parsed = network.method_args(
        [0, "LINESTRING(0 0, 1 0)", [3.5, 3], {"ref": "Net.Links[1].Lanes[2]"}, None, True]
    )
    assert parsed[:3] == [0, "LINESTRING(0 0, 1 0)", (3.5, 3)]
    assert isinstance(parsed[3], network.Ref) and parsed[3].path == "Net.Links[1].Lanes[2]"
    assert parsed[4] is None and parsed[5] is True
    assert network.echo_args(parsed) == [
        0,
        "LINESTRING(0 0, 1 0)",
        [3.5, 3],
        {"ref": "Net.Links[1].Lanes[2]"},
        None,
        True,
    ]
    assert network.method_args(None) == []
