"""Life-cycle tools (PLAN 7.1): vissim_connect, vissim_health, vissim_info, vissim_restart, vissim_close."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..errors import ErrorCode, VissimMcpError, describe_exception, is_connection_error
from ..service import VissimService
from ..worker import WorkerContext
from ._common import register_tool

#: Network collections counted by vissim_info (all listed in the Vissim 2026 type library, INet).
INFO_COLLECTIONS = (
    "Links",
    "Nodes",
    "SignalControllers",
    "SignalHeads",
    "VehicleInputs",
    "VehicleRoutingDecisionsStatic",
    "DataCollectionMeasurements",
    "VehicleTravelTimeMeasurements",
    "QueueCounters",
)
#: Simulation attributes reported by vissim_info (documented, docs/com-names.md).
INFO_SIMULATION_ATTRIBUTES = ("SimPeriod", "SimRes")
_MAX_ERROR_CHARS = 200


def collection_counts(vissim: Any, errors: dict[str, str]) -> dict[str, Any]:
    """``Count`` of every collection in :data:`INFO_COLLECTIONS`, each read on its own (worker thread).

    A count that cannot be read becomes null and is explained in ``errors``; a lost connection (also a call
    that failed in transit) is raised.
    """
    counts: dict[str, Any] = {}
    net = vissim.Net
    for name in INFO_COLLECTIONS:
        try:
            counts[name] = getattr(net, name).Count
        except Exception as exc:
            if is_connection_error(exc):
                raise
            counts[name] = None
            errors[name] = describe_exception(exc, _MAX_ERROR_CHARS)
    return counts


def read_info(ctx: WorkerContext) -> dict[str, Any]:
    """COM task of vissim_info: collection counts and simulation attributes, each read on its own.

    A value that cannot be read becomes null and is explained in ``errors``; a lost connection (also a call
    that failed in transit) is raised.
    """
    vissim = ctx.vissim
    if vissim is None:
        raise VissimMcpError(ErrorCode.NOT_CONNECTED, "Vissim is not connected")
    errors: dict[str, str] = {}
    result: dict[str, Any] = {"counts": collection_counts(vissim, errors)}
    simulation = vissim.Simulation
    for attribute in INFO_SIMULATION_ATTRIBUTES:
        try:
            result[attribute] = simulation.AttValue(attribute)
        except Exception as exc:
            if is_connection_error(exc):
                raise
            result[attribute] = None
            errors[f"Simulation.{attribute}"] = describe_exception(exc, _MAX_ERROR_CHARS)
    if errors:
        result["errors"] = errors
    return result


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_connect(progid: str | None = None) -> dict[str, Any]:
        """Start a NEW PTV Vissim instance through COM and connect this server to it.

        Call it once per session when vissim_health shows connected=false; every other Vissim tool needs it.
        An already open Vissim window is never attached to. Starting takes about 10-60 s. A license dialog
        ('License not found', license selection) is detected: the started instance is closed and the error
        explains what to do.

        Returns: pid (the Vissim process; null if it could not be identified - then it is not force-closed on
        exit), progid, job_object (true = Windows closes Vissim even if this server dies), generation,
        elapsed_s, warnings.
        Errors: ALREADY_CONNECTED (use vissim_restart for a fresh instance), COM_ERROR (see its hint, e.g. the
        COM server is not registered or the license has no COM module), TIMEOUT_HUNG (a dialog blocks
        start-up).

        Parameters: progid - optional COM ProgID, default VISSIM_PROGID (e.g. "Vissim.Vissim" or
        "Vissim.Vissim.2026").
        Example: vissim_connect()
        """
        return await service.connect(progid)

    async def vissim_health() -> dict[str, Any]:
        """Report the MCP server and Vissim connection state. Never calls COM, so it answers instantly.

        Call it first in every session and whenever something looks wrong (a hung call, a lost connection).
        Returns: connected, connecting, pid, vissim_alive, progid, generation, busy_seconds and current_task
        (a COM call that is running or hung), queue_depth, job (short simulation status), loaded_file,
        job_object, shutting_down, settings (the non-secret VISSIM_* configuration, including the allowed
        directories), version, mcp_version, python, uptime_s, last_error and warnings.

        No parameters. Example: vissim_health()
        """
        return service.health()

    async def vissim_info() -> dict[str, Any]:
        """Summarise the model in Vissim: object counts of the main network collections and simulation
        settings.

        Returns: loaded_file (the network loaded through this server, null otherwise), saved_file (the last
        file written by vissim_save_network, null otherwise), pid, progid, counts ({Links, Nodes,
        SignalControllers, SignalHeads, VehicleInputs, VehicleRoutingDecisionsStatic,
        DataCollectionMeasurements, VehicleTravelTimeMeasurements, QueueCounters} -> number; null when a count
        could not be read), SimPeriod and SimRes, and errors (only when some value could not be read).
        Reads a few values through COM, normally in well under a second.
        Errors: NOT_CONNECTED (call vissim_connect), TIMEOUT_HUNG, COM_ERROR.

        No parameters. Example: vissim_info()
        """
        data = await service.call(read_info, task_name="vissim_info")
        return {
            "loaded_file": service.loaded_file,
            "saved_file": service.saved_file,
            "pid": service.pid,
            "progid": service.progid,
            **data,
        }

    async def vissim_restart() -> dict[str, Any]:
        """Replace Vissim with a fresh instance: kill the current one, then start and connect a new one.

        Use it after TIMEOUT_HUNG (a modal dialog or a hang), after a crash or when the connection was lost;
        it also works when nothing is connected. Only the Vissim process this server started is killed
        (checked by PID, name and start time). Unsaved changes of the old instance are LOST and a running
        simulation job is aborted.

        Returns: the vissim_connect fields (pid, progid, job_object, generation, elapsed_s, warnings) plus
        previous_pid, killed and aborted_job_id.

        No parameters. Example: vissim_restart()
        """
        return await service.restart()

    async def vissim_close() -> dict[str, Any]:
        """Close Vissim at the end of the work and free the license seat.

        Calls Exit(), waits up to 10 s for the process to end, then terminates and finally kills it - only the
        process this server started. There is no save option: unsaved changes are LOST, so save first with
        vissim_save_network. A running simulation job is aborted; a vissim_connect that is still starting
        Vissim is cancelled.

        Returns: closed, method ("exit", "terminate" or "kill"; null when it had already exited), pid,
        warnings, aborted_job_id (when a job was aborted).
        Errors: NOT_CONNECTED when nothing is connected.

        No parameters. Example: vissim_close()
        """
        return await service.close()

    register_tool(
        mcp,
        vissim_connect,
        name="vissim_connect",
        title="Start and connect Vissim",
        settings=settings,
        read_only=False,
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_health,
        name="vissim_health",
        title="Vissim server health",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_info,
        name="vissim_info",
        title="Vissim model summary",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_restart,
        name="vissim_restart",
        title="Restart Vissim",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_close,
        name="vissim_close",
        title="Close Vissim",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
