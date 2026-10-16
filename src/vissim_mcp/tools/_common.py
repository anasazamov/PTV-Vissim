"""The single wrapper shared by every MCP tool (implementation contract, section 3).

A tool body is an ``async`` function that returns a dict of result fields (without ``"ok"``) or raises
:class:`~vissim_mcp.errors.VissimMcpError`. :func:`register_tool` wraps it so that the registered tool

* always returns ``CallToolResult`` with one text block of compact JSON - ``{"ok": true, ...}`` or
  ``{"ok": false, "error": {"code", "message", "hint"?}}`` - and ``isError`` mirroring ``ok``;
* never raises to FastMCP: ``VissimMcpError`` becomes an error payload, any other ``Exception`` becomes
  ``INTERNAL`` (traceback to the log, short message to the client);
* converts values with ``to_jsonable`` and fits the result into ``VISSIM_MAX_RESULT_CHARS``;
* logs tool name, outcome and duration at INFO - never argument or attribute values.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ..config import Settings
from ..errors import ErrorCode, VissimMcpError
from ..output import error_payload, fit_payload, ok, to_jsonable, to_tool_result

log = logging.getLogger(__name__)

ToolBody = Callable[..., Awaitable[Mapping[str, Any] | None]]
ToolFunction = Callable[..., Awaitable[CallToolResult]]

MAX_DESCRIPTION_CHARS = 4096
_MAX_INTERNAL_MESSAGE_CHARS = 300
_ENCODING_FAILURE_TEXT = (
    '{"ok":false,"error":{"code":"INTERNAL","message":"failed to encode the tool result",'
    '"hint":"Internal server error; details are in the server log (vissim_tail_log)."}}'
)


def tool_handler(
    name: str, settings: Settings, *, rows_key: str = "rows"
) -> Callable[[ToolBody], ToolFunction]:
    """Decorator turning a tool body into a never-raising tool function returning ``CallToolResult``.

    The wrapper keeps the body's signature (``functools.wraps``), so FastMCP derives the same input schema.
    """

    def decorator(body: ToolBody) -> ToolFunction:
        if not inspect.iscoroutinefunction(body):
            raise TypeError(f"tool {name}: the body must be an async function")

        @functools.wraps(body)
        async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
            started = time.perf_counter()
            payload = await _run_body(name, body, args, kwargs)
            result, final = _finish(name, payload, settings.max_result_chars, rows_key)
            _log_outcome(name, final, time.perf_counter() - started)
            return result

        return wrapper

    return decorator


def register_tool(
    mcp: FastMCP,
    body: ToolBody,
    *,
    name: str,
    title: str,
    settings: Settings,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool | None = None,
    rows_key: str = "rows",
) -> ToolFunction:
    """Wrap ``body`` with :func:`tool_handler` and register it (unstructured output, PLAN 7 annotations)."""
    description = inspect.cleandoc(body.__doc__ or "")
    if not description:
        raise ValueError(f"tool {name}: a docstring (tool description) is required")
    if len(description) >= MAX_DESCRIPTION_CHARS:
        raise ValueError(f"tool {name}: description must be shorter than {MAX_DESCRIPTION_CHARS} characters")
    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )
    function = tool_handler(name, settings, rows_key=rows_key)(body)
    mcp.tool(
        name=name,
        title=title,
        description=description,
        annotations=annotations,
        structured_output=False,
    )(function)
    return function


async def _run_body(
    name: str, body: ToolBody, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        fields = await body(*args, **kwargs)
    except VissimMcpError as err:
        return error_payload(err)
    except Exception as exc:
        log.exception("tool %s failed with an unexpected error", name)
        return error_payload(
            VissimMcpError(
                ErrorCode.INTERNAL, f"internal error in {name}: {type(exc).__name__}: {_short(exc)}"
            )
        )
    if fields is None:
        return ok()
    if not isinstance(fields, Mapping):
        log.error("tool %s returned %s instead of a mapping", name, type(fields).__name__)
        return error_payload(
            VissimMcpError(ErrorCode.INTERNAL, f"internal error in {name}: unexpected result type")
        )
    return ok(**{str(key): value for key, value in fields.items()})


def _finish(
    name: str, payload: dict[str, Any], max_chars: int, rows_key: str
) -> tuple[CallToolResult, dict[str, Any]]:
    try:
        conversion_warnings: list[str] = []
        jsonable = to_jsonable(payload, conversion_warnings)
        if conversion_warnings:
            existing = jsonable.get("warnings")
            merged = list(existing) if isinstance(existing, list) else []
            merged.extend(w for w in conversion_warnings if w not in merged)
            jsonable["warnings"] = merged
        final = fit_payload(jsonable, max_chars, rows_key=rows_key)
        return to_tool_result(final), final
    except Exception:
        log.exception("tool %s: failed to encode the result", name)
        fallback = {"ok": False, "error": {"code": ErrorCode.INTERNAL.value}}
        return CallToolResult(
            content=[TextContent(type="text", text=_ENCODING_FAILURE_TEXT)], isError=True
        ), fallback


def _log_outcome(name: str, payload: Mapping[str, Any], elapsed_s: float) -> None:
    if payload.get("ok"):
        log.info("tool %s ok in %.3f s", name, elapsed_s)
        return
    error = payload.get("error")
    code = error.get("code") if isinstance(error, Mapping) else None
    log.info("tool %s failed (%s) in %.3f s", name, code or "unknown", elapsed_s)


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or "-"
    text = " ".join(text.split())
    if len(text) > _MAX_INTERNAL_MESSAGE_CHARS:
        text = text[: _MAX_INTERNAL_MESSAGE_CHARS - 3] + "..."
    return text
