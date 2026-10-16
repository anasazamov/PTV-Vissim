"""Fake ``pywintypes`` (test double; shadows pywin32 via ``PYTHONPATH=tests/fakes``).

Provides pywin32-compatible ``com_error`` (args ``(hresult, strerror, excepinfo, argerror)``), ``error``
(Win32 API error, args ``(winerror, funcname, strerror)``) and ``TimeType``/``Time``. The MCP SDK imports
``pywintypes`` on Windows (stdio client), so this module must stay importable and side-effect free.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

__fake__ = True


class error(Exception):
    """Win32 API error: ``args == (winerror, funcname, strerror)``."""

    @property
    def winerror(self) -> Any:
        return self.args[0] if len(self.args) > 0 else None

    @property
    def funcname(self) -> Any:
        return self.args[1] if len(self.args) > 1 else None

    @property
    def strerror(self) -> Any:
        return self.args[2] if len(self.args) > 2 else None


class com_error(Exception):
    """COM error: ``args == (hresult, strerror, excepinfo, argerror)``.

    ``excepinfo`` is None or ``(wCode, source, description, helpfile, helpcontext, scode)``.
    """

    @property
    def hresult(self) -> Any:
        return self.args[0] if len(self.args) > 0 else None

    @property
    def strerror(self) -> Any:
        return self.args[1] if len(self.args) > 1 else None

    @property
    def excepinfo(self) -> Any:
        return self.args[2] if len(self.args) > 2 else None

    @property
    def argerror(self) -> Any:
        return self.args[3] if len(self.args) > 3 else None


class TimeType(_dt.datetime):
    """pywin32 (>= 300) returns COM dates as a ``datetime`` subclass with a ``Format`` method."""

    def Format(self, fmt: str = "%c") -> str:
        return self.strftime(fmt)


def Time(value: Any) -> TimeType:
    """``pywintypes.Time(datetime | timestamp)``."""
    if isinstance(value, _dt.datetime):
        return TimeType(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            tzinfo=value.tzinfo,
        )
    if isinstance(value, (int, float)):
        moment = _dt.datetime.fromtimestamp(value)
        return Time(moment)
    raise TypeError(f"cannot convert {type(value).__name__} to a pywintypes time")
