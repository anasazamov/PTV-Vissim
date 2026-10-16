"""Life-cycle tools (PLAN 7.1).

Milestone M1 ships ``vissim_health``; ``vissim_connect``, ``vissim_info``, ``vissim_restart`` and
``vissim_close`` follow in M2 together with the COM worker.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..service import VissimService
from ._common import register_tool


def register(mcp: FastMCP, service: VissimService) -> None:
    async def vissim_health() -> dict[str, Any]:
        """Report the MCP server and Vissim connection state. Never calls COM, so it answers instantly.

        Call it first in every session and whenever something looks wrong (a hung call, a lost connection).
        Returns: connected, pid, vissim_alive, generation, busy_seconds and current_task (a COM call that is
        running or hung), queue_depth, job (short simulation status), loaded_file, job_object, shutting_down,
        settings (the non-secret VISSIM_* configuration, including the allowed directories), version,
        mcp_version, python, uptime_s, last_error and warnings (configuration problems).

        No parameters. Example: vissim_health()
        """
        return service.health()

    register_tool(
        mcp,
        vissim_health,
        name="vissim_health",
        title="Vissim server health",
        settings=service.settings,
        read_only=True,
        idempotent=True,
    )
