"""The single wrapper shared by every MCP tool (implementation contract, section 3).

A tool body is an ``async`` function that returns a dict of result fields (without ``"ok"``) or raises
:class:`~vissim_mcp.errors.VissimMcpError`. :func:`register_tool` wraps it so that the registered tool

* always returns ``CallToolResult`` with one text block of compact JSON - ``{"ok": true, ...}`` or
  ``{"ok": false, "error": {"code", "message", "hint"?}}`` - and ``isError`` mirroring ``ok``;
* never raises to FastMCP: ``VissimMcpError`` becomes an error payload, any other ``Exception`` becomes
  ``INTERNAL`` (traceback to the log, short message to the client);
* converts values with ``to_jsonable`` and fits the result into ``VISSIM_MAX_RESULT_CHARS``;
* logs tool name, outcome and duration at INFO - never argument or attribute values.

:class:`AuditRecord` gives every write tool exactly one audit record per call (PLAN 6.8) - for a success, a
refusal by a policy and a failure alike.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import TypeAdapter, ValidationError, WithJsonSchema

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


# Lenient argument types for write tools. FastMCP validates arguments against the signature *before* the tool
# body runs - such a refusal would bypass the audit record and the {"ok": false, "error": ...} format. These
# types keep the JSON schema the client sees but accept any value; the body validates it (inside its
# AuditRecord) and raises INVALID_ARGUMENT.
StringArg = Annotated[Any, WithJsonSchema({"type": "string"})]
OptionalStringArg = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "null"}]})]
FlagArg = Annotated[Any, WithJsonSchema({"type": "boolean"})]
_BOOL_ADAPTER: TypeAdapter[bool] = TypeAdapter(bool)


def flag(value: Any, field: str) -> bool:
    """A boolean argument, accepted as FastMCP would have accepted it (``true``, ``"false"``, ``0``, ...);
    anything else is ``INVALID_ARGUMENT``."""
    try:
        return _BOOL_ADAPTER.validate_python(value)
    except ValidationError:
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT, f"{field} must be true or false, got {value!r}", hint=""
        ) from None


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
        return error_payload(err, err.warnings)
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


class AuditRecord:
    """Context manager writing exactly one audit record for one write-tool call (PLAN 6.8).

    ``fields`` collects the record (``args`` first; ``old``, ``new``, ``dry_run``, ``sha256``, ... via
    :meth:`update`). On exit the outcome is added - ``ok: true``, or ``ok: false`` with ``error_code`` and
    ``error_message`` (``INTERNAL`` for an unexpected exception, ``cancelled: true`` when the call was
    cancelled) - and the record is written through ``service.write_audit``. Writing never raises: a failure
    becomes a warning in :attr:`warnings` (and in ``VissimMcpError.warnings`` of the error being raised), so
    the tool result tells the client that the operation is missing from the audit log. Tools use it as
    ``async with``: the record is then written in a thread, never on the event loop.
    """

    def __init__(self, service: Any, tool: str, args: Mapping[str, Any]) -> None:
        self._service = service
        self.tool = tool
        self.fields: dict[str, Any] = {"args": dict(args)}
        self.warnings: list[str] = []
        self.written = False

    def update(self, **fields: Any) -> None:
        self.fields.update(fields)

    def __enter__(self) -> AuditRecord:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self._finish(exc, self._write(self._outcome(exc)))
        return False  # never swallow the exception

    async def __aenter__(self) -> AuditRecord:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Like ``__exit__``, but the file write runs in a thread: a slow audit folder (network share,
        virus scanner, stalled disk) must not block the event loop - every other tool and the shutdown."""
        fields = self._outcome(exc)
        self._finish(exc, await asyncio.to_thread(self._write, fields))
        return False

    def _outcome(self, exc: BaseException | None) -> dict[str, Any]:
        fields = dict(self.fields)
        if exc is None:
            fields["ok"] = True
        elif isinstance(exc, VissimMcpError):
            fields.update(ok=False, error_code=exc.code.value, error_message=exc.message)
        elif isinstance(exc, Exception):
            fields.update(
                ok=False,
                error_code=ErrorCode.INTERNAL.value,
                error_message=f"{type(exc).__name__}: {_short(exc)}",
            )
        else:  # asyncio.CancelledError and friends: the client went away while the call was running
            fields.update(ok=False, cancelled=True)
        return fields

    def _write(self, fields: dict[str, Any]) -> str | None:
        try:
            return self._service.write_audit(self.tool, fields)  # type: ignore[no-any-return]
        except Exception as audit_exc:  # write_audit never raises; this is the last line of defence
            log.warning("audit record of %s could not be written: %s", self.tool, audit_exc)
            return f"the audit record of this {self.tool} call could not be written ({_short(audit_exc)})"

    def _finish(self, exc: BaseException | None, warning: str | None) -> None:
        self.written = True
        if warning:
            self.warnings.append(warning)
            if isinstance(exc, VissimMcpError) and warning not in exc.warnings:
                exc.warnings.append(warning)
