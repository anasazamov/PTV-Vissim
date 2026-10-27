"""Simulation tools (PLAN 7.4): vissim_configure_simulation, vissim_start_simulation,
vissim_simulation_status, vissim_wait_simulation, vissim_resume_simulation, vissim_stop_simulation.

A simulation never runs inside one tool call (PLAN 0.2): ``vissim_start_simulation`` creates a background job
(:mod:`vissim_mcp.jobs`) and returns at once; the COM worker advances it one chunk at a time between other
calls. Status and wait never touch COM. Configure, start, resume and stop change Vissim or the job and write
one audit record per call (PLAN 6.8).
"""

from __future__ import annotations

import asyncio
import functools
import math
import time
from collections.abc import Mapping
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from ..config import MAX_SIM_PERIOD_S
from ..errors import ErrorCode, VissimMcpError, describe_exception, is_com_error, is_connection_error
from ..jobs import (
    ACTIVE_STATES,
    MAX_SEED,
    JobRegistry,
    JobState,
    SimulationJob,
    StartRequest,
    job_running_error,
    prepare_job,
)
from ..policy import check_attribute_writable
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, FlagArg, flag, register_tool
from .objects import attribute_name, attribute_value

#: Polling interval of vissim_wait_simulation (PLAN 7.4: asyncio.sleep(1)); tests shorten it.
WAIT_POLL_S = 1.0
MAX_RESOLUTION = 1000
MAX_EXTRA_ATTRIBUTES = 50
QUICK_MODE_PATH = "Graphics.CurrentNetworkWindow.QuickMode"
LOST_CONNECTION_WARNING = (
    "the connection to Vissim was lost, so this job cannot continue; call vissim_restart (it aborts the job)"
)

OptionalIntArg = Annotated[Any, WithJsonSchema({"anyOf": [{"type": "integer"}, {"type": "null"}]})]
IntArg = Annotated[Any, WithJsonSchema({"type": "integer"})]
PauseAtArg = Annotated[
    Any, WithJsonSchema({"anyOf": [{"type": "array", "items": {"type": "integer"}}, {"type": "null"}]})
]
ExtraArg = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "object", "additionalProperties": {"type": ["number", "string", "boolean"]}},
                {"type": "null"},
            ]
        }
    ),
]


def _invalid(message: str) -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, message, hint="")


