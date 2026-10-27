"""FastMCP application, stdio transport and process exit (PLAN 4.1, 6.1, 6.4).

Transport: ``mcp.server.stdio.stdio_server(stdin=..., stdout=...)`` (available in the pinned SDK range) with

* stdout = :class:`ProtocolWriter` over the protected stream returned by ``stdio_guard.install()`` - UTF-8, LF
  line ends, every message written completely, nothing buffered;
* stdin = :class:`EofSignallingReader` - when the client closes stdin, the reader thread itself runs the exit
  sequence. Claude Code (Windows) closes stdin and kills the process tree 300 ms later, so the server must not
  wait for the SDK task group, in-flight tool calls or a hung COM worker: ``service.shutdown_fast()`` (<= ~200
  ms), a bounded log flush and ``os._exit``. A watchdog timer guarantees the exit even if one of these steps
  blocks.
"""

from __future__ import annotations

import functools
import io
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import IO, BinaryIO, NoReturn, TextIO, cast

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server

from . import __version__
from .config import Settings
from .logging_setup import flush_logging
from .service import VissimService
from .tools import files, lifecycle, objects

log = logging.getLogger(__name__)

SERVER_NAME = "vissim"
INSTRUCTIONS = (
    "Controls PTV Vissim (traffic simulation) through COM on this Windows PC. "
    "Start every session with vissim_health; if Vissim is not connected, call vissim_connect. "
    "Files must be inside the allowed directories shown by vissim_health; originals in read-only folders are "
    "loaded and saved under a new name. Find exact object and attribute names with vissim_describe. "
    "Before changing attributes run the tool with dry_run=true, show old/new values and get approval. "
    "Nothing is saved automatically: save explicitly with vissim_save_network (to a new file name). "
    "Simulations run as background jobs: vissim_start_simulation returns at once, then poll with "
    "vissim_wait_simulation. Call vissim_close at the end of the work to free the license seat."
)
#: Tool modules in registration order (= the order of tools/list).
TOOL_MODULES = (lifecycle, files, objects)
#: Hard upper bound for the exit sequence (Claude Code kills the process tree 300 ms after closing stdin).
EXIT_WATCHDOG_S = 0.25

_exit_lock = threading.Lock()


def build_server(service: VissimService, settings: Settings) -> FastMCP:
    """Create the FastMCP application and register all tool modules."""
    mcp = FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS, log_level="WARNING")
    mcp._mcp_server.version = __version__  # serverInfo.version (FastMCP would report the SDK version)
    for module in TOOL_MODULES:
        module.register(mcp, service)
    return mcp


class ProtocolWriter:
    """Thread-safe text writer for the protected protocol stream.

    Encodes UTF-8 (lone surrogates replaced), never translates newlines and loops until every byte is written,
    so a partial write on a pipe can never corrupt the JSON-RPC stream.
    """

    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        data = memoryview(text.encode("utf-8", errors="replace"))
        with self._lock:
            while data:
                written = self._raw.write(data)
                if written is None:  # non-blocking stream not ready
                    time.sleep(0.001)
                    continue
                if written <= 0:
                    raise OSError("the protocol stream accepted no data")
                data = data[written:]
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._raw.flush()


class EofSignallingReader:
    """Line reader for the MCP transport that runs ``on_eof`` in the reading thread at end of input.

    A read error (closed or invalid handle) counts as end of input. ``on_eof`` is called at most once; in the
    server it never returns (it exits the process).
    """

    def __init__(self, stream: TextIO, on_eof: Callable[[], None]) -> None:
        self._stream = stream
        self._on_eof = on_eof
        self._eof = False

    def readline(self) -> str:
        if self._eof:
            return ""
        try:
            line = self._stream.readline()
        except (OSError, ValueError):
            log.warning("reading stdin failed; treating it as end of input", exc_info=True)
            line = ""
        if not line:
            self._eof = True
            self._on_eof()
        return line


async def serve(
    proto_out: BinaryIO,
    service: VissimService,
    settings: Settings,
    stdin: BinaryIO | None = None,
    on_eof: Callable[[], None] | None = None,
) -> None:
    """Run one MCP session over stdio until the client goes away."""
    mcp = build_server(service, settings)
    if stdin is None:
        if sys.stdin is None:
            raise RuntimeError(
                "stdin is not available; vissim-mcp must be started by an MCP client over stdio"
            )
        stdin = sys.stdin.buffer
    text_in = io.TextIOWrapper(stdin, encoding="utf-8", errors="replace")
    reader = EofSignallingReader(text_in, on_eof or (lambda: None))
    # anyio's AsyncFile is duck-typed: the reader only needs readline(), the writer write() and flush().
    stdin_stream = anyio.wrap_file(cast("IO[str]", reader))
    stdout_stream = anyio.wrap_file(cast("IO[str]", ProtocolWriter(proto_out)))
    async with stdio_server(stdin=stdin_stream, stdout=stdout_stream) as (read_stream, write_stream):
        low_level = mcp._mcp_server
        await low_level.run(read_stream, write_stream, low_level.create_initialization_options())


def run(proto_out: BinaryIO, service: VissimService, settings: Settings) -> NoReturn:
    """Serve until stdin closes, then exit the process (never returns)."""

    def on_eof() -> None:
        terminate_process(service, 0, "stdin closed")

    exit_code = 0
    reason = "MCP session ended"
    try:
        anyio.run(functools.partial(serve, proto_out, service, settings, None, on_eof))
    except KeyboardInterrupt:
        exit_code, reason = 130, "interrupted"
    except BaseException:
        log.exception("MCP server stopped because of an unexpected error")
        exit_code, reason = 1, "unexpected error"
    terminate_process(service, exit_code, reason)


def terminate_process(service: VissimService, exit_code: int, reason: str) -> NoReturn:
    """PLAN 6.4 exit: shutdown_fast (kills Vissim first when configured), bounded log flush, ``os._exit``.

    Safe to call from any thread, more than once. The watchdog makes the exit unconditional after
    ``EXIT_WATCHDOG_S`` even if a step blocks (e.g. a log handler stuck on a full stderr pipe).
    """
    _arm_exit_watchdog(exit_code)
    with _exit_lock:
        try:
            service.shutdown_fast()
        except BaseException:
            pass
        try:
            log.info("exiting: %s (code %d)", reason, exit_code)
        except BaseException:
            pass
        flush_logging(0.05)
        os._exit(exit_code)


def _arm_exit_watchdog(exit_code: int) -> None:
    try:
        timer = threading.Timer(EXIT_WATCHDOG_S, os._exit, args=(exit_code,))
        timer.daemon = True
        timer.start()
    except Exception:
        pass
