"""Fake ``win32com.client.dynamic`` (test double): ``Dispatch(progid)`` returns a ``FakeVissim``.

Configured through environment variables, so protocol tests can drive the server subprocess:

* ``FAKE_VISSIM_BAD_PROGID`` - ``1``/``*`` makes every ProgID unknown, otherwise a ``;``-separated list of
  ProgIDs that raise ``CO_E_CLASSSTRING`` (ProgIDs not starting with ``Vissim.Vissim`` always do).
* ``FAKE_VISSIM_HANG_S`` - seconds ``Dispatch`` blocks before returning (slow start or a license dialog).
* ``FAKE_VISSIM_DISPATCH_ERROR`` - HRESULT (e.g. ``-2146959355`` or ``0x80080005``) raised after the hang.
* ``FAKE_VISSIM_SPAWN`` / ``FAKE_VISSIM_SPAWN_ENV`` - like real COM, the stand-in "Vissim" process starts
  *before* the hang; if it dies during the hang (e.g. the server killed it because it showed a license
  dialog), ``Dispatch`` fails with ``CO_E_SERVER_EXEC_FAILURE``. On success it becomes ``FakeVissim.process``.
* every other ``FAKE_VISSIM_*`` option of ``fake_vissim.options_from_env`` (noise, network, faults, ...).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

__fake__ = True

_TESTS_DIR = Path(__file__).resolve().parents[3]
if str(_TESTS_DIR) not in sys.path:
    sys.path.append(str(_TESTS_DIR))

import pythoncom  # noqa: E402 - the fake next to this package

from fake_vissim import (  # noqa: E402
    CO_E_CLASSSTRING,
    CO_E_NOTINITIALIZED,
    CO_E_SERVER_EXEC_FAILURE,
    FakeVissim,
    is_registered_progid,
    make_com_error,
    options_from_env,
    spawn_standin,
)


def Dispatch(
    IDispatch: Any,
    userName: str | None = None,
    createClass: Any = None,
    typeinfo: Any = None,
    clsctx: Any = None,
) -> FakeVissim:
    is_initialized = getattr(pythoncom, "_is_initialized", None)
    if is_initialized is not None and not is_initialized():
        raise make_com_error(CO_E_NOTINITIALIZED)
    if not isinstance(IDispatch, str):
        raise TypeError("the fake Dispatch only accepts a ProgID or CLSID string")
    progid = IDispatch.strip()
    env = os.environ
    if _is_bad_progid(progid, env.get("FAKE_VISSIM_BAD_PROGID", "")) or not is_registered_progid(progid):
        raise make_com_error(CO_E_CLASSSTRING)
    options = options_from_env(env)
    spawn_cmd = options.pop("spawn_cmd", None)
    spawn_env = options.pop("spawn_env", None)
    process = spawn_standin(spawn_cmd, spawn_env) if spawn_cmd else None
    hang_s = float(env.get("FAKE_VISSIM_HANG_S") or 0)
    if hang_s > 0:
        _wait_for_startup(hang_s, process)
    error = (env.get("FAKE_VISSIM_DISPATCH_ERROR") or "").strip()
    if error:
        hresult = int(error, 0)
        if hresult >= 0x8000_0000:
            hresult -= 0x1_0000_0000
        raise make_com_error(hresult)  # like real COM, a failed start may leave the process running
    vissim = FakeVissim(progid, **options)
    vissim.process = process
    return vissim


def _wait_for_startup(seconds: float, process: Any) -> None:
    """Block ``seconds``; fail like COM does when the server process dies before it registered."""
    deadline = time.monotonic() + seconds
    while True:
        if process is not None and process.poll() is not None:
            raise make_com_error(CO_E_SERVER_EXEC_FAILURE)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def _is_bad_progid(progid: str, setting: str) -> bool:
    setting = setting.strip()
    if not setting:
        return False
    if setting in ("1", "*"):
        return True
    return progid.lower() in {item.strip().lower() for item in setting.split(";") if item.strip()}
