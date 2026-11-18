"""tools/signals.py (PLAN 7.5): vissim_set_signal_state in process against FakeVissim.

Covers the state write (ContrByCOM switched on by Vissim), the release, the JOB_RUNNING refusal while a job
runs (allowed while paused and without a job), unknown controller/group, argument errors and the audit
records.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

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


def test_boolean_attributes_arrive_as_integers_from_real_vissim() -> None:
    """Real Vissim returns ContrByCOM as 1 / 0 (M6); the tool reports true / false."""

    class Group:
        def __init__(self, value: Any) -> None:
            self.value = value

        def AttValue(self, name: str) -> Any:
            return self.value

    errors: dict[str, str] = {}
    assert signals._read_flag(Group(1), "ContrByCOM", errors) is True
    assert signals._read_flag(Group(0), "ContrByCOM", errors) is False
    assert signals._read_flag(Group(True), "ContrByCOM", errors) is True
    assert signals._read_flag(Group(None), "ContrByCOM", errors) is None
    assert signals._read_flag(Group(7), "ContrByCOM", errors) == 7
    assert errors == {}


async def test_no_simulation_run_in_progress_is_explained(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    """Real Vissim outside a run (M6): SigState reads as empty and neither attribute can be written."""
    h, mcp = await connected(harness)
    group = h.vissim.collection(SGS)._find(1)
    for key in ("sigstate", "contrbycom"):
        group._attrs[key].value = None
        group._attrs[key].editable = False
    for arguments in ({"state": "GREEN"}, {"release_to_controller": True}):
        result = await call(mcp, "vissim_set_signal_state", {"sc_no": 1, "sg_no": 1, **arguments})
        assert result["error"]["code"] == "COM_ERROR", result
        assert result["error"]["hint"] == signals.NO_SIMULATION_HINT
        assert "pause_at" in result["error"]["hint"]
    records = audit_records(isolated_env["audit"], "vissim_set_signal_state")
    assert [r["old"] for r in records] == [{"SigState": None, "ContrByCOM": None}] * 2


# --- vissim_set_signal_program

PROGRAM = "vissim_set_signal_program"


def _decoded(h: Harness, sc_no: int = 1) -> str:
    stored = h.vissim.attribute("SignalControllers", sc_no, "IntSupplyData")
    return base64.b64decode(stored).decode("utf-8")


async def test_signal_program_is_written_as_vissig_xml(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(harness)
    existing = sorted(h.vissim.collection(SGS)._all())
    groups = [
        {"sg_no": no, "green_start_s": 5 * index, "green_end_s": 5 * index + 4}
        for index, no in enumerate(existing)
    ]
    groups.append({"sg_no": 90, "green_start_s": 50, "green_end_s": 10})  # a new group; green wraps around
    result = await call(mcp, PROGRAM, {"sc_no": 1, "cycle_s": 60, "groups": groups})
    assert result["ok"] is True, result
    assert result["created_groups"] == [90] and result["stored"] is True and result["ProgNo"] == 1
    assert result["cycle_s"] == 60 and result["amber_s"] == 3 and result["red_amber_s"] == 1
    assert result["groups"][-1] == {"sg_no": 90, "green_start_s": 50.0, "green_end_s": 10.0}
    assert 90 in h.vissim.collection(SGS)._all()

    xml = _decoded(h)
    root = ElementTree.fromstring(xml.encode("utf-8"))
    assert root.tag == "sc" and root.get("id") == "1"
    (prog,) = root.findall("./progs/prog")
    assert prog.get("id") == "1" and prog.get("cycletime") == "60000"
    assert [sg.get("id") for sg in root.findall("./sgs/sg")] == [str(no) for no in [*existing, 90]]
    wrapped = prog.find("./sgs/sg[@sg_id='90']")
    assert wrapped is not None
    commands = {cmd.get("display"): cmd.get("begin") for cmd in wrapped.findall("./cmds/cmd")}
    # green begins at 50 s; red begins when the 3 s of amber after the end of green (10 s) are over
    assert commands == {"3": "50000", "1": "13000"}
    fixed = {
        state.get("display"): state.get("duration") for state in wrapped.findall("./fixedstates/fixedstate")
    }
    assert fixed == {"2": "1000", "4": "3000"}
    assert "\r\n" in xml

    (record,) = audit_records(isolated_env["audit"], PROGRAM)
    assert record["ok"] is True and record["created_groups"] == [90]
    assert record["written"] == ["IntSupplyData", "ProgNo"] and record["old"]["program_chars"] == 0


async def test_signal_program_dry_run_and_refusals(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(harness)
    existing = sorted(h.vissim.collection(SGS)._all())
    full = [{"sg_no": no, "green_start_s": 0, "green_end_s": 20} for no in existing]
    dry = await call(
        mcp,
        PROGRAM,
        {
            "sc_no": 1,
            "cycle_s": 60,
            "groups": [*full, {"sg_no": 77, "green_start_s": 30, "green_end_s": 50}],
            "dry_run": True,
        },
    )
    assert dry["ok"] is True and dry["dry_run"] is True and dry["created_groups"] == [77]
    assert "stored" not in dry and 77 not in h.vissim.collection(SGS)._all()
    assert h.vissim.attribute("SignalControllers", 1, "IntSupplyData") == ""

    incomplete = await call(mcp, PROGRAM, {"sc_no": 1, "cycle_s": 60, "groups": full[:1]})
    assert incomplete["error"]["code"] == "INVALID_ARGUMENT"
    assert "are not in groups" in incomplete["error"]["message"]
    unknown = await call(mcp, PROGRAM, {"sc_no": 99, "cycle_s": 60, "groups": full})
    assert unknown["error"]["code"] == "RESOLVE_FAILED"
    bad: list[dict[str, Any]] = [
        {"sc_no": 1, "cycle_s": 0, "groups": full},
        {"sc_no": 1, "cycle_s": "60", "groups": full},
        {"sc_no": 1, "cycle_s": 60, "groups": []},
        {"sc_no": 1, "cycle_s": 60, "groups": [{"sg_no": 1, "green_start_s": 5}]},
        {"sc_no": 1, "cycle_s": 60, "groups": [{"sg_no": 1, "green_start_s": 5, "green_end_s": 5}]},
        {"sc_no": 1, "cycle_s": 60, "groups": [{"sg_no": 1, "green_start_s": 5, "green_end_s": 70}]},
        {"sc_no": 1, "cycle_s": 60, "groups": [{"sg_no": 1, "green_start_s": 0, "green_end_s": 58}]},
        {"sc_no": 1, "cycle_s": 60, "groups": [full[0], full[0]]},
        {"sc_no": 1, "cycle_s": 60, "groups": full, "amber_s": -1},
        {"sc_no": 0, "cycle_s": 60, "groups": full},
    ]
    for arguments in bad:
        refused = await call(mcp, PROGRAM, arguments)
        assert refused["error"]["code"] == "INVALID_ARGUMENT", (arguments, refused)
    assert h.vissim.attribute("SignalControllers", 1, "IntSupplyData") == ""
    assert all(r["ok"] is False for r in audit_records(isolated_env["audit"], PROGRAM)[1:])


async def test_signal_program_is_refused_while_a_job_is_active(harness: MakeHarness) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 600)
    assert (await call(mcp, "vissim_start_simulation", {"runs": 1, "pause_at": [100]}))["ok"] is True
    groups = [
        {"sg_no": no, "green_start_s": 0, "green_end_s": 20} for no in sorted(h.vissim.collection(SGS)._all())
    ]
    refused = await call(mcp, PROGRAM, {"sc_no": 1, "cycle_s": 60, "groups": groups})
    assert refused["error"]["code"] == "JOB_RUNNING"
    await call(mcp, "vissim_stop_simulation")


def test_program_xml_escapes_names() -> None:
    xml = signals.fixed_time_program_xml(
        3, 'Main "A" & <B>', 90000, [signals.GroupTimes(1, 0, 30000)], {1: "North & 'South'"}, 3000, 0
    )
    root = ElementTree.fromstring(xml.encode("utf-8"))
    assert root.get("name") == 'Main "A" & <B>' and root.get("id") == "3"
    assert root.find("./sgs/sg").get("name") == "North & 'South'"  # type: ignore[union-attr]
    assert root.find("./progs/prog").get("cycletime") == "90000"  # type: ignore[union-attr]
