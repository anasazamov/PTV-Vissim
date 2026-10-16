"""Shared helpers for in-process tests of ``VissimService`` and the life-cycle tools.

``Harness`` bundles a service wired to ``FakeProcs`` (instead of ``winproc``) and a ``BackendFactory`` whose
every ``Dispatch`` "starts" a COM-launched fake Vissim process. Timings of the service are shortened so that
the close/restart escalation steps take milliseconds. ``StubJob`` is the minimal simulation job of the M2
tests (``jobs.py`` arrives in M4): it implements the service's ``SimulationJobHook``.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fake_backend import BackendFactory
from fake_procs import FakeProcess, FakeProcs
from fake_vissim import FakeVissim
from vissim_mcp.config import Settings, load_settings
from vissim_mcp.service import VissimService

WAIT_S = 5.0


@dataclass
class Harness:
    service: VissimService
    procs: FakeProcs
    backends: BackendFactory

    @property
    def settings(self) -> Settings:
        return self.service.settings

    @property
    def vissim(self) -> FakeVissim:
        """The FakeVissim of the latest successful Dispatch."""
        vissim = self.backends.vissim
        assert vissim is not None, "no successful Dispatch yet"
        return vissim

    @property
    def process(self) -> FakeProcess:
        """The fake process of the connected Vissim (by the service's PID)."""
        pid = self.service.pid
        assert pid is not None, "the service has no PID"
        return self.procs.table[pid]

    def close(self) -> None:
        self.service.shutdown_fast()
        self.backends.release_all()


def fast_timings(service: VissimService) -> VissimService:
    service.startup_poll_s = 0.01
    service.process_poll_s = 0.01
    service.close_exit_call_timeout_s = 2.0
    service.close_exit_wait_s = 0.5
    service.close_terminate_wait_s = 0.5
    service.close_kill_wait_s = 0.5
    service.restart_kill_wait_s = 0.5
    service.call_failed_grace_s = 0.2
    return service


def make_harness(
    env: Mapping[str, str] | None = None,
    *,
    procs: FakeProcs | None = None,
    on_dispatch: Callable[[Any, str], Any] | None = None,
    spawn_options: dict[str, Any] | None = None,
    **backend_options: Any,
) -> Harness:
    """A harness; ``env`` overrides ``VISSIM_*`` settings, the rest configures the fake backend/Vissim."""
    procs = procs if procs is not None else FakeProcs()
    backends = BackendFactory(procs, spawn_options=spawn_options, on_dispatch=on_dispatch, **backend_options)
    settings = load_settings({**os.environ, **(env or {})})
    service = fast_timings(VissimService(settings, backends, procs=procs))
    return Harness(service, procs, backends)


async def eventually(predicate: Callable[[], Any], timeout: float = WAIT_S, interval: float = 0.005) -> None:
    """Poll ``predicate`` on the event loop until it is truthy (AssertionError after ``timeout``)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


class StubJob:
    """Minimal simulation job (the service's ``SimulationJobHook``): every tick reads ``SimSec`` through COM.

    ``block`` (an Event) makes a tick wait until it is set - a long simulation chunk; ``raises`` is raised by
    every tick after that (a tick that fails to record its own error). ``tick_calls`` counts every tick,
    ``ticks`` the successful ones; ``failures`` records the service's ``fail()`` calls.
    """

    def __init__(
        self,
        job_id: str = "job-1",
        generation: int = 1,
        state: str = "running",
        *,
        block: threading.Event | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.id = job_id
        self.generation = generation
        self.state = state
        self.block = block
        self.raises = raises
        self.ticks = 0
        self.tick_calls = 0
        self.threads: set[str] = set()
        self.abort_reasons: list[str] = []
        self.failures: list[dict[str, Any]] = []

    def tick(self, vissim: Any, settings: Settings) -> None:
        self.tick_calls += 1
        self.threads.add(threading.current_thread().name)
        vissim.Simulation.AttValue("SimSec")
        if self.block is not None:
            self.block.wait(WAIT_S)
        if self.raises is not None:
            raise self.raises
        self.ticks += 1

    def abort(self, reason: str) -> None:
        self.abort_reasons.append(reason)
        self.state = "aborted"

    def fail(self, error: dict[str, Any]) -> None:
        self.failures.append(error)
        self.state = "error"

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "state": self.state,
            "run": 1,
            "runs": 1,
            "simsec": 0.0,
            "percent": 0.0,
            "ticks": self.ticks,
        }


class StubRegistry:
    def __init__(self, current: Any = None) -> None:
        self.current = current
