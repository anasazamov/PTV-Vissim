"""FakeBackend: the ``ComBackend`` interface (implementation contract section 4) around ``FakeVissim``.

For fast in-process unit tests of the worker, service and tools - no pywin32, no subprocess. It mirrors the
real backend's rules: ``dispatch`` fails with ``CO_E_NOTINITIALIZED`` on a thread that did not call
``co_initialize``, unknown ProgIDs fail with ``CO_E_CLASSSTRING``, and the created ``FakeVissim`` is bound to
the dispatching thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from fake_vissim import (
    CO_E_CLASSSTRING,
    CO_E_NOTINITIALIZED,
    PLUMBING_MEMBERS,
    FakeVissim,
    is_registered_progid,
    make_com_error,
)
from vissim_mcp.errors import ErrorCode, VissimMcpError, is_com_error

_INVKIND_TO_KIND = {1: "method", 2: "property_get", 4: "property_put", 8: "property_put"}
_PLUMBING_NAMES = frozenset(name for name, _memid in PLUMBING_MEMBERS)


class FakeBackend:
    """In-process ComBackend: ``co_initialize``, ``co_uninitialize``, ``dispatch``, ``pump_messages``,
    ``is_com_error``, ``type_info_members``.

    ``dispatch_delay_s`` blocks ``dispatch`` (until ``release_dispatch()``), ``dispatch_error`` (an HRESULT or
    an exception instance) makes it fail; every other keyword argument is passed to ``FakeVissim``.
    """

    name = "fake"

    def __init__(
        self,
        *,
        factory: Callable[[str], FakeVissim] | None = None,
        dispatch_delay_s: float = 0.0,
        dispatch_error: int | BaseException | None = None,
        **vissim_options: Any,
    ) -> None:
        self._factory = factory or (lambda progid: FakeVissim(progid, **vissim_options))
        self.dispatch_delay_s = dispatch_delay_s
        self.dispatch_error = dispatch_error
        self.instances: list[FakeVissim] = []
        self.events: list[tuple[str, str]] = []
        self.pump_count = 0
        self._init_counts: dict[int, int] = {}
        self._lock = threading.Lock()
        self._dispatch_release = threading.Event()

    # --- ComBackend

    def co_initialize(self) -> None:
        ident = threading.get_ident()
        with self._lock:
            self._init_counts[ident] = self._init_counts.get(ident, 0) + 1
        self._event("co_initialize")

    def co_uninitialize(self) -> None:
        ident = threading.get_ident()
        with self._lock:
            count = self._init_counts.get(ident, 0)
            if count <= 1:
                self._init_counts.pop(ident, None)
            else:
                self._init_counts[ident] = count - 1
        self._event("co_uninitialize")

    def dispatch(self, progid: str) -> FakeVissim:
        self._event("dispatch")
        if not self.is_initialized():
            raise make_com_error(CO_E_NOTINITIALIZED)
        if not is_registered_progid(progid):
            raise make_com_error(CO_E_CLASSSTRING)
        if self.dispatch_delay_s > 0:
            self._dispatch_release.wait(self.dispatch_delay_s)
        error = self.dispatch_error
        if isinstance(error, BaseException):
            raise error
        if error is not None:
            raise make_com_error(int(error))
        vissim = self._factory(progid)
        with self._lock:
            self.instances.append(vissim)
        return vissim

    def pump_messages(self) -> None:
        with self._lock:
            self.pump_count += 1

    def is_com_error(self, exc: BaseException) -> bool:
        return is_com_error(exc)

    def type_info_members(self, obj: Any) -> list[dict[str, str]]:
        """Members from COM type info: plumbing removed, de-duplicated, sorted by (name, kind)."""
        try:
            type_info = obj._oleobj_.GetTypeInfo()
        except AttributeError:
            raise VissimMcpError(ErrorCode.INFO_UNAVAILABLE, "object has no COM type information") from None
        except Exception as exc:
            if is_com_error(exc):
                raise VissimMcpError(
                    ErrorCode.INFO_UNAVAILABLE, "object has no COM type information"
                ) from None
            raise
        members: set[tuple[str, str]] = set()
        for index in range(type_info.GetTypeAttr().cFuncs):
            desc = type_info.GetFuncDesc(index)
            name = type_info.GetNames(desc.memid)[0]
            kind = _INVKIND_TO_KIND.get(desc.invkind)
            if kind is None or name in _PLUMBING_NAMES:
                continue
            members.add((name, kind))
        return [{"name": name, "kind": kind} for name, kind in sorted(members)]

    # --- test API

    def is_initialized(self) -> bool:
        with self._lock:
            return self._init_counts.get(threading.get_ident(), 0) > 0

    def release_dispatch(self) -> None:
        """End a ``dispatch_delay_s`` wait immediately."""
        self._dispatch_release.set()

    @property
    def last(self) -> FakeVissim | None:
        with self._lock:
            return self.instances[-1] if self.instances else None

    def _event(self, name: str) -> None:
        with self._lock:
            self.events.append((name, threading.current_thread().name))
