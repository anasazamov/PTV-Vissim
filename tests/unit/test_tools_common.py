"""tools/_common.py: the wrapper every tool goes through (contract section 3)."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult

from vissim_mcp.config import Settings
from vissim_mcp.errors import ErrorCode, VissimMcpError
from vissim_mcp.output import NONFINITE_WARNING, TRUNCATION_HINT
from vissim_mcp.tools._common import MAX_DESCRIPTION_CHARS, register_tool, tool_handler


def payload_of(result: CallToolResult) -> dict[str, Any]:
    assert isinstance(result, CallToolResult)
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def test_success_becomes_ok_payload(settings: Settings) -> None:
    @tool_handler("t_ok", settings)
    async def body(x: int = 1) -> dict[str, Any]:
        return {"value": x, "path": "D:\\Модели\\a.inpx"}

    result = await body(x=5)
    assert result.isError is False
    assert payload_of(result) == {"ok": True, "value": 5, "path": "D:\\Модели\\a.inpx"}


async def test_vissim_error_becomes_error_payload(settings: Settings) -> None:
    @tool_handler("t_err", settings)
    async def body() -> dict[str, Any]:
        raise VissimMcpError(ErrorCode.NOT_CONNECTED, "Vissim is not connected")

    result = await body()
    assert result.isError is True
    payload = payload_of(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_CONNECTED"
    assert "vissim_connect" in payload["error"]["hint"]


async def test_unexpected_exception_becomes_internal(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    @tool_handler("t_boom", settings)
    async def body() -> dict[str, Any]:
        raise RuntimeError("boom\n  details")

    with caplog.at_level(logging.INFO, logger="vissim_mcp"):
        result = await body()
    payload = payload_of(result)
    assert result.isError is True
    assert payload["error"]["code"] == "INTERNAL"
    assert payload["error"]["message"] == "internal error in t_boom: RuntimeError: boom details"
    assert any(record.exc_info for record in caplog.records)  # traceback goes to the log


async def test_cancellation_is_not_swallowed(settings: Settings) -> None:
    @tool_handler("t_cancel", settings)
    async def body() -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await body()


async def test_conversion_warnings_are_merged(settings: Settings) -> None:
    @tool_handler("t_nan", settings)
    async def body() -> dict[str, Any]:
        return {"v": float("nan"), "warnings": ["from the tool"]}

    payload = payload_of(await body())
    assert payload["v"] is None
    assert payload["warnings"] == ["from the tool", NONFINITE_WARNING]


async def test_results_are_fitted_to_max_result_chars(settings: Settings) -> None:
    small = dataclasses.replace(settings, max_result_chars=1000)

    @tool_handler("t_rows", small)
    async def body() -> dict[str, Any]:
        return {
            "rows": [[i, "x" * 40] for i in range(500)],
            "total": 500,
            "returned": 500,
            "truncated": False,
        }

    result = await body()
    assert len(result.content[0].text) <= 1000
    payload = payload_of(result)
    assert payload["truncated"] is True and payload["hint"] == TRUNCATION_HINT


async def test_none_and_non_mapping_results(settings: Settings) -> None:
    @tool_handler("t_none", settings)
    async def nothing() -> None:
        return None

    @tool_handler("t_list", settings)
    async def wrong() -> Any:
        return [1, 2]

    assert payload_of(await nothing()) == {"ok": True}
    assert payload_of(await wrong())["error"]["code"] == "INTERNAL"


def test_sync_bodies_are_rejected(settings: Settings) -> None:
    def body() -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        tool_handler("t_sync", settings)(body)  # type: ignore[arg-type]


def test_signature_and_docstring_are_preserved(settings: Settings) -> None:
    async def body(path: str, attributes: list[str], dry_run: bool = False) -> dict[str, Any]:
        """Doc."""
        return {}

    wrapped = tool_handler("t_sig", settings)(body)
    assert list(inspect.signature(wrapped).parameters) == ["path", "attributes", "dry_run"]
    assert wrapped.__doc__ == "Doc."
    assert inspect.iscoroutinefunction(wrapped)


async def test_logging_records_outcome_but_no_values(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    @tool_handler("t_log", settings)
    async def body(secret: str) -> dict[str, Any]:
        return {"echo": secret}

    with caplog.at_level(logging.INFO, logger="vissim_mcp"):
        await body(secret="SECRET-ATTRIBUTE-VALUE")
    assert any("tool t_log ok in" in record.getMessage() for record in caplog.records)
    assert "SECRET-ATTRIBUTE-VALUE" not in caplog.text


async def test_register_tool_sets_annotations_and_clean_description(settings: Settings) -> None:
    mcp = FastMCP("test", log_level="WARNING")

    async def x_tool(dry_run: bool = False) -> dict[str, Any]:
        """Change something.

        Details:
            Indented detail line.
        """
        return {"dry_run": dry_run}

    register_tool(
        mcp, x_tool, name="x_tool", title="X tool", settings=settings, read_only=False, destructive=True
    )
    (tool,) = await mcp.list_tools()
    assert tool.name == "x_tool"
    assert tool.description == "Change something.\n\nDetails:\n    Indented detail line."
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.openWorldHint is False
    assert tool.inputSchema["properties"]["dry_run"]["default"] is False
    result = await mcp.call_tool("x_tool", {"dry_run": True})
    assert payload_of(result) == {"ok": True, "dry_run": True}  # type: ignore[arg-type]


def test_register_tool_requires_a_short_docstring(settings: Settings) -> None:
    mcp = FastMCP("test", log_level="WARNING")

    async def undocumented() -> dict[str, Any]:
        return {}

    async def verbose() -> dict[str, Any]:
        return {}

    verbose.__doc__ = "x" * MAX_DESCRIPTION_CHARS
    with pytest.raises(ValueError, match="docstring"):
        register_tool(mcp, undocumented, name="u", title="U", settings=settings, read_only=True)
    with pytest.raises(ValueError, match="shorter than"):
        register_tool(mcp, verbose, name="v", title="V", settings=settings, read_only=True)
