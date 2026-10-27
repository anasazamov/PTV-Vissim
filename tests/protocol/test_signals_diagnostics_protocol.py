"""Protocol tests for the signal and diagnostic tools (PLAN 10.2, 11 row M5) and the complete tool catalogue.

The server runs as ``python -X utf8 -m vissim_mcp`` with the fake pywin32 modules. Checks: tools/list holds
exactly the 22 tools of PLAN 7 (each with a description below 4096 characters and annotations);
set_signal_state and tail_log over stdio; exec_python is disabled by default and, when enabled, nothing the
executed code writes to stdout (``print``, ``sys.stdout``, ``os.write(1)``) reaches the protocol stream.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from protocol_helpers import ALL_TOOLS, finish, handshake

from stdio_client import StdioServer

StartServer = Callable[..., StdioServer]
SAFE_ENV = {
    "VISSIM_EXE_PATTERN": r"^vissim_mcp_test_standin\d*\.exe$",
    "VISSIM_PID_DETECT_S": "0.3",
}
PLAN_7_TOOLS = sorted(
    [
        # 7.1 life cycle
        "vissim_connect",
        "vissim_health",
        "vissim_info",
        "vissim_restart",
        "vissim_close",
        # 7.2 files
        "vissim_load_network",
        "vissim_save_network",
        # 7.3 objects
        "vissim_describe",
        "vissim_list_objects",
        "vissim_get_attributes",
        "vissim_set_attributes",
        "vissim_set_attribute_bulk",
        # 7.4 simulation
        "vissim_configure_simulation",
        "vissim_start_simulation",
        "vissim_simulation_status",
        "vissim_wait_simulation",
        "vissim_resume_simulation",
        "vissim_stop_simulation",
        # 7.5 results and signals
        "vissim_export_csv",
        "vissim_set_signal_state",
        # 7.6 diagnostics
        "vissim_tail_log",
        "vissim_exec_python",
    ]
)


def audit_records(audit_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for file in sorted(audit_dir.glob("audit-*.jsonl"))
        for line in file.read_text(encoding="utf-8").splitlines()
    ]


def test_tools_list_is_exactly_the_plan_7_catalogue(start_server: StartServer) -> None:
    server = start_server(env=SAFE_ENV)
    tools = handshake(server)
    assert sorted(tools) == PLAN_7_TOOLS
    assert len(tools) == 22 and list(tools) == ALL_TOOLS
    for name, tool in tools.items():
        description = tool.get("description") or ""
        assert description and len(description) < 4096, name
        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), name
        assert isinstance(annotations.get("readOnlyHint"), bool), name
        assert isinstance(annotations.get("destructiveHint"), bool), name
        assert annotations.get("title"), name
    assert tools["vissim_tail_log"]["annotations"]["readOnlyHint"] is True
    assert list(tools["vissim_tail_log"]["inputSchema"]["properties"]) == ["lines"]
    assert tools["vissim_exec_python"]["annotations"]["destructiveHint"] is True
    assert tools["vissim_set_signal_state"]["annotations"]["readOnlyHint"] is False
    finish(server)


def test_signal_state_tail_log_and_disabled_exec(
    start_server: StartServer, isolated_env: dict[str, Path]
) -> None:
    server = start_server(env=SAFE_ENV)
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    signal = server.call_tool("vissim_set_signal_state", {"sc_no": 1, "sg_no": 2, "state": "green"})[
        "payload"
    ]
    assert signal["ok"] is True, signal
    assert signal["SigState"] == "GREEN" and signal["ContrByCOM"] is True
    released = server.call_tool(
        "vissim_set_signal_state", {"sc_no": 1, "sg_no": 2, "release_to_controller": True}
    )["payload"]
    assert released["ok"] is True and released["ContrByCOM"] is False

    disabled = server.call_tool("vissim_exec_python", {"code": "result = 1"})["payload"]
    assert disabled["ok"] is False and disabled["error"]["code"] == "EXEC_DISABLED"

    tail = server.call_tool("vissim_tail_log", {"lines": 200})["payload"]
    assert tail["ok"] is True and tail["path"].endswith("vissim-mcp.log")
    text = "\n".join(tail["lines"])
    assert (
        "tool vissim_set_signal_state ok" in text and "tool vissim_exec_python failed (EXEC_DISABLED)" in text
    )
    finish(server)

    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] != "vissim_connect"]
    assert [(r["tool"], r["ok"]) for r in records] == [
        ("vissim_set_signal_state", True),
        ("vissim_set_signal_state", True),
        ("vissim_exec_python", False),
    ]
    assert records[2]["args"] == {"code": "result = 1"} and records[2]["error_code"] == "EXEC_DISABLED"


def test_exec_output_never_reaches_the_protocol_stream(
    start_server: StartServer, isolated_env: dict[str, Path]
) -> None:
    server = start_server(env={**SAFE_ENV, "VISSIM_MCP_ALLOW_EXEC": "1"})
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    code = (
        "import os, sys\n"
        "print('EXEC-NOISE print')\n"
        "sys.stdout.write('EXEC-NOISE sys.stdout\\n')\n"
        "sys.stdout.flush()\n"
        "os.write(1, b'EXEC-NOISE fd1\\n')\n"
        "result = {'links': Vissim.Net.Links.Count, 'text': 'Registon — Регистан'}\n"
    )
    executed = server.call_tool("vissim_exec_python", {"code": code})["payload"]
    assert executed["ok"] is True, executed
    assert executed["result"] == {"links": 10, "text": "Registon — Регистан"}
    assert executed["output"] == "EXEC-NOISE print\n"
    for exiting in ("exit(3)", "quit()", "raise SystemExit"):
        refused = server.call_tool("vissim_exec_python", {"code": exiting})["payload"]
        assert refused["ok"] is False and refused["error"]["code"] == "INVALID_ARGUMENT", refused
    no_input = server.call_tool("vissim_exec_python", {"code": "result = input()"})["payload"]
    assert no_input["error"]["code"] == "INVALID_ARGUMENT"
    # the session survives: exit() did not close stdin, input() did not read the protocol stream
    assert server.call_tool("vissim_health")["payload"]["ok"] is True
    finish(server)

    stdout = b"".join(server.stdout_lines)
    # print() is captured into the result (inside a JSON-RPC message); the raw writes went to stderr
    assert b"EXEC-NOISE sys.stdout" not in stdout and b"EXEC-NOISE fd1" not in stdout
    assert all(line.startswith(b"{") for line in server.stdout_lines)
    stderr = server.stderr_text()
    assert "EXEC-NOISE sys.stdout" in stderr and "EXEC-NOISE fd1" in stderr
    records = [
        r
        for r in audit_records(isolated_env["audit"])
        if r["tool"] == "vissim_exec_python" and r.get("phase") != "start"
    ]
    assert [r["ok"] for r in records] == [True, False, False, False, False]
    assert records[0]["args"] == {"code": code}


def test_exec_code_is_audited_before_it_ends_the_server(
    start_server: StartServer, isolated_env: dict[str, Path]
) -> None:
    """Code that ends the process (os._exit) leaves no outcome record: the start record has the code."""
    server = start_server(env={**SAFE_ENV, "VISSIM_MCP_ALLOW_EXEC": "1"})
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    code = "import os\nos._exit(7)"
    server.start_tool_call("vissim_exec_python", {"code": code})
    assert server.wait(20) == 7
    records = [r for r in audit_records(isolated_env["audit"]) if r["tool"] == "vissim_exec_python"]
    assert [(r["phase"], r["args"]) for r in records] == [("start", {"code": code})]


def test_exec_child_processes_do_not_inherit_the_protocol_stdin(
    start_server: StartServer, isolated_env: dict[str, Path]
) -> None:
    """A child started without stdin=DEVNULL gets NUL as stdin, not the MCP pipe (it used to hang)."""
    server = start_server(env={**SAFE_ENV, "VISSIM_MCP_ALLOW_EXEC": "1", "VISSIM_CALL_TIMEOUT_S": "30"})
    handshake(server)
    assert server.call_tool("vissim_connect", timeout=30)["payload"]["ok"] is True
    code = (
        "import subprocess, sys\n"
        "child = subprocess.run([sys.executable, '-c', "
        "'import sys; data = sys.stdin.read(); print(\"CHILD-STDOUT\", len(data))'], timeout=20)\n"
        "result = child.returncode\n"
    )
    executed = server.call_tool("vissim_exec_python", {"code": code}, timeout=40)["payload"]
    assert executed["ok"] is True and executed["result"] == 0, executed
    # the session still works: the child read nothing of the protocol stream
    assert server.call_tool("vissim_health")["payload"]["ok"] is True
    finish(server)
    assert all(line.startswith(b"{") for line in server.stdout_lines)
    assert "CHILD-STDOUT 0" in server.stderr_text()
