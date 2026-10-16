"""Error codes, the server exception type and COM error mapping (PLAN 6.3, 7.7).

COM error texts are localized (on the reference PC they are Russian), so errors are classified by HRESULT
only. This module never imports pywin32: ``is_com_error`` is duck-typed and works with the real
``pywintypes.com_error`` as well as with the test fakes.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """The 16 error codes of PLAN 7.7 (a ``str`` enum: ``ErrorCode.COM_ERROR == "COM_ERROR"``)."""

    NOT_CONNECTED = "NOT_CONNECTED"
    ALREADY_CONNECTED = "ALREADY_CONNECTED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PATH_DENIED = "PATH_DENIED"
    READ_ONLY_PATH = "READ_ONLY_PATH"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    ATTRIBUTE_DENIED = "ATTRIBUTE_DENIED"
    RESOLVE_FAILED = "RESOLVE_FAILED"
    COM_ERROR = "COM_ERROR"
    TIMEOUT_HUNG = "TIMEOUT_HUNG"
    JOB_RUNNING = "JOB_RUNNING"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    EXEC_DISABLED = "EXEC_DISABLED"
    INFO_UNAVAILABLE = "INFO_UNAVAILABLE"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    INTERNAL = "INTERNAL"


#: Default hint per code, used when a ``VissimMcpError`` is raised without an explicit hint.
DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.NOT_CONNECTED: "Call vissim_connect first (vissim_health shows the connection state).",
    ErrorCode.ALREADY_CONNECTED: "Vissim is already connected; use vissim_restart to start a fresh instance.",
    ErrorCode.PATH_DENIED: (
        "Only paths inside VISSIM_ALLOWED_DIRS (or CLAUDE_PROJECT_DIR) are allowed; "
        "vissim_health shows the configured directories."
    ),
    ErrorCode.READ_ONLY_PATH: (
        "This directory is read-only (VISSIM_READONLY_DIRS, default <root>\\models\\original); "
        "save under another name or folder."
    ),
    ErrorCode.FILE_NOT_FOUND: (
        "Check the path; relative paths are resolved against the first allowed directory."
    ),
    ErrorCode.ATTRIBUTE_DENIED: (
        "Writing file/path attributes is disabled (VISSIM_ALLOW_PATH_ATTRS=0) so that file policies "
        "cannot be bypassed."
    ),
    ErrorCode.RESOLVE_FAILED: "Check the object path segment by segment with vissim_describe.",
    ErrorCode.TIMEOUT_HUNG: (
        "Vissim may have a modal dialog open - check its window, or call vissim_restart."
    ),
    ErrorCode.JOB_RUNNING: (
        "A simulation job is running: wait with vissim_wait_simulation "
        "or stop it with vissim_stop_simulation."
    ),
    ErrorCode.JOB_NOT_FOUND: "Use vissim_simulation_status without job_id to see the current job.",
    ErrorCode.EXEC_DISABLED: (
        "vissim_exec_python is disabled; it is enabled only with VISSIM_MCP_ALLOW_EXEC=1."
    ),
    ErrorCode.INFO_UNAVAILABLE: (
        "This object exposes no COM type information; use vissim_get_attributes with known attribute names."
    ),
    ErrorCode.SHUTTING_DOWN: "The server is shutting down; reconnect it with /mcp in Claude Code.",
    ErrorCode.INTERNAL: "Internal server error; details are in the server log (vissim_tail_log).",
}


class VissimMcpError(Exception):
    """An error reported to the client as an error payload.

    Payload: ``{"ok": false, "error": {"code", "message", "hint"?, "details"?}}``. ``hint=None`` selects the
    default hint of the code (if any); pass ``hint=""`` to send no hint at all.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.message = str(message)
        self.hint = DEFAULT_HINTS.get(self.code) if hint is None else (hint or None)
        self.details = dict(details) if details else None
        super().__init__(self.message)

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def __repr__(self) -> str:
        return f"VissimMcpError({self.code.value!r}, {self.message!r})"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = dict(self.details)
        return out


# --- HRESULTs

