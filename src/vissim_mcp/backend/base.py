"""The COM backend interface (PLAN 4.1, 4.3).

``worker.py`` and ``jobs.py`` never import ``pythoncom`` / ``win32com``: they reach COM only through a
:class:`ComBackend` - :class:`~vissim_mcp.backend.real.RealComBackend` (pywin32) in production, the test fake
(``tests/fake_backend.py``) in unit tests. Every method except ``is_com_error`` is called on the COM worker
thread.

:func:`type_info_members` lists an object's members from its COM type information ("best effort",
``vissim_describe``). It needs no pywin32 import: it only calls methods of the object's ``_oleobj_``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..errors import (
    ErrorCode,
    VissimMcpError,
    com_error_parts,
    hresult_hex,
    is_com_error,
    is_disconnect_error,
)

# INVOKEKIND and FUNCFLAGS values (oaidl.h) - standard COM constants, not Vissim names.
INVOKE_FUNC = 1
INVOKE_PROPERTYGET = 2
INVOKE_PROPERTYPUT = 4
INVOKE_PROPERTYPUTREF = 8
FUNCFLAG_FRESTRICTED = 0x1

#: IUnknown / IDispatch plumbing that every dispatch interface lists; never shown to the client.
PLUMBING_MEMBERS = frozenset(
    {"QueryInterface", "AddRef", "Release", "GetTypeInfoCount", "GetTypeInfo", "GetIDsOfNames", "Invoke"}
)
#: Upper bound for the FUNCDESC loop (protects against corrupt type information).
MAX_TYPE_INFO_FUNCS = 10_000

_INVKIND_TO_KIND = {
    INVOKE_FUNC: "method",
    INVOKE_PROPERTYGET: "property_get",
    INVOKE_PROPERTYPUT: "property_put",
    INVOKE_PROPERTYPUTREF: "property_put",
}
_NO_TYPE_INFO = "the object exposes no COM type information"


@runtime_checkable
class ComBackend(Protocol):
    """What the COM worker needs from a COM implementation."""

    #: Short backend name for logs and health ("pywin32", "fake").
    name: str

    def co_initialize(self) -> None:
        """Initialise COM on the calling (worker) thread as a single-threaded apartment."""

    def co_uninitialize(self) -> None:
        """Balance :meth:`co_initialize` on the same thread (after all COM references are dropped)."""

    def dispatch(self, progid: str) -> Any:
        """Create the Vissim COM root object (late-bound dispatch, no generated cache)."""

    def pump_messages(self) -> None:
        """Process waiting window messages of the worker thread (an STA must pump)."""

    def is_com_error(self, exc: BaseException) -> bool:
        """True when ``exc`` is a COM error (``pywintypes.com_error`` or a compatible fake)."""

    def type_info_members(self, obj: Any) -> list[dict[str, str]]:
        """``[{name, kind}]`` from the COM type info of ``obj`` (see :func:`type_info_members`)."""


def type_info_members(obj: Any) -> list[dict[str, str]]:
    """Members of ``obj`` from its COM type information: ``[{"name", "kind"}]``, sorted and de-duplicated.

    ``kind`` is ``method``, ``property_get`` or ``property_put``. IUnknown/IDispatch plumbing and restricted
    members are left out. Raises ``VissimMcpError(INFO_UNAVAILABLE)`` when the object has no type information;
    other COM errors (e.g. a disconnected Vissim) propagate unchanged.
    """
    try:
        oleobj = obj._oleobj_
    except AttributeError:
        raise VissimMcpError(ErrorCode.INFO_UNAVAILABLE, _NO_TYPE_INFO) from None
    try:
        type_info = oleobj.GetTypeInfo()
        count = int(type_info.GetTypeAttr().cFuncs)
    except AttributeError:
        raise VissimMcpError(ErrorCode.INFO_UNAVAILABLE, _NO_TYPE_INFO) from None
    except Exception as exc:
        if not is_com_error(exc) or is_disconnect_error(exc):
            raise
        hresult, _, _ = com_error_parts(exc)
        details = {"hresult": hresult, "hresult_hex": hresult_hex(hresult)} if hresult is not None else None
        raise VissimMcpError(ErrorCode.INFO_UNAVAILABLE, _NO_TYPE_INFO, details=details) from None

    members: set[tuple[str, str]] = set()
    for index in range(max(0, min(count, MAX_TYPE_INFO_FUNCS))):
        desc = type_info.GetFuncDesc(index)
        kind = _INVKIND_TO_KIND.get(int(desc.invkind))
        if kind is None or int(getattr(desc, "wFuncFlags", 0) or 0) & FUNCFLAG_FRESTRICTED:
            continue
        names = type_info.GetNames(desc.memid)
        if not names:
            continue
        name = str(names[0])
        if name and name not in PLUMBING_MEMBERS:
            members.add((name, kind))
    return [{"name": name, "kind": kind} for name, kind in sorted(members)]
