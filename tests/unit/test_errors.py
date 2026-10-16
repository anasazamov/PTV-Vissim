"""errors.py (PLAN 6.3, 7.7, 10.1): error codes, VissimMcpError and the com_error -> code/hint mapping."""

from __future__ import annotations

import pytest
import pywintypes  # the fake (tests/fakes) - see conftest

from vissim_mcp import errors
from vissim_mcp.errors import (
    DEFAULT_HINTS,
    DISCONNECTED_HRESULTS,
    HRESULT_HINTS,
    ErrorCode,
    VissimMcpError,
    effective_hresult,
    from_com_error,
    hresult_hex,
    is_com_error,
    is_disconnect_error,
    to_signed_hresult,
)

PLAN_CODES = {
    "NOT_CONNECTED",
    "ALREADY_CONNECTED",
    "INVALID_ARGUMENT",
    "PATH_DENIED",
    "READ_ONLY_PATH",
    "FILE_NOT_FOUND",
    "ATTRIBUTE_DENIED",
    "RESOLVE_FAILED",
    "COM_ERROR",
    "TIMEOUT_HUNG",
    "JOB_RUNNING",
    "JOB_NOT_FOUND",
    "EXEC_DISABLED",
    "INFO_UNAVAILABLE",
    "SHUTTING_DOWN",
    "INTERNAL",
}

#: Constant name -> unsigned HRESULT from the Windows SDK headers (independent check of the signed values).
SDK_HRESULTS = {
    "CO_E_CLASSSTRING": 0x800401F3,
    "CO_E_NOTINITIALIZED": 0x800401F0,
    "RPC_E_DISCONNECTED": 0x80010108,
    "RPC_S_SERVER_UNAVAILABLE": 0x800706BA,
    "CO_E_SERVER_EXEC_FAILURE": 0x80080005,
    "RPC_E_CALL_REJECTED": 0x80010001,
    "RPC_E_SERVERCALL_RETRYLATER": 0x8001010A,
    "RPC_E_SERVERFAULT": 0x80010105,
    "RPC_E_WRONG_THREAD": 0x8001010E,
    "CO_E_OBJNOTCONNECTED": 0x800401FD,
    "DISP_E_EXCEPTION": 0x80020009,
    "DISP_E_MEMBERNOTFOUND": 0x80020003,
    "DISP_E_UNKNOWNNAME": 0x80020006,
    "DISP_E_TYPEMISMATCH": 0x80020005,
    "DISP_E_BADPARAMCOUNT": 0x8002000E,
    "E_ACCESSDENIED": 0x80070005,
    "E_FAIL": 0x80004005,
    "E_INVALIDARG": 0x80070057,
}


def com_error(hresult: int, text: str = "text", excepinfo: tuple | None = None) -> Exception:
    return pywintypes.com_error(hresult, text, excepinfo, None)


# --- codes


def test_error_codes_are_exactly_the_plan_codes() -> None:
    assert {code.value for code in ErrorCode} == PLAN_CODES
    assert len(ErrorCode) == 16
    assert ErrorCode.COM_ERROR == "COM_ERROR"
    assert isinstance(ErrorCode.COM_ERROR, str)
    assert str(ErrorCode.TIMEOUT_HUNG) == "TIMEOUT_HUNG"


def test_error_to_dict_uses_the_default_hint() -> None:
    err = VissimMcpError(ErrorCode.NOT_CONNECTED, "Vissim is not connected")
    assert err.to_dict() == {
        "code": "NOT_CONNECTED",
        "message": "Vissim is not connected",
        "hint": DEFAULT_HINTS[ErrorCode.NOT_CONNECTED],
    }
    assert str(err) == "NOT_CONNECTED: Vissim is not connected"


def test_explicit_hint_and_details() -> None:
    err = VissimMcpError("COM_ERROR", "boom", hint="do this", details={"hresult": -1})
    assert err.code is ErrorCode.COM_ERROR
    assert err.to_dict() == {
        "code": "COM_ERROR",
        "message": "boom",
        "hint": "do this",
        "details": {"hresult": -1},
    }


def test_empty_hint_suppresses_the_default() -> None:
    assert "hint" not in VissimMcpError(ErrorCode.NOT_CONNECTED, "x", hint="").to_dict()


def test_codes_without_default_hint() -> None:
    assert "hint" not in VissimMcpError(ErrorCode.INVALID_ARGUMENT, "bad").to_dict()


def test_unknown_code_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        VissimMcpError("NOT_A_CODE", "x")


def test_timeout_hint_mentions_dialog_and_restart() -> None:
    hint = DEFAULT_HINTS[ErrorCode.TIMEOUT_HUNG]
    assert "modal dialog" in hint and "vissim_restart" in hint


# --- HRESULTs


@pytest.mark.parametrize(("name", "unsigned"), sorted(SDK_HRESULTS.items()))
def test_hresult_constants_match_the_windows_sdk(name: str, unsigned: int) -> None:
    value = getattr(errors, name)
    assert value == unsigned - 0x1_0000_0000
    assert hresult_hex(value) == f"0x{unsigned:08X}"


def test_every_contract_hresult_has_a_specific_hint() -> None:
    for name in SDK_HRESULTS:
        assert getattr(errors, name) in HRESULT_HINTS, name


def test_disconnected_set() -> None:
    assert DISCONNECTED_HRESULTS == {
        errors.RPC_E_DISCONNECTED,
        errors.RPC_S_SERVER_UNAVAILABLE,
        errors.CO_E_OBJNOTCONNECTED,
    }


