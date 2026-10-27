"""tools/files.py (PLAN 7.2, 6.7, 6.8): vissim_load_network and vissim_save_network in process, on the fakes.

Every call writes exactly one audit record (success, refusal, failure); a failing audit log never stops the
operation but adds a warning to the result.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from service_harness import Harness, StubJob, StubRegistry
from vissim_mcp.policy import NON_ASCII_WARNING
from vissim_mcp.server import build_server
from vissim_mcp.tools.files import READ_ONLY_LOAD_WARNING


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
    (root / "scenarios").mkdir()
    (root / "scenarios" / "base.inpx").write_text("<network/>", encoding="utf-8")
    (root / "scenarios" / "base.layx").write_text("<layout/>", encoding="utf-8")
    original = root / "models" / "original"
    original.mkdir(parents=True)
    (original / "city.inpx").write_text("<network/>", encoding="utf-8")
    return root


@pytest.fixture
def setup(harness: Callable[..., Harness], projects: Path) -> tuple[Harness, FastMCP]:
    h = harness()
    return h, build_server(h.service, h.settings)


async def connected(setup: tuple[Harness, FastMCP]) -> tuple[Harness, FastMCP]:
    h, mcp = setup
    assert (await call(mcp, "vissim_connect"))["ok"] is True
    return h, mcp


async def test_tool_schemas_and_annotations(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = setup
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    load = tools["vissim_load_network"]
    assert list(load.inputSchema["properties"]) == ["inpx_path", "layx_path", "additive"]
    assert load.inputSchema["required"] == ["inpx_path"]
    save = tools["vissim_save_network"]
    assert list(save.inputSchema["properties"]) == ["save_as", "overwrite_loaded"]
    for tool in (load, save):
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is True


# --- vissim_load_network


async def test_load_network_happy_path(
    setup: tuple[Harness, FastMCP], projects: Path, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(setup)
    payload = await call(
        mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx", "layx_path": "scenarios/base.layx"}
    )
    expected = str((projects / "scenarios" / "base.inpx").resolve())
    assert payload["loaded_file"] == expected
    assert payload["layout_file"] == str((projects / "scenarios" / "base.layx").resolve())
    assert payload["layout_loaded"] is True and payload["additive"] is False
    assert payload["counts"]["Links"] == 10 and payload["counts"]["SignalControllers"] == 2
    assert "warnings" not in payload and payload["elapsed_s"] >= 0
    assert h.vissim.loaded_net_path == expected and h.vissim.last_load_additive is False
    assert h.vissim.layout_path == payload["layout_file"]
    assert h.service.loaded_file == expected
    info = await call(mcp, "vissim_info")
    assert info["loaded_file"] == expected

    record = audit_records(isolated_env["audit"])[-1]
    assert record["tool"] == "vissim_load_network" and record["ok"] is True
    assert record["args"]["inpx_path"] == "scenarios/base.inpx" and record["path"] == expected
    assert record["generation"] == 1 and record["pid"] == h.service.pid and record["error_code"] is None


async def test_load_network_additive_keeps_the_loaded_file(
    setup: tuple[Harness, FastMCP], projects: Path
) -> None:
    h, mcp = await connected(setup)
    await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    (projects / "scenarios" / "extra.inpx").write_text("<n/>", encoding="utf-8")
    payload = await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/extra.inpx", "additive": True})
    assert payload["additive"] is True and h.vissim.last_load_additive is True
    assert h.service.loaded_file == str((projects / "scenarios" / "base.inpx").resolve())
    assert h.service.is_loaded_file(projects / "scenarios" / "extra.inpx")


async def test_load_network_errors(
    setup: tuple[Harness, FastMCP], projects: Path, tmp_path: Path, isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    not_connected = await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    assert not_connected["error"]["code"] == "NOT_CONNECTED"
    await call(mcp, "vissim_connect")
    outside = tmp_path / "elsewhere.inpx"
    outside.write_text("x", encoding="utf-8")
    cases = {
        str(outside): "PATH_DENIED",
        "scenarios/missing.inpx": "FILE_NOT_FOUND",
        "scenarios/base.layx": "INVALID_ARGUMENT",
        "scenarios/base.inpx:stream": "INVALID_ARGUMENT",
    }
    for path, code in cases.items():
        payload = await call(mcp, "vissim_load_network", {"inpx_path": path})
        assert payload["error"]["code"] == code, path
    bad_layout = await call(
        mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx", "layx_path": "scenarios/base.inpx"}
    )
    assert bad_layout["error"]["code"] == "INVALID_ARGUMENT"
    assert h.vissim.load_count == 0  # nothing reached Vissim
    records = audit_records(isolated_env["audit"])
    assert [r["error_code"] for r in records] == ["NOT_CONNECTED", *cases.values(), "INVALID_ARGUMENT"]
    assert all(r["ok"] is False for r in records)


async def test_load_network_refused_while_a_job_is_active(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = await connected(setup)
    h.service.jobs = StubRegistry(StubJob("job-7", generation=1, state="paused"))
    payload = await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    assert payload["error"]["code"] == "JOB_RUNNING"
    assert payload["error"]["details"]["job_id"] == "job-7"
    h.service.jobs = StubRegistry(StubJob("job-8", generation=1, state="finished"))
    assert (await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"}))["ok"] is True


async def test_load_network_com_error_clears_the_loaded_file(
    setup: tuple[Harness, FastMCP], projects: Path
) -> None:
    h, mcp = await connected(setup)
    await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    h.vissim.reject_non_ascii_paths = True
    folder = projects / "Samarqand_ko'cha_Самарканд"
    folder.mkdir()
    (folder / "tarmoq.inpx").write_text("x", encoding="utf-8")
    payload = await call(mcp, "vissim_load_network", {"inpx_path": str(folder / "tarmoq.inpx")})
    assert payload["error"]["code"] == "COM_ERROR"
    assert h.service.loaded_file is None  # unknown which network Vissim holds now
    assert h.service.is_loaded_file(folder / "tarmoq.inpx")  # still protected against overwriting


async def test_load_network_layout_failure_is_a_warning(
    setup: tuple[Harness, FastMCP], projects: Path
) -> None:
    h, mcp = await connected(setup)
    layout = projects / "scenarios" / "base.layx"

    original_load_net = h.vissim.LoadNet

    def load_net_then_remove_layout(path: str, additive: bool = False) -> None:
        original_load_net(path, additive)
        layout.unlink()

    h.vissim.LoadNet = load_net_then_remove_layout  # type: ignore[method-assign]
    payload = await call(
        mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx", "layx_path": "scenarios/base.layx"}
    )
    assert payload["ok"] is True and payload["layout_loaded"] is False
    assert "layout could not be loaded" in payload["warnings"][0]


async def test_load_network_timeout_is_timeout_hung(harness: Callable[..., Harness], projects: Path) -> None:
    h = harness({"VISSIM_LOAD_TIMEOUT_S": "0.3"})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    h.vissim.hang_on("LoadNet", 5.0)
    payload = await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    assert payload["error"]["code"] == "TIMEOUT_HUNG"
    h.vissim.release_hang()


async def test_load_from_readonly_original_warns(setup: tuple[Harness, FastMCP]) -> None:
    _h, mcp = await connected(setup)
    payload = await call(mcp, "vissim_load_network", {"inpx_path": "models/original/city.inpx"})
    assert payload["ok"] is True and READ_ONLY_LOAD_WARNING in payload["warnings"]


async def test_load_non_ascii_path_warns(setup: tuple[Harness, FastMCP], projects: Path) -> None:
    h, mcp = await connected(setup)
    folder = projects / "Samarqand_ko'cha" / "Самарканд"
    folder.mkdir(parents=True)
    (folder / "tarmoq.inpx").write_text("x", encoding="utf-8")
    payload = await call(mcp, "vissim_load_network", {"inpx_path": "Samarqand_ko'cha/Самарканд/tarmoq.inpx"})
    assert payload["ok"] is True and payload["warnings"] == [NON_ASCII_WARNING]
    assert h.vissim.loaded_net_path == str((folder / "tarmoq.inpx").resolve())


# --- vissim_save_network


async def test_save_network_new_file(
    setup: tuple[Harness, FastMCP], projects: Path, isolated_env: dict[str, Path]
) -> None:
    h, mcp = await connected(setup)
    payload = await call(mcp, "vissim_save_network", {"save_as": "scenarios/v2.inpx"})
    target = (projects / "scenarios" / "v2.inpx").resolve()
    assert payload["saved_path"] == str(target) and payload["backup_path"] is None
    assert payload["overwritten"] is False
    assert payload["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert payload["size_bytes"] == target.stat().st_size
    assert h.service.saved_file == str(target)
    record = audit_records(isolated_env["audit"])[-1]
    assert record["tool"] == "vissim_save_network" and record["ok"] is True
    assert record["sha256"] == payload["sha256"] and record["path"] == str(target)
    assert "backup_path" not in record  # nothing was replaced


async def test_save_network_backs_up_an_existing_file(
    setup: tuple[Harness, FastMCP], projects: Path, isolated_env: dict[str, Path]
) -> None:
    _h, mcp = await connected(setup)
    target = projects / "scenarios" / "v2.inpx"
    target.write_text("old content", encoding="utf-8")
    (projects / "scenarios" / "v2.layx").write_text("old layout", encoding="utf-8")
    payload = await call(mcp, "vissim_save_network", {"save_as": str(target)})
    assert payload["overwritten"] is True
    backup = Path(payload["backup_path"])
    assert backup.parent == (projects / "scenarios" / "backups").resolve()
    assert backup.name.startswith("v2_") and backup.suffix == ".inpx"
    assert backup.read_text(encoding="utf-8") == "old content"
    assert Path(payload["layout_backup_path"]).read_text(encoding="utf-8") == "old layout"
    assert target.read_text(encoding="utf-8") != "old content"
    record = audit_records(isolated_env["audit"])[-1]
    assert record["backup_path"] == payload["backup_path"] and record["overwritten"] is True


async def test_save_network_guards_the_loaded_file(setup: tuple[Harness, FastMCP], projects: Path) -> None:
    _h, mcp = await connected(setup)
    await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    refused = await call(mcp, "vissim_save_network", {"save_as": "scenarios/base.inpx"})
    assert refused["error"]["code"] == "INVALID_ARGUMENT" and "overwrite_loaded" in refused["error"]["hint"]
    assert (projects / "scenarios" / "base.inpx").read_text(encoding="utf-8") == "<network/>"
    allowed = await call(
        mcp, "vissim_save_network", {"save_as": "SCENARIOS/BASE.INPX", "overwrite_loaded": True}
    )
    assert allowed["ok"] is True and allowed["backup_path"]
    assert Path(allowed["backup_path"]).read_text(encoding="utf-8") == "<network/>"


async def test_save_network_policy_errors(
    setup: tuple[Harness, FastMCP], projects: Path, tmp_path: Path, isolated_env: dict[str, Path]
) -> None:
    h, mcp = setup
    assert (await call(mcp, "vissim_save_network", {"save_as": "x.inpx"}))["error"]["code"] == "NOT_CONNECTED"
    await call(mcp, "vissim_connect")
    cases = {
        "models/original/copy.inpx": "READ_ONLY_PATH",
        "models/original/city.inpx": "READ_ONLY_PATH",
        str(tmp_path / "outside.inpx"): "PATH_DENIED",
        "scenarios/v2.layx": "INVALID_ARGUMENT",
        "no_such_folder/v2.inpx": "FILE_NOT_FOUND",
    }
    for path, code in cases.items():
        payload = await call(mcp, "vissim_save_network", {"save_as": path, "overwrite_loaded": True})
        assert payload["error"]["code"] == code, path
    assert h.vissim.saved_paths == []
    assert [r["error_code"] for r in audit_records(isolated_env["audit"])] == [
        "NOT_CONNECTED",
        *cases.values(),
    ]


async def test_save_network_checks_the_layout_next_to_the_target(
    setup: tuple[Harness, FastMCP], projects: Path, tmp_path: Path, isolated_env: dict[str, Path]
) -> None:
    """``<target>.layx`` may be rewritten with the network: a hard link to a read-only original (or a link
    out of the allowed directories) must be refused before anything is backed up or saved."""
    h, mcp = await connected(setup)
    original_layout = projects / "models" / "original" / "city.layx"
    original_layout.write_text("<original layout/>", encoding="utf-8")
    linked = projects / "scenarios" / "v2.layx"
    os.link(original_layout, linked)
    payload = await call(mcp, "vissim_save_network", {"save_as": "scenarios/v2.inpx"})
    assert payload["error"]["code"] == "PATH_DENIED"
    assert payload["error"]["details"]["role"] == "layout"
    assert "hard links" in payload["error"]["message"] and "v2.layx" in payload["error"]["message"]
    assert h.vissim.saved_paths == []
    assert not (projects / "scenarios" / "backups").exists()
    assert original_layout.stat().st_nlink == 2  # untouched
    record = audit_records(isolated_env["audit"])[-1]
    assert record["tool"] == "vissim_save_network" and record["error_code"] == "PATH_DENIED"

    outside = tmp_path / "foreign"
    outside.mkdir()
    (outside / "v3.layx").write_text("<foreign/>", encoding="utf-8")
    try:
        os.symlink(outside / "v3.layx", projects / "scenarios" / "v3.layx")
    except OSError:
        return  # symbolic links need a privilege (or developer mode) on Windows
    payload = await call(mcp, "vissim_save_network", {"save_as": "scenarios/v3.inpx"})
    assert payload["error"]["code"] == "PATH_DENIED" and payload["error"]["details"]["role"] == "layout"
    assert h.vissim.saved_paths == []

    plain = await call(mcp, "vissim_save_network", {"save_as": "scenarios/base.inpx"})
    assert plain["ok"] is True and plain["layout_backup_path"]  # a normal companion layout is fine


async def test_save_network_com_error_names_the_backup(
    setup: tuple[Harness, FastMCP], projects: Path
) -> None:
    h, mcp = await connected(setup)
    folder = projects / "Самарканд"
    folder.mkdir()
    (folder / "v.inpx").write_text("previous", encoding="utf-8")
    h.vissim.reject_non_ascii_paths = True
    payload = await call(mcp, "vissim_save_network", {"save_as": str(folder / "v.inpx")})
    assert payload["error"]["code"] == "COM_ERROR"
    assert "previous version is kept" in payload["error"]["message"]
    assert Path(payload["error"]["details"]["backup_path"]).read_text(encoding="utf-8") == "previous"


async def test_audit_failure_does_not_stop_the_save(
    harness: Callable[..., Harness], projects: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "audit-blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    h = harness({"VISSIM_AUDIT_DIR": str(blocker)})
    mcp = build_server(h.service, h.settings)
    await call(mcp, "vissim_connect")
    payload = await call(mcp, "vissim_save_network", {"save_as": "scenarios/v3.inpx"})
    assert payload["ok"] is True
    assert (projects / "scenarios" / "v3.inpx").is_file()
    assert any("audit record" in warning for warning in payload["warnings"])
    refused = await call(mcp, "vissim_save_network", {"save_as": "models/original/x.inpx"})
    assert refused["error"]["code"] == "READ_ONLY_PATH"
    assert any("audit record" in warning for warning in refused["warnings"])


async def test_save_then_reload_round_trip(setup: tuple[Harness, FastMCP]) -> None:
    h, mcp = await connected(setup)
    await call(
        mcp, "vissim_set_attributes", {"path": "Net.Links[1]", "values": {"Name": "Registon ko'chasi"}}
    )
    await call(mcp, "vissim_save_network", {"save_as": "scenarios/named.inpx"})
    await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/base.inpx"})
    assert (await call(mcp, "vissim_get_attributes", {"path": "Net.Links[1]", "attributes": ["Name"]}))[
        "values"
    ] == {"Name": "Link 1"}
    await call(mcp, "vissim_load_network", {"inpx_path": "scenarios/named.inpx"})
    values = (await call(mcp, "vissim_get_attributes", {"path": "Net.Links[1]", "attributes": ["Name"]}))[
        "values"
    ]
    assert values == {"Name": "Registon ko'chasi"}
    assert h.service.loaded_file and h.service.loaded_file.endswith("named.inpx")


async def test_file_tools_audit_schema_invalid_arguments(
    setup: tuple[Harness, FastMCP], isolated_env: dict[str, Path]
) -> None:
    _h, mcp = await connected(setup)
    cases: list[tuple[str, dict[str, Any]]] = [
        ("vissim_save_network", {"save_as": 5}),
        ("vissim_save_network", {"save_as": "scenarios/v9.inpx", "overwrite_loaded": "maybe"}),
        ("vissim_load_network", {"inpx_path": ["scenarios/base.inpx"]}),
        ("vissim_load_network", {"inpx_path": "scenarios/base.inpx", "additive": {"x": 1}}),
    ]
    for name, arguments in cases:
        payload = await call(mcp, name, arguments)
        assert payload["error"]["code"] == "INVALID_ARGUMENT", arguments
    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] != "vissim_connect"]
    assert [(r["tool"], r["error_code"]) for r in records] == [(n, "INVALID_ARGUMENT") for n, _a in cases]
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    save = tools["vissim_save_network"].inputSchema["properties"]
    assert save["save_as"]["type"] == "string" and save["overwrite_loaded"]["type"] == "boolean"
