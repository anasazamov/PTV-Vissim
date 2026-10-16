r"""The real process chain inside the server (PLAN 6.4, 10.4) with a harmless stand-in Vissim (Windows).

In the other protocol tests the fake ``Dispatch`` starts no process, so the server never knows a PID. Here it
starts a stand-in through ``FAKE_VISSIM_SPAWN``: a copy of the base ``python.exe`` named
``vissim_mcp_standin_<pytest PID>.exe`` (it matches only this session's test ``VISSIM_EXE_PATTERN``, never the
real ``^vissim\d*\.exe$`` nor the stand-ins of a concurrent test run) with ``-Embedding`` on its command line,
like a COM-launched Vissim. So the production code runs in the server process: psutil PID diff -> COM-launch
check -> kill-on-close Job Object -> identity-checked kill, the kill on stdin EOF within the 300 ms budget,
and the Job Object ending the stand-in when the server dies abruptly. No real Vissim is ever listed, killed or
put into a job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest
from protocol_helpers import finish, handshake

from stdio_client import StdioServer

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows processes and job objects")

StartServer = Callable[..., StdioServer]
#: Unique per test session: servers of a concurrent test run (or the pid=None protocol tests, whose pattern
# is ``^vissim_mcp_test_standin\d*\.exe$``) never see - let alone adopt or kill - this session's stand-ins.
STANDIN_NAME = f"vissim_mcp_standin_{os.getpid()}.exe"
STANDIN_PATTERN = rf"^vissim_mcp_standin_{os.getpid()}\.exe$"
STANDIN_LIFETIME_S = 300  # upper bound if every clean-up failed


def _standin_env() -> dict[str, str]:
    return {"PYTHONHOME": sys.base_prefix, "PYTHONPATH": ""}  # an empty value removes the variable


def _kill_standins(folder: Path) -> None:
    """Kill stand-ins started from ``folder`` (never any other process)."""
    wanted = os.path.normcase(str(folder))
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if (proc.info["name"] or "").lower() != STANDIN_NAME or not proc.info["exe"]:
                continue
            if os.path.normcase(os.path.dirname(proc.info["exe"])) == wanted:
                proc.kill()
        except psutil.Error:
            pass


@pytest.fixture(scope="module")
def standin_exe(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The stand-in executable: the base interpreter and its DLLs copied into a temporary folder."""
    base = Path(getattr(sys, "_base_executable", None) or sys.executable)
    folder = tmp_path_factory.mktemp("standin")
    exe = folder / STANDIN_NAME
    try:
        shutil.copy2(base, exe)
        for dll in base.parent.glob("*.dll"):
            shutil.copy2(dll, folder / dll.name)
        probe = subprocess.run(
            [str(exe), "-S", "-c", "print('standin ok')"],
            capture_output=True,
            timeout=30,
            env={**{k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}, **_standin_env()},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"the base interpreter cannot be used as a stand-in: {exc}")
    if probe.stdout.strip() != b"standin ok":
        pytest.skip(f"the stand-in does not start: {probe.stderr[-300:]!r}")
    yield exe
    _kill_standins(folder)


def standin_server_env(exe: Path, **extra: str) -> dict[str, str]:
    command = [str(exe), "-S", "-c", f"import time; time.sleep({STANDIN_LIFETIME_S})", "-Embedding"]
    return {
        "VISSIM_EXE_PATTERN": STANDIN_PATTERN,
        "VISSIM_PID_DETECT_S": "5",
        "FAKE_VISSIM_SPAWN": json.dumps(command),
        "FAKE_VISSIM_SPAWN_ENV": json.dumps(_standin_env()),
        **extra,
    }


def test_pid_job_object_restart_and_the_kill_on_stdin_eof(
    start_server: StartServer, standin_exe: Path
) -> None:
    server = start_server(env=standin_server_env(standin_exe))
    handshake(server)
    connect = server.call_tool("vissim_connect", timeout=30)["payload"]
    assert connect["ok"] is True and connect["job_object"] is True, connect
    assert "warnings" not in connect, connect
    first = psutil.Process(connect["pid"])
    assert first.name().lower() == STANDIN_NAME and first.cmdline()[-1] == "-Embedding"
    health = server.call_tool("vissim_health")["payload"]
    assert health["pid"] == first.pid and health["vissim_alive"] is True and health["job_object"] is True

    restart = server.call_tool("vissim_restart", timeout=30)["payload"]
    assert restart["ok"] is True, restart
    assert restart["previous_pid"] == first.pid and restart["killed"] is True
    assert restart["job_object"] is True and restart["pid"] not in (None, first.pid)
    _gone, alive = psutil.wait_procs([first], timeout=5)
    assert not alive, "restart did not end the previous stand-in"
    second = psutil.Process(restart["pid"])

    finish(server)  # stdin EOF: VISSIM_KILL_ON_EXIT=1 kills the stand-in; exit within 300 ms
    _gone, alive = psutil.wait_procs([second], timeout=5)
    assert not alive, "the stand-in survived the end of the MCP session"


def test_vissim_close_ends_the_standin(start_server: StartServer, standin_exe: Path) -> None:
    server = start_server(env=standin_server_env(standin_exe))
    handshake(server)
    pid = server.call_tool("vissim_connect", timeout=30)["payload"]["pid"]
    assert pid is not None
    proc = psutil.Process(pid)
    close = server.call_tool("vissim_close", timeout=30)["payload"]
    assert close == {"ok": True, "closed": True, "method": "exit", "pid": pid}
    _gone, alive = psutil.wait_procs([proc], timeout=5)
    assert not alive
    health = server.call_tool("vissim_health")["payload"]
    assert health["connected"] is False and health["pid"] is None
    finish(server)


@pytest.mark.parametrize("use_job_object", [True, False], ids=["job-object", "control-without-job"])
def test_the_job_object_ends_the_standin_when_the_server_dies_abruptly(
    start_server: StartServer, standin_exe: Path, use_job_object: bool
) -> None:
    """PLAN 6.4 / 10.4: however the server dies (here: TerminateProcess, no stdin EOF, no shutdown sequence),
    Windows closes its job handle and ends Vissim. The control run without the job object proves that it is
    the job that ends the stand-in."""
    env = standin_server_env(standin_exe, VISSIM_USE_JOB_OBJECT="1" if use_job_object else "0")
    server = start_server(env=env)
    handshake(server)
    connect = server.call_tool("vissim_connect", timeout=30)["payload"]
    assert connect["ok"] is True and connect["job_object"] is use_job_object, connect
    standin = psutil.Process(connect["pid"])
    interpreter = standin.parent()  # the server interpreter (the venv launcher's child)
    assert interpreter is not None
    assert server.process.pid in (interpreter.pid, interpreter.ppid())
    interpreter.kill()
    _gone, alive = psutil.wait_procs([standin], timeout=10 if use_job_object else 1.0)
    if use_job_object:
        assert not alive, "the stand-in survived the abrupt death of the server"
    else:
        assert alive, "without the job object the stand-in should have survived"
        standin.kill()
