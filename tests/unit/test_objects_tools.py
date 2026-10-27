"""tools/objects.py (PLAN 7.3, 6.5, 6.7, 6.8): describe, list, get, set (dry_run) and bulk on FakeVissim."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness
from vissim_mcp.output import TRUNCATION_HINT, dumps
from vissim_mcp.server import build_server
from vissim_mcp.tools.objects import COM_OBJECT_MARKER, MAX_BULK_ITEMS, plain_value


async def call(mcp: FastMCP, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await mcp.call_tool(name, arguments or {})
    assert isinstance(result, CallToolResult)
    text = result.content[0].text
    payload: dict[str, Any] = json.loads(text)
    assert payload["ok"] is (not result.isError)
    return payload


def audit_records(audit_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for file in sorted(audit_dir.glob("audit-*.jsonl")):
        records.extend(json.loads(line) for line in file.read_text(encoding="utf-8").splitlines())
    return records


@pytest.fixture
async def setup(harness: Callable[..., Harness]) -> tuple[Harness, FastMCP]:
    h = harness()
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def test_object_tools_annotations(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("vissim_describe", "vissim_list_objects", "vissim_get_attributes"):
        assert tools[name].annotations.readOnlyHint is True  # type: ignore[union-attr]
    for name in ("vissim_set_attributes", "vissim_set_attribute_bulk"):
        assert tools[name].annotations.destructiveHint is True  # type: ignore[union-attr]
    bulk_items = tools["vissim_set_attribute_bulk"].inputSchema
    assert bulk_items["required"] == ["collection", "attribute", "items"]


async def test_object_tools_need_a_connection(harness: Callable[..., Harness]) -> None:
    h = harness()
    mcp = build_server(h.service, h.settings)
    calls = {
        "vissim_describe": {"path": "Net"},
        "vissim_list_objects": {"collection": "Net.Links", "attributes": ["No"]},
        "vissim_get_attributes": {"path": "Net.Links[1]", "attributes": ["No"]},
        "vissim_set_attributes": {"path": "Net.Links[1]", "values": {"Name": "x"}},
        "vissim_set_attribute_bulk": {
            "collection": "Net.Links",
            "attribute": "Name",
            "items": [{"key": 1, "value": "x"}],
        },
    }
    for name, arguments in calls.items():
        assert (await call(mcp, name, arguments))["error"]["code"] == "NOT_CONNECTED", name


# --- vissim_describe


async def test_describe_members(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    payload = await call(mcp, "vissim_describe", {"path": "Net.Links"})
    assert payload["path"] == "Net.Links" and payload["interface"]
    members = {(m["name"], m["kind"]) for m in payload["members"]}
    assert ("ItemByKey", "method") in members and ("GetMultipleAttributes", "method") in members
    assert ("Count", "property_get") in members
    assert not {name for name, _kind in members} & {"QueryInterface", "Invoke", "GetTypeInfo"}
    assert "attributes" not in payload

    root = await call(mcp, "vissim_describe", {"path": ""})
    assert root["path"] == "Vissim"
    assert {"LoadNet", "SaveNetAs", "Net"} <= {m["name"] for m in root["members"]}


async def test_describe_with_attributes(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    payload = await call(mcp, "vissim_describe", {"path": "Net.Links", "include_attributes": True})
    attributes = {row["code"]: row for row in payload["attributes"]}
    assert attributes["NumLanes"]["editable"] is True and attributes["No"]["editable"] is False
    assert attributes["NumLanes"]["value_type"] == "Integer" and attributes["NumLanes"]["name"]
    assert payload["attributes_total"] == len(payload["attributes"])
    assert payload["attributes_truncated"] is False

    single = await call(mcp, "vissim_describe", {"path": "Net.Links[1]", "include_attributes": True})
    assert single["attributes"] is None and "attributes_error" in single


async def test_describe_attributes_are_bounded(harness: Callable[..., Harness]) -> None:
    h = harness({"VISSIM_MAX_ROWS": "2"})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_describe", {"path": "Net.Links", "include_attributes": True})
    assert len(payload["attributes"]) == 2 and payload["attributes_total"] > 2
    assert payload["attributes_truncated"] is True


async def test_describe_info_unavailable(harness: Callable[..., Harness]) -> None:
    h = harness(no_type_info=True)
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_describe", {"path": "Net.Links"})
    assert payload["error"]["code"] == "INFO_UNAVAILABLE"
    with_attrs = await call(mcp, "vissim_describe", {"path": "Net.Links", "include_attributes": True})
    assert with_attrs["ok"] is True and with_attrs["members"] is None
    assert with_attrs["members_error"]["code"] == "INFO_UNAVAILABLE"
    assert with_attrs["attributes"]


async def test_describe_errors(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    assert (await call(mcp, "vissim_describe", {"path": "Net._oleobj_"}))["error"][
        "code"
    ] == "INVALID_ARGUMENT"
    missing = await call(mcp, "vissim_describe", {"path": "Net.Links[999]"})
    assert missing["error"]["code"] == "RESOLVE_FAILED"
    assert missing["error"]["message"].startswith("Segment 'Links[999]' on 'Net'")
    method = await call(mcp, "vissim_describe", {"path": "Net.Links.ItemByKey"})
    assert method["error"]["code"] == "RESOLVE_FAILED"
    value = await call(mcp, "vissim_describe", {"path": "Net.Links.Count"})
    assert value["error"]["code"] == "INVALID_ARGUMENT"


# --- vissim_list_objects


async def test_list_objects_happy_path_and_paging(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    first = await call(
        mcp, "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No", "Name"], "limit": 4}
    )
    assert first["columns"] == ["No", "Name"] and first["collection"] == "Net.Links"
    assert first["rows"] == [[1, "Link 1"], [2, "Link 2"], [3, "Link 3"], [4, "Link 4"]]
    assert (first["total"], first["offset"], first["returned"], first["truncated"]) == (10, 0, 4, True)
    last = await call(
        mcp, "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No"], "offset": 8, "limit": 4}
    )
    assert last["rows"] == [[9], [10]] and last["truncated"] is False and last["returned"] == 2
    nested = await call(
        mcp, "vissim_list_objects", {"collection": "Net.SignalControllers[2].SGs", "attributes": ["Name"]}
    )
    assert nested["rows"][0] == ["SG 2-1"] and nested["total"] == 4


async def test_list_objects_limit_is_clamped_with_a_warning(harness: Callable[..., Harness]) -> None:
    h = harness({"VISSIM_MAX_ROWS": "3"})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(
        mcp, "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No"], "limit": 50}
    )
    assert payload["returned"] == 3 and payload["truncated"] is True
    assert any("VISSIM_MAX_ROWS" in warning for warning in payload["warnings"])


async def test_list_objects_fits_the_result_size(harness: Callable[..., Harness]) -> None:
    h = harness({"VISSIM_MAX_RESULT_CHARS": "2000"})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    links = h.vissim.collection("Links")
    for key in range(1, 11):
        links._require(key)._attrs["name"].value = "N" * 300  # direct, on the test thread: no COM hook
    payload = await call(
        mcp, "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No", "Name"]}
    )
    assert payload["ok"] is True and payload["truncated"] is True
    assert payload["hint"] == TRUNCATION_HINT and 0 < payload["returned"] < 10
    assert len(dumps(payload)) <= 2000  # the server's compact encoding


async def test_list_objects_errors(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    unknown = await call(
        mcp, "vissim_list_objects", {"collection": "Net.Links", "attributes": ["No", "Bogus", "Name"]}
    )
    assert unknown["error"]["code"] == "COM_ERROR"
    assert unknown["error"]["details"]["invalid_attributes"] == ["Bogus"]
    for arguments in (
        {"collection": "Net.Links", "attributes": []},
        {"collection": "Net.Links", "attributes": ["No"], "offset": -1},
        {"collection": "Net.Links", "attributes": ["No"], "limit": 0},
        {"collection": "Net.Links[1]", "attributes": ["No"]},
        {"collection": "Net..Links", "attributes": ["No"]},
    ):
        payload = await call(mcp, "vissim_list_objects", arguments)
        assert payload["error"]["code"] == "INVALID_ARGUMENT", arguments


# --- vissim_get_attributes


async def test_get_attributes(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    payload = await call(
        mcp, "vissim_get_attributes", {"path": "Net.Links[3]", "attributes": ["Name", "NumLanes", "Nope"]}
    )
    assert payload["path"] == "Net.Links[3]"
    assert payload["values"] == {"Name": "Link 3", "NumLanes": 1}
    assert list(payload["errors"]) == ["Nope"]
    simulation = await call(mcp, "vissim_get_attributes", {"path": "Simulation", "attributes": ["SimPeriod"]})
    assert simulation["values"] == {"SimPeriod": 3600} and simulation["errors"] == {}
    missing = await call(mcp, "vissim_get_attributes", {"path": "Net.Links[77]", "attributes": ["Name"]})
    assert missing["error"]["code"] == "RESOLVE_FAILED"


async def test_get_attributes_lost_connection_is_an_error(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    h.vissim.crash()
    payload = await call(mcp, "vissim_get_attributes", {"path": "Net.Links[1]", "attributes": ["Name"]})
    assert payload["error"]["code"] == "COM_ERROR"
    assert (await call(mcp, "vissim_health"))["connected"] is False


def test_plain_value_hides_com_objects(fake_vissim: Any) -> None:
    assert plain_value((1, (fake_vissim.Net, "x"))) == [1, [COM_OBJECT_MARKER, "x"]]


# --- vissim_set_attributes


async def test_set_attributes_dry_run_changes_nothing(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    payload = await call(
        mcp,
        "vissim_set_attributes",
        {"path": "Net.Links[2]", "values": {"Name": "Registon", "NumLanes": 3}, "dry_run": True},
    )
    assert payload["dry_run"] is True
    assert payload["old"] == {"Name": "Link 2", "NumLanes": 3}
    assert payload["new"] == {"Name": "Registon", "NumLanes": 3}
    assert payload["failed"] == []
    assert h.vissim.attribute("Links", 2, "Name") == "Link 2"
    record = audit_records(isolated_env["audit"])[-1]
    assert record["tool"] == "vissim_set_attributes" and record["dry_run"] is True and record["ok"] is True
    assert record["old"] == payload["old"] and record["new"] == payload["new"] and record["written"] == []


async def test_set_attributes_writes_and_reads_back(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    payload = await call(
        mcp,
        "vissim_set_attributes",
        {"path": "Net.Links[2]", "values": {"Name": "Registon", "NumLanes": 2, "Length2D": 5.0, "Bogus": 1}},
    )
    assert payload["ok"] is True and payload["dry_run"] is False
    assert payload["old"] == {"Name": "Link 2", "NumLanes": 3, "Length2D": 75.0}
    assert payload["new"] == {"Name": "Registon", "NumLanes": 2, "Length2D": 75.0}
    stages = {(f["attribute"], f["stage"]) for f in payload["failed"]}
    assert stages == {("Bogus", "read"), ("Length2D", "write")}  # Length2D is read-only
    assert any("failed" in warning for warning in payload["warnings"])
    assert h.vissim.attribute("Links", 2, "Name") == "Registon"
    record = audit_records(isolated_env["audit"])[-1]
    assert record["written"] == ["Name", "NumLanes"] and record["path"] == "Net.Links[2]"
    assert record["new"]["Name"] == "Registon" and len(record["failed"]) == 2


async def test_set_attributes_policy_and_validation(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    denied = await call(
        mcp,
        "vissim_set_attributes",
        {"path": "Net.SignalControllers[1]", "values": {"SupplyFile1": "C:/x.sig"}},
    )
    assert denied["error"]["code"] == "ATTRIBUTE_DENIED"
    for values in ({}, {"Name": None}, {"Name": [1, 2]}, {"Name": "a", "NAME": "b"}):
        payload = await call(mcp, "vissim_set_attributes", {"path": "Net.Links[1]", "values": values})
        assert payload["error"]["code"] == "INVALID_ARGUMENT", values
    records = audit_records(isolated_env["audit"])
    assert records[0]["error_code"] == "ATTRIBUTE_DENIED" and records[0]["ok"] is False
    assert len(records) == 5
    assert h.vissim.attribute("SignalControllers", 1, "SupplyFile1") == ""


async def test_path_attributes_allowed_by_setting(harness: Callable[..., Harness]) -> None:
    h = harness({"VISSIM_ALLOW_PATH_ATTRS": "1"})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(
        mcp, "vissim_set_attributes", {"path": "Net.SignalControllers[1]", "values": {"SupplyFile1": "a.sig"}}
    )
    assert payload["ok"] is True and payload["new"] == {"SupplyFile1": "a.sig"}


async def test_set_attributes_simulation_and_signal_group(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    sim = await call(mcp, "vissim_set_attributes", {"path": "Simulation", "values": {"SimPeriod": 900}})
    assert sim["old"] == {"SimPeriod": 3600} and sim["new"] == {"SimPeriod": 900}
    sg = await call(
        mcp,
        "vissim_set_attributes",
        {"path": "Net.SignalControllers[1].SGs[2]", "values": {"Name": "Shimol"}},
    )
    assert sg["new"] == {"Name": "Shimol"}


async def test_set_attributes_audit_failure_is_a_warning(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    h = harness({"VISSIM_AUDIT_DIR": str(blocker)})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_set_attributes", {"path": "Net.Links[1]", "values": {"Name": "Yangi"}})
    assert payload["ok"] is True and payload["new"] == {"Name": "Yangi"}
    assert any("audit record" in warning for warning in payload["warnings"])


# --- vissim_set_attribute_bulk


async def test_bulk_dry_run(setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]) -> None:
    h, mcp = setup
    items = [{"key": 1, "value": 800}, {"key": 2, "value": 900.5}, {"key": 9, "value": 1}]
    payload = await call(
        mcp,
        "vissim_set_attribute_bulk",
        {"collection": "Net.VehicleInputs", "attribute": "Volume(1)", "items": items, "dry_run": True},
    )
    assert payload["dry_run"] is True and payload["updated"] == 0 and payload["total"] == 3
    assert payload["changes"] == [
        {"key": 1, "old": 500.0, "new": 800},
        {"key": 2, "old": 600.0, "new": 900.5},
    ]
    assert payload["failed_total"] == 1 and payload["failed"][0]["key"] == 9
    assert payload["failed"][0]["stage"] == "read"
    assert h.vissim.attribute("VehicleInputs", 1, "Volume(1)") == 500.0
    record = audit_records(isolated_env["audit"])[-1]
    assert record["tool"] == "vissim_set_attribute_bulk" and record["dry_run"] is True
    assert record["args"]["items"] == [[1, 800], [2, 900.5], [9, 1]] and record["written"] == []
    assert record["path"] == "Net.VehicleInputs" and record["attribute"] == "Volume(1)"


async def test_bulk_writes_in_one_task(setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]) -> None:
    h, mcp = setup
    items = [{"key": key, "value": f"Ko'cha {key}"} for key in range(1, 11)]
    payload = await call(
        mcp, "vissim_set_attribute_bulk", {"collection": "Net.Links", "attribute": "Name", "items": items}
    )
    assert payload["updated"] == 10 and payload["failed"] == [] and payload["changes_total"] == 10
    assert payload["changes"][0] == {"key": 1, "old": "Link 1", "new": "Ko'cha 1"}
    assert h.vissim.attribute("Links", 10, "Name") == "Ko'cha 10"
    calls = [member for member, _thread in h.vissim.calls]
    assert calls.count("Links.ItemByKey") >= 10
    write_tasks = [line for line in calls if line.endswith("SetAttValue")]
    assert len(write_tasks) == 10
    assert len({thread for _member, thread in h.vissim.calls}) == 1  # all on the COM worker thread
    record = audit_records(isolated_env["audit"])[-1]
    assert record["new"]["10"] == "Ko'cha 10" and len(record["written"]) == 10


async def test_bulk_partial_failure(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    items = [{"key": 1, "value": 2}, {"key": 2, "value": 99}, {"key": 3, "value": "x"}]
    payload = await call(
        mcp, "vissim_set_attribute_bulk", {"collection": "Net.Links", "attribute": "NumLanes", "items": items}
    )
    assert payload["ok"] is True and payload["updated"] == 1
    assert {f["key"] for f in payload["failed"]} == {2, 3}
    assert all(f["stage"] == "write" for f in payload["failed"])


async def test_bulk_validation(setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]) -> None:
    _h, mcp = setup
    too_many = [{"key": index, "value": 1} for index in range(MAX_BULK_ITEMS + 1)]
    cases: list[tuple[dict[str, Any], str]] = [
        ({"collection": "Net.Links", "attribute": "Name", "items": too_many}, "INVALID_ARGUMENT"),
        ({"collection": "Net.Links", "attribute": "Name", "items": []}, "INVALID_ARGUMENT"),
        (
            {
                "collection": "Net.Links",
                "attribute": "Name",
                "items": [{"key": 1, "value": "a"}, {"key": "1", "value": "b"}],
            },
            "INVALID_ARGUMENT",
        ),
        (
            {
                "collection": "Net.SignalControllers",
                "attribute": "SupplyFile1",
                "items": [{"key": 1, "value": "x"}],
            },
            "ATTRIBUTE_DENIED",
        ),
        (
            {"collection": "Net.Links[1]", "attribute": "Name", "items": [{"key": 1, "value": "x"}]},
            "INVALID_ARGUMENT",
        ),
        (
            {"collection": "Net.Nothing", "attribute": "Name", "items": [{"key": 1, "value": "x"}]},
            "RESOLVE_FAILED",
        ),
    ]
    for arguments, code in cases:
        payload = await call(mcp, "vissim_set_attribute_bulk", arguments)
        assert payload["error"]["code"] == code, arguments
    records = audit_records(isolated_env["audit"])
    assert [r["error_code"] for r in records] == [code for _a, code in cases]
    assert records[0]["args"]["items_total"] == MAX_BULK_ITEMS + 1


async def test_bulk_at_the_item_limit(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = setup
    links = h.vissim.collection("Links")
    for key in range(11, MAX_BULK_ITEMS + 1):
        links._add(key, {"Name": f"Link {key}"})
    items = [{"key": key, "value": 2} for key in range(1, MAX_BULK_ITEMS + 1)]
    payload = await call(
        mcp,
        "vissim_set_attribute_bulk",
        {"collection": "Net.Links", "attribute": "NumLanes", "items": items, "dry_run": True},
    )
    assert payload["ok"] is True and payload["total"] == MAX_BULK_ITEMS and payload["failed_total"] == 0
    assert payload["changes_total"] == MAX_BULK_ITEMS and payload["changes_truncated"] is True
    assert len(dumps(payload)) <= h.settings.max_result_chars
