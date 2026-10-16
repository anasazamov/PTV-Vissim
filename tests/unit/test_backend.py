"""backend/base.py and backend/real.py (PLAN 4.1, 4.3): the pywin32 backend against the fake pywin32 modules
(tests/fakes shadow the real ones in this process), its test-mode guard, and type info members."""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest
import pythoncom
import pywintypes
from win32com.client import dynamic

from fake_backend import FakeBackend
from fake_vissim import RPC_E_DISCONNECTED, FakeVissim, make_com_error
from vissim_mcp.backend import ComBackend, RealComBackend, type_info_members
from vissim_mcp.errors import ErrorCode, VissimMcpError


def on_thread(fn: Callable[[], Any], name: str = "backend-test") -> dict[str, Any]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # inspected by the test
            box["error"] = exc

    thread = threading.Thread(target=target, name=name)
    thread.start()
    thread.join(10)
    return box


def test_real_backend_drives_the_fake_pywin32_modules() -> None:
    backend = RealComBackend()

    def work() -> tuple[str, int, str | None]:
        backend.co_initialize()
        try:
            vissim = backend.dispatch("Vissim.Vissim")
            backend.pump_messages()
            return type(vissim).__name__, vissim.Net.Links.Count, pythoncom._apartment()
        finally:
            backend.co_uninitialize()

    assert on_thread(work) == {"value": ("FakeVissim", 10, "sta")}  # STA (COINIT_APARTMENTTHREADED)
    assert ("CoInitializeEx", "backend-test") in pythoncom.calls
    assert ("CoUninitialize", "backend-test") in pythoncom.calls
    assert pythoncom.pump_counts.get("backend-test", 0) >= 1
    assert backend.name == "pywin32"


def test_real_backend_dispatch_needs_an_initialised_thread() -> None:
    error = on_thread(lambda: RealComBackend().dispatch("Vissim.Vissim"))["error"]
    assert isinstance(error, pywintypes.com_error)


@pytest.mark.parametrize("real_module", ["pythoncom", "dynamic"])
def test_real_dispatch_is_refused_in_test_mode(monkeypatch: pytest.MonkeyPatch, real_module: str) -> None:
    """Contract section 0: with VISSIM_MCP_FORBID_REAL set, a non-fake pywin32 is never dispatched."""
    dispatched: list[str] = []
    monkeypatch.setattr(dynamic, "Dispatch", lambda progid: dispatched.append(progid))
    if real_module == "pythoncom":
        monkeypatch.setitem(sys.modules, "pythoncom", types.ModuleType("pythoncom"))  # no __fake__ marker
    else:
        monkeypatch.delattr(dynamic, "__fake__")
    with pytest.raises(VissimMcpError) as info:
        RealComBackend().dispatch("Vissim.Vissim")
    assert info.value.code is ErrorCode.INTERNAL
    assert "VISSIM_MCP_FORBID_REAL" in info.value.message
    assert dispatched == []


def test_missing_pywin32_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pythoncom", None)  # "import pythoncom" raises ImportError
    monkeypatch.setitem(sys.modules, "win32com.client", None)
    backend = RealComBackend()
    for action in (backend.co_initialize, backend.co_uninitialize, backend.pump_messages):
        with pytest.raises(VissimMcpError) as info:
            action()
        assert info.value.code is ErrorCode.INTERNAL and "pywin32 is not available" in info.value.message
    with pytest.raises(VissimMcpError) as info:
        backend.dispatch("Vissim.Vissim")
    assert "pywin32 is not available" in info.value.message


def test_backends_satisfy_the_protocol() -> None:
    real, fake = RealComBackend(), FakeBackend()
    assert isinstance(real, ComBackend) and isinstance(fake, ComBackend)
    assert real.is_com_error(make_com_error(RPC_E_DISCONNECTED))
    assert not real.is_com_error(pywintypes.error(5, "OpenProcess", "Access is denied."))
    assert not real.is_com_error(ValueError())


def test_type_info_members(fake_vissim: FakeVissim) -> None:
    members = type_info_members(fake_vissim)
    assert members == sorted(members, key=lambda member: (member["name"], member["kind"]))
    assert len(members) == len({(m["name"], m["kind"]) for m in members})
    names = {member["name"] for member in members}
    assert {"QueryInterface", "AddRef", "Release", "Invoke", "GetTypeInfo"}.isdisjoint(names)
    assert {"name": "LoadNet", "kind": "method"} in members
    assert {"name": "Net", "kind": "property_get"} in members
    link = type_info_members(fake_vissim.Net.Links.ItemByKey(1))
    assert {"name": "AttValue", "kind": "property_get"} in link
    assert {"name": "AttValue", "kind": "property_put"} in link
    assert RealComBackend().type_info_members(fake_vissim) == members


def test_type_info_unavailable() -> None:
    for obj in (FakeVissim(no_type_info=True), object(), 42):
        with pytest.raises(VissimMcpError) as info:
            type_info_members(obj)
        assert info.value.code is ErrorCode.INFO_UNAVAILABLE


def test_type_info_of_a_lost_connection_raises_the_com_error() -> None:
    vissim = FakeVissim()
    vissim.crash(kill_process=False)
    with pytest.raises(pywintypes.com_error) as info:
        type_info_members(vissim)
    assert info.value.hresult == RPC_E_DISCONNECTED
