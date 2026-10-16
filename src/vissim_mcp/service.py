"""VissimService: the Vissim connection and its life cycle (PLAN 6.2 - 6.4).

The service owns the COM worker of the current *generation* and the bookkeeping around the Vissim process
(which PID is ours, the kill-on-close Job Object) and the simulation job hook. It never touches COM itself:
COM work is a function submitted to the worker (``call``).

* ``connect`` - new worker (generation + 1) -> ``Dispatch`` on the worker thread, watched by a start-up
  watchdog (license dialog, ``VISSIM_CONNECT_TIMEOUT_S``, cancellation) -> PID detection -> Job Object.
* ``call`` - submit + watchdog (``TIMEOUT_HUNG``), COM error mapping, disconnect detection, late results of a
  replaced worker are discarded.
* ``restart`` - kill the known PID, abandon the worker, abort the job, connect again; when the new start
  fails, the error still reports what happened to the previous Vissim.
* ``close`` - ``Exit()`` -> wait up to 10 s -> terminate -> kill, identity-checked; cancellation-safe.
* ``health`` - COM-free snapshot; ``shutdown_fast`` - the bounded exit sequence (stdin EOF).

Safety: a process is killed only when its PID is known *and* it still has the recorded name and create time
(``winproc``). Connect clean-up kills only a single new COM-launched process; with several candidates, or when
the process list could not be read before ``Dispatch``, nothing is killed.

Simulation jobs (M4) plug in through ``self.jobs`` (a registry with a ``current`` job, see
:class:`JobRegistryHook`): the worker's background step ticks the current job when it is ``running`` and
belongs to the worker's generation; ``restart``/``close`` abort it. A ``tick`` that raises fails its job once
instead of being retried every poll.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import platform
import struct
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata
from typing import Any, Protocol

from . import __version__, winproc
from .config import Settings, is_valid_progid
from .errors import (
    CO_E_SERVER_EXEC_FAILURE,
    HRESULT_HINTS,
    RESTART_HINT,
    ErrorCode,
    VissimMcpError,
    from_com_error,
    is_call_failed_error,
    is_com_error,
    is_connection_error,
    is_disconnect_error,
)
from .logging_setup import flush_logging
from .worker import ComWorker, WorkerContext

log = logging.getLogger(__name__)

#: PLAN 6.4: the worker is given at most 100 ms to stop during shutdown (it is a daemon thread).
SHUTDOWN_WORKER_JOIN_S = 0.1
DISPATCH_TASK = "Dispatch"
EXIT_TASK = "Exit"
#: Task name prefix of background steps (simulation chunks) shown in health and TIMEOUT_HUNG messages.
CHUNK_TASK_PREFIX = "simulation chunk"
ABORTED_STATE = "aborted"
ERROR_STATE = "error"
# How Exit() went in vissim_close. After a *delivered* Exit() (it returned, or failed with a COM error) the
# service waits up to close_exit_wait_s for the process before it escalates to terminate/kill (PLAN 6.4).
_EXIT_RETURNED = "returned"
_EXIT_GONE = "gone"  # Vissim went away while answering (disconnect / call failed in transit / process ended)
_EXIT_FAILED = "failed"  # another COM error
_EXIT_TIMEOUT = "timeout"  # no answer within close_exit_call_timeout_s (a dialog?) - the process was watched
_EXIT_NOT_RUN = "not_run"  # Exit() was never executed
_EXIT_DELIVERED = frozenset({_EXIT_RETURNED, _EXIT_GONE, _EXIT_FAILED})
_JOB_SUMMARY_KEYS = ("job_id", "state", "run", "runs", "simsec", "percent")
_ACTIVE_JOB_STATES = ("running", "paused")
_LICENSE_HINT = HRESULT_HINTS[CO_E_SERVER_EXEC_FAILURE]
_STARTUP_TIMEOUT_HINT = (
    "Vissim probably shows a dialog (license selection, 'License not found', a crash report). Check the "
    "Vissim window, make the license the default one so that Vissim starts without questions, then call "
    "vissim_connect again (or raise VISSIM_CONNECT_TIMEOUT_S for a slow start)."
)


class SimulationJobHook(Protocol):
    """What the service needs from a simulation job (implemented by ``jobs.SimulationJob`` in M4)."""

    id: str
    generation: int
    state: Any  # ``JobState`` (a str enum): running, paused, finished, stopped, error, aborted

    def tick(self, vissim: Any, settings: Settings) -> None:
        """One chunk on the worker thread (PLAN 6.6). Must not overwrite an ``aborted`` state."""

    def abort(self, reason: str) -> None:
        """Mark the job ``aborted`` (called from the event loop thread; idempotent)."""

    def fail(self, error: dict[str, Any]) -> None:
        """Mark the job ``error`` with ``error`` (``{code, message, hint?, details?}``).

        Optional. Called on the worker thread by the service's safety net when ``tick`` raised instead of
        recording its own error; without this method the job is aborted instead.
        """

    def snapshot(self) -> dict[str, Any]:
        """COM-free status (``job_id, state, run, runs, simsec, percent, ...``)."""


class JobRegistryHook(Protocol):
    """``self.jobs``: the registry of simulation jobs (``jobs.JobRegistry`` in M4)."""

    current: SimulationJobHook | None


@dataclass
class _Discarded:
    """What ``_discard_connection`` dropped."""

    pid: int | None = None
    create_time: float | None = None
    existed: bool = False  # there was a connection (or a lost one) to drop
    killed: bool = False
    may_still_run: bool = False  # the previous Vissim was not proven ended (PID unknown, kill refused, ...)
    aborted_job_id: str | None = None
    warnings: list[str] = field(default_factory=list)


class _ComRootReleased(VissimMcpError):
    """A call reached a worker whose COM root is gone (``ctx.vissim`` is None)."""


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _short(exc: BaseException, limit: int = 300) -> str:
    if isinstance(exc, VissimMcpError):
        text = exc.message
    elif is_com_error(exc):
        text = from_com_error(exc).message
    else:
        text = f"{type(exc).__name__}: {exc}"
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def _wait_for_worker(future: Future[Any], timeout_s: float) -> tuple[bool, Any]:
    """Wait at most ``timeout_s`` for a worker future: ``(True, result)`` or ``(False, None)`` on timeout.

    The task's own exception is raised unchanged (also a ``TimeoutError`` raised *by the task* - it is never
    mistaken for the watchdog). On timeout or cancellation of the caller, a task that is still queued is
    cancelled (the worker skips it); a running COM call cannot be interrupted and its late result is dropped.
    """
    wrapped = asyncio.wrap_future(future)
    try:
        done, _pending = await asyncio.wait({wrapped}, timeout=max(0.0, timeout_s))
    finally:
        if not wrapped.done():
            wrapped.cancel()
    if not done:
        return False, None
    return True, wrapped.result()


def _dispatch_task(progid: str, ctx: WorkerContext) -> None:
    ctx.vissim = ctx.backend.dispatch(progid)


def _exit_task(ctx: WorkerContext) -> bool:
    """``Exit()`` on the COM root; False when there was no root to call (nothing was done)."""
    vissim = ctx.vissim
    if vissim is None:
        return False
    try:
        vissim.Exit()
    finally:
        ctx.vissim = None
    return True


def _with_vissim(fn: Callable[[WorkerContext], Any], ctx: WorkerContext) -> Any:
    """Every ``call()`` task needs the COM root; without it (released by ``Exit()``) fail with NOT_CONNECTED
    instead of an ``AttributeError`` deep inside the task."""
    if ctx.vissim is None:
        raise _ComRootReleased(
            ErrorCode.NOT_CONNECTED, "the Vissim COM object of this connection was already released"
        )
    return fn(ctx)


class VissimService:
    """Connection state and life cycle of the single Vissim instance managed by this server."""

    # Timing (seconds). Class attributes so that tests can shorten them per instance.
    startup_poll_s = 0.25
    close_exit_call_timeout_s = 10.0
    close_exit_wait_s = 10.0
    close_terminate_wait_s = 5.0
    close_kill_wait_s = 5.0
    restart_kill_wait_s = 5.0
    process_poll_s = 0.1
    #: After RPC_S_CALL_FAILED the Vissim process may still be ending: watch it this long before deciding.
    call_failed_grace_s = 1.0

    def __init__(
        self,
        settings: Settings,
        backend_factory: Callable[[], Any] | None = None,
        audit: Any | None = None,
        *,
        procs: Any | None = None,
    ) -> None:
        if backend_factory is None:
            from .backend.real import RealComBackend

            backend_factory = RealComBackend
        self.settings = settings
        self.backend_factory = backend_factory
        self.audit = audit
        self.procs: Any = procs if procs is not None else winproc

        # Connection state: written by connect/restart/close (event loop thread), read without COM by
        # health().
        self.connected = False
        self.connecting = False
        self.pid: int | None = None
        self.pid_create_time: float | None = None
        self.progid: str | None = None
        self.generation = 0
        self.job_object_active = False
        self.loaded_file: str | None = None
        self.connect_warnings: list[str] = []
        self.shutting_down = False
        self.last_error: dict[str, Any] | None = None
        self.worker: ComWorker | Any | None = None  # ComWorker of the current generation
        self.jobs: JobRegistryHook | Any | None = None  # JobRegistry (M4)

        self.started_at = time.monotonic()
        self._mcp_version = _package_version("mcp")
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False
        self._lifecycle_lock = asyncio.Lock()
        self._connect_abort: str | None = None
        self._startup_candidate: winproc.ProcInfo | None = None
        self._cleanup_killed_pid: int | None = None
        self._job_object: Any | None = None
        self._job_attempts: dict[tuple[int, float | None], tuple[bool, str | None]] = {}
        # Background safety net (written on the worker thread): the job whose tick raised is never ticked
        # again; the generation whose background step itself raised was logged with a traceback once.
        self._failed_tick_job: Any | None = None
        self._background_error_generation: int | None = None

    # --- health

    def health(self) -> dict[str, Any]:
        """Server and connection state for ``vissim_health``. Never calls COM and never blocks."""
        worker = self.worker
        busy_seconds = float(getattr(worker, "busy_seconds", 0.0) or 0.0) if worker is not None else 0.0
        alive = self._vissim_alive()
        warnings = [*self.settings.warnings, *self.connect_warnings]
        if self.connected and alive is False:
            warnings.append(f"the Vissim process (PID {self.pid}) is no longer running; call vissim_restart")
        return {
            "connected": self.connected,
            "connecting": self.connecting,
            "pid": self.pid,
            "vissim_alive": alive,
            "progid": self.progid,
            "generation": self.generation,
            "busy_seconds": round(busy_seconds, 1),
            "current_task": getattr(worker, "current_task", None) if worker is not None else None,
            "queue_depth": int(getattr(worker, "queue_depth", 0) or 0) if worker is not None else 0,
            "job": self._job_summary(),
            "loaded_file": self.loaded_file,
            "job_object": self.job_object_active,
            "shutting_down": self.shutting_down,
            "settings": self.settings.public_dict(),
            "version": __version__,
            "mcp_version": self._mcp_version,
            "python": f"{platform.python_version()} ({struct.calcsize('P') * 8}-bit)",
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "last_error": self.last_error,
            "warnings": warnings,
        }

    def _vissim_alive(self) -> bool | None:
        """None without a known PID; otherwise whether that exact process (PID + create time) still runs."""
        pid = self.pid
        if pid is None:
            return None
        try:
            return bool(self.procs.pid_matches(pid, self.pid_create_time, self.settings.exe_pattern))
        except Exception:
            log.debug("vissim_alive check failed", exc_info=True)
            return None

    def _job_summary(self) -> dict[str, Any] | None:
        try:
            current = getattr(self.jobs, "current", None) if self.jobs is not None else None
            if current is None:
                return None
            snapshot = current.snapshot()
        except Exception:
            log.debug("job snapshot failed", exc_info=True)
            return None
        return {key: snapshot.get(key) for key in _JOB_SUMMARY_KEYS}

    # --- COM calls

    async def call(
        self, fn: Callable[[WorkerContext], Any], *, timeout_s: float | None = None, task_name: str
    ) -> Any:
        """Run ``fn(ctx)`` on the COM worker and return its (plain data) result (PLAN 6.3).

        Errors: ``SHUTTING_DOWN``, ``NOT_CONNECTED``, ``TIMEOUT_HUNG`` (the worker stays busy - COM calls
        cannot be interrupted - and ``health`` shows ``busy_seconds``), ``COM_ERROR`` (mapped by HRESULT; a
        disconnect, or a failed call whose Vissim process is gone, marks the connection lost).
        ``VissimMcpError`` raised by ``fn`` passes through unchanged; any other exception of ``fn`` propagates
        (the tool wrapper reports it as ``INTERNAL``).
        """
        self._check_not_shutting_down()
        worker = self.worker
        if not self.connected or worker is None:
            raise self._not_connected_error()
        timeout = self.settings.call_timeout_s if timeout_s is None else float(timeout_s)
        future = worker.submit(functools.partial(_with_vissim, fn), task_name)
        try:
            done, result = await _wait_for_worker(future, timeout)
        except _ComRootReleased as err:
            self._raise_if_superseded(worker, task_name)
            self._mark_disconnected(worker, err, task_name)
            raise
        except VissimMcpError:
            self._raise_if_superseded(worker, task_name)
            raise
        except Exception as exc:
            self._raise_if_superseded(worker, task_name)
            if is_com_error(exc):
                raise await self._com_error(worker, exc, task_name) from None
            raise
        if not done:
            raise self._timeout_error(worker, task_name, timeout)
        self._raise_if_superseded(worker, task_name)
        return result

    def _raise_if_superseded(self, worker: Any, task_name: str) -> None:
        if worker is not self.worker:
            raise VissimMcpError(
                ErrorCode.NOT_CONNECTED,
                f"Vissim was restarted or closed while '{task_name}' was running; its result was discarded",
                hint="Check the state with vissim_health and repeat the call if needed.",
            )

    def _timeout_error(self, worker: Any, task_name: str, timeout: float) -> VissimMcpError:
        busy_task = getattr(worker, "current_task", None)
        busy_seconds = float(getattr(worker, "busy_seconds", 0.0) or 0.0)
        details = {
            "task": task_name,
            "timeout_s": timeout,
            "busy_task": busy_task,
            "busy_seconds": round(busy_seconds, 1),
        }
        if busy_task is None or busy_task == task_name:
            message = (
                f"COM call '{task_name}' did not finish within {timeout:g} s; Vissim may have a modal dialog "
                "open - check its window, or call vissim_restart"
            )
        elif getattr(worker, "busy_background", False) or str(busy_task).startswith(CHUNK_TASK_PREFIX):
            message = (
                f"COM call '{task_name}' could not start within {timeout:g} s because the worker is busy "
                f"with a simulation chunk ('{busy_task}', {busy_seconds:.0f} s); wait with "
                "vissim_wait_simulation, stop the job with vissim_stop_simulation, or call vissim_restart if "
                "Vissim hangs"
            )
        else:
            message = (
                f"COM call '{task_name}' could not start within {timeout:g} s: the worker is still busy "
                f"with '{busy_task}' for {busy_seconds:.0f} s; Vissim may have a modal dialog open - check "
                "its window, or call vissim_restart"
            )
        error = VissimMcpError(ErrorCode.TIMEOUT_HUNG, message, details=details)
        self._record_error(error, task_name)
        log.warning("%s", message)
        return error

    async def _com_error(self, worker: Any, exc: BaseException, task_name: str) -> VissimMcpError:
        error = from_com_error(exc)
        if await self._connection_gone(exc):
            error.hint = RESTART_HINT
            error.details = {**(error.details or {}), "disconnected": True}
            self._mark_disconnected(worker, error, task_name)
        self._record_error(error, task_name)
        return error

    async def _connection_gone(self, exc: BaseException) -> bool:
        """Whether a COM error means the Vissim connection is gone.

        A disconnect HRESULT says so by itself. Otherwise the Vissim process decides (when its PID is known):
        after a call that failed in transit (RPC_S_CALL_FAILED, typical for a server that is exiting or
        crashing) it is watched for ``call_failed_grace_s``. With an unknown PID such an error alone does not
        mark the connection lost - the next call reports a real disconnect.
        """
        if is_disconnect_error(exc):
            return True
        pid, create_time = self.pid, self.pid_create_time
        if pid is not None and is_call_failed_error(exc):
            return await self._wait_gone(pid, create_time, self.call_failed_grace_s)
        return self._vissim_alive() is False

    def _mark_disconnected(self, worker: Any, error: VissimMcpError, task_name: str) -> None:
        if worker is not self.worker or not self.connected:
            return
        self.connected = False
        log.warning(
            "Vissim connection lost (generation %d, PID %s) during '%s': %s",
            self.generation,
            self.pid,
            task_name,
            error.message,
        )

    def _not_connected_error(self) -> VissimMcpError:
        if self.connecting:
            return VissimMcpError(
                ErrorCode.NOT_CONNECTED,
                "Vissim is still starting (vissim_connect is in progress)",
                hint="Wait for vissim_connect to finish; vissim_health shows connecting=true meanwhile.",
            )
        if self.worker is not None or self.pid is not None:
            return VissimMcpError(
                ErrorCode.NOT_CONNECTED,
                "the connection to Vissim was lost (Vissim was closed or crashed)",
                hint=RESTART_HINT,
            )
        return VissimMcpError(ErrorCode.NOT_CONNECTED, "Vissim is not connected")

    def _check_not_shutting_down(self) -> None:
        if self.shutting_down:
            raise VissimMcpError(ErrorCode.SHUTTING_DOWN, "the server is shutting down")

    def _record_error(self, error: VissimMcpError, task: str | None) -> None:
        record: dict[str, Any] = {"code": error.code.value, "message": error.message, "at": _now_iso()}
        if task:
            record["task"] = task
        self.last_error = record

    # --- connect

    async def connect(self, progid: str | None = None) -> dict[str, Any]:
        """Start a new Vissim through COM and connect to it (PLAN 6.4, 7.1 + start-up watchdog).

        Returns ``{pid, progid, job_object, generation, elapsed_s, warnings}``. A connection whose Vissim
        process is verifiably gone counts as lost (not ``ALREADY_CONNECTED``) and is replaced.
        """
        self._check_not_shutting_down()
        if self.connecting:
            raise VissimMcpError(
                ErrorCode.ALREADY_CONNECTED,
                "vissim_connect is already in progress",
                hint="Wait for it to finish (vissim_health shows connecting=true) or call vissim_restart.",
            )
        async with self._lifecycle_lock:
            self._check_not_shutting_down()
            prior: list[str] = []
            if self.connected:
                if self._vissim_alive() is not False:
                    pid_text = self.pid if self.pid is not None else "unknown"
                    raise VissimMcpError(
                        ErrorCode.ALREADY_CONNECTED,
                        f"Vissim is already connected (PID {pid_text}, generation {self.generation})",
                    )
                note = (
                    f"the previous connection was dropped: the Vissim process (PID {self.pid}) was no longer "
                    "running"
                )
                self._mark_disconnected(
                    self.worker, VissimMcpError(ErrorCode.NOT_CONNECTED, note), "vissim_connect"
                )
                prior.append(note)
            return await self._connect_locked(progid, prior_warnings=prior)

    def _normalize_progid(self, progid: str | None) -> str:
        chosen = (progid or "").strip() or self.settings.progid
        if not is_valid_progid(chosen):
            raise VissimMcpError(
                ErrorCode.INVALID_ARGUMENT,
                f"invalid ProgID {chosen!r} (expected e.g. 'Vissim.Vissim' or 'Vissim.Vissim.2026', "
                "or a CLSID)",
                hint="",
            )
        return chosen

    async def _connect_locked(
        self, progid: str | None, *, prior_warnings: list[str] | None = None
    ) -> dict[str, Any]:
        chosen = self._normalize_progid(progid)
        warnings: list[str] = list(prior_warnings or [])
        if self.worker is not None or self.pid is not None:  # a lost connection: clean it up first
            stale = await self._discard_and_wait(
                "the previous Vissim connection was lost", announce_kill=True
            )
            warnings.extend(stale.warnings)

        pattern = self.settings.exe_pattern
        before: dict[int, winproc.ProcInfo] | None
        try:
            before = dict(await asyncio.to_thread(self.procs.vissim_processes, pattern))
        except Exception as exc:
            log.warning("listing processes before Dispatch failed: %s", exc)
            before = None
            warnings.append(
                "could not list the running processes before starting Vissim; PID detection, forced kill and "
                "the job object are disabled for this connection"
            )

        self.generation += 1
        generation = self.generation
        worker = ComWorker(
            self.backend_factory(),
            generation,
            background_step=self._background_step,
            on_background_error=functools.partial(self._on_background_error, generation),
        )
        self.worker = worker
        self.progid = chosen
        self.connecting = True
        self._connect_abort = None
        self._startup_candidate = None
        self._cleanup_killed_pid = None
        self._job_attempts.clear()
        self.loaded_file = None
        self.connect_warnings = []
        started = time.monotonic()
        log.info("connecting to Vissim (ProgID %s, generation %d)", chosen, generation)
        try:
            worker.start()
            future = worker.submit(functools.partial(_dispatch_task, chosen), DISPATCH_TASK)
            wrapped = await self._watch_startup(future, before, started)
            try:
                wrapped.result()
            except VissimMcpError:
                raise
            except Exception as exc:
                if is_com_error(exc):
                    error = from_com_error(exc)
                    error.message = f"starting Vissim failed (Dispatch {chosen!r}): {error.message}"
                    raise error from None
                raise

            pid: int | None = None
            create_time: float | None = None
            if before is not None:
                pid, create_time, pid_warnings = await asyncio.to_thread(
                    self.procs.detect_new_pid, before, pattern, self.settings.pid_detect_s
                )
                warnings.extend(pid_warnings)
            self._check_connect_abort()

            job_object = False
            if pid is not None and self.settings.job_object_enabled:
                job_object, job_warning = self._assign_job_object(pid, create_time)
                if job_warning:
                    warnings.append(job_warning)
            self.pid, self.pid_create_time = pid, create_time
            self.job_object_active = job_object
            self.connect_warnings = list(warnings)
            self.connected = True
            self.last_error = None
        except BaseException as exc:
            self._cleanup_failed_connect(worker, before, exc)
            raise
        finally:
            self.connecting = False
            self._connect_abort = None
            self._startup_candidate = None

        elapsed = time.monotonic() - started
        log.info(
            "connected to Vissim in %.1f s: ProgID %s, PID %s, job object %s, generation %d",
            elapsed,
            chosen,
            pid,
            job_object,
            generation,
        )
        for warning in warnings:
            log.warning("connect: %s", warning)
        return {
            "pid": pid,
            "progid": chosen,
            "job_object": job_object,
            "generation": generation,
            "elapsed_s": round(elapsed, 1),
            "warnings": warnings,
        }

    async def _watch_startup(
        self, future: Future[Any], before: Mapping[int, winproc.ProcInfo] | None, started: float
    ) -> asyncio.Future[Any]:
        """Wait for ``Dispatch``; meanwhile watch new COM-launched Vissim processes (EXTENSION watchdog).

        Raises (after which the caller cleans up): ``COM_ERROR`` when a license dialog shows up,
        ``TIMEOUT_HUNG`` after ``VISSIM_CONNECT_TIMEOUT_S``, ``NOT_CONNECTED`` when restart/close cancel the
        connect, ``SHUTTING_DOWN``.
        """
        wrapped = asyncio.wrap_future(future)
        timeout = self.settings.connect_timeout_s
        deadline = started + timeout
        last_titles: dict[int, list[str]] = {}
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._startup_timeout(timeout, last_titles)
                done, _pending = await asyncio.wait({wrapped}, timeout=min(self.startup_poll_s, remaining))
                if done:
                    return wrapped
                self._check_connect_abort()
                if before is None:
                    continue
                candidates = await self._new_com_processes(before)
                self._startup_candidate = candidates[0] if len(candidates) == 1 else None
                if self._startup_candidate is not None and self.settings.job_object_enabled:
                    # Early: if the server dies during a long start-up, Windows still ends this Vissim.
                    self._assign_job_object(self._startup_candidate.pid, self._startup_candidate.create_time)
                for candidate in candidates:
                    titles = await asyncio.to_thread(self.procs.window_titles, candidate.pid)
                    if titles and titles != last_titles.get(candidate.pid):
                        log.info("Vissim PID %d windows: %s", candidate.pid, titles)
                    last_titles[candidate.pid] = list(titles)
                    if self.procs.looks_like_license_dialog(titles):
                        raise self._license_dialog_error(candidate, titles, candidates)
        finally:
            if not wrapped.done():
                wrapped.cancel()  # a Dispatch that has not started yet is skipped by the worker

    async def _new_com_processes(self, before: Mapping[int, winproc.ProcInfo]) -> list[winproc.ProcInfo]:
        try:
            current = await asyncio.to_thread(self.procs.vissim_processes, self.settings.exe_pattern)
        except Exception:
            log.debug("listing processes during start-up failed", exc_info=True)
            return []
        return self._com_candidates(current, before)

    @staticmethod
    def _com_candidates(
        current: Mapping[int, winproc.ProcInfo], before: Mapping[int, winproc.ProcInfo]
    ) -> list[winproc.ProcInfo]:
        new = winproc.new_processes(current, before)
        return sorted((info for info in new.values() if info.com_launched), key=lambda info: info.pid)

    def _check_connect_abort(self) -> None:
        if self.shutting_down:
            raise VissimMcpError(ErrorCode.SHUTTING_DOWN, "the server is shutting down")
        if self._connect_abort:
            raise VissimMcpError(
                ErrorCode.NOT_CONNECTED, f"vissim_connect was cancelled by {self._connect_abort}", hint=""
            )

    def _request_connect_abort(self, by: str) -> bool:
        """Ask an in-progress connect to give up (it cleans up after itself); True when one was running."""
        if self.connecting:
            self._connect_abort = by
            return True
        return False

    def _license_dialog_error(
        self, candidate: winproc.ProcInfo, titles: list[str], candidates: list[winproc.ProcInfo]
    ) -> VissimMcpError:
        joined = "; ".join(titles)
        killed = False
        if len(candidates) == 1:
            killed = self._kill_process(
                candidate.pid, candidate.create_time, "license dialog during start-up"
            )
        if killed:
            outcome = f"the COM-launched Vissim (PID {candidate.pid}) was closed"
        elif len(candidates) > 1:
            outcome = f"{len(candidates)} new COM-launched Vissim processes exist, so none was killed"
        else:
            outcome = f"the COM-launched Vissim (PID {candidate.pid}) could not be closed - close it manually"
        log.warning("license dialog detected while starting Vissim (PID %d): %s", candidate.pid, joined)
        return VissimMcpError(
            ErrorCode.COM_ERROR,
            f"Vissim showed a license dialog while starting ('{joined}'); {outcome}",
            hint=_LICENSE_HINT,
            details={"window_titles": list(titles), "pid": candidate.pid, "killed": killed},
        )

    def _startup_timeout(self, timeout: float, last_titles: Mapping[int, list[str]]) -> VissimMcpError:
        details: dict[str, Any] = {"timeout_s": timeout}
        titles = {str(pid): value for pid, value in last_titles.items() if value}
        if titles:
            details["window_titles"] = titles
        return VissimMcpError(
            ErrorCode.TIMEOUT_HUNG,
            f"Vissim did not finish starting within {timeout:g} s (VISSIM_CONNECT_TIMEOUT_S)",
            hint=_STARTUP_TIMEOUT_HINT,
            details=details,
        )

    def _cleanup_failed_connect(
        self, worker: ComWorker, before: Mapping[int, winproc.ProcInfo] | None, exc: BaseException
    ) -> None:
        """After any connect failure (also cancellation): kill the single new COM-launched Vissim, abandon the
        worker and reset the state. Synchronous - it must also work in a cancelled task."""
        killed_pid: int | None = None
        if before is not None:
            try:
                current = self.procs.vissim_processes(self.settings.exe_pattern)
                candidates = self._com_candidates(current, before)
            except Exception:
                log.debug("listing processes for the connect clean-up failed", exc_info=True)
                candidates = []
            if len(candidates) == 1:
                candidate = candidates[0]
                if self._kill_process(candidate.pid, candidate.create_time, "vissim_connect failed"):
                    killed_pid = candidate.pid
            elif len(candidates) > 1:
                log.warning(
                    "connect clean-up: %d new COM-launched Vissim processes (%s); none was killed",
                    len(candidates),
                    ", ".join(str(info.pid) for info in candidates),
                )
        if killed_pid is None and isinstance(exc, VissimMcpError) and exc.details:
            if exc.details.get("killed") and isinstance(exc.details.get("pid"), int):
                killed_pid = exc.details["pid"]  # killed already by the license-dialog check
        self._cleanup_killed_pid = killed_pid
        worker.abandon("vissim_connect failed; this COM call was discarded")
        if self.worker is worker:
            self._reset_connection_state()
        if isinstance(exc, VissimMcpError):
            self._record_error(exc, DISPATCH_TASK)
            if killed_pid is not None and exc.details is not None and "killed" not in exc.details:
                exc.details["killed_pid"] = killed_pid
        elif isinstance(exc, asyncio.CancelledError):
            log.warning("vissim_connect was cancelled; the start-up was cleaned up")
        else:
            self._record_error(VissimMcpError(ErrorCode.INTERNAL, _short(exc)), DISPATCH_TASK)
        log.warning("vissim_connect failed (generation %d): %s", worker.generation, _short(exc))

    def _assign_job_object(self, pid: int, create_time: float | None) -> tuple[bool, str | None]:
        """Put ``pid`` into the (lazily created, reused) kill-on-close job; ``(assigned, warning)``. Cached
        per process so that the start-up watchdog and the final assignment do not repeat attempts."""
        key = (pid, create_time)
        if key not in self._job_attempts:
            self._job_attempts[key] = self._try_assign_job_object(pid, create_time)
        return self._job_attempts[key]

    def _try_assign_job_object(self, pid: int, create_time: float | None) -> tuple[bool, str | None]:
        fallback = "Vissim is then killed on exit only when the server exits normally (VISSIM_KILL_ON_EXIT)"
        if self._job_object is None:
            try:
                self._job_object = self.procs.JobObject.create()
            except Exception as exc:
                log.warning("creating the job object failed: %s", exc)
                self._job_object = None
            if self._job_object is None:
                return False, f"could not create the kill-on-close job object; {fallback}"
        try:
            self._job_object.assign(pid, create_time)
        except Exception as exc:
            log.warning("assigning Vissim PID %d to the job object failed: %s", pid, exc)
            return False, (
                f"could not put Vissim (PID {pid}) into the kill-on-close job object ({_short(exc, 160)}); "
                "if Vissim runs as administrator, start it as a normal user. "
                f"{fallback[0].upper()}{fallback[1:]}"
            )
        log.info("Vissim PID %d is in the kill-on-close job object", pid)
        return True, None

    # --- restart / close

    async def restart(self) -> dict[str, Any]:
        """Kill the known Vissim, abandon its worker (generation++), abort the job, connect again (PLAN 6.4).

        Returns the ``connect`` fields plus ``previous_pid``, ``killed`` and ``aborted_job_id``; the warnings
        about the previous Vissim come first (health shows them too). When the new start fails, its error
        carries ``previous_pid``, ``previous_killed``, ``aborted_job_id`` and ``previous_warnings`` in
        ``details``, and says so when the previous Vissim may still be running (it may hold the license).
        """
        self._check_not_shutting_down()
        self._request_connect_abort("vissim_restart")
        async with self._lifecycle_lock:
            self._check_not_shutting_down()
            progid = self.progid or self.settings.progid
            previous = await self._discard_and_wait("Vissim was restarted")
            try:
                result = await self._connect_locked(progid, prior_warnings=previous.warnings)
            except BaseException as exc:
                self._report_failed_restart(previous, exc)
                raise
        log.info(
            "Vissim restarted: previous PID %s (killed %s), new PID %s, generation %d",
            previous.pid,
            previous.killed,
            result.get("pid"),
            self.generation,
        )
        return {
            **result,
            "previous_pid": previous.pid,
            "killed": previous.killed,
            "aborted_job_id": previous.aborted_job_id,
        }

    def _report_failed_restart(self, previous: _Discarded, exc: BaseException) -> None:
        """The new start of a restart failed: keep what happened to the previous Vissim visible (the error
        details, the error message when it may still run, and the health warnings)."""
        if previous.warnings:
            self.connect_warnings = list(previous.warnings)  # health shows them until the next connect
        if not previous.existed or not isinstance(exc, VissimMcpError):
            return
        details = dict(exc.details or {})
        details["previous_pid"] = previous.pid
        details["previous_killed"] = previous.killed
        if previous.aborted_job_id is not None:
            details["aborted_job_id"] = previous.aborted_job_id
        if previous.warnings:
            details["previous_warnings"] = list(previous.warnings)
        exc.details = details
        if previous.may_still_run:
            which = f"PID {previous.pid}" if previous.pid is not None else "PID unknown"
            exc.message = (
                f"{exc.message}; note: the previous Vissim ({which}) was not killed and may still be "
                "running - with a single license seat it blocks the new instance: close it manually, then "
                "call vissim_connect"
            )
        self._record_error(exc, DISPATCH_TASK)

    async def close(self) -> dict[str, Any]:
        """``Exit()`` -> wait <= 10 s for the process -> terminate -> kill (PLAN 6.4, 7.1). No save option.

        Returns ``{closed, method (exit|terminate|kill|null), pid, warnings, aborted_job_id?}``. A connect
        that is still starting Vissim is cancelled (and cleaned up) instead.

        Cancellation-safe: if the call is cancelled half-way (e.g. while ``Exit()`` waits behind a "Save
        changes?" dialog), the connection is left *lost* - not connected, worker abandoned, PID kept - so that
        vissim_close, vissim_connect, vissim_restart or the shutdown can still end that Vissim.
        """
        self._check_not_shutting_down()
        cancelled_connect = self._request_connect_abort("vissim_close")
        async with self._lifecycle_lock:
            self._check_not_shutting_down()
            worker, pid, create_time = self.worker, self.pid, self.pid_create_time
            if worker is None and pid is None:
                if cancelled_connect:
                    return self._cancelled_start_result()
                raise VissimMcpError(
                    ErrorCode.NOT_CONNECTED, "Vissim is not connected; there is nothing to close", hint=""
                )
            try:
                result = await self._close_locked(worker, pid, create_time)
            except BaseException:
                self._abandon_close(worker, pid)
                raise
        log.info("vissim_close: closed %s, method %s, PID %s", result["closed"], result["method"], pid)
        return result

    async def _close_locked(
        self, worker: ComWorker | Any | None, pid: int | None, create_time: float | None
    ) -> dict[str, Any]:
        warnings: list[str] = []
        aborted = self._abort_current_job("Vissim was closed")
        was_alive = pid is not None and self._pid_alive(pid, create_time)
        outcome: str | None = None  # None: Exit() was not attempted (connection lost, worker gone)
        if self.connected and worker is not None and worker.alive and (pid is None or was_alive):
            outcome, exit_warning = await self._call_exit(worker, pid, create_time)
            if exit_warning:
                warnings.append(exit_warning)
        self.connected = False

        method: str | None = None
        closed = False
        if pid is None:
            closed = outcome in (_EXIT_RETURNED, _EXIT_GONE)
            method = "exit" if closed else None
            warnings.append(
                "the Vissim PID is unknown, so the server cannot verify that the process ended"
                + ("" if closed else "; close Vissim manually")
            )
        elif not was_alive:
            closed = True
            warnings.append(f"Vissim (PID {pid}) had already exited")
        else:
            method = await self._end_process(pid, create_time, wait_first=outcome in _EXIT_DELIVERED)
            closed = method is not None
            if not closed:
                warnings.append(f"Vissim (PID {pid}) could not be ended; close it in Task Manager")
        if worker is not None:
            worker.stop(ErrorCode.NOT_CONNECTED, "Vissim was closed")
        self._reset_connection_state()
        result: dict[str, Any] = {"closed": closed, "method": method, "pid": pid, "warnings": warnings}
        if aborted:
            result["aborted_job_id"] = aborted
        return result

    def _abandon_close(self, worker: ComWorker | Any | None, pid: int | None) -> None:
        """vissim_close was cancelled (or failed) half-way: leave a consistent *lost connection*.

        The worker is abandoned (a late ``Exit()`` result is ignored, queued calls fail at once); the PID is
        kept, so the next vissim_close / vissim_connect / vissim_restart or the shutdown can still end this
        Vissim (identity-checked). Synchronous - it runs in a cancelled task.
        """
        self.connected = False
        if worker is not None:
            worker.abandon("vissim_close was cancelled; this COM call was discarded")
        if self.worker is worker:
            self.worker = None
        if pid is not None:
            note = (
                f"vissim_close was cancelled before it finished; Vissim (PID {pid}) may still be running - "
                "call vissim_close again or vissim_restart"
            )
        else:
            note = (
                "vissim_close was cancelled before it finished; Vissim (PID unknown) may still be running - "
                "close it manually if its window is still open"
            )
        self.connect_warnings = [*self.connect_warnings, note]
        log.warning("%s", note)

    def _cancelled_start_result(self) -> dict[str, Any]:
        killed_pid = self._cleanup_killed_pid
        warnings = ["vissim_connect was still starting Vissim; the start was cancelled"]
        if killed_pid is None:
            warnings.append(
                "no starting Vissim process was identified; "
                "if a Vissim window remains open, close it manually"
            )
        log.info("vissim_close cancelled a connect in progress (killed PID %s)", killed_pid)
        return {
            "closed": True,
            "method": "kill" if killed_pid is not None else None,
            "pid": killed_pid,
            "warnings": warnings,
        }

    async def _call_exit(
        self, worker: ComWorker | Any, pid: int | None, create_time: float | None
    ) -> tuple[str, str | None]:
        """Run ``Exit()`` on the worker; returns ``(outcome, warning)`` (the ``_EXIT_*`` values).

        With a known PID the process is watched meanwhile: when it ends, Exit() did its job, whatever its
        reply (a reply lost in transit typically surfaces only later as RPC_S_CALL_FAILED). A disconnect or
        call-failed error is "Vissim went away while answering" (expected); any other COM error still means
        that Exit() was delivered, so the caller waits for the process before it escalates.
        """
        timeout = self.close_exit_call_timeout_s
        wrapped = asyncio.wrap_future(worker.submit(_exit_task, EXIT_TASK))
        deadline = time.monotonic() + timeout
        answered = False
        try:
            while True:
                if wrapped.done():
                    answered = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                poll = remaining if pid is None else min(self.process_poll_s, remaining)
                await asyncio.wait({wrapped}, timeout=poll)
                if not wrapped.done() and pid is not None and not self._pid_alive(pid, create_time):
                    log.info("Vissim PID %d ended while Exit() was pending", pid)
                    return _EXIT_GONE, None
        finally:
            if not wrapped.done():
                wrapped.cancel()  # an Exit() that has not started yet is skipped by the worker
        if not answered:
            ending = "; ending the process" if pid is not None else ""
            return (
                _EXIT_TIMEOUT,
                f"Exit() did not return within {timeout:g} s (Vissim may show a dialog){ending}",
            )
        if wrapped.cancelled():
            return _EXIT_NOT_RUN, "Exit() was cancelled before it ran"
        exc = wrapped.exception()
        if exc is None:
            if wrapped.result():
                return _EXIT_RETURNED, None
            return _EXIT_NOT_RUN, "Exit() was not called: the COM object was already released"
        if is_connection_error(exc):
            return _EXIT_GONE, None  # Vissim went away while answering Exit() - expected
        if is_com_error(exc):
            return _EXIT_FAILED, f"Exit() failed ({_short(exc, 160)})"
        return _EXIT_NOT_RUN, f"Exit() could not be called ({_short(exc, 160)})"

    async def _end_process(self, pid: int, create_time: float | None, *, wait_first: bool) -> str | None:
        """After a delivered ``Exit()`` wait (``close_exit_wait_s``) for the process, then escalate:
        terminate -> kill (identity-checked).

        Returns the method that ended the process, or None when it is still running.
        """
        if wait_first and await self._wait_gone(pid, create_time, self.close_exit_wait_s):
            return "exit"
        if not self._pid_alive(pid, create_time):
            return "exit"  # it ended by itself in the meantime
        pattern = self.settings.exe_pattern
        for how, wait_s in (("terminate", self.close_terminate_wait_s), ("kill", self.close_kill_wait_s)):
            try:
                sent = bool(getattr(self.procs, how)(pid, create_time, pattern))
            except Exception:
                log.warning("%s of Vissim PID %d failed", how, pid, exc_info=True)
                sent = False
            if (sent or not self._pid_alive(pid, create_time)) and await self._wait_gone(
                pid, create_time, wait_s
            ):
                return how
        return None

    def _discard_connection(self, *, kill: bool, reason: str) -> _Discarded:
        """Drop the current connection: kill the known Vissim (identity-checked), abandon the worker, abort
        the job and reset the state. Every case in which the previous Vissim may survive gets a warning."""
        worker, pid, create_time = self.worker, self.pid, self.pid_create_time
        dropped = _Discarded(pid=pid, create_time=create_time, existed=worker is not None or pid is not None)
        if kill and pid is not None:
            dropped.killed = self._kill_process(pid, create_time, reason)
            if not dropped.killed and self._pid_alive(pid, create_time):
                dropped.may_still_run = True
                dropped.warnings.append(
                    f"the previous Vissim (PID {pid}) is still running and could not be killed (does it "
                    "run as administrator?); close it manually - with a single license seat a new instance "
                    "may fail to start"
                )
        elif kill and worker is not None:
            dropped.may_still_run = True
            dropped.warnings.append(
                "the PID of the previous Vissim was not detected, so it was not killed; close it manually "
                "if it is still open"
            )
        if worker is not None:
            worker.abandon(f"{reason}; this COM call was discarded")
        dropped.aborted_job_id = self._abort_current_job(reason)
        self._reset_connection_state()
        return dropped

    async def _discard_and_wait(self, reason: str, *, announce_kill: bool = False) -> _Discarded:
        """``_discard_connection(kill=True)``, then wait (``restart_kill_wait_s``) until a killed Vissim is
        gone; ``announce_kill`` adds a warning that it was killed (connect cleaning up a lost connection)."""
        dropped = self._discard_connection(kill=True, reason=reason)
        if dropped.killed and dropped.pid is not None:
            if announce_kill:
                dropped.warnings.append(f"the previous, unresponsive Vissim (PID {dropped.pid}) was killed")
            if not await self._wait_gone(dropped.pid, dropped.create_time, self.restart_kill_wait_s):
                dropped.may_still_run = True
                dropped.warnings.append(
                    f"the previous Vissim (PID {dropped.pid}) was still running after the kill"
                )
        return dropped

    def _reset_connection_state(self) -> None:
        self.connected = False
        self.worker = None
        self.pid = None
        self.pid_create_time = None
        self.progid = None
        self.job_object_active = False
        self.loaded_file = None
        self.connect_warnings = []

    def _kill_process(self, pid: int, create_time: float | None, why: str) -> bool:
        try:
            killed = bool(self.procs.kill(pid, create_time, self.settings.exe_pattern))
        except Exception:
            log.warning("killing Vissim PID %d failed", pid, exc_info=True)
            return False
        if killed:
            log.warning("killed Vissim PID %d (%s)", pid, why)
        return killed

    def _pid_alive(self, pid: int, create_time: float | None) -> bool:
        try:
            return bool(self.procs.pid_matches(pid, create_time, self.settings.exe_pattern))
        except Exception:
            log.debug("pid_matches failed", exc_info=True)
            return True  # unknown: assume it still runs (callers then try the identity-checked steps)

    async def _wait_gone(self, pid: int, create_time: float | None, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            if not self._pid_alive(pid, create_time):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.process_poll_s)

    # --- simulation job hook (M4)

    def _background_step(self, ctx: WorkerContext) -> None:
        """Worker background step: tick the current job when it runs and belongs to this connection.

        Nothing is ticked while the connection is lost or the COM root is gone. A ``tick`` that raises
        (``tick`` should record its own errors, M4 - this is the safety net) fails its job once and that job
        is never ticked again, so a broken tick cannot turn into an error loop at the worker's poll rate.
        """
        registry = self.jobs
        if registry is None or ctx.stopping or ctx.vissim is None or not self.connected:
            return
        job = getattr(registry, "current", None)
        if (
            job is None
            or job is self._failed_tick_job
            or getattr(job, "generation", None) != ctx.generation
            or job.state != "running"
        ):
            return
        try:
            with ctx.task(f"{CHUNK_TASK_PREFIX} (job {getattr(job, 'id', '?')})", background=True):
                job.tick(ctx.vissim, self.settings)
        except Exception as exc:
            self._tick_failed(ctx.generation, job, exc)

    def _tick_failed(self, generation: int, job: Any, exc: BaseException) -> None:
        """``job.tick`` raised (worker thread): mark that job failed, report it once, detect a lost Vissim.

        A failure of a replaced generation (e.g. a chunk that failed because restart killed its Vissim) is
        only logged: it must not touch the state of the current connection.
        """
        worker = self.worker
        if worker is None or getattr(worker, "generation", None) != generation:
            log.info("background step of replaced generation %d failed: %s", generation, _short(exc))
            return
        self._failed_tick_job = job
        job_id = str(getattr(job, "id", None) or "?")
        if isinstance(exc, VissimMcpError):
            error = VissimMcpError(exc.code, exc.message, exc.hint or "", exc.details)
        elif is_com_error(exc):
            error = from_com_error(exc, context=f"simulation job {job_id}")
        else:
            error = VissimMcpError(
                ErrorCode.INTERNAL, f"simulation job {job_id}: the simulation step failed: {_short(exc)}"
            )
        gone = is_disconnect_error(exc) or self._vissim_alive() is False
        if gone:
            error.hint = RESTART_HINT
            error.details = {**(error.details or {}), "disconnected": True}
        log.error(
            "simulation job %s failed in a background step (generation %d); the job is stopped: %s",
            job_id,
            generation,
            error.message,
            exc_info=exc,
        )
        self._fail_job(job, error)
        self._record_error(error, "background step")
        if gone:
            self._mark_disconnected(worker, error, "background step")

    def _fail_job(self, job: Any, error: VissimMcpError) -> None:
        """``job.fail(error)`` (state ``error``); a job without that hook method is aborted instead."""
        try:
            fail = getattr(job, "fail", None)
            if callable(fail):
                fail(error.to_dict())
                return
            abort = getattr(job, "abort", None)
            if callable(abort):
                abort(error.message)
            else:
                job.state = ERROR_STATE
        except Exception:
            log.exception("marking simulation job %s as failed raised", getattr(job, "id", "?"))

    def _on_background_error(self, generation: int, exc: BaseException) -> None:
        """The worker's safety net: the background step itself raised (a bug outside ``tick``).

        The traceback is logged once per generation (later errors at DEBUG), so a recurring bug cannot flood
        the logs. Errors of a replaced generation are only logged.
        """
        worker = self.worker
        if worker is None or getattr(worker, "generation", None) != generation:
            log.info("background step of replaced generation %d failed: %s", generation, _short(exc))
            return
        if self._background_error_generation != generation:
            self._background_error_generation = generation
            log.error("background step failed (generation %d): %s", generation, _short(exc), exc_info=exc)
        else:
            log.debug("background step failed again (generation %d): %s", generation, _short(exc))
        code = ErrorCode.COM_ERROR if is_com_error(exc) else ErrorCode.INTERNAL
        self.last_error = {
            "code": code.value,
            "message": _short(exc),
            "task": "background step",
            "at": _now_iso(),
        }
        if is_disconnect_error(exc) and self.connected:
            self.connected = False
            log.warning("Vissim connection lost during a background step (generation %d)", generation)

    def _abort_current_job(self, reason: str) -> str | None:
        """Abort the current job when it is running or paused; returns its id."""
        registry = self.jobs
        job = getattr(registry, "current", None) if registry is not None else None
        if job is None or job.state not in _ACTIVE_JOB_STATES:
            return None
        job_id = str(getattr(job, "id", None) or getattr(job, "job_id", "") or "") or None
        try:
            abort = getattr(job, "abort", None)
            if callable(abort):
                abort(reason)
            else:  # a job object without the hook method: set the state directly
                job.state = ABORTED_STATE
        except Exception:
            log.exception("aborting job %s failed", job_id)
        log.warning("simulation job %s aborted: %s", job_id, reason)
        return job_id

    # --- shutdown

    def shutdown_fast(self) -> None:
        """PLAN 6.4 shutdown sequence (stdin EOF or end of the MCP session): bounded (~200 ms), never raises.

        Order matters: (1) refuse new work, (2) kill Vissim when VISSIM_KILL_ON_EXIT=1 and its PID is known
        (also a Vissim that is still starting, when it is the single new COM-launched process), without
        waiting, (3) stop the worker, waiting at most 100 ms, (4) flush logs. Idempotent and thread-safe; the
        caller exits the process right afterwards (the Job Object kills Vissim even if step 2 could not).
        """
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self.shutting_down = True
        started = time.monotonic()
        if self.settings.kill_on_exit:
            targets: list[tuple[int, float | None]] = []
            if self.pid is not None:
                targets.append((self.pid, self.pid_create_time))
            candidate = self._startup_candidate
            if candidate is not None and candidate.pid != self.pid:
                targets.append((candidate.pid, candidate.create_time))
            for pid, create_time in targets:
                try:
                    if self.procs.kill(pid, create_time, self.settings.exe_pattern):
                        log.warning("killed Vissim PID %d on shutdown (VISSIM_KILL_ON_EXIT=1)", pid)
                except Exception:
                    log.debug("kill on shutdown failed", exc_info=True)
        worker = self.worker
        if worker is not None:
            try:
                worker.stop(ErrorCode.SHUTTING_DOWN, "the server is shutting down")
                worker.join(SHUTDOWN_WORKER_JOIN_S)
            except Exception:
                log.debug("worker stop failed during shutdown", exc_info=True)
        log.info("shutdown sequence finished in %.0f ms", (time.monotonic() - started) * 1000)
        flush_logging(0.05)
