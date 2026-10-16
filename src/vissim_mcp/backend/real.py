"""The pywin32 COM backend (PLAN 4.1): ``pythoncom`` + ``win32com.client.dynamic``.

pywin32 is imported lazily inside the methods, never at module import: importing ``pythoncom`` initialises
COM on the importing thread, and that must be the COM worker thread, not the asyncio main thread. The module
therefore also imports on Linux (CI), where every method fails with a clear "pywin32 is not installed" error.

``dispatch`` uses late binding (``dynamic.Dispatch``), never the makepy/gencache layer (a stale cache is a
classic source of failures after Vissim updates or reboots).

Test safety (contract section 0): while ``VISSIM_MCP_FORBID_REAL`` is set, ``dispatch`` refuses to run unless
the loaded ``pythoncom`` and ``win32com.client.dynamic`` are the test fakes (``__fake__ = True``).
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from ..config import ENV_FORBID_REAL, forbid_real_enabled
from ..errors import ErrorCode, VissimMcpError, is_com_error
from .base import type_info_members

_PYWIN32_MISSING = (
    "pywin32 is not available in this Python environment; vissim-mcp needs Windows with pywin32 "
    "(pip install pywin32) to talk to Vissim"
)


class RealComBackend:
    """``ComBackend`` on top of pywin32. All methods but ``is_com_error`` run on the COM worker thread."""

    name = "pywin32"

    def co_initialize(self) -> None:
        pythoncom = _import_pythoncom()
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)

    def co_uninitialize(self) -> None:
        pythoncom = _import_pythoncom()
        pythoncom.CoUninitialize()

    def dispatch(self, progid: str) -> Any:
        try:
            from win32com.client import dynamic
        except ImportError:
            raise VissimMcpError(ErrorCode.INTERNAL, _PYWIN32_MISSING, hint="") from None
        if forbid_real_enabled() and not _fakes_loaded(dynamic):
            raise VissimMcpError(
                ErrorCode.INTERNAL,
                f"real COM dispatch is disabled because {ENV_FORBID_REAL} is set (test mode)",
                hint=f"{ENV_FORBID_REAL} is meant only for the test suite; remove it from the environment.",
            )
        return dynamic.Dispatch(progid)

    def pump_messages(self) -> None:
        _import_pythoncom().PumpWaitingMessages()

    def is_com_error(self, exc: BaseException) -> bool:
        return is_com_error(exc)

    def type_info_members(self, obj: Any) -> list[dict[str, str]]:
        return type_info_members(obj)


def _import_pythoncom() -> ModuleType:
    try:
        import pythoncom
    except ImportError:
        raise VissimMcpError(ErrorCode.INTERNAL, _PYWIN32_MISSING, hint="") from None
    return pythoncom


def _fakes_loaded(dynamic: ModuleType) -> bool:
    """True when both the loaded ``pythoncom`` and ``win32com.client.dynamic`` are the test fakes."""
    pythoncom = sys.modules.get("pythoncom")
    return bool(getattr(pythoncom, "__fake__", False)) and bool(getattr(dynamic, "__fake__", False))
