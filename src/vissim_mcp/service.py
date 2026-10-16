"""VissimService: owner of the Vissim connection state (PLAN 6.3, 6.4).

This module never touches COM directly. Milestone M1 provides the COM-free part - the state fields,
``health()`` and ``shutdown_fast()``. M2 adds the COM worker (``call``, ``connect``, ``restart``, ``close``)
on top of the same fields; ``health()`` already reads the worker and job objects through the interfaces fixed
in the implementation contract (``ComWorker.busy_seconds/current_task/queue_depth``, ``JobRegistry.current``,
``SimulationJob.snapshot``).
"""

from __future__ import annotations

import logging
import platform
import struct
import threading
import time
from collections.abc import Callable
from importlib import metadata
from typing import Any

from . import __version__
from .config import Settings
from .logging_setup import flush_logging

log = logging.getLogger(__name__)

#: PLAN 6.4: the worker is given at most 100 ms to stop during shutdown (it is a daemon thread).
SHUTDOWN_WORKER_JOIN_S = 0.1
_JOB_SUMMARY_KEYS = ("job_id", "state", "run", "runs", "simsec", "percent")


def _package_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


class VissimService:
    """Connection state and life cycle of the single Vissim instance managed by this server."""

    def __init__(
        self,
        settings: Settings,
        backend_factory: Callable[[], Any] | None = None,
        audit: Any | None = None,
        *,
        procs: Any | None = None,
    ) -> None:
        self.settings = settings
        self.backend_factory = backend_factory
        self.audit = audit
        self.procs = procs

        # Connection state: written by connect/restart/close (M2), read without COM by health().
        self.connected = False
        self.pid: int | None = None
        self.pid_create_time: float | None = None
        self.progid: str | None = None
        self.generation = 0
        self.job_object_active = False
        self.loaded_file: str | None = None
        self.connect_warnings: list[str] = []
        self.shutting_down = False
        self.last_error: dict[str, Any] | None = None
        self.worker: Any | None = None  # ComWorker of the current generation (M2)
        self.jobs: Any | None = None  # JobRegistry (M4)

        self.started_at = time.monotonic()
        self._mcp_version = _package_version("mcp")
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False

    # --- health

    def health(self) -> dict[str, Any]:
        """Server and connection state for ``vissim_health``. Never calls COM and never blocks."""
        worker = self.worker
        busy_seconds = float(getattr(worker, "busy_seconds", 0.0) or 0.0) if worker is not None else 0.0
        return {
            "connected": self.connected,
            "pid": self.pid,
            "vissim_alive": self._vissim_alive(),
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
            "warnings": [*self.settings.warnings, *self.connect_warnings],
        }

    def _vissim_alive(self) -> bool | None:
        """None without a known PID; otherwise whether that exact process (PID + create time) still runs."""
        pid = self.pid
        if pid is None:
            return None
        try:
            if self.procs is not None:
                return bool(self.procs.pid_matches(pid, self.pid_create_time, self.settings.exe_pattern))
            import psutil

            return bool(psutil.pid_exists(pid))
        except Exception:
            log.debug("vissim_alive check failed", exc_info=True)
            return None

    def _job_summary(self) -> dict[str, Any] | None:
        current = getattr(self.jobs, "current", None) if self.jobs is not None else None
        if current is None:
            return None
        try:
            snapshot = current.snapshot()
        except Exception:
            log.debug("job snapshot failed", exc_info=True)
            return None
        return {key: snapshot.get(key) for key in _JOB_SUMMARY_KEYS}

    # --- shutdown

    def shutdown_fast(self) -> None:
        """PLAN 6.4 shutdown sequence (stdin EOF or end of the MCP session): bounded (~200 ms), never raises.

        Order matters: (1) refuse new work, (2) kill Vissim when VISSIM_KILL_ON_EXIT=1 and its PID is known
        (added in M2 together with winproc), (3) stop the worker without waiting more than 100 ms, (4) flush
        logs. Idempotent and thread-safe; the caller exits the process right afterwards.
        """
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self.shutting_down = True
        started = time.monotonic()
        worker = self.worker
        if worker is not None:
            try:
                worker.stop()
                worker.join(SHUTDOWN_WORKER_JOIN_S)
            except Exception:
                log.debug("worker stop failed during shutdown", exc_info=True)
        log.info("shutdown sequence finished in %.0f ms", (time.monotonic() - started) * 1000)
        flush_logging(0.05)
