"""tools/diagnostics.py (PLAN 7.6): vissim_tail_log and vissim_exec_python in process.

tail_log: limits, missing / rotated / locked log file, reading from the end, response-size fitting, no path
parameter. exec_python: disabled by default (the refusal is audited), enabled through the settings, the
namespace, the full code in the audit record, captured print output, errors and timeouts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness, eventually
from vissim_mcp.logging_setup import LOG_FILE_NAME
from vissim_mcp.server import build_server
from vissim_mcp.tools import diagnostics
from vissim_mcp.tools import simulation as simulation_tools

MakeHarness = Callable[..., Harness]
EXEC_ON = {"VISSIM_MCP_ALLOW_EXEC": "1"}


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


def write_log(folder: Path, lines: list[str], name: str = LOG_FILE_NAME) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes("".join(f"{line}\n" for line in lines).encode("utf-8"))
    return path


def server(
    harness: MakeHarness, env: dict[str, str] | None = None, **options: Any
) -> tuple[Harness, FastMCP]:
    h = harness(env, **options)
    return h, build_server(h.service, h.settings)


# --- vissim_tail_log


async def test_tail_log_schema_has_no_path(harness: MakeHarness) -> None:
    _h, mcp = server(harness)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    tail = tools["vissim_tail_log"]
    assert list(tail.inputSchema["properties"]) == ["lines"]
    assert tail.annotations is not None and tail.annotations.readOnlyHint is True
    exec_tool = tools["vissim_exec_python"]
    assert list(exec_tool.inputSchema["properties"]) == ["code"]
    assert exec_tool.annotations is not None and exec_tool.annotations.destructiveHint is True


async def test_tail_log_default_and_limits(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    lines = [f"line {number} Самарқанд" for number in range(1, 1201)]
    path = write_log(isolated_env["log"], lines)
    _h, mcp = server(harness)
    default = await call(mcp, "vissim_tail_log")
    assert default["lines"] == lines[-100:] and default["returned"] == 100 and default["more"] is True
    assert default["path"] == str(path) and default["files"] == [str(path)]
    five = await call(mcp, "vissim_tail_log", {"lines": 5})
    assert five["lines"] == lines[-5:]
    capped = await call(mcp, "vissim_tail_log", {"lines": 10_000})
    assert capped["returned"] == 500 and capped["lines"] == lines[-500:]
    assert "reduced to 500" in capped["warnings"][0]
    for bad in (0, -1):
        refused = await call(mcp, "vissim_tail_log", {"lines": bad})
        assert refused["error"]["code"] == "INVALID_ARGUMENT", bad


async def test_tail_log_whole_short_file(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    write_log(isolated_env["log"], ["a", "b", "c"])
    _h, mcp = server(harness)
    result = await call(mcp, "vissim_tail_log", {"lines": 50})
    assert result["lines"] == ["a", "b", "c"] and result["more"] is False


async def test_tail_log_missing_file(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    _h, mcp = server(harness)
    result = await call(mcp, "vissim_tail_log")
    assert result["ok"] is True and result["lines"] == [] and result["files"] == []
    assert "does not exist" in result["warnings"][0]


async def test_tail_log_fills_up_from_the_rotated_file(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    backup = write_log(isolated_env["log"], [f"old {n}" for n in range(1, 21)], LOG_FILE_NAME + ".1")
    current = write_log(isolated_env["log"], ["new 1", "new 2"])
    _h, mcp = server(harness)
    result = await call(mcp, "vissim_tail_log", {"lines": 5})
    assert result["lines"] == ["old 18", "old 19", "old 20", "new 1", "new 2"]
    assert result["files"] == [str(backup), str(current)] and result["more"] is True
    # just rotated: the current file is still empty
    current.write_bytes(b"")
    fresh = await call(mcp, "vissim_tail_log", {"lines": 2})
    assert fresh["lines"] == ["old 19", "old 20"]


async def test_tail_log_locked_file(
    harness: MakeHarness, isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(isolated_env["log"], ["x"])
    original = diagnostics._tail_file

    def locked(path: Path, count: int) -> tuple[list[str], bool]:
        if path.name == LOG_FILE_NAME:
            raise PermissionError(13, "The process cannot access the file")
        return original(path, count)

    monkeypatch.setattr(diagnostics, "_tail_file", locked)
    _h, mcp = server(harness)
    result = await call(mcp, "vissim_tail_log")
    assert result["ok"] is True and result["lines"] == []
    assert "could not be read" in result["warnings"][0] and "PermissionError" in result["warnings"][0]


async def test_tail_log_reads_only_the_end(
    harness: MakeHarness, isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "TAIL_BLOCK_BYTES", 256)
    lines = [f"{n:06d} " + "x" * 50 for n in range(20_000)]  # about 1.2 MB
    write_log(isolated_env["log"], lines)
    result = diagnostics.read_log_tail(isolated_env["log"] / LOG_FILE_NAME, 3)
    assert result["lines"] == lines[-3:] and result["more"] is True
    monkeypatch.setattr(diagnostics, "MAX_TAIL_BYTES", 1000)
    capped = diagnostics.read_log_tail(isolated_env["log"] / LOG_FILE_NAME, 500)
    assert 0 < len(capped["lines"]) < 20 and capped["lines"][-1] == lines[-1] and capped["more"] is True


async def test_tail_log_fits_the_response_size(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    lines = [f"{n:04d} " + "y" * 1900 for n in range(500)]
    write_log(isolated_env["log"], lines + ["z" * 5000])
    _h, mcp = server(harness, {"VISSIM_MAX_RESULT_CHARS": "20000"})
    result = await call(mcp, "vissim_tail_log", {"lines": 500})
    assert result["ok"] is True and 0 < result["returned"] < 20
    assert result["lines"][-1].endswith("...") and len(result["lines"][-1]) == diagnostics.MAX_LINE_CHARS
    assert result["lines"][-2] == lines[-1] and result["more"] is True
    assert any("oldest line(s) were left out" in w for w in result["warnings"])


# --- vissim_exec_python


async def test_exec_disabled_by_default_and_audited(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = server(harness)
    assert h.settings.allow_exec is False
    await call(mcp, "vissim_connect")
    code = "Vissim.Net.Links.ItemByKey(1).SetAttValue('Name', 'hacked')\nresult = 1"
    refused = await call(mcp, "vissim_exec_python", {"code": code})
    assert refused["error"]["code"] == "EXEC_DISABLED"
    assert "VISSIM_MCP_ALLOW_EXEC=1" in refused["error"]["hint"]
    assert h.vissim.attribute("Links", 1, "Name") == "Link 1"
    (record,) = audit_records(isolated_env["audit"], "vissim_exec_python")
    assert record["ok"] is False and record["error_code"] == "EXEC_DISABLED"
    assert record["args"] == {"code": code}
    # even an invalid argument is refused as disabled first (nothing about the code is evaluated)
    assert (await call(mcp, "vissim_exec_python", {"code": 5}))["error"]["code"] == "EXEC_DISABLED"


async def test_exec_enabled_runs_code_and_audits_it(
    harness: MakeHarness, isolated_env: dict[str, Path]
) -> None:
    h, mcp = server(harness, EXEC_ON)
    assert h.settings.allow_exec is True
    await call(mcp, "vissim_connect")
    code = (
        "names = sorted(k for k in globals() if not k.startswith('__'))\n"
        "link = Vissim.Net.Links.ItemByKey(2)\n"
        "link.SetAttValue('Name', 'Registon')\n"
        "print('links:', Vissim.Net.Links.Count)\n"
        "result = {'names': names, 'name': link.AttValue('Name'), 'nan': float('nan'), 'obj': link, "
        "'tuple': (1, 2)}\n"
    )
    result = await call(mcp, "vissim_exec_python", {"code": code})
    assert result["ok"] is True, result
    value = result["result"]
    assert value["names"] == ["Vissim", "result"]
    assert value["name"] == "Registon" and value["nan"] is None and value["tuple"] == [1, 2]
    assert isinstance(value["obj"], str)  # a COM object becomes text on the worker thread
    assert result["output"] == "links: 10\n"
    assert h.vissim.attribute("Links", 2, "Name") == "Registon"
    (record,) = audit_records(isolated_env["audit"], "vissim_exec_python")
    assert record["ok"] is True and record["args"]["code"] == code and record["result_type"] == "dict"


async def test_exec_without_result_returns_null(harness: MakeHarness) -> None:
    _h, mcp = server(harness, EXEC_ON)
    await call(mcp, "vissim_connect")
    result = await call(mcp, "vissim_exec_python", {"code": "x = 1"})
    assert result["ok"] is True and result["result"] is None and result["output"] == ""


async def test_exec_errors(harness: MakeHarness, isolated_env: dict[str, Path]) -> None:
    h, mcp = server(harness, EXEC_ON)
    not_connected = await call(mcp, "vissim_exec_python", {"code": "result = 1"})
    assert not_connected["error"]["code"] == "NOT_CONNECTED"
    await call(mcp, "vissim_connect")
    for bad in ("", "   ", 5, None, "x" * (diagnostics.MAX_CODE_CHARS + 1)):
        assert (await call(mcp, "vissim_exec_python", {"code": bad}))["error"]["code"] == "INVALID_ARGUMENT"
    syntax = await call(mcp, "vissim_exec_python", {"code": "result = ("})
    assert syntax["error"]["code"] == "INVALID_ARGUMENT" and "does not compile" in syntax["error"]["message"]

    raised = await call(mcp, "vissim_exec_python", {"code": "print('before')\nresult = 1 / 0"})
    assert raised["error"]["code"] == "INVALID_ARGUMENT"
    assert "ZeroDivisionError" in raised["error"]["message"]
    assert "line 2" in raised["error"]["details"]["traceback"]
    assert raised["error"]["details"]["output"] == "before\n"

    com = await call(mcp, "vissim_exec_python", {"code": "result = Vissim.Net.Links.ItemByKey(999)"})
    assert com["error"]["code"] == "COM_ERROR" and "traceback" in com["error"]["details"]

    exited = await call(mcp, "vissim_exec_python", {"code": "exit(3)"})
    assert exited["error"]["code"] == "INVALID_ARGUMENT" and "SystemExit" in exited["error"]["message"]

    no_input = await call(mcp, "vissim_exec_python", {"code": "result = input()"})
    assert no_input["error"]["code"] == "INVALID_ARGUMENT" and "input()" in no_input["error"]["message"]

    assert h.service.connected  # none of these broke the connection
    assert (await call(mcp, "vissim_exec_python", {"code": "result = Vissim.Net.Links.Count"}))[
        "result"
    ] == 10
    records = audit_records(isolated_env["audit"], "vissim_exec_python")
    assert len(records) == 12 and all("code" in r["args"] for r in records)


async def test_exec_output_is_bounded(harness: MakeHarness) -> None:
    _h, mcp = server(harness, EXEC_ON)
    await call(mcp, "vissim_connect")
    result = await call(
        mcp, "vissim_exec_python", {"code": "for i in range(10000):\n    print('abcdefghij')"}
    )
    assert result["ok"] is True and len(result["output"]) == diagnostics.MAX_OUTPUT_CHARS
    assert any("output was cut" in w for w in result["warnings"])


async def test_exec_times_out_with_the_call_timeout(harness: MakeHarness) -> None:
    h, mcp = server(harness, {**EXEC_ON, "VISSIM_CALL_TIMEOUT_S": "0.3"})
    await call(mcp, "vissim_connect")
    h.vissim.hang_next_call(5)
    result = await call(mcp, "vissim_exec_python", {"code": "result = Vissim.Net.Links.Count"})
    assert result["error"]["code"] == "TIMEOUT_HUNG"
    h.vissim.release_hang()


async def test_exec_refused_while_a_job_runs(
    harness: MakeHarness, isolated_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(simulation_tools, "WAIT_POLL_S", 0.02)
    h, mcp = server(harness, EXEC_ON, wall_s_per_sim_s=0.002)
    await call(mcp, "vissim_connect")
    h.vissim.set_simulation_attribute("SimPeriod", 3600)
    await call(mcp, "vissim_start_simulation", {"chunk_sim_s": 60, "pause_at": [1800]})
    await eventually(lambda: h.service.jobs.current.simsec > 0)  # type: ignore[union-attr]
    refused = await call(mcp, "vissim_exec_python", {"code": "result = 1"})
    assert refused["error"]["code"] == "JOB_RUNNING"
    assert (await call(mcp, "vissim_wait_simulation", {"max_wait_s": 30}))["state"] == "paused"
    paused = await call(mcp, "vissim_exec_python", {"code": "result = Vissim.Simulation.AttValue('SimSec')"})
    assert paused["ok"] is True and paused["result"] == 1800
    await call(mcp, "vissim_stop_simulation")
