"""Protocol tests for the life-cycle tools (PLAN 10.2, 11 row M2).

``python -X utf8 -m vissim_mcp`` runs as a subprocess with ``PYTHONPATH=tests/fakes``: the real backend code
path (``RealComBackend`` -> ``win32com.client.dynamic.Dispatch``) drives the fake pywin32 modules, and the
real ``winproc`` lists processes. ``VISSIM_EXE_PATTERN`` is set to a name no real program has, so the server
never inspects, kills or jobs a real process (the fake Dispatch starts none: the PID stays unknown).

Exit timing: the server must be gone within 300 ms of stdin closing - also while a tool call is blocked in a
hung COM call on the worker thread.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from protocol_helpers import LIFECYCLE_TOOLS, finish, handshake

from stdio_client import StdioServer
from vissim_mcp.errors import CO_E_SERVER_EXEC_FAILURE, HRESULT_HINTS
from vissim_mcp.winproc import PID_NOT_DETECTED_WARNING

StartServer = Callable[..., StdioServer]
SAFE_ENV = {
    "VISSIM_EXE_PATTERN": r"^vissim_mcp_test_standin\d*\.exe$",
    "VISSIM_PID_DETECT_S": "0.3",
}
NOISE_KINDS = ("print()", "os.write(1)", "native WriteFile")


def answered(server: StdioServer, request_id: int) -> bool:
    return any(json.loads(line).get("id") == request_id for line in server.stdout_lines)


def test_connect_info_restart_close_with_stdout_noise(start_server: StartServer) -> None:
    """Every fake COM call prints to stdout (print, os.write(1), native WriteFile) on the worker thread;
    stdout must still carry only JSON-RPC lines."""
    server = start_server(env={**SAFE_ENV, "FAKE_VISSIM_PRINT_STDOUT": "1"})
    tools = handshake(server)
    assert list(tools) == LIFECYCLE_TOOLS

    connect = server.call_tool("vissim_connect", timeout=30)
    assert connect["result"]["isError"] is False
    payload = connect["payload"]
    assert payload["generation"] == 1 and payload["progid"] == "Vissim.Vissim"
    assert payload["pid"] is None and payload["job_object"] is False
    assert payload["warnings"] == [PID_NOT_DETECTED_WARNING]

    health = server.call_tool("vissim_health")["payload"]
    assert health["connected"] is True and health["generation"] == 1
    assert health["busy_seconds"] == 0.0 and health["current_task"] is None

    info = server.call_tool("vissim_info")["payload"]
    assert info["ok"] is True and info["counts"]["Links"] == 10 and info["SimPeriod"] == 3600

    restart = server.call_tool("vissim_restart", timeout=30)["payload"]
    assert restart["ok"] is True and restart["generation"] == 2 and restart["killed"] is False

    close = server.call_tool("vissim_close", timeout=30)["payload"]
    assert close["closed"] is True and close["method"] == "exit" and close["pid"] is None

    after = server.call_tool("vissim_health")["payload"]
    assert after["connected"] is False and after["generation"] == 2

    finish(server)
    assert b"FAKE-VISSIM-NOISE" not in b"".join(server.stdout_lines)
    stderr = server.stderr_text()
    for kind in NOISE_KINDS:
        assert f"FAKE-VISSIM-NOISE {kind} in Links.Count" in stderr, kind
        assert f"FAKE-VISSIM-NOISE {kind} in Vissim.Exit" in stderr, kind


def test_connect_failure_through_the_real_backend(start_server: StartServer) -> None:
    server = start_server(env={**SAFE_ENV, "FAKE_VISSIM_DISPATCH_ERROR": "0x80080005"})
    handshake(server)
    call = server.call_tool("vissim_connect", timeout=30)
    assert call["result"]["isError"] is True
    error = call["payload"]["error"]
    assert error["code"] == "COM_ERROR" and error["details"]["hresult_hex"] == "0x80080005"
    assert error["hint"] == HRESULT_HINTS[CO_E_SERVER_EXEC_FAILURE]
    health = server.call_tool("vissim_health")["payload"]
    assert health["connected"] is False and health["last_error"]["code"] == "COM_ERROR"
    finish(server)


def test_stdin_eof_exits_fast_while_connect_hangs_in_dispatch(start_server: StartServer) -> None:
    """FAKE_VISSIM_HANG_S: Dispatch blocks (like Vissim behind a license dialog)."""
    server = start_server(env={**SAFE_ENV, "FAKE_VISSIM_HANG_S": "60"})
    handshake(server)
    request_id = server.start_tool_call("vissim_connect")
    health = server.wait_for_health(lambda p: p["connecting"] and p["current_task"] == "Dispatch")
    assert health["connected"] is False
    finish(server)  # <= 300 ms although the COM worker is stuck
    assert not answered(server, request_id)


def test_stdin_eof_exits_fast_while_a_tool_call_hangs(start_server: StartServer) -> None:
    server = start_server(env={**SAFE_ENV, "FAKE_VISSIM_HANG_ON": "Vissim.Net:60"})
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    request_id = server.start_tool_call("vissim_info")
    server.wait_for_health(lambda p: p["current_task"] == "vissim_info" and p["busy_seconds"] > 0)
    finish(server)
    assert not answered(server, request_id)


def test_timeout_hung_then_restart_through_the_protocol(start_server: StartServer) -> None:
    """A hung COM call: TIMEOUT_HUNG with the modal-dialog hint, health shows the busy worker, vissim_restart
    gives a fresh connection, and the exit stays fast with the abandoned worker still blocked."""
    server = start_server(
        env={**SAFE_ENV, "FAKE_VISSIM_HANG_ON": "Vissim.Net:60", "VISSIM_CALL_TIMEOUT_S": "0.5"}
    )
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True

    timed_out = server.call_tool("vissim_info", timeout=30)
    assert timed_out["result"]["isError"] is True
    error = timed_out["payload"]["error"]
    assert error["code"] == "TIMEOUT_HUNG"
    assert "did not finish within 0.5 s" in error["message"]
    assert "modal dialog" in error["hint"] and "vissim_restart" in error["hint"]

    health = server.call_tool("vissim_health")["payload"]
    assert health["busy_seconds"] > 0 and health["current_task"] == "vissim_info"
    assert health["last_error"]["code"] == "TIMEOUT_HUNG"

    restart = server.call_tool("vissim_restart", timeout=30)["payload"]
    assert restart["ok"] is True and restart["generation"] == 2
    assert any("was not detected, so it was not killed" in w for w in restart["warnings"])
    fresh = server.call_tool("vissim_health")["payload"]
    assert fresh["connected"] is True and fresh["busy_seconds"] == 0.0 and fresh["generation"] == 2
    finish(server)
