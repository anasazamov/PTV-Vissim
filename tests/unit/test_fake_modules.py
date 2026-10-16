"""The fake pywin32 modules (tests/fakes) and FakeBackend."""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import threading
import time
from typing import Any

import pytest
import pythoncom
import pywintypes
import win32com
import win32com.client
from win32com.client import dynamic

from fake_backend import FakeBackend
from fake_procs import FakeProcs
from fake_vissim import (
    CO_E_CLASSSTRING,
    CO_E_NOTINITIALIZED,
    CO_E_SERVER_EXEC_FAILURE,
    FakeVissim,
)
from vissim_mcp.errors import ErrorCode, VissimMcpError


def in_thread(fn: Any, *, init: bool = False) -> dict[str, Any]:
    """Run ``fn`` in a fresh thread (optionally with CoInitializeEx) and return its value or error."""
    box: dict[str, Any] = {}

    def target() -> None:
        if init:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        try:
            box["value"] = fn()
        except BaseException as exc:  # the test inspects it
            box["error"] = exc
        finally:
            if init:
                pythoncom.CoUninitialize()

    thread = threading.Thread(target=target, name="fake-com-test")
    thread.start()
    thread.join(10)
    return box


def test_all_modules_are_the_fakes() -> None:
    for module in (pythoncom, pywintypes, win32com, win32com.client, dynamic):
        assert getattr(module, "__fake__", False) is True, module.__name__
    assert win32com.client.Dispatch is dynamic.Dispatch
    assert pythoncom.com_error is pywintypes.com_error


def test_mcp_sdk_sees_the_fake_pywintypes() -> None:
    import mcp  # noqa: F401 - on Windows it imports pywintypes (stdio client helpers)

    assert sys.modules["pywintypes"] is pywintypes
    if sys.platform == "win32":
        from mcp.os.win32 import utilities

        assert utilities.pywintypes is pywintypes  # the SDK's own import got the fake, and still works


def test_co_initialize_is_per_thread() -> None:
    def probe() -> tuple[bool, str | None]:
        return pythoncom._is_initialized(), pythoncom._apartment()

    assert in_thread(probe)["value"] == (False, None)
    assert in_thread(probe, init=True)["value"] == (True, "sta")


def test_changing_the_apartment_mode_fails() -> None:
    def change() -> None:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)

    error = in_thread(change, init=True)["error"]
    assert isinstance(error, pywintypes.com_error) and error.hresult == pythoncom.RPC_E_CHANGED_MODE


def test_pump_waiting_messages() -> None:
    assert in_thread(pythoncom.PumpWaitingMessages)["value"] == 0
    assert pythoncom.pump_counts["fake-com-test"] >= 1


def test_com_error_and_time_types() -> None:
    error = pywintypes.com_error(-1, "text", (0, "src", "desc", None, 0, -2), None)
    assert (error.hresult, error.strerror, error.excepinfo[2], error.argerror) == (-1, "text", "desc", None)
    api_error = pywintypes.error(5, "OpenProcess", "Access is denied.")
    assert (api_error.winerror, api_error.funcname, api_error.strerror) == (
        5,
        "OpenProcess",
        "Access is denied.",
    )
    moment = pywintypes.Time(dt.datetime(2026, 10, 16, 8, 30))
    assert isinstance(moment, dt.datetime) and moment.Format("%H:%M") == "08:30"
    assert isinstance(pywintypes.Time(0), pywintypes.TimeType)


# --- dispatch


def test_dispatch_requires_co_initialize() -> None:
    error = in_thread(lambda: dynamic.Dispatch("Vissim.Vissim"))["error"]
    assert isinstance(error, pywintypes.com_error) and error.hresult == CO_E_NOTINITIALIZED


def test_dispatch_returns_a_fake_vissim_bound_to_the_thread() -> None:
    def make() -> tuple[str, str, int]:
        vissim = dynamic.Dispatch("Vissim.Vissim.2026")
        return type(vissim).__name__, vissim.owner_thread_name, vissim.Net.Links.Count

    assert in_thread(make, init=True)["value"] == ("FakeVissim", "fake-com-test", 10)


