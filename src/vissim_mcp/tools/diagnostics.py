"""Diagnostic tools (PLAN 7.6): vissim_tail_log, vissim_exec_python.

``vissim_tail_log`` returns the last lines of the server's own log file (``VISSIM_LOG_DIR\\vissim-mcp.log``,
plus its newest rotated backup when the current file is shorter than requested). There is no path parameter:
no other file can be read through it. The file is read from its end in blocks (never the whole file), off the
event loop; a missing, rotated or locked file is reported as a warning, not an error.

``vissim_exec_python`` runs a piece of Python on the COM worker thread (PLAN 8.3). It is disabled unless
``VISSIM_MCP_ALLOW_EXEC=1`` (``EXEC_DISABLED``); every call - the refused ones too - writes one audit record
with the full code (PLAN 6.8). The code sees only ``Vissim`` (the COM root) and ``result`` (plus the
builtins); its value of ``result`` is returned through ``to_jsonable``. ``print`` inside the code is captured
and returned as ``output``; ``input``/``breakpoint``/``help`` are disabled because stdin is the MCP stream.
Anything else the code writes to stdout ends up on stderr (``stdio_guard``), never on the protocol stream.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import io
import os
import traceback
from pathlib import Path
from types import CodeType
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..errors import ErrorCode, VissimMcpError, from_com_error, is_com_error, is_connection_error
from ..jobs import job_running_error
from ..logging_setup import log_file_path
from ..output import dumps, to_jsonable
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, StringArg, register_tool
from .signals import running_job

DEFAULT_TAIL_LINES = 100
MAX_TAIL_LINES = 500
#: At most this many bytes are read from the end of each log file (500 lines of about 4 KB each).
MAX_TAIL_BYTES = 2_000_000
TAIL_BLOCK_BYTES = 64 * 1024
MAX_LINE_CHARS = 2000
#: Room kept free for the other fields and the warnings when the lines are cut to the response size.
_TAIL_RESERVE_CHARS = 2000

EXEC_FILENAME = "<vissim_exec_python>"
MAX_CODE_CHARS = 100_000
MAX_OUTPUT_CHARS = 20_000
MAX_TRACEBACK_CHARS = 4000


# --- vissim_tail_log


def _tail_file(path: Path, count: int) -> tuple[list[str], bool]:
    """The last ``count`` lines of ``path`` (read from the end); second value: the file has more lines.

    Raises ``OSError`` (missing, locked, ...) to the caller.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        position = end
        data = b""
        while position > 0 and data.count(b"\n") <= count and end - position < MAX_TAIL_BYTES:
            step = min(TAIL_BLOCK_BYTES, position, MAX_TAIL_BYTES - (end - position))
            position -= step
            handle.seek(position)
            block = handle.read(step)
            data = block + data
            if len(block) < step:  # the file shrank while reading (rotated): keep what we have
                break
    lines = data.decode("utf-8", errors="replace").splitlines()
    more = position > 0
    if more and lines:
        lines = lines[1:]  # the first line is (probably) cut
    if len(lines) > count:
        lines, more = lines[-count:], True
    return lines, more


def read_log_tail(path: Path, count: int) -> dict[str, Any]:
    """Last ``count`` lines of the server log at ``path``; older lines from ``<path>.1`` fill up a short file.

    Never raises: a missing, unreadable (locked) or rotated file becomes a warning.
    """
    warnings: list[str] = []
    files: list[str] = []
    lines: list[str] = []
    more = False
    try:
        lines, more = _tail_file(path, count)
        files.append(str(path))
    except FileNotFoundError:
        warnings.append(f"the server log file {path} does not exist (yet)")
    except OSError as exc:
        warnings.append(f"the server log file {path} could not be read ({type(exc).__name__}: {exc})")
    if len(lines) < count and not more:
        backup = path.with_name(path.name + ".1")
        try:
            older, more = _tail_file(backup, count - len(lines))
        except FileNotFoundError:
            pass
        except OSError as exc:
            warnings.append(f"the rotated log file {backup} could not be read ({type(exc).__name__}: {exc})")
        else:
            lines = older + lines
            files.insert(0, str(backup))
    shortened = False
    for index, line in enumerate(lines):
        if len(line) > MAX_LINE_CHARS:
            lines[index] = line[: MAX_LINE_CHARS - 3] + "..."
            shortened = True
    if shortened:
        warnings.append(f"lines longer than {MAX_LINE_CHARS} characters were shortened")
    return {"lines": lines, "more": more, "files": files, "warnings": warnings}