def test_hresult_helpers() -> None:
    assert hresult_hex(-2146959355) == "0x80080005"
    assert hresult_hex(5) == "0x00000005"
    assert to_signed_hresult(0x80080005) == -2146959355
    assert to_signed_hresult(-2146959355) == -2146959355
    assert to_signed_hresult(5) == 5


# --- com_error


def test_is_com_error_is_duck_typed() -> None:
    class com_error_like(Exception):
        pass

    com_error_like.__name__ = "com_error"

    class SubclassedComError(pywintypes.com_error):
        pass

    assert is_com_error(com_error(errors.E_FAIL))
    assert is_com_error(com_error_like())
    assert is_com_error(SubclassedComError(errors.E_FAIL, "x", None, None))
    assert not is_com_error(ValueError("x"))
    assert not is_com_error(pywintypes.error(5, "OpenProcess", "Access is denied."))


@pytest.mark.parametrize("hresult", sorted(HRESULT_HINTS))
def test_known_hresults_map_to_com_error_with_their_hint(hresult: int) -> None:
    err = from_com_error(com_error(hresult, "localized text"))
    assert err.code is ErrorCode.COM_ERROR
    assert err.hint == HRESULT_HINTS[hresult]
    assert err.message == "localized text"
    assert err.details == {"hresult": hresult, "hresult_hex": hresult_hex(hresult)}


def test_specific_hints() -> None:
    assert "Register COM Server" in from_com_error(com_error(errors.CO_E_CLASSSTRING)).hint
    not_initialized = from_com_error(com_error(errors.CO_E_NOTINITIALIZED)).hint
    assert "Internal error" in not_initialized and "thread" in not_initialized
    for hresult in DISCONNECTED_HRESULTS:
        assert "vissim_restart" in from_com_error(com_error(hresult)).hint
    license_hint = from_com_error(com_error(errors.CO_E_SERVER_EXEC_FAILURE)).hint
    assert "license" in license_hint.lower() and "dialog" in license_hint


def test_localized_message_is_kept_verbatim() -> None:
    # The exact error seen in M0 (Russian Windows): classification relies on the HRESULT only.
    exc = com_error(errors.CO_E_SERVER_EXEC_FAILURE, "Ошибка при выполнении приложения-сервера")
    err = from_com_error(exc)
    assert err.message == "Ошибка при выполнении приложения-сервера"
    assert err.details["hresult_hex"] == "0x80080005"
    assert "license" in err.hint.lower()


def test_excepinfo_description_and_scode_win() -> None:
    excepinfo = (0, "Vissim", "AttValue failed: unknown attribute 'Foo'.", None, 0, errors.E_INVALIDARG)
    exc = com_error(errors.DISP_E_EXCEPTION, "Exception occurred.", excepinfo)
    err = from_com_error(exc, context="Segment 'Foo' on 'Net.Links[1]'")
    assert err.message == "Segment 'Foo' on 'Net.Links[1]': AttValue failed: unknown attribute 'Foo'."
    assert err.details == {
        "hresult": errors.DISP_E_EXCEPTION,
        "hresult_hex": "0x80020009",
        "scode": errors.E_INVALIDARG,
        "scode_hex": "0x80070057",
        "source": "Vissim",
    }
    assert err.hint == HRESULT_HINTS[errors.E_INVALIDARG]
    assert effective_hresult(exc) == errors.E_INVALIDARG


def test_disp_exception_without_scode_uses_its_own_hint() -> None:
    exc = com_error(errors.DISP_E_EXCEPTION, "Exception occurred.", (0, None, "Vissim says no", None, 0, 0))
    err = from_com_error(exc)
    assert err.message == "Vissim says no"
    assert err.hint == HRESULT_HINTS[errors.DISP_E_EXCEPTION]
    assert "scode" not in err.details


def test_disconnect_detection_also_looks_at_scode() -> None:
    wrapped = com_error(
        errors.DISP_E_EXCEPTION, "x", (0, "Vissim", "gone", None, 0, errors.RPC_E_DISCONNECTED)
    )
    assert is_disconnect_error(wrapped)
    assert "vissim_restart" in from_com_error(wrapped).hint
    assert is_disconnect_error(com_error(errors.RPC_S_SERVER_UNAVAILABLE))
    assert not is_disconnect_error(com_error(errors.E_FAIL))
    assert not is_disconnect_error(RuntimeError("not a com error"))


def test_unsigned_hresults_are_normalised() -> None:
    err = from_com_error(pywintypes.com_error(0x80080005, "x", None, None))
    assert err.details["hresult"] == -2146959355


def test_unknown_hresult_gets_a_generic_hint() -> None:
    err = from_com_error(com_error(-2147000000, "odd"))
    assert err.code is ErrorCode.COM_ERROR
    assert "vissim_health" in err.hint


@pytest.mark.parametrize(
    "args",
    [(), ("only text",), (None, None, None, None), (True, "bool is not an HRESULT"), (-1, "", None, None)],
)
def test_malformed_com_errors_never_crash(args: tuple) -> None:
    err = from_com_error(pywintypes.com_error(*args))
    assert err.code is ErrorCode.COM_ERROR
    assert err.message
    assert err.hint


def test_long_messages_are_truncated() -> None:
    exc = com_error(errors.DISP_E_EXCEPTION, "x", (0, "Vissim", "y" * 5000, None, 0, errors.E_FAIL))
    assert len(from_com_error(exc).message) == 1000