def _optional_int(value: Any, field: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{field} must be an integer, got {value!r}")
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise _invalid(f"{field} must be an integer, got {value!r}")
        value = int(value)
    if not minimum <= value <= maximum:
        raise _invalid(f"{field} must be between {minimum} and {maximum}, got {value}")
    return int(value)


def extra_values(extra: Any, allow_path_attrs: bool) -> list[tuple[str, Any]]:
    """``extra`` of vissim_configure_simulation as validated ``(attribute, value)`` pairs (attribute
    policy applied)."""
    if extra is None:
        return []
    if not isinstance(extra, Mapping):
        raise _invalid("extra must be an object {Simulation attribute: value}")
    if len(extra) > MAX_EXTRA_ATTRIBUTES:
        raise _invalid(f"at most {MAX_EXTRA_ATTRIBUTES} extra attributes, got {len(extra)}")
    pairs: list[tuple[str, Any]] = []
    for raw_name, raw_value in extra.items():
        name = attribute_name(raw_name, "extra attribute name")
        check_attribute_writable(name, allow_path_attrs)
        pairs.append((name, attribute_value(raw_value, f"extra value of {name!r}")))
    return pairs


# --- COM tasks (worker thread)


def _try(failed: list[dict[str, Any]], label: str, action: Any) -> bool:
    """Run one setting; a Vissim error goes to ``failed`` (a lost connection and bugs are raised)."""
    try:
        action()
    except Exception as exc:
        if is_connection_error(exc) or not is_com_error(exc):
            raise
        failed.append({"attribute": label, "error": describe_exception(exc, 300)})
        return False
    return True


def configure_task(
    changes: list[tuple[str, Any]],
    quick_mode: bool,
    suspend_gui: bool,
    seed_attr: str,
    ctx: WorkerContext,
) -> dict[str, Any]:
    """Write the Simulation attributes one by one, set QuickMode and the GUI updates, read the result back."""
    vissim = ctx.vissim
    simulation = vissim.Simulation
    applied: list[str] = []
    failed: list[dict[str, Any]] = []
    for name, value in changes:
        if _try(failed, f"Simulation.{name}", functools.partial(simulation.SetAttValue, name, value)):
            applied.append(f"Simulation.{name}")

    def set_quick_mode() -> None:
        vissim.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", quick_mode)

    if _try(failed, QUICK_MODE_PATH, set_quick_mode):
        applied.append(QUICK_MODE_PATH)
    if suspend_gui:
        if _try(failed, "SuspendUpdateGUI", vissim.SuspendUpdateGUI):
            applied.append("SuspendUpdateGUI")
    elif _try(failed, "ResumeUpdateGUI", functools.partial(vissim.ResumeUpdateGUI, True)):
        applied.append("ResumeUpdateGUI")

    current: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for key, attribute in (("period_s", "SimPeriod"), ("resolution", "SimRes"), ("seed", seed_attr)):
        try:
            current[key] = simulation.AttValue(attribute)
        except Exception as exc:
            if is_connection_error(exc) or not is_com_error(exc):
                raise
            current[key] = None
            errors[f"Simulation.{attribute}"] = describe_exception(exc, 300)
    return {**current, "applied": applied, "failed": failed, "errors": errors}


def stop_paused_task(job: SimulationJob, ctx: WorkerContext) -> str:
    return job.stop_paused(ctx.vissim)


# --- tools


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    def registry() -> JobRegistry:
        jobs = service.jobs
        if not isinstance(jobs, JobRegistry):  # never None in the server; a guard for odd test wiring
            raise VissimMcpError(ErrorCode.INTERNAL, "the simulation job registry is not available")
        return jobs

    def connection_warnings(job: SimulationJob) -> list[str]:
        if job.state in ACTIVE_STATES and (not service.connected or job.generation != service.generation):
            return [LOST_CONNECTION_WARNING]
        return []

    def status(job: SimulationJob) -> dict[str, Any]:
        snapshot = job.snapshot()
        snapshot["warnings"] = [*snapshot.get("warnings", []), *connection_warnings(job)]
        return snapshot

    async def vissim_configure_simulation(
        period_s: OptionalIntArg = None,
        resolution: OptionalIntArg = None,
        seed: OptionalIntArg = None,
        max_speed: FlagArg = True,
        quick_mode: FlagArg = True,
        suspend_gui: FlagArg = True,
        extra: ExtraArg = None,
    ) -> dict[str, Any]:
        """Set the simulation parameters before vissim_start_simulation: period, resolution, random seed and
        speed options.

        Every setting is written on its own; a value Vissim refuses goes to failed, the others are still
        applied. Refused while a simulation job is running or paused. Defaults run at maximum speed:
        simulation speed unlimited (UseMaxSimSpeed), QuickMode on in the network window, GUI updates suspended
        (suspend_gui=false resumes them). Unverified Simulation attributes (e.g. NumCores) go through extra;
        names containing File/Path/Folder/Dir are refused. Written to the audit log.

        Parameters: period_s - simulation period in simulation seconds (SimPeriod); resolution - time steps
        per simulation second (SimRes); seed - random seed (VISSIM_SEED_ATTR, default RandSeed); max_speed,
        quick_mode, suspend_gui - booleans; extra - {Simulation attribute: value}.
        Returns: period_s, resolution, seed (read back from Vissim), applied, failed ([{attribute, error}]),
        errors (values that could not be read back), warnings.
        Errors: JOB_RUNNING, INVALID_ARGUMENT, ATTRIBUTE_DENIED, NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_configure_simulation(period_s=3600, resolution=10, seed=42)
        """
        args = {
            "period_s": period_s,
            "resolution": resolution,
            "seed": seed,
            "max_speed": max_speed,
            "quick_mode": quick_mode,
            "suspend_gui": suspend_gui,
            "extra": extra,
        }
        async with AuditRecord(service, "vissim_configure_simulation", args) as audit:
            period = _optional_int(period_s, "period_s", 1, MAX_SIM_PERIOD_S)
            res = _optional_int(resolution, "resolution", 1, MAX_RESOLUTION)
            seed_value = _optional_int(seed, "seed", 1, MAX_SEED)
            use_max_speed = flag(max_speed, "max_speed")
            quick = flag(quick_mode, "quick_mode")
            suspend = flag(suspend_gui, "suspend_gui")
            extras = extra_values(extra, settings.allow_path_attrs)
            service.ensure_connected()
            active = service.active_job()
            if active is not None:
                raise job_running_error(active)
            changes: list[tuple[str, Any]] = []
            if period is not None:
                changes.append(("SimPeriod", period))
            if res is not None:
                changes.append(("SimRes", res))
            if seed_value is not None:
                changes.append((settings.seed_attr, seed_value))
            changes.append(("UseMaxSimSpeed", use_max_speed))
            changes.extend(extras)
            task = functools.partial(configure_task, changes, quick, suspend, settings.seed_attr)
            data = await service.call(task, task_name="vissim_configure_simulation")
            audit.update(new=dict(changes), applied=data["applied"], failed=data["failed"])
        warnings: list[str] = []
        if data["failed"]:
            warnings.append(f"{len(data['failed'])} setting(s) failed; see failed")
        result = {
            "period_s": data["period_s"],
            "resolution": data["resolution"],
            "seed": data["seed"],
            "applied": data["applied"],
            "failed": data["failed"],
        }
        if data["errors"]:
            result["errors"] = data["errors"]
        result["warnings"] = [*warnings, *audit.warnings]
        return result

    async def vissim_start_simulation(
        runs: IntArg = 1,
        seed: OptionalIntArg = None,
        seed_increment: IntArg = 1,
        chunk_sim_s: OptionalIntArg = None,
        pause_at: PauseAtArg = None,
    ) -> dict[str, Any]:
        """Start a simulation as a background job and return at once with its job_id.

        Then poll with vissim_wait_simulation (or vissim_simulation_status) until state is finished. The job
        runs the period set in Vissim (vissim_configure_simulation) in chunks of chunk_sim_s simulation
        seconds; other tools are served between two chunks. Several runs (replications) use the seeds seed,
        seed+seed_increment, ... - give a seed for runs > 1 (without one the current seed is read; if it
        cannot be read the call is refused). pause_at pauses every run at those simulation seconds (state
        paused, e.g. to change a signal state); continue with vissim_resume_simulation. Only one job at a
        time. A simulation run still in progress in Vissim is stopped first (with a warning). Written to the
        audit log.

        Parameters: runs (1..1000, default 1); seed (>= 1); seed_increment (>= 1, default 1); chunk_sim_s
        (default VISSIM_CHUNK_SIM_S); pause_at - simulation seconds strictly between 0 and the period.
        Returns: job_id, state, period, runs, seeds (one per run; null = Vissim's current seed), chunk,
        pause_at, warnings (e.g. "ensure evaluation keeps previous runs" for runs > 1).
        Errors: JOB_RUNNING, INVALID_ARGUMENT (bad values, SimPeriod <= 0, "seed required for runs>1"),
        NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_start_simulation(runs=3, seed=42, seed_increment=1)
        """
        args = {
            "runs": runs,
            "seed": seed,
            "seed_increment": seed_increment,
            "chunk_sim_s": chunk_sim_s,
            "pause_at": pause_at,
        }
        async with AuditRecord(service, "vissim_start_simulation", args) as audit:
            request = StartRequest(
                runs, seed, seed_increment, chunk_sim_s, pause_at, default_chunk=settings.chunk_sim_s
            )
            service.ensure_connected()
            jobs = registry()
            active = jobs.active()
            if active is not None:
                raise job_running_error(active)
            task = functools.partial(prepare_job, jobs, request, settings)
            data = await service.call(task, task_name="vissim_start_simulation")
            audit.update(job_id=data["job_id"], seeds=data["seeds"], period=data["period"])
        return {
            "job_id": data["job_id"],
            "state": data["state"],
            "period": data["period"],
            "runs": data["runs"],
            "seeds": data["seeds"],
            "chunk": data["chunk"],
            "pause_at": data["pause_at"],
            "warnings": [*data["warnings"], *audit.warnings],
        }

    async def vissim_simulation_status(job_id: str | None = None) -> dict[str, Any]:
        """Report the state of a simulation job without calling COM (answers instantly, also during a chunk).

        Parameters: job_id - optional; default the current (latest) job. The last 20 jobs are kept.
        Returns: job_id, state (running, paused, finished, stopped, error, aborted), run, runs, simsec,
        period, percent (of all runs), elapsed_s, eta_s, seeds, pause_at, paused_at, stop_requested,
        run_wall_s (wall seconds per finished run), error ({code, message, hint?, details?} when state is
        error), abort_reason, warnings.
        Errors: JOB_NOT_FOUND.

        Example: vissim_simulation_status()
        """
        return status(registry().get(job_id))

    async def vissim_wait_simulation(job_id: str | None = None, max_wait_s: float = 60) -> dict[str, Any]:
        """Wait until a simulation job leaves the running state, at most max_wait_s seconds (capped at
        VISSIM_WAIT_MAX_S, default 90), then report its status. Call it repeatedly until state is finished,
        stopped, error or aborted (paused also returns, for work at a pause point). Never calls COM.

        Parameters: job_id - optional, default the current job; max_wait_s - seconds to wait (0 = no wait).
        Returns: the vissim_simulation_status fields plus waited_s.
        Errors: JOB_NOT_FOUND, INVALID_ARGUMENT.

        Example: vissim_wait_simulation(max_wait_s=60)
        """
        if isinstance(max_wait_s, bool) or not isinstance(max_wait_s, (int, float)):
            raise _invalid(f"max_wait_s must be a number, got {max_wait_s!r}")
        if not math.isfinite(max_wait_s) or max_wait_s < 0:
            raise _invalid(f"max_wait_s must be a number >= 0, got {max_wait_s!r}")
        job = registry().get(job_id)
        warnings: list[str] = []
        limit = float(max_wait_s)
        if limit > settings.wait_max_s:
            warnings.append(
                f"max_wait_s {max_wait_s:g} was reduced to {settings.wait_max_s:g} (VISSIM_WAIT_MAX_S)"
            )
            limit = settings.wait_max_s
        started = time.monotonic()
        deadline = started + limit
        while job.state is JobState.RUNNING and not connection_warnings(job):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(WAIT_POLL_S, remaining))
        result = status(job)
        result["waited_s"] = round(time.monotonic() - started, 2)
        result["warnings"] = [*warnings, *result["warnings"]]
        return result

    async def vissim_resume_simulation(job_id: str | None = None) -> dict[str, Any]:
        """Continue a paused simulation job (paused at a pause_at point) - it runs on in the background.

        Parameters: job_id - optional, default the current job.
        Returns: the vissim_simulation_status fields (state running).
        Errors: INVALID_ARGUMENT (the job is not paused), JOB_NOT_FOUND, NOT_CONNECTED.

        Example: vissim_resume_simulation()
        """
        async with AuditRecord(service, "vissim_resume_simulation", {"job_id": job_id}) as audit:
            job = registry().get(job_id)
            audit.update(job_id=job.id)
            if job.state is JobState.PAUSED:
                service.ensure_connected()
                if job.generation != service.generation:
                    raise VissimMcpError(
                        ErrorCode.NOT_CONNECTED,
                        f"simulation job {job.id} belongs to a previous Vissim connection",
                        hint="Start a new job with vissim_start_simulation.",
                    )
            job.resume()
        result = status(job)
        result["warnings"] = [*result["warnings"], *audit.warnings]
        return result

    async def vissim_stop_simulation(job_id: str | None = None) -> dict[str, Any]:
        """Stop a simulation job: a running job stops at the end of its current chunk (Simulation.Stop()),
        a paused one at once. The results of completed runs stay in Vissim.

        Parameters: job_id - optional, default the current job.
        Returns: job_id, stopping (true when a stop was requested or done), state (stopped for a paused job;
        still running until the chunk ends - poll with vissim_wait_simulation), warnings.
        Errors: JOB_NOT_FOUND, NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_stop_simulation()
        """
        async with AuditRecord(service, "vissim_stop_simulation", {"job_id": job_id}) as audit:
            job = registry().get(job_id)
            audit.update(job_id=job.id, state_before=job.state.value)
            warnings: list[str] = []
            stopping = True
            if job.state not in ACTIVE_STATES:
                stopping = False
                warnings.append(f"simulation job {job.id} is already {job.state.value}; nothing to stop")
            elif connection_warnings(job):
                job.abort("stopped while the connection to Vissim was lost")
                warnings.append(LOST_CONNECTION_WARNING)
            elif not job.request_stop():
                # Paused - also when the worker paused the job after the state check above. stop_paused
                # turns a job resumed meanwhile into a stop request.
                await service.call(
                    functools.partial(stop_paused_task, job), task_name="vissim_stop_simulation"
                )
                stopping = job.state is JobState.STOPPED or job.stop_requested
                if not stopping:
                    warnings.append(f"simulation job {job.id} is {job.state.value}; it was not stopped")
            audit.update(state_after=job.state.value)
        return {
            "job_id": job.id,
            "stopping": stopping,
            "state": job.state.value,
            "warnings": [*warnings, *audit.warnings],
        }

    register_tool(
        mcp,
        vissim_configure_simulation,
        name="vissim_configure_simulation",
        title="Configure the simulation",
        settings=settings,
        read_only=False,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_start_simulation,
        name="vissim_start_simulation",
        title="Start a simulation job",
        settings=settings,
        read_only=False,
        destructive=True,  # a simulation run in progress in Vissim is stopped first
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_simulation_status,
        name="vissim_simulation_status",
        title="Simulation job status",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_wait_simulation,
        name="vissim_wait_simulation",
        title="Wait for a simulation job",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_resume_simulation,
        name="vissim_resume_simulation",
        title="Resume a paused simulation job",
        settings=settings,
        read_only=False,
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_stop_simulation,
        name="vissim_stop_simulation",
        title="Stop a simulation job",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=True,
    )
