"""Protocol tests (PLAN 10.2): ``python -X utf8 -m vissim_mcp`` as a subprocess, spoken to over stdio.

Every test checks that *each* stdout line is a JSON-RPC 2.0 message. Exit timing: Claude Code (Windows) closes
stdin and kills the process tree 300 ms later, so the server must be gone within 300 ms of stdin closing. The
budget is measured from just before ``stdin.close()`` until the process (the venv launcher *and* its child
interpreter) has exited; no extra slack is added for CI.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from noise import NOISE_LABELS
from protocol_helpers import ALL_TOOLS, finish, handshake

from stdio_client import StdioServer
from vissim_mcp import __version__

LAUNCHER = Path(__file__).resolve().with_name("noisy_launcher.py")
StartServer = Callable[..., StdioServer]


def test_initialize_tools_list_health_and_fast_exit(
    start_server: StartServer, isolated_env: dict[str, Path]
) -> None:
    server = start_server()
    tools = handshake(server)
    assert list(tools) == ALL_TOOLS
    health_tool = tools["vissim_health"]
    assert health_tool["annotations"]["readOnlyHint"] is True
    assert health_tool["inputSchema"]["type"] == "object"

    call = server.call_tool("vissim_health")
    assert call["result"]["isError"] is False
    payload = call["payload"]
    assert payload["ok"] is True
    assert payload["connected"] is False and payload["pid"] is None
    assert payload["version"] == __version__
    assert payload["settings"]["VISSIM_ALLOWED_DIRS"] == [str(isolated_env["projects"].resolve())]
    assert payload["settings"]["VISSIM_LOG_DIR"] == str(isolated_env["log"].resolve())

    finish(server)
    log_text = (isolated_env["log"] / "vissim-mcp.log").read_text(encoding="utf-8")
    assert "starting" in log_text and "tool vissim_health ok" in log_text
    assert "exiting: stdin closed (code 0)" in log_text


def test_deliberate_noise_never_reaches_stdout(start_server: StartServer) -> None:
    """print(), sys.stdout/sys.__stdout__ writes, os.write(1) and native WriteFile - before the server starts
    (launcher) and from the fake COM layer on every call (FAKE_VISSIM_PRINT_STDOUT=1)."""
    server = start_server(
        args=[str(LAUNCHER)],
        env={"NOISY_LAUNCHER_FAKE_COM": "1", "FAKE_VISSIM_PRINT_STDOUT": "1"},
    )
    tools = handshake(server)
    assert list(tools) == ALL_TOOLS
    assert server.call_tool("vissim_health")["payload"]["ok"] is True
    finish(server)

    stdout = b"".join(server.stdout_lines)
    assert b"NOISY-LAUNCHER" not in stdout
    assert b"FAKE-VISSIM-NOISE" not in stdout
    stderr = server.stderr_text()
    for label in NOISE_LABELS:
        assert f"NOISY-LAUNCHER {label}" in stderr, label
    for kind in ("print()", "os.write(1)", "native WriteFile"):
        assert f"FAKE-VISSIM-NOISE {kind} in Links.Count" in stderr, kind


def test_garbage_input_does_not_break_the_session(start_server: StartServer) -> None:
    server = start_server()
    handshake(server)
    server.send_raw(b"this is not json\n")
    server.send_raw(b'{"jsonrpc": "2.0", "id": 99, "method": "no/such/method"}\n')
    tools = server.request("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ALL_TOOLS
    assert server.call_tool("vissim_health")["payload"]["ok"] is True
    finish(server)


def test_ping_and_unknown_tool(start_server: StartServer) -> None:
    server = start_server()
    handshake(server)
    assert server.request("ping")["result"] == {}
    unknown = server.request("tools/call", {"name": "vissim_nope", "arguments": {}})
    assert unknown["result"]["isError"] is True
    assert server.call_tool("vissim_health")["payload"]["ok"] is True
    finish(server)


def test_exit_when_stdin_closes_before_initialize(start_server: StartServer) -> None:
    server = start_server()
    server.close_stdin()
    assert server.wait(timeout=30) == 0
    server.finish_reading()
    assert server.stdout_lines == []


def test_non_ascii_values_are_utf8_on_the_wire(start_server: StartServer, tmp_path: Path) -> None:
    special = tmp_path / "Samarqand_ko'cha_Самарканд"
    special.mkdir()
    server = start_server(env={"VISSIM_ALLOWED_DIRS": str(special)})
    handshake(server)
    payload = server.call_tool("vissim_health")["payload"]
    assert payload["settings"]["VISSIM_ALLOWED_DIRS"] == [str(special.resolve())]
    finish(server)  # strict UTF-8 decoding of every stdout line


def test_startup_is_fast(start_server: StartServer) -> None:
    started = time.perf_counter()
    server = start_server()
    server.initialize()
    elapsed = time.perf_counter() - started
    finish(server)
    assert elapsed < 5.0, f"initialize answered after {elapsed:.2f} s"


# --- command line


def run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "vissim_mcp", *args],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_version_goes_to_stdout(server_env: dict[str, str]) -> None:
    result = run_cli(["--version"], server_env)
    assert result.returncode == 0
    assert result.stdout == f"vissim-mcp {__version__}\n".encode()


def test_check_prints_the_configuration_to_stderr(
    server_env: dict[str, str], isolated_env: dict[str, Path]
) -> None:
    result = run_cli(["--check"], server_env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    shown = json.loads(result.stderr.decode("utf-8"))
    assert shown["VISSIM_PROGID"] == "Vissim.Vissim"
    assert shown["VISSIM_ALLOWED_DIRS"] == [str(isolated_env["projects"].resolve())]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VISSIM_CALL_TIMEOUT_S", "-1"),
        ("VISSIM_WAIT_MAX_S", "200"),
        ("VISSIM_EXE_PATTERN", ""),
        ("VISSIM_KILL_ON_EXIT", "maybe"),
        ("VISSIM_PROGID", "not a progid"),
    ],
)
def test_invalid_configuration_exits_with_code_2(server_env: dict[str, str], name: str, value: str) -> None:
    for args in ([], ["--check"]):
        result = run_cli(args, {**server_env, name: value})
        assert result.returncode == 2
        assert result.stdout == b""
        assert f"configuration error: {name}:" in result.stderr.decode("utf-8")


def test_unknown_argument_exits_with_code_2(server_env: dict[str, str]) -> None:
    result = run_cli(["--no-such-flag"], server_env)
    assert result.returncode == 2
    assert result.stdout == b""
