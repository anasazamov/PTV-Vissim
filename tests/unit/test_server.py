"""server.py: FastMCP application, protocol writer and EOF-signalling reader (in-process parts)."""

from __future__ import annotations

import io
import json
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from vissim_mcp import __version__
from vissim_mcp.config import Settings
from vissim_mcp.server import EofSignallingReader, ProtocolWriter, build_server
from vissim_mcp.service import VissimService


async def test_build_server_registers_all_tool_modules(service: VissimService, settings: Settings) -> None:
    mcp = build_server(service, settings)
    tools = await mcp.list_tools()
    assert [tool.name for tool in tools] == [
        "vissim_connect",
        "vissim_health",
        "vissim_info",
        "vissim_restart",
        "vissim_close",
        "vissim_load_network",
        "vissim_save_network",
        "vissim_describe",
        "vissim_list_objects",
        "vissim_get_attributes",
        "vissim_set_attributes",
        "vissim_set_attribute_bulk",
        "vissim_configure_simulation",
        "vissim_start_simulation",
        "vissim_simulation_status",
        "vissim_wait_simulation",
        "vissim_resume_simulation",
        "vissim_stop_simulation",
        "vissim_export_csv",
        "vissim_set_signal_state",
        "vissim_tail_log",
        "vissim_exec_python",
    ]
    assert len(tools) == 22  # PLAN 7: the complete v1.0 catalogue
    for tool in tools:
        assert tool.description and len(tool.description) < 4096, tool.name
        assert f"Example: {tool.name}(" in tool.description, tool.name
    tool = tools[1]
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.inputSchema.get("properties", {}) == {}
    assert tool.description and tool.description.startswith("Report the MCP server")
    assert len(tool.description) < 4096
    assert mcp.name == "vissim"
    assert mcp._mcp_server.version == __version__
    assert mcp.instructions and "vissim_health" in mcp.instructions


async def test_vissim_health_in_process(
    service: VissimService, settings: Settings, isolated_env: dict[str, Path]
) -> None:
    mcp = build_server(service, settings)
    result = await mcp.call_tool("vissim_health", {})
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["connected"] is False
    assert payload["pid"] is None
    assert payload["version"] == __version__
    assert payload["mcp_version"] == metadata.version("mcp")
    assert payload["settings"]["VISSIM_ALLOWED_DIRS"] == [str(isolated_env["projects"].resolve())]
    assert "warnings" not in payload  # clean configuration -> no warnings key


class TrickleRaw(io.RawIOBase):
    """A raw stream that accepts at most ``chunk`` bytes per write call (like a partial pipe write)."""

    def __init__(self, chunk: int) -> None:
        self.chunk = chunk
        self.data = bytearray()
        self.flushes = 0

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        part = bytes(b[: self.chunk])
        self.data.extend(part)
        return len(part)

    def flush(self) -> None:
        self.flushes += 1


def test_protocol_writer_completes_partial_writes_and_keeps_lf() -> None:
    raw = TrickleRaw(chunk=3)
    writer = ProtocolWriter(raw)  # type: ignore[arg-type]
    text = '{"jsonrpc":"2.0","result":"Самарқанд ko\'cha"}\n'
    assert writer.write(text) == len(text)
    writer.flush()
    assert bytes(raw.data) == text.encode("utf-8")
    assert b"\r\n" not in raw.data
    assert raw.flushes == 1


def test_protocol_writer_replaces_lone_surrogates() -> None:
    raw = TrickleRaw(chunk=1000)
    ProtocolWriter(raw).write("bad \udc80\n")  # type: ignore[arg-type]
    bytes(raw.data).decode("utf-8")


def test_protocol_writer_refuses_a_dead_stream() -> None:
    class Dead(TrickleRaw):
        def write(self, b: Any) -> int:
            return 0

    with pytest.raises(OSError):
        ProtocolWriter(Dead(1)).write("x\n")  # type: ignore[arg-type]


def test_eof_reader_reports_end_of_input_once() -> None:
    calls: list[int] = []
    reader = EofSignallingReader(io.StringIO("first\nsecond\n"), lambda: calls.append(1))
    assert reader.readline() == "first\n"
    assert reader.readline() == "second\n"
    assert calls == []
    assert reader.readline() == ""
    assert reader.readline() == ""
    assert calls == [1]


def test_eof_reader_treats_read_errors_as_end_of_input() -> None:
    class Broken:
        def readline(self) -> str:
            raise OSError(6, "The handle is invalid")

    calls: list[int] = []
    reader = EofSignallingReader(Broken(), lambda: calls.append(1))  # type: ignore[arg-type]
    assert reader.readline() == ""
    assert calls == [1]