CO_E_CLASSSTRING = -2147221005  # 0x800401F3 "Invalid class string"
CO_E_NOTINITIALIZED = -2147221008  # 0x800401F0 CoInitialize has not been called
RPC_E_DISCONNECTED = -2147417848  # 0x80010108 object disconnected from its clients
RPC_S_SERVER_UNAVAILABLE = -2147023174  # 0x800706BA RPC server unavailable
CO_E_SERVER_EXEC_FAILURE = -2146959355  # 0x80080005 server execution failed
RPC_E_CALL_REJECTED = -2147418111  # 0x80010001 call rejected by callee
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A server busy, retry later
RPC_E_SERVERFAULT = -2147417851  # 0x80010105 server threw an exception
RPC_E_WRONG_THREAD = -2147417842  # 0x8001010E interface marshalled for a different thread
CO_E_OBJNOTCONNECTED = -2147220995  # 0x800401FD object not connected to server
DISP_E_EXCEPTION = -2147352567  # 0x80020009 exception occurred (details in EXCEPINFO)
DISP_E_MEMBERNOTFOUND = -2147352573  # 0x80020003 member not found
DISP_E_UNKNOWNNAME = -2147352570  # 0x80020006 unknown name
DISP_E_TYPEMISMATCH = -2147352571  # 0x80020005 type mismatch
DISP_E_BADPARAMCOUNT = -2147352562  # 0x8002000E invalid number of parameters
E_ACCESSDENIED = -2147024891  # 0x80070005 access denied
E_FAIL = -2147467259  # 0x80004005 unspecified error
E_INVALIDARG = -2147024809  # 0x80070057 invalid argument

#: HRESULTs meaning "Vissim is gone": the service marks itself disconnected and suggests vissim_restart.
DISCONNECTED_HRESULTS = frozenset({RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE, CO_E_OBJNOTCONNECTED})

_RESTART_HINT = "Vissim was closed or crashed (the COM connection is gone). Call vissim_restart."
#: Hint for every "the connection is gone" error (disconnect HRESULTs, a vanished Vissim process).
RESTART_HINT = _RESTART_HINT

HRESULT_HINTS: dict[int, str] = {
    CO_E_CLASSSTRING: (
        "The Vissim COM server is not registered or VISSIM_PROGID is wrong. Start Vissim once as "
        "administrator and use Help > Register COM Server; check VISSIM_PROGID (e.g. 'Vissim.Vissim')."
    ),
    CO_E_NOTINITIALIZED: (
        "Internal error: COM was used on a thread where it is not initialised (all COM calls must run on the "
        "vissim-com worker thread). Please report this; vissim_restart may recover."
    ),
    RPC_E_DISCONNECTED: _RESTART_HINT,
    RPC_S_SERVER_UNAVAILABLE: _RESTART_HINT,
    CO_E_OBJNOTCONNECTED: _RESTART_HINT,
    CO_E_SERVER_EXEC_FAILURE: (
        "Vissim started but did not register with COM - usually a modal dialog (license selection or "
        "'License not found') is open. Demo/Student licenses have no COM module; activate a full license and "
        "make it the default."
    ),
    RPC_E_CALL_REJECTED: (
        "Vissim rejected the call because it is busy (often a modal dialog is open). Close the dialog in the "
        "Vissim window and retry."
    ),
    RPC_E_SERVERCALL_RETRYLATER: (
        "Vissim is busy and asked to retry later. Wait a moment and retry; "
        "check the Vissim window for a dialog."
    ),
    RPC_E_SERVERFAULT: (
        "Vissim raised an internal error while executing the call. The model state may be inconsistent; "
        "consider vissim_restart."
    ),
    RPC_E_WRONG_THREAD: (
        "Internal error: a COM object was used from the wrong thread. Please report this; vissim_restart may "
        "recover."
    ),
    DISP_E_EXCEPTION: (
        "Vissim reported an error for this call (see the message). Check object keys, attribute names and "
        "values with vissim_describe or vissim_get_attributes."
    ),
    DISP_E_MEMBERNOTFOUND: (
        "The member exists but cannot be used this way (e.g. a read-only attribute, or a method used as a "
        "property). Check it with vissim_describe."
    ),
    DISP_E_UNKNOWNNAME: (
        "Unknown method or property name. Check the exact name with vissim_describe (names differ between "
        "Vissim versions)."
    ),
    DISP_E_TYPEMISMATCH: (
        "A value has the wrong type for this attribute or parameter. Check the expected type with "
        "vissim_describe(include_attributes=true)."
    ),
    DISP_E_BADPARAMCOUNT: "Wrong number of parameters for this COM method.",
    E_ACCESSDENIED: (
        "Access denied. Do not run Vissim as administrator while the server runs as a normal user (or vice "
        "versa); check file permissions."
    ),
    E_FAIL: "Vissim reported an unspecified failure (see the message); check the Vissim window for messages.",
    E_INVALIDARG: "Vissim rejected an argument as invalid (wrong value, key or range).",
}