@pytest.mark.parametrize("progid", ["Excel.Application", "Vissim", ""])
def test_unregistered_progids(progid: str) -> None:
    error = in_thread(lambda: dynamic.Dispatch(progid), init=True)["error"]
    assert isinstance(error, pywintypes.com_error) and error.hresult == CO_E_CLASSSTRING


def test_vissim_clsid_is_registered() -> None:
    value = in_thread(lambda: dynamic.Dispatch("{A93F962D-8DE3-48FC-9F71-B12A0F730FA3}"), init=True)["value"]
    assert isinstance(value, FakeVissim)


def test_env_bad_progid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VISSIM_BAD_PROGID", "Vissim.Vissim.2026")
    assert "error" in in_thread(lambda: dynamic.Dispatch("vissim.vissim.2026"), init=True)
    assert "value" in in_thread(lambda: dynamic.Dispatch("Vissim.Vissim"), init=True)
    monkeypatch.setenv("FAKE_VISSIM_BAD_PROGID", "1")
    assert "error" in in_thread(lambda: dynamic.Dispatch("Vissim.Vissim"), init=True)


def test_env_dispatch_error_and_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VISSIM_HANG_S", "0.2")
    monkeypatch.setenv("FAKE_VISSIM_DISPATCH_ERROR", "0x80080005")
    started = time.perf_counter()
    error = in_thread(lambda: dynamic.Dispatch("Vissim.Vissim"), init=True)["error"]
    assert time.perf_counter() - started >= 0.18
    assert isinstance(error, pywintypes.com_error) and error.hresult == CO_E_SERVER_EXEC_FAILURE


def test_env_options_reach_the_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VISSIM_NETWORK", "empty")
    assert in_thread(lambda: dynamic.Dispatch("Vissim.Vissim").Net.Links.Count, init=True)["value"] == 0


