"""Fake ``pythoncom`` (test double; shadows pywin32 via ``PYTHONPATH=tests/fakes``).

No real COM: it only keeps per-thread initialisation state so that the fake ``Dispatch`` can fail with
``CO_E_NOTINITIALIZED`` on a thread that never called ``CoInitializeEx`` - exactly like real COM. The
production guard ``VISSIM_MCP_FORBID_REAL`` allows dispatching only when this module (``__fake__ = True``) is
loaded.
"""

from __future__ import annotations

import threading
from collections import deque

import pywintypes

__fake__ = True

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2
COINIT_DISABLE_OLE1DDE = 0x4
COINIT_SPEED_OVER_MEMORY = 0x8
RPC_E_CHANGED_MODE = -2147417850

com_error = pywintypes.com_error

_state = threading.local()
_lock = threading.Lock()
#: (function, thread name) of CoInitializeEx / CoUninitialize calls, for assertions.
calls: deque[tuple[str, str]] = deque(maxlen=1000)
#: PumpWaitingMessages calls per thread name.
pump_counts: dict[str, int] = {}


def CoInitializeEx(flags: int) -> None:
    mode = "sta" if flags & COINIT_APARTMENTTHREADED else "mta"
    count = getattr(_state, "count", 0)
    if count > 0 and getattr(_state, "mode", None) != mode:
        raise com_error(RPC_E_CHANGED_MODE, "Cannot change thread mode after it is set.", None, None)
    _state.mode = mode
    _state.count = count + 1
    _record("CoInitializeEx")


def CoInitialize() -> None:
    CoInitializeEx(COINIT_APARTMENTTHREADED)


def CoUninitialize() -> None:
    count = getattr(_state, "count", 0)
    if count > 0:
        _state.count = count - 1
        if _state.count == 0:
            _state.mode = None
    _record("CoUninitialize")


def PumpWaitingMessages(firstMessage: int = 0, lastMessage: int = -1) -> int:
    name = threading.current_thread().name
    with _lock:
        pump_counts[name] = pump_counts.get(name, 0) + 1
    return 0  # 1 would mean WM_QUIT was received


def _is_initialized() -> bool:
    """True if the calling thread has called CoInitializeEx (and not balanced it with CoUninitialize)."""
    return getattr(_state, "count", 0) > 0


def _apartment() -> str | None:
    """``"sta"``, ``"mta"`` or None for the calling thread."""
    return getattr(_state, "mode", None) if _is_initialized() else None


def _record(function: str) -> None:
    with _lock:
        calls.append((function, threading.current_thread().name))