_GENERIC_COM_HINT = "COM call failed; see the message. vissim_health shows whether Vissim is still connected."
_MAX_COM_MESSAGE_CHARS = 1000


def to_signed_hresult(value: int) -> int:
    """Normalise an HRESULT given as unsigned 32-bit (0x80004005) to the signed form pywin32 uses."""
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value >= 0x8000_0000 else value


def hresult_hex(value: int) -> str:
    """``-2146959355`` -> ``"0x80080005"``."""
    return f"0x{value & 0xFFFFFFFF:08X}"


def is_com_error(exc: BaseException) -> bool:
    """True for ``pywintypes.com_error`` (real or fake, or a subclass) without importing pywin32.

    ``pywintypes.error`` (a Win32 API error with args ``(winerror, funcname, strerror)``) lives in the same
    module but is deliberately *not* treated as a COM error.
    """
    return any(cls.__name__ == "com_error" for cls in type(exc).__mro__)


def com_error_parts(exc: BaseException) -> tuple[int | None, str | None, tuple[Any, ...] | None]:
    """``(hresult, strerror, excepinfo)`` from com_error args ``(hresult, strerror, excepinfo, argerr)``."""
    args: tuple[Any, ...] = tuple(getattr(exc, "args", ()) or ())
    hresult = (
        args[0] if len(args) > 0 and isinstance(args[0], int) and not isinstance(args[0], bool) else None
    )
    strerror = args[1] if len(args) > 1 and isinstance(args[1], str) else None
    excepinfo = args[2] if len(args) > 2 and isinstance(args[2], tuple) else None
    if hresult is not None:
        hresult = to_signed_hresult(hresult)
    return hresult, strerror, excepinfo


def effective_hresult(exc: BaseException) -> int | None:
    """The most specific HRESULT: EXCEPINFO's scode for DISP_E_EXCEPTION, otherwise the top-level HRESULT."""
    hresult, _, excepinfo = com_error_parts(exc)
    scode = _excepinfo_scode(excepinfo)
    if hresult == DISP_E_EXCEPTION and scode:
        return scode
    return hresult


def is_disconnect_error(exc: BaseException) -> bool:
    """True when a COM error means the Vissim process/connection is gone."""
    if not is_com_error(exc):
        return False
    hresult, _, excepinfo = com_error_parts(exc)
    return hresult in DISCONNECTED_HRESULTS or _excepinfo_scode(excepinfo) in DISCONNECTED_HRESULTS


def hint_for_hresult(hresult: int | None) -> str:
    if hresult is None:
        return _GENERIC_COM_HINT
    return HRESULT_HINTS.get(to_signed_hresult(hresult), _GENERIC_COM_HINT)


def from_com_error(exc: BaseException, context: str | None = None) -> VissimMcpError:
    """Map a ``pywintypes.com_error`` to ``VissimMcpError(COM_ERROR)`` with message, hint and details."""
    hresult, strerror, excepinfo = com_error_parts(exc)
    description = _excepinfo_text(excepinfo, 2)
    source = _excepinfo_text(excepinfo, 1)
    scode = _excepinfo_scode(excepinfo)

    message = description or (strerror.strip() if strerror and strerror.strip() else None)
    if not message:
        message = str(exc).strip() or type(exc).__name__
    if len(message) > _MAX_COM_MESSAGE_CHARS:
        message = message[: _MAX_COM_MESSAGE_CHARS - 3] + "..."
    if context:
        message = f"{context}: {message}"

    details: dict[str, Any] = {}
    if hresult is not None:
        details["hresult"] = hresult
        details["hresult_hex"] = hresult_hex(hresult)
    if scode:
        details["scode"] = scode
        details["scode_hex"] = hresult_hex(scode)
    if source:
        details["source"] = source

    hint = hint_for_hresult(effective_hresult(exc))
    return VissimMcpError(ErrorCode.COM_ERROR, message, hint=hint, details=details or None)


def _excepinfo_text(excepinfo: tuple[Any, ...] | None, index: int) -> str | None:
    if not excepinfo or len(excepinfo) <= index:
        return None
    value = excepinfo[index]
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _excepinfo_scode(excepinfo: tuple[Any, ...] | None) -> int | None:
    if not excepinfo or len(excepinfo) <= 5:
        return None
    value = excepinfo[5]
    if isinstance(value, int) and not isinstance(value, bool) and value != 0:
        return to_signed_hresult(value)
    return None