def fit_tail(lines: list[str], max_chars: int) -> tuple[list[str], bool]:
    """Drop the oldest lines until the JSON of the rest fits in ``max_chars`` (minus a reserve)."""
    budget = max_chars - _TAIL_RESERVE_CHARS
    used = 2
    kept = 0
    for line in reversed(lines):
        size = len(dumps(line)) + 1
        if used + size > budget:
            break
        used += size
        kept += 1
    if kept == len(lines):
        return lines, False
    return lines[len(lines) - kept :], True


# --- vissim_exec_python


class _BoundedOutput(io.StringIO):
    """Collects ``print`` output of the executed code, at most ``limit`` characters."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit
        self.truncated = False
        self._size = 0

    def write(self, text: str) -> int:
        room = self.limit - self._size
        if room <= 0:
            self.truncated = self.truncated or bool(text)
            return len(text)
        if len(text) > room:
            self.truncated = True
            text_part = text[:room]
        else:
            text_part = text
        self._size += len(text_part)
        super().write(text_part)
        return len(text)


def _disabled(name: str) -> Any:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            f"{name}() is not available in vissim_exec_python (stdin is the MCP protocol stream)"
        )

    return refuse


def exec_builtins(output: _BoundedOutput) -> dict[str, Any]:
    """A copy of the builtins whose ``print`` writes to ``output`` and whose stdin readers are disabled."""
    namespace = dict(vars(builtins))
    real_print = builtins.print

    def captured_print(
        *args: Any, sep: str | None = " ", end: str | None = "\n", file: Any = None, flush: bool = False
    ) -> None:
        real_print(*args, sep=sep, end=end, file=output if file is None else file, flush=False)

    namespace["print"] = captured_print
    for name in ("input", "breakpoint", "help", "license"):
        namespace[name] = _disabled(name)
    # site's exit()/quit() close sys.stdin - the MCP protocol stream - before raising SystemExit
    namespace["exit"] = namespace["quit"] = _exit
    return namespace


def _exit(code: Any = None) -> Any:
    raise SystemExit(code)


def compile_code(code: Any) -> CodeType:
    if not isinstance(code, str) or not code.strip():
        raise VissimMcpError(ErrorCode.INVALID_ARGUMENT, "code must be a non-empty string of Python", hint="")
    if len(code) > MAX_CODE_CHARS:
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT,
            f"code must be at most {MAX_CODE_CHARS} characters, got {len(code)}",
            hint="",
        )
    try:
        return compile(code, EXEC_FILENAME, "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        line = getattr(exc, "lineno", None)
        where = f" (line {line})" if line else ""
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT,
            f"the code does not compile{where}: {type(exc).__name__}: {exc}",
            hint="",
        ) from None


def _code_traceback(exc: BaseException) -> str:
    """The traceback limited to the frames of the executed code."""
    frames = [frame for frame in traceback.extract_tb(exc.__traceback__) if frame.filename == EXEC_FILENAME]
    text = "".join(traceback.format_list(frames)) + "".join(traceback.format_exception_only(type(exc), exc))
    return text if len(text) <= MAX_TRACEBACK_CHARS else "..." + text[-(MAX_TRACEBACK_CHARS - 3) :]


def exec_task(compiled: CodeType, service: VissimService, ctx: WorkerContext) -> dict[str, Any]:
    """Run ``compiled`` with ``{"Vissim", "result"}`` on the COM worker; return the JSON-able ``result``.

    Conversion (``to_jsonable``) happens here, on the worker thread, so no COM object leaves it. A job resumed
    after the tool's own check is caught here, right before the code runs.
    """
    job = running_job(service)
    if job is not None:
        raise job_running_error(job)
    output = _BoundedOutput(MAX_OUTPUT_CHARS)
    namespace: dict[str, Any] = {"__builtins__": exec_builtins(output), "Vissim": ctx.vissim, "result": None}
    try:
        exec(compiled, namespace)
    except Exception as exc:
        if is_connection_error(exc):
            raise
        error = _code_error(exc, output)
        namespace.clear()
        raise error from None
    except SystemExit as exc:
        namespace.clear()
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT,
            f"the code called exit() / raised SystemExit({exc.code!r})",
            hint="Set result instead of exiting.",
            details=_output_details(output),
        ) from None
    warnings: list[str] = []
    result = to_jsonable(namespace.get("result"), warnings)
    namespace.clear()
    data: dict[str, Any] = {"result": result, "output": output.getvalue(), "warnings": warnings}
    if output.truncated:
        data["warnings"].append(f"output was cut to {MAX_OUTPUT_CHARS} characters")
    return data


def _output_details(output: _BoundedOutput) -> dict[str, Any]:
    text = output.getvalue()
    return {"output": text} if text else {}


def _code_error(exc: Exception, output: _BoundedOutput) -> VissimMcpError:
    details = {"traceback": _code_traceback(exc), **_output_details(output)}
    if is_com_error(exc):
        error = from_com_error(exc)
        error.message = f"the code failed with a COM error: {error.message}"
        error.details = {**(error.details or {}), **details}
        return error
    if isinstance(exc, VissimMcpError):
        exc.details = {**(exc.details or {}), **details}
        return exc
    text = " ".join(str(exc).split())[:300]
    return VissimMcpError(
        ErrorCode.INVALID_ARGUMENT,
        f"the code raised {type(exc).__name__}: {text}" if text else f"the code raised {type(exc).__name__}",
        hint="See details.traceback; fix the code and call again.",
        details=details,
    )


# --- tools


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_tail_log(lines: int = DEFAULT_TAIL_LINES) -> dict[str, Any]:
        """Return the last lines of the MCP server's own log file (vissim-mcp.log in VISSIM_LOG_DIR) for
        troubleshooting: tool calls, errors with their tracebacks, Vissim start-up and shut-down. Never calls
        COM; answers also while Vissim is busy. Only this log file can be read - there is no path parameter.
        When the current file is shorter than requested (just rotated), the newest rotated file fills up.

        Parameters: lines - number of lines, 1..500 (default 100; more is reduced to 500).
        Returns: path (the log file), lines (oldest first), returned, more (older lines exist), files (the
        files read), warnings (e.g. the log file does not exist yet or is locked).
        Errors: INVALID_ARGUMENT.

        Example: vissim_tail_log(lines=50)
        """
        if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
            raise VissimMcpError(
                ErrorCode.INVALID_ARGUMENT, f"lines must be an integer >= 1, got {lines!r}", hint=""
            )
        warnings: list[str] = []
        if lines > MAX_TAIL_LINES:
            warnings.append(f"lines {lines} was reduced to {MAX_TAIL_LINES}")
            lines = MAX_TAIL_LINES
        path = log_file_path(settings)
        data = await asyncio.to_thread(read_log_tail, path, lines)
        warnings.extend(data["warnings"])
        kept, cut = fit_tail(data["lines"], settings.max_result_chars - len(dumps(warnings)))
        if cut:
            warnings.append(
                f"the {len(data['lines']) - len(kept)} oldest line(s) were left out to fit the response size "
                "(VISSIM_MAX_RESULT_CHARS)"
            )
        return {
            "path": str(path),
            "lines": kept,
            "returned": len(kept),
            "more": data["more"] or cut,
            "files": data["files"],
            "warnings": warnings,
        }

    async def vissim_exec_python(code: StringArg) -> dict[str, Any]:
        """Run a short piece of Python against the Vissim COM object - a last resort for what the other tools
        cannot do. Disabled unless the server runs with VISSIM_MCP_ALLOW_EXEC=1 (EXEC_DISABLED); use it only
        on a test PC. Every call, also a refused one, is written to the audit log with the full code.

        The code runs on the COM worker thread with the normal call timeout (VISSIM_CALL_TIMEOUT_S). Its
        namespace holds only Vissim (the COM root object) and result (None) plus the Python builtins; assign
        the value to return to result (numbers, strings, lists, dicts; other objects become text). print()
        output is returned in output; input() is not available. Refused while a simulation job is running.

        Parameters: code - Python source (statements; at most 100000 characters).
        Returns: result, output (printed text, at most 20000 characters), warnings.
        Errors: EXEC_DISABLED, INVALID_ARGUMENT (syntax error, or the code raised - see details.traceback),
        JOB_RUNNING, NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_exec_python(code="result = [l.AttValue('No') for l in Vissim.Net.Links]")
        """
        async with AuditRecord(service, "vissim_exec_python", {"code": code}) as audit:
            if not settings.allow_exec:
                raise VissimMcpError(ErrorCode.EXEC_DISABLED, "vissim_exec_python is disabled on this server")
            compiled = compile_code(code)
            service.ensure_connected()
            job = running_job(service)
            if job is not None:
                raise job_running_error(job)
            task = functools.partial(exec_task, compiled, service)
            data = await service.call(task, task_name="vissim_exec_python")
            audit.update(result_type=type(data["result"]).__name__, output_chars=len(data["output"]))
        return {
            "result": data["result"],
            "output": data["output"],
            "warnings": [*data["warnings"], *audit.warnings],
        }

    register_tool(
        mcp,
        vissim_tail_log,
        name="vissim_tail_log",
        title="Tail the server log",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_exec_python,
        name="vissim_exec_python",
        title="Run Python against Vissim",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
