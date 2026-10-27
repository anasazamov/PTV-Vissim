"""File-descriptor level stdout protection (PLAN 6.1).

Every byte on stdout must be MCP JSON-RPC. ``contextlib.redirect_stdout`` is not enough: it is process-global
Python state and does not catch writes made by C code or DLLs (COM servers, pywin32, ``os.write(1, ...)``).

``install()`` therefore works below Python:

1. ``proto_fd = os.dup(1)`` - a private duplicate of the real stdout; only the MCP transport writes to it.
2. ``os.dup2(2, 1)`` - fd 1 now points to stderr. On Windows ``SetStdHandle(STD_OUTPUT_HANDLE, ...)`` is
   updated too, so native code that calls ``GetStdHandle(STD_OUTPUT_HANDLE)`` also writes to stderr.
3. ``sys.stdout = sys.stderr`` - ``print()`` goes to stderr.
4. The protected stream ``open(proto_fd, "wb", buffering=0)`` is returned for the MCP transport.
5. stdin likewise: a private non-inheritable duplicate of fd 0 becomes ``sys.stdin`` (the MCP transport reads
   it), and fd 0 - plus ``STD_INPUT_HANDLE`` on Windows - points at ``os.devnull``. A child process
   (started by ``vissim_exec_python`` code or by a DLL) therefore never inherits the protocol pipe: it can
   neither block on it nor read JSON-RPC bytes from it.

The original stdout handle is closed by step 2, so the protocol pipe is reachable only through ``proto_fd``.
This module imports only the standard library: ``vissim_mcp.__main__`` calls ``install()`` before anything
else is imported.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import BinaryIO, cast

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

_lock = threading.Lock()
_proto_out: BinaryIO | None = None


def install() -> BinaryIO:
    """Redirect fd 1 (and the Windows std output handle) to stderr; return the protected protocol stream.

    Idempotent: later calls return the stream created by the first call. Never raises because of missing
    standard handles (pythonw, detached processes): without a usable stderr, fd 1 and fd 2 both point to
    ``os.devnull``; without a usable stdout the protocol stream itself is ``os.devnull``.
    """
    global _proto_out
    with _lock:
        if _proto_out is not None:
            return _proto_out
        _flush_quietly(sys.stdout)

        if not _fd_is_valid(2):
            # No usable stderr (closed or never opened): point fd 2 at os.devnull, so that stray output and
            # writes to sys.stderr are discarded instead of raising. This must happen before the dup below,
            # which would otherwise receive the free number 2.
            _point_to_devnull(2)
        proto_fd = _dup_above_std(1)
        os.dup2(2, 1)

        if sys.platform == "win32":
            _set_win32_std_handle(STD_OUTPUT_HANDLE, 2)
        _protect_stdin()

        if sys.stderr is None:  # pythonw / detached: Python created no stderr object
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr

        if proto_fd is None:
            _proto_out = cast(BinaryIO, open(os.devnull, "wb", buffering=0))
        else:
            _proto_out = cast(BinaryIO, open(proto_fd, "wb", buffering=0))
        return _proto_out


def is_installed() -> bool:
    """True once ``install()`` has run in this process."""
    return _proto_out is not None


def _flush_quietly(stream: object) -> None:
    try:
        stream.flush()  # type: ignore[attr-defined]
    except Exception:
        pass


def _fd_is_valid(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _dup_above_std(fd: int) -> int | None:
    """``os.dup(fd)`` that never returns 0, 1 or 2 (those numbers may be free when std handles are missing);
    None if ``fd`` itself is not open."""
    temporaries: list[int] = []
    try:
        new_fd = os.dup(fd)
        while new_fd <= 2:
            temporaries.append(new_fd)
            new_fd = os.dup(fd)
        return new_fd
    except OSError:
        return None
    finally:
        for temporary in temporaries:
            os.close(temporary)


def _point_to_devnull(fd: int) -> None:
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    if devnull_fd != fd:
        os.dup2(devnull_fd, fd)
        os.close(devnull_fd)


def _protect_stdin() -> None:
    """Move the protocol stdin to a private non-inheritable fd (``sys.stdin``); fd 0 becomes ``os.devnull``.

    Without a usable stdin nothing changes (the server then reports that stdin is missing). Best effort: on a
    failure the original stdin stays in place.
    """
    if not _fd_is_valid(0):
        return
    private_fd = _dup_above_std(0)  # os.dup: non-inheritable (PEP 446)
    if private_fd is None:
        return
    try:
        devnull_fd = os.open(os.devnull, os.O_RDONLY)
        if devnull_fd != 0:
            os.dup2(devnull_fd, 0)
            os.close(devnull_fd)
    except OSError as exc:  # pragma: no cover - only on broken environments
        os.close(private_fd)
        _warn(f"stdin could not be moved to a private descriptor: {exc}")
        return
    if sys.platform == "win32":
        _set_win32_std_handle(STD_INPUT_HANDLE, 0)
    sys.stdin = open(private_fd, encoding="utf-8", errors="replace")


def _warn(text: str) -> None:
    try:
        sys.stderr.write(f"vissim-mcp: warning: {text}\n")
    except Exception:
        pass


def _set_win32_std_handle(std_handle: int, source_fd: int) -> None:
    """Point the Windows standard handle ``std_handle`` at the OS handle behind ``source_fd``. Best effort."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_std_handle = kernel32.SetStdHandle
        set_std_handle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
        set_std_handle.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(source_fd)
        if not set_std_handle(std_handle & 0xFFFFFFFF, handle):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception as exc:  # pragma: no cover - only on broken Windows environments
        _warn(f"SetStdHandle({std_handle}) failed: {exc}")
