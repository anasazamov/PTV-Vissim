"""tools/results.py (PLAN 7.5, 6.7, 6.8): vissim_export_csv in process, on the fakes.

The CSV is ';'-separated UTF-8 with BOM; the target passes the write policy; every call - success, refusal,
failure - writes exactly one audit record.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness
from vissim_mcp.policy import NON_ASCII_WARNING
from vissim_mcp.server import build_server
from vissim_mcp.tools.results import csv_cell

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


@pytest.fixture
def projects(isolated_env: dict[str, Path]) -> Path:
    root = isolated_env["projects"]
    (root / "results").mkdir()
    (root / "models" / "original").mkdir(parents=True)
    return root


async def connected(harness: MakeHarness) -> tuple[Harness, FastMCP]:
    h = harness()
    mcp = build_server(h.service, h.settings)
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def test_export_csv_happy_path(
    harness: MakeHarness, projects: Path, isolated_env: dict[str, Path]
) -> None:
    _h, mcp = await connected(harness)
    result = await call(
        mcp,
        "vissim_export_csv",
        {
            "collection": "Net.Links",
            "attributes": ["No", "Name", "NumLanes"],
            "csv_path": "results/links.csv",
        },
    )
    assert result["ok"] is True, result
    target = projects / "results" / "links.csv"
    assert result["path"] == str(target.resolve()) and result["rows"] == 10
    assert result["columns"] == ["No", "Name", "NumLanes"] and result["overwritten"] is False
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert hashlib.sha256(raw).hexdigest() == result["sha256"] and len(raw) == result["size_bytes"]
    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=";"))
    assert rows[0] == ["No", "Name", "NumLanes"]
    assert rows[1][:2] == ["1", "Link 1"] and len(rows) == 11
    assert not list((projects / "results").glob("*.tmp"))

    again = await call(
        mcp, "vissim_export_csv", {"collection": "Net.Links", "attributes": ["No"], "csv_path": str(target)}
    )
    assert again["overwritten"] is True and again["rows"] == 10
    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] == "vissim_export_csv"]
    assert len(records) == 2 and all(r["ok"] for r in records)
    assert records[0]["path"] == str(target.resolve()) and records[0]["rows"] == 10
    assert records[0]["sha256"] == result["sha256"] and records[1]["overwritten"] is True


async def test_export_csv_non_ascii_path(harness: MakeHarness, projects: Path) -> None:
    folder = projects / "Самарканд"
    folder.mkdir()
    _h, mcp = await connected(harness)
    result = await call(
        mcp,
        "vissim_export_csv",
        {"collection": "Net.Links", "attributes": ["No"], "csv_path": "Самарканд/l.csv"},
    )
    assert result["ok"] is True and result["warnings"] == [NON_ASCII_WARNING]
    assert (folder / "l.csv").is_file()


@pytest.mark.parametrize(
    ("csv_path", "code"),
    [
        ("models/original/links.csv", "READ_ONLY_PATH"),
        ("results/links.txt", "INVALID_ARGUMENT"),
        ("missing_folder/links.csv", "FILE_NOT_FOUND"),
        ("../outside.csv", "PATH_DENIED"),
    ],
)
async def test_export_csv_policy_errors_are_audited(
    harness: MakeHarness, projects: Path, isolated_env: dict[str, Path], csv_path: str, code: str
) -> None:
    _h, mcp = await connected(harness)
    result = await call(
        mcp, "vissim_export_csv", {"collection": "Net.Links", "attributes": ["No"], "csv_path": csv_path}
    )
    assert result["error"]["code"] == code
    (record,) = audit_records(isolated_env["audit"])
    assert record["tool"] == "vissim_export_csv" and record["ok"] is False and record["error_code"] == code
    assert not list(projects.rglob("*.csv"))


async def test_export_csv_unknown_attribute_and_bad_arguments(
    harness: MakeHarness, projects: Path, isolated_env: dict[str, Path]
) -> None:
    _h, mcp = await connected(harness)
    unknown = await call(
        mcp,
        "vissim_export_csv",
        {"collection": "Net.Links", "attributes": ["No", "Bogus"], "csv_path": "results/x.csv"},
    )
    assert unknown["error"]["code"] == "COM_ERROR"
    assert unknown["error"]["details"]["invalid_attributes"] == ["Bogus"]
    bad_path = await call(
        mcp,
        "vissim_export_csv",
        {"collection": "Net..Links", "attributes": ["No"], "csv_path": "results/x.csv"},
    )
    assert bad_path["error"]["code"] == "INVALID_ARGUMENT"
    no_attributes = await call(
        mcp, "vissim_export_csv", {"collection": "Net.Links", "attributes": [], "csv_path": "results/x.csv"}
    )
    assert no_attributes["error"]["code"] == "INVALID_ARGUMENT"
    missing = await call(
        mcp,
        "vissim_export_csv",
        {"collection": "Net.Links[999]", "attributes": ["No"], "csv_path": "results/x.csv"},
    )
    assert missing["error"]["code"] == "RESOLVE_FAILED"
    assert not (projects / "results" / "x.csv").exists()
    codes = [r["error_code"] for r in audit_records(isolated_env["audit"])]
    assert codes == ["COM_ERROR", "INVALID_ARGUMENT", "INVALID_ARGUMENT", "RESOLVE_FAILED"]


async def test_export_csv_needs_a_connection(harness: MakeHarness, projects: Path) -> None:
    h = harness()
    mcp = build_server(h.service, h.settings)
    result = await call(
        mcp,
        "vissim_export_csv",
        {"collection": "Net.Links", "attributes": ["No"], "csv_path": "results/x.csv"},
    )
    assert result["error"]["code"] == "NOT_CONNECTED"


async def test_export_of_simulation_results(harness: MakeHarness, projects: Path) -> None:
    h, mcp = await connected(harness)
    h.vissim.set_simulation_attribute("SimPeriod", 120)
    await call(mcp, "vissim_start_simulation", {"runs": 2, "seed": 3})
    for _ in range(100):
        status = await call(mcp, "vissim_wait_simulation", {"max_wait_s": 1})
        if status["state"] != "running":
            break
    assert status["state"] == "finished"
    result = await call(
        mcp,
        "vissim_export_csv",
        {
            "collection": "Net.SimulationRuns",
            "attributes": ["No", "RandSeed"],
            "csv_path": "results/runs.csv",
        },
    )
    assert result["rows"] == 2
    lines = (projects / "results" / "runs.csv").read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["No;RandSeed", "1;3", "2;4"]


def test_csv_cell() -> None:
    assert csv_cell(None) == "" and csv_cell(True) == 1 and csv_cell(2.5) == 2.5 and csv_cell("a") == "a"
    assert csv_cell([1, None, "x"]) == "1,,x"