def test_dispatch_fails_when_the_spawned_server_process_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like real COM: the stand-in starts before the start-up hang; if it dies meanwhile (e.g. the server
    killed it because of a license dialog), Dispatch fails with CO_E_SERVER_EXEC_FAILURE at once."""
    executable = getattr(sys, "_base_executable", None) or sys.executable
    monkeypatch.setenv("FAKE_VISSIM_SPAWN", json.dumps([executable, "-c", "pass"]))
    monkeypatch.setenv("FAKE_VISSIM_HANG_S", "30")
    started = time.perf_counter()
    error = in_thread(lambda: dynamic.Dispatch("Vissim.Vissim"), init=True)["error"]
    assert time.perf_counter() - started < 10
    assert isinstance(error, pywintypes.com_error) and error.hresult == CO_E_SERVER_EXEC_FAILURE


# --- FakeBackend


def test_fake_backend_requires_co_initialize(fake_backend: FakeBackend) -> None:
    with pytest.raises(pywintypes.com_error) as info:
        fake_backend.dispatch("Vissim.Vissim")
    assert info.value.hresult == CO_E_NOTINITIALIZED


def test_fake_backend_lifecycle(fake_backend: FakeBackend) -> None:
    def work() -> int:
        fake_backend.co_initialize()
        try:
            vissim = fake_backend.dispatch("Vissim.Vissim")
            fake_backend.pump_messages()
            return vissim.Net.Links.Count
        finally:
            fake_backend.co_uninitialize()

    box = in_thread(work)
    assert box["value"] == 10
    assert [name for name, _thread in fake_backend.events] == ["co_initialize", "dispatch", "co_uninitialize"]
    assert {thread for _name, thread in fake_backend.events} == {"fake-com-test"}
    assert fake_backend.pump_count == 1
    assert fake_backend.last is fake_backend.instances[0]
    assert fake_backend.last.owner_thread_name == "fake-com-test"


def test_fake_backend_errors_and_delay() -> None:
    backend = FakeBackend(dispatch_error=CO_E_SERVER_EXEC_FAILURE)
    backend.co_initialize()
    try:
        with pytest.raises(pywintypes.com_error) as info:
            backend.dispatch("Vissim.Vissim")
        assert info.value.hresult == CO_E_SERVER_EXEC_FAILURE
        with pytest.raises(pywintypes.com_error) as info:
            backend.dispatch("Word.Application")
        assert info.value.hresult == CO_E_CLASSSTRING
        slow = FakeBackend(dispatch_delay_s=30)
        slow.co_initialize()
        threading.Timer(0.1, slow.release_dispatch).start()
        started = time.perf_counter()
        assert isinstance(slow.dispatch("Vissim.Vissim"), FakeVissim)
        assert time.perf_counter() - started < 5
    finally:
        backend.co_uninitialize()


def test_fake_backend_dispatch_fails_when_the_started_process_dies() -> None:
    procs = FakeProcs()
    backend = FakeBackend(dispatch_delay_s=30, on_dispatch=lambda progid: procs.spawn())

    def start() -> Any:
        backend.co_initialize()
        return backend.dispatch("Vissim.Vissim")

    threading.Timer(0.1, lambda: next(iter(procs.table.values())).end("kill")).start()
    started = time.perf_counter()
    error = in_thread(start)["error"]
    assert time.perf_counter() - started < 5
    assert isinstance(error, pywintypes.com_error) and error.hresult == CO_E_SERVER_EXEC_FAILURE
    assert backend.instances == []


def test_fake_backend_links_the_process_to_the_fake_vissim() -> None:
    procs = FakeProcs()
    backend = FakeBackend(on_dispatch=lambda progid: procs.spawn())
    backend.co_initialize()
    first = backend.dispatch("Vissim.Vissim")
    process = first.process
    assert process is procs.table[process.pid] and process.alive
    first.Exit()  # Vissim leaves by itself
    assert process.ended_by == "exit" and not first.crashed

    second = backend.dispatch("Vissim.Vissim")
    procs.kill(second.process.pid, second.process.create_time, re.compile(r"^vissim\d*\.exe$", re.I))
    assert second.crashed  # a killed process disconnects the COM object
    with pytest.raises(pywintypes.com_error):
        _ = second.Net


def test_fake_backend_passes_options_to_fake_vissim() -> None:
    backend = FakeBackend(network="empty", stall_runs=3)
    backend.co_initialize()
    vissim = backend.dispatch("Vissim.Vissim")
    assert vissim.Net.Links.Count == 0 and vissim.stall_runs == 3


def test_type_info_members(fake_backend: FakeBackend, fake_vissim: FakeVissim) -> None:
    members = fake_backend.type_info_members(fake_vissim)
    assert members == sorted(members, key=lambda m: (m["name"], m["kind"]))
    names = {m["name"] for m in members}
    assert {"QueryInterface", "AddRef", "Invoke", "GetTypeInfo"}.isdisjoint(names)
    assert {"name": "LoadNet", "kind": "method"} in members
    assert {"name": "Net", "kind": "property_get"} in members
    link_members = fake_backend.type_info_members(fake_vissim.Net.Links.ItemByKey(1))
    assert {"name": "AttValue", "kind": "property_get"} in link_members
    assert {"name": "AttValue", "kind": "property_put"} in link_members
    assert {"name": "SetAttValue", "kind": "method"} in link_members
    collection_members = {m["name"] for m in fake_backend.type_info_members(fake_vissim.Net.Links)}
    assert {"Count", "ItemByKey", "GetMultipleAttributes", "Attributes"} <= collection_members


def test_type_info_unavailable(fake_backend: FakeBackend) -> None:
    for obj in (FakeVissim(no_type_info=True), object()):
        with pytest.raises(VissimMcpError) as info:
            fake_backend.type_info_members(obj)
        assert info.value.code is ErrorCode.INFO_UNAVAILABLE


def test_fake_backend_is_com_error(fake_backend: FakeBackend) -> None:
    assert fake_backend.is_com_error(pywintypes.com_error(-1, "x", None, None))
    assert not fake_backend.is_com_error(ValueError())
