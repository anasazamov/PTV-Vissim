"""Simulation jobs (PLAN 6.6): the state machine of a background simulation and the job registry.

States::

    running -> paused <-> running -> finished | stopped | error | aborted

A job is created on the COM worker by :func:`prepare_job` (``vissim_start_simulation``) and registered in the
service's :class:`JobRegistry`. The worker's background step then calls :meth:`SimulationJob.tick` - one chunk
per call - whenever the queue is empty, so tool calls are served between two chunks (PLAN 6.2).

``tick`` (worker thread)::

    if stop_requested:  Simulation.Stop(); state = stopped
    target = min(next_break, period, nearest pause_at > position)
    if target < period:                                   # chunk branch
        SimBreakAt = target; RunContinuous(); new = SimSec
        new <= simsec          -> error "simulation did not advance"
        new >  target + 1      -> error "did not pause at SimBreakAt"
        new <  target - 1      -> early pause (a stale break point): accepted, one warning
        else (target reached)  -> pause point: paused; chunk boundary: next_break += chunk
    else:                                                 # final branch
        SimBreakAt = VISSIM_BREAK_DISABLE_VALUE; RunContinuous(); new = SimSec
        simsec < new < period - 1          -> early pause (error after MAX_EARLY_PAUSES in a row)
        new == simsec (simsec > 0)         -> error "simulation did not advance"
        else the run is complete (SimSec after a run is never relied upon): next run (seed, simsec 0) or
        finished
    COM error -> error (code, message, hint, details); a lost connection is raised to the service, which
    fails the job and marks the connection lost.

Status reads (:meth:`SimulationJob.snapshot`) never touch COM: they read the fields the worker writes (plain
attribute reads are atomic under the GIL); state transitions take a lock so that ``abort`` (event loop) and a
finishing chunk (worker) cannot overwrite each other - a terminal state is final.

This module never imports ``pythoncom`` / ``win32com`` (PLAN 4.3).
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any

from .config import MAX_SIM_PERIOD_S, Settings
from .errors import ErrorCode, VissimMcpError, from_com_error, is_com_error, is_connection_error

log = logging.getLogger(__name__)


class JobState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    STOPPED = "stopped"
    ERROR = "error"
    ABORTED = "aborted"


ACTIVE_STATES = frozenset({JobState.RUNNING, JobState.PAUSED})
TERMINAL_STATES = frozenset({JobState.FINISHED, JobState.STOPPED, JobState.ERROR, JobState.ABORTED})

MAX_RUNS = 1000
MAX_PAUSE_POINTS = 1000
MAX_SEED = 2**31 - 1
#: Consecutive early pauses tolerated in the final branch before the job fails.
MAX_EARLY_PAUSES = 3
#: Simulation seconds by which SimSec may differ from a break target and still count as "reached".
TARGET_TOLERANCE_S = 1.0
#: Below this difference two simulation seconds are equal.
EPS = 1e-6
#: Finished jobs kept besides the current one (JOB_NOT_FOUND beyond that).
HISTORY_SIZE = 20

MULTI_RUN_WARNING = (
    "ensure evaluation keeps previous runs (Evaluation attribute KeepPrevResults, docs/com-names.md); "
    "otherwise only the results of the last run remain"
)
STALE_BREAK_WARNING = (
    "the simulation paused before the requested break point (an old SimBreakAt or a break set in Vissim); "
    "the job continued from there"
)
_BREAK_HINT = (
    "SimBreakAt does not behave as expected (see VISSIM_BREAK_DISABLE_VALUE and docs/com-names.md), or the "
    "simulation was stopped in the Vissim window. Check Vissim, then start a new job."
)


def _num(value: float) -> int | float:
    """A simulation second for output: an int when it is integral."""
    return int(value) if float(value).is_integer() else round(float(value), 3)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _invalid(message: str, hint: str = "") -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _int_arg(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{field} must be an integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise _invalid(f"{field} must be an integer, got {value!r}")
        value = int(value)
    if not minimum <= value <= maximum:
        raise _invalid(f"{field} must be between {minimum} and {maximum}, got {value}")
    return int(value)


class StartRequest:
    """The validated, period-independent arguments of ``vissim_start_simulation``."""

    def __init__(
        self,
        runs: Any = 1,
        seed: Any = None,
        seed_increment: Any = 1,
        chunk_sim_s: Any = None,
        pause_at: Any = None,
        *,
        default_chunk: int,
    ) -> None:
        self.runs = _int_arg(runs, "runs", 1, MAX_RUNS)
        self.seed = None if seed is None else _int_arg(seed, "seed", 1, MAX_SEED)
        self.seed_increment = _int_arg(seed_increment, "seed_increment", 1, MAX_SEED)
        self.chunk = (
            int(default_chunk)
            if chunk_sim_s is None
            else _int_arg(chunk_sim_s, "chunk_sim_s", 1, MAX_SIM_PERIOD_S)
        )
        if pause_at is None:
            points: list[int] = []
        elif isinstance(pause_at, (str, bytes)) or not isinstance(pause_at, Sequence):
            raise _invalid("pause_at must be a list of integers (simulation seconds)")
        else:
            if len(pause_at) > MAX_PAUSE_POINTS:
                raise _invalid(f"at most {MAX_PAUSE_POINTS} pause_at values, got {len(pause_at)}")
            points = [
                _int_arg(value, f"pause_at[{index}]", 1, MAX_SIM_PERIOD_S)
                for index, value in enumerate(pause_at)
            ]
        self.pause_at = sorted(set(points))


def compute_seeds(seed: int | None, runs: int, increment: int) -> list[int | None]:
    """``seed + (k-1) * increment`` for run k; ``[None]`` for a single run without a seed."""
    if seed is None:
        return [None] * runs
    seeds: list[int | None] = [seed + k * increment for k in range(runs)]
    last = seeds[-1]
    if last is not None and last > MAX_SEED:
        raise _invalid(
            f"the seed of run {runs} would be {last}, above {MAX_SEED}; use a smaller seed or increment"
        )
    return seeds


class SimulationJob:
    """One simulation job (PLAN 6.6 fields plus ``paused_at``, ``generation`` and ``early_pauses``)."""

    def __init__(
        self,
        *,
        job_id: str,
        generation: int,
        period: float,
        runs: int,
        chunk: int,
        seeds: list[int | None],
        pause_at: list[int],
        seed_attr: str,
        warnings: list[str] | None = None,
    ) -> None:
        self.id = job_id
        self.generation = int(generation)
        self.state = JobState.RUNNING
        self.run = 1
        self.runs = int(runs)
        self.period = float(period)
        self.chunk = int(chunk)
        self.next_break = float(chunk)
        self.simsec = 0.0
        self.seeds = list(seeds)
        self.pause_at = list(pause_at)
        self.seed_attr = seed_attr
        self.stop_requested = False
        self.started_at = time.monotonic()
        self.created = _now_iso()
        self.run_started_at = self.started_at
        self.run_wall_s: list[float] = []
        self.ended_at: float | None = None
        self.error: dict[str, Any] | None = None
        self.warnings: list[str] = list(warnings or [])
        self.paused_at: float | None = None
        self.abort_reason: str | None = None
        self.early_pauses = 0
        self.chunks = 0
        # The scheduling position: the last target reached (or the second of an early pause). Pause points
        # at or below it are behind the job, whatever SimSec reads.
        self._position = 0.0
        self._lock = threading.RLock()

    # --- state transitions (any thread)

    def _transition(self, new: JobState, *, error: dict[str, Any] | None = None) -> bool:
        """Move to ``new`` unless the job already ended (a terminal state is final); True when moved."""
        with self._lock:
            if self.state in TERMINAL_STATES:
                return False
            if new is JobState.PAUSED and self.state is not JobState.RUNNING:
                return False
            self.state = new
            if new in TERMINAL_STATES:
                self.ended_at = time.monotonic()
                self.paused_at = None
            if error is not None:
                self.error = error
            return True

    def abort(self, reason: str) -> None:
        """Mark the job ``aborted`` (restart, close); idempotent, never overwrites a terminal state."""
        with self._lock:
            if not self._transition(JobState.ABORTED):
                return
            self.abort_reason = str(reason)
        log.warning("simulation job %s aborted: %s", self.id, reason)

    def fail(self, error: dict[str, Any]) -> None:
        """Mark the job ``error`` (``{code, message, hint?, details?}``)."""
        if self._transition(JobState.ERROR, error=dict(error)):
            log.warning("simulation job %s failed: %s", self.id, error.get("message"))

    def request_stop(self) -> None:
        """A running job stops at the next chunk boundary (the next ``tick``)."""
        with self._lock:
            if self.state is JobState.RUNNING:
                self.stop_requested = True

    def resume(self) -> None:
        """``paused`` -> ``running``; ``INVALID_ARGUMENT`` in any other state."""
        with self._lock:
            if self.state is not JobState.PAUSED:
                raise VissimMcpError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"simulation job {self.id} is {self.state.value}; only a paused job can be resumed",
                    hint="vissim_simulation_status shows the state of the job.",
                    details={"job_id": self.id, "state": self.state.value},
                )
            self.state = JobState.RUNNING
            self.paused_at = None

    def stop_paused(self, vissim: Any) -> str:
        """Worker thread: end the paused run with ``Simulation.Stop()`` -> ``stopped``. Returns the state."""
        with self._lock:
            if self.state is not JobState.PAUSED:
                return self.state.value
        vissim.Simulation.Stop()
        self._transition(JobState.STOPPED)
        return self.state.value

    # --- the chunk (worker thread)

    def tick(self, vissim: Any, settings: Settings) -> None:
        """One step of the state machine (PLAN 6.6). Records its own errors; a lost connection is raised so
        that the service fails the job and marks the connection lost."""
        if self.state is not JobState.RUNNING:
            return
        try:
            self._step(vissim.Simulation, settings)
        except VissimMcpError as err:
            self.fail(err.to_dict())
        except Exception as exc:
            if not is_com_error(exc) or is_connection_error(exc):
                raise
            self.fail(from_com_error(exc, context=f"simulation job {self.id}, run {self.run}").to_dict())

    def _step(self, simulation: Any, settings: Settings) -> None:
        if self.stop_requested:
            if self.simsec > 0:
                simulation.Stop()
            self._transition(JobState.STOPPED)
            return
        target = self._target()
        self.chunks += 1
        if target < self.period - EPS:
            self._chunk(simulation, target)
        else:
            self._final(simulation, settings)

    def _target(self) -> float:
        candidates = [self.next_break, self.period]
        candidates.extend(point for point in self.pause_at if point > self._position + EPS)
        return float(min(candidates))

    def _did_not_advance(self, new: float) -> VissimMcpError:
        return VissimMcpError(
            ErrorCode.COM_ERROR,
            f"simulation did not advance (run {self.run}: SimSec {_num(new)} after RunContinuous, before "
            f"{_num(self.simsec)})",
            hint=_BREAK_HINT,
            details={
                "job_id": self.id,
                "run": self.run,
                "simsec": _num(self.simsec),
                "sim_sec_read": _num(new),
            },
        )

    def _chunk(self, simulation: Any, target: float) -> None:
        simulation.SetAttValue("SimBreakAt", _num(target))
        simulation.RunContinuous()
        new = float(simulation.AttValue("SimSec"))
        if new <= self.simsec + EPS:
            raise self._did_not_advance(new)
        if new > target + TARGET_TOLERANCE_S:
            raise VissimMcpError(
                ErrorCode.COM_ERROR,
                f"simulation did not pause at SimBreakAt {_num(target)} (run {self.run}: SimSec {_num(new)})",
                hint=_BREAK_HINT,
                details={
                    "job_id": self.id,
                    "run": self.run,
                    "target": _num(target),
                    "sim_sec_read": _num(new),
                },
            )
        if new < target - TARGET_TOLERANCE_S:
            self._early_pause(new)
            return
        self.simsec = new
        self._position = target
        self.early_pauses = 0
        while self.next_break <= target + EPS:
            self.next_break += self.chunk
        if any(abs(point - target) <= EPS for point in self.pause_at):
            with self._lock:
                if self._transition(JobState.PAUSED):
                    self.paused_at = new
                    log.info("simulation job %s paused at %s s (run %d)", self.id, _num(new), self.run)

    def _early_pause(self, new: float) -> None:
        self.simsec = new
        self._position = new
        if STALE_BREAK_WARNING not in self.warnings:
            self.warnings.append(STALE_BREAK_WARNING)
            log.warning("simulation job %s: early pause at %s s (run %d)", self.id, _num(new), self.run)

    def _final(self, simulation: Any, settings: Settings) -> None:
        simulation.SetAttValue("SimBreakAt", settings.break_disable_value)
        simulation.RunContinuous()
        new = float(simulation.AttValue("SimSec"))
        if self.simsec + EPS < new < self.period - TARGET_TOLERANCE_S:
            self.early_pauses += 1
            if self.early_pauses > MAX_EARLY_PAUSES:
                raise VissimMcpError(
                    ErrorCode.COM_ERROR,
                    f"simulation keeps pausing before the end of run {self.run} ({self.early_pauses} early "
                    f"pauses, last at SimSec {_num(new)})",
                    hint=_BREAK_HINT,
                    details={"job_id": self.id, "run": self.run, "sim_sec_read": _num(new)},
                )
            self._early_pause(new)
            return
        if self.simsec > EPS and abs(new - self.simsec) <= EPS:
            raise self._did_not_advance(new)
        self._run_complete(simulation)

    def _run_complete(self, simulation: Any) -> None:
        if self.state is not JobState.RUNNING:
            return  # aborted (restart/close) while the run was finishing: leave Vissim alone
        now = time.monotonic()
        self.run_wall_s.append(round(now - self.run_started_at, 3))
        self.early_pauses = 0
        if self.run < self.runs:
            seed = self.seeds[self.run]
            if seed is not None:
                simulation.SetAttValue(self.seed_attr, seed)
            self.run += 1
            self.simsec = 0.0
            self._position = 0.0
            self.next_break = float(self.chunk)
            self.run_started_at = now
            log.info("simulation job %s: run %d of %d started (seed %s)", self.id, self.run, self.runs, seed)
            return
        self.simsec = self.period
        if self._transition(JobState.FINISHED):
            log.info("simulation job %s finished (%d run(s))", self.id, self.runs)

    # --- status (no COM)

    def percent(self) -> float:
        if self.state is JobState.FINISHED:
            return 100.0
        done = (self.run - 1) * self.period + min(max(self.simsec, 0.0), self.period)
        total = self.runs * self.period
        return round(min(100.0, done / total * 100.0), 2) if total > 0 else 0.0

    def snapshot(self) -> dict[str, Any]:
        """The COM-free status of ``vissim_simulation_status`` (PLAN 7.4)."""
        state = self.state
        end = self.ended_at
        elapsed = (end if end is not None else time.monotonic()) - self.started_at
        percent = self.percent()
        eta: float | None = None
        if state is JobState.RUNNING and 0 < percent < 100:
            eta = round(elapsed * (100.0 - percent) / percent, 1)
        elif state is JobState.FINISHED:
            eta = 0.0
        paused_at = self.paused_at
        return {
            "job_id": self.id,
            "state": state.value,
            "run": self.run,
            "runs": self.runs,
            "simsec": _num(self.simsec),
            "period": _num(self.period),
            "percent": percent,
            "elapsed_s": round(elapsed, 1),
            "eta_s": eta,
            "seeds": list(self.seeds),
            "pause_at": list(self.pause_at),
            "paused_at": None if paused_at is None else _num(paused_at),
            "stop_requested": self.stop_requested,
            "run_wall_s": list(self.run_wall_s),
            "created": self.created,
            "error": dict(self.error) if self.error else None,
            "abort_reason": self.abort_reason,
            "warnings": list(self.warnings),
        }


class JobRegistry:
    """The current job plus the last :data:`HISTORY_SIZE` earlier ones (thread-safe)."""

    def __init__(self, history_size: int = HISTORY_SIZE) -> None:
        self._lock = threading.RLock()
        self.current: SimulationJob | None = None
        self._history: deque[SimulationJob] = deque(maxlen=history_size)

    def active(self) -> SimulationJob | None:
        """The current job when it is running or paused."""
        job = self.current
        return job if job is not None and job.state in ACTIVE_STATES else None

    def add(self, job: SimulationJob) -> None:
        """Make ``job`` the current one; ``JOB_RUNNING`` while another job is running or paused."""
        with self._lock:
            active = self.active()
            if active is not None:
                raise job_running_error(active)
            if self.current is not None:
                self._history.appendleft(self.current)
            self.current = job

    def get(self, job_id: str | None = None) -> SimulationJob:
        """The job ``job_id`` (the current one when None); ``JOB_NOT_FOUND`` otherwise."""
        with self._lock:
            if job_id is None or (isinstance(job_id, str) and not job_id.strip()):
                if self.current is None:
                    raise VissimMcpError(
                        ErrorCode.JOB_NOT_FOUND,
                        "no simulation job has been started yet",
                        hint="Start one with vissim_start_simulation.",
                    )
                return self.current
            wanted = str(job_id).strip()
            for job in (self.current, *self._history):
                if job is not None and job.id == wanted:
                    return job
        raise VissimMcpError(
            ErrorCode.JOB_NOT_FOUND,
            f"simulation job {wanted!r} not found (only the last {self._history.maxlen + 1} jobs are kept)",
        )

    def jobs(self) -> list[SimulationJob]:
        with self._lock:
            return [job for job in (self.current, *self._history) if job is not None]


def job_running_error(job: Any) -> VissimMcpError:
    state = getattr(job, "state", "active")
    job_id = getattr(job, "id", None)
    return VissimMcpError(
        ErrorCode.JOB_RUNNING,
        f"simulation job {job_id} is {getattr(state, 'value', state)}",
        details={"job_id": job_id, "state": str(getattr(state, "value", state))},
    )


def new_job_id() -> str:
    return f"sim-{uuid.uuid4().hex[:10]}"


def _read_seed(simulation: Any, seed_attr: str) -> int | None:
    """The current seed for ``runs > 1`` without a seed; None when it cannot be read as an integer >= 1."""
    try:
        value = simulation.AttValue(seed_attr)
    except Exception as exc:
        if is_connection_error(exc):
            raise
        log.info("reading %s for a multi-run job failed: %s", seed_attr, exc)
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and 1 <= value <= MAX_SEED:
        return value
    return None


def prepare_job(registry: JobRegistry, request: StartRequest, settings: Settings, ctx: Any) -> dict[str, Any]:
    """COM task of ``vissim_start_simulation`` (worker thread): validate against Vissim, apply the first seed,
    create and register the job. Returns its snapshot plus ``chunk``.

    Errors: ``JOB_RUNNING`` (another job is running or paused), ``INVALID_ARGUMENT`` (SimPeriod <= 0,
    pause_at outside (0, period), no seed for runs > 1), COM errors of the reads/writes.
    """
    active = registry.active()
    if active is not None:
        raise job_running_error(active)
    simulation = ctx.vissim.Simulation
    period_value = simulation.AttValue("SimPeriod")
    try:
        period = float(period_value)
    except (TypeError, ValueError):
        period = float("nan")
    if not (math.isfinite(period) and period > 0):
        raise _invalid(
            f"Simulation.SimPeriod must be > 0, got {period_value!r}",
            hint="Set it with vissim_configure_simulation(period_s=...).",
        )
    outside = [point for point in request.pause_at if not 0 < point < period]
    if outside:
        raise _invalid(
            f"pause_at values must lie strictly between 0 and the period ({_num(period)} s): {outside}"
        )
    warnings: list[str] = []
    simsec = float(simulation.AttValue("SimSec") or 0.0)
    if simsec > 0:
        simulation.Stop()
        warnings.append(f"a simulation run was in progress at {_num(simsec)} s; it was stopped first")
    seed = request.seed
    if seed is None and request.runs > 1:
        seed = _read_seed(simulation, settings.seed_attr)
        if seed is None:
            raise _invalid(
                "seed required for runs>1",
                hint=(
                    f"Pass seed (and seed_increment): replications with the same seed give identical results "
                    f"(the current {settings.seed_attr} could not be read)."
                ),
            )
        warnings.append(f"seed not given: the current {settings.seed_attr} {seed} is used for run 1")
    seeds = compute_seeds(seed, request.runs, request.seed_increment)
    if seeds[0] is not None:
        simulation.SetAttValue(settings.seed_attr, seeds[0])
    if request.runs > 1:
        warnings.append(MULTI_RUN_WARNING)
    job = SimulationJob(
        job_id=new_job_id(),
        generation=ctx.generation,
        period=period,
        runs=request.runs,
        chunk=request.chunk,
        seeds=seeds,
        pause_at=request.pause_at,
        seed_attr=settings.seed_attr,
        warnings=warnings,
    )
    registry.add(job)
    log.info(
        "simulation job %s started: %d run(s) of %s s, chunk %d s, seeds %s, pause_at %s",
        job.id,
        job.runs,
        _num(period),
        job.chunk,
        seeds,
        job.pause_at,
    )
    return {**job.snapshot(), "chunk": job.chunk}
