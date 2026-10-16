"""Helpers shared by the protocol tests (the server as a subprocess, spoken to over stdio)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_TESTS_DIR = str(Path(__file__).resolve().parents[1])
if _TESTS_DIR not in sys.path:
    sys.path.append(_TESTS_DIR)

from stdio_client import StdioServer  # noqa: E402
from vissim_mcp import __version__  # noqa: E402

#: Claude Code (Windows) closes stdin and kills the process tree 300 ms later (PLAN 3.3).
EXIT_BUDGET_S = 0.300
LIFECYCLE_TOOLS = ["vissim_connect", "vissim_health", "vissim_info", "vissim_restart", "vissim_close"]


def handshake(server: StdioServer) -> dict[str, dict[str, Any]]:
    """initialize -> notifications/initialized -> tools/list; returns the tools by name."""
    init = server.initialize()
    result = init["result"]
    assert result["serverInfo"] == {"name": "vissim", "version": __version__}
    assert "vissim_health" in result.get("instructions", "")
    assert "tools" in result["capabilities"]
    tools = server.request("tools/list")["result"]["tools"]
    return {tool["name"]: tool for tool in tools}


def finish(server: StdioServer) -> float:
    """Close stdin, check the exit budget and code, and validate the complete stdout."""
    exit_s = server.close_stdin_and_time_exit()
    assert server.process.returncode == 0, server.stderr_text()
    server.finish_reading()
    server.assert_stdout_is_jsonrpc()
    assert exit_s < EXIT_BUDGET_S, f"server needed {exit_s * 1000:.0f} ms to exit after stdin closed"
    return exit_s
