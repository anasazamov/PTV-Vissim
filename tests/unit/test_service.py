"""service.py (M1 part): health() without COM and the bounded shutdown sequence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from vissim_mcp import __version__
from vissim_mcp.config import Settings, load_settings
from vissim_mcp.service import SHUTDOWN_WORKER_JOIN_S, VissimService

HEALTH_KEYS = {
    "connected",
    "pid",
    "vissim_alive",
    "generation",
    "busy_seconds",
    "current_task",
    "queue_depth",
    "job",
    "loaded_file",
    "job_object",
    "settings",
    "version",
    "mcp_version",
    "python",
    "uptime_s",
    "last_error",
    "warnings",
}


def test_health_without_connection(service: VissimService, settings: Settings) -> None:
    health = service.health()
    assert HEALTH_KEYS <= set(health)
    assert health["connected"] is False
    assert health["pid"] is None
    assert health["vissim_alive"] is None
    assert health["generation"] == 0
    assert health["busy_seconds"] == 0.0
    assert health["current_task"] is None
    assert health["queue_depth"] == 0
    assert health["job"] is None
    assert health["job_object"] is False
    assert health["shutting_down"] is False
    assert health["settings"] == settings.public_dict()
    assert health["version"] == __version__
    assert "64-bit" in health["python"] or "32-bit" in health["python"]
    assert health["warnings"] == []
    json.dumps(health, allow_nan=False)


def test_health_reads_worker_and_job_through_their_interfaces(settings: Settings) -> None:
    service = VissimService(settings)
    service.worker = SimpleNamespace(busy_seconds=3.26, current_task="LoadNet", queue_depth=2)
    snapshot = {
        "job_id": "j1",
        "state": "running",
        "run": 1,
        "runs": 3,
        "simsec": 120.0,
        "percent": 1.1,
        "x": 1,
    }
    service.jobs = SimpleNamespace(current=SimpleNamespace(snapshot=lambda: snapshot))
    health = service.health()
    assert health["busy_seconds"] == 3.3
    assert health["current_task"] == "LoadNet"
    assert health["queue_depth"] == 2
    assert health["job"] == {
        "job_id": "j1",
        "state": "running",
        "run": 1,
        "runs": 3,
        "simsec": 120.0,
        "percent": 1.1,
    }


def test_vissim_alive_uses_the_identity_check(settings: Settings) -> None:
    seen: list[tuple[Any, ...]] = []

    def pid_matches(pid: int, create_time: float | None, pattern: Any) -> bool:
        seen.append((pid, create_time, pattern.pattern))
        return True

    service = VissimService(settings, procs=SimpleNamespace(pid_matches=pid_matches))
    service.pid, service.pid_create_time = 4242, 1.5
    assert service.health()["vissim_alive"] is True
    assert seen == [(4242, 1.5, settings.exe_pattern.pattern)]

    def broken(*_args: Any) -> bool:
        raise OSError("access denied")

    service.procs = SimpleNamespace(pid_matches=broken)
    assert service.health()["vissim_alive"] is None


def test_health_shows_configuration_warnings(tmp_path: Path) -> None:
    settings = load_settings({"VISSIM_LOG_DIR": str(tmp_path / "l"), "VISSIM_AUDIT_DIR": str(tmp_path / "a")})
    warnings = VissimService(settings).health()["warnings"]
    assert any("every file operation will be denied" in w for w in warnings)


class RecordingWorker:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = fail

    def stop(self) -> None:
        self.calls.append(("stop", None))
        if self.fail:
            raise RuntimeError("worker already dead")

    def join(self, timeout: float) -> None:
        self.calls.append(("join", timeout))


def test_shutdown_fast_is_bounded_and_idempotent(settings: Settings) -> None:
    service = VissimService(settings)
    worker = RecordingWorker()
    service.worker = worker
    started = time.perf_counter()
    service.shutdown_fast()
    assert time.perf_counter() - started < 0.2
    assert worker.calls == [("stop", None), ("join", SHUTDOWN_WORKER_JOIN_S)]
    assert service.shutting_down is True
    service.shutdown_fast()
    assert len(worker.calls) == 2
    assert service.health()["shutting_down"] is True


def test_shutdown_fast_never_raises(settings: Settings) -> None:
    service = VissimService(settings)
    service.worker = RecordingWorker(fail=True)
    service.shutdown_fast()
    assert service.shutting_down is True
