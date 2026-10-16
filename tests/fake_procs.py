"""FakeProcs: an in-memory stand-in for ``vissim_mcp.winproc`` (process table, kills, job objects, windows).

In-process service and tool tests use it instead of the real module, so they never list, kill or job a real
process. The decision rules it shares with production (``new_processes``, ``choose_new_pid``,
``cmdline_is_com_launched``, ``looks_like_license_dialog``) are imported from winproc, so the tests exercise
the real rules.

``FakeProcess`` also has the ``subprocess.Popen`` methods ``FakeVissim`` uses for its stand-in process
(``poll``, ``terminate``, ``kill``, ``wait``): attached as ``FakeVissim.process``, ``Exit()`` ends it like a
real Vissim leaving, and ``crash()`` kills it.

``FakeProcs.events`` records every kill/terminate/job call in order (``("kill", pid)``, ...), so tests can
assert the order of steps (e.g. the shutdown kill before the worker stop).
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from vissim_mcp import winproc
from vissim_mcp.winproc import ProcInfo

VISSIM_EXE = r"C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe"
LICENSE_DIALOG_TITLE = "Управление лицензиями - PTV Vissim"
MAIN_WINDOW_TITLE = "PTV Vissim 2026 - (no network)"


class FakeProcess:
    """One entry of the fake process table."""

    def __init__(
        self,
        procs: FakeProcs,
        pid: int,
        name: str,
        create_time: float,
        cmdline: Sequence[str],
        titles: Sequence[str],
        *,
        survives_terminate: bool = False,
        survives_kill: bool = False,
        cmdline_readable: bool = True,
        refuses_end: bool = False,
    ) -> None:
        self._procs = procs
        self.pid = pid
        self.name = name
        self.create_time = create_time
        self.cmdline = tuple(cmdline)
        self.titles = list(titles)
        self.survives_terminate = survives_terminate
        self.survives_kill = survives_kill
        self.cmdline_readable = cmdline_readable  # False: reading it fails (AccessDenied): cmdline None
        self.refuses_end = refuses_end  # kill/terminate are refused (AccessDenied, e.g. an elevated Vissim)
        self.alive = True
        self.ended_by: str | None = None
        self.on_end: list[Callable[[str], None]] = []

    @property
    def info(self) -> ProcInfo:
        cmdline = self.cmdline if self.cmdline_readable else None
        return ProcInfo(self.pid, self.name, self.create_time, cmdline)

    def end(self, how: str) -> None:
        """The process ends (``how`` = exit, terminate, kill, crash); runs the ``on_end`` callbacks once."""
        with self._procs._lock:
            if not self.alive:
                return
            self.alive = False
            self.ended_by = how
            callbacks = list(self.on_end)
        for callback in callbacks:
            callback(how)

    # --- subprocess.Popen-like API for FakeVissim.process

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:  # FakeVissim.Exit(): Vissim leaves by itself
        self.end("exit")

    def kill(self) -> None:  # FakeVissim.crash()
        self.end("crash")

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.alive:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"fake process {self.pid} is still running")
            time.sleep(0.01)
        return 0

    def __repr__(self) -> str:
        state = "alive" if self.alive else f"ended by {self.ended_by}"
        return f"<FakeProcess {self.pid} {self.name} {state}>"


class FakeJobObject:
    def __init__(self, procs: FakeProcs) -> None:
        self._procs = procs
        self.assigned: list[tuple[int, float | None]] = []

    def assign(self, pid: int, create_time: float | None = None) -> None:
        procs = self._procs
        procs._event("assign", pid)
        error = procs.assign_error
        if error is not None:
            raise error
        process = procs.table.get(pid)
        if process is None or not process.alive:
            raise OSError(87, f"OpenProcess(PID {pid}) failed: The parameter is incorrect")
        if create_time is not None and not winproc.same_create_time(process.create_time, create_time):
            raise ProcessLookupError(3, f"PID {pid} belongs to another process now (PID reused)")
        self.assigned.append((pid, create_time))

    def contains(self, pid: int) -> bool:
        return any(entry[0] == pid for entry in self.assigned)


class _JobObjectFactory:
    def __init__(self, procs: FakeProcs) -> None:
        self._procs = procs

    def create(self) -> FakeJobObject | None:
        procs = self._procs
        procs.job_create_calls += 1
        if procs.job_create_fails:
            return None
        job = FakeJobObject(procs)
        procs.jobs.append(job)
        return job


class FakeProcs:
    """The winproc API over an in-memory process table (thread-safe)."""

    PID_NOT_DETECTED_WARNING = winproc.PID_NOT_DETECTED_WARNING

    def __init__(self, *, first_pid: int = 4000) -> None:
        self._lock = threading.RLock()
        self._pids = itertools.count(first_pid, 4)
        self._clock = itertools.count(1)
        self.table: dict[int, FakeProcess] = {}
        self.events: list[tuple[str, int | None]] = []
        self.kill_calls: list[tuple[int | None, float | None]] = []
        self.terminate_calls: list[tuple[int | None, float | None]] = []
        self.killed: list[int] = []
        self.terminated: list[int] = []
        self.jobs: list[FakeJobObject] = []
        self.job_create_calls = 0
        self.job_create_fails = False
        self.assign_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.list_calls = 0
        self.JobObject = _JobObjectFactory(self)

    # --- test API

    def spawn(
        self,
        name: str = "Vissim260.exe",
        *,
        com: bool = True,
        titles: Sequence[str] = (MAIN_WINDOW_TITLE,),
        pid: int | None = None,
        create_time: float | None = None,
        survives_terminate: bool = False,
        survives_kill: bool = False,
        cmdline_readable: bool = True,
        refuses_end: bool = False,
    ) -> FakeProcess:
        """Add a running process; ``com=True`` gives it the ``-Embedding`` command line of a COM launch.

        Create times grow with every spawn (the newest process has the largest one). Passing the ``pid`` of
        an ended process simulates PID reuse. ``cmdline_readable=False``: its command line cannot be read
        (access denied); ``refuses_end``: kill and terminate fail (access denied) and it keeps running.
        """
        with self._lock:
            new_pid = next(self._pids) if pid is None else pid
            when = float(1_790_000_000 + next(self._clock)) if create_time is None else create_time
            exe = VISSIM_EXE if name.lower().startswith("vissim") else rf"C:\Tools\{name}"
            cmdline = [exe, "-Embedding"] if com else [exe]
            process = FakeProcess(
                self,
                new_pid,
                name,
                when,
                cmdline,
                titles,
                survives_terminate=survives_terminate,
                survives_kill=survives_kill,
                cmdline_readable=cmdline_readable,
                refuses_end=refuses_end,
            )
            self.table[new_pid] = process
            return process

    def alive_pids(self) -> set[int]:
        with self._lock:
            return {pid for pid, process in self.table.items() if process.alive}

    def all_jobs_contain(self, pid: int) -> bool:
        return any(job.contains(pid) for job in self.jobs)

    def _event(self, name: str, pid: int | None) -> None:
        with self._lock:
            self.events.append((name, pid))

    # --- winproc API

    def vissim_processes(self, pattern: re.Pattern[str]) -> dict[int, ProcInfo]:
        with self._lock:
            self.list_calls += 1
            if self.list_error is not None:
                raise self.list_error
            return {
                pid: process.info
                for pid, process in self.table.items()
                if process.alive and pattern.fullmatch(process.name)
            }

    def detect_new_pid(
        self,
        before: Mapping[int, ProcInfo] | Collection[int],
        pattern: re.Pattern[str],
        timeout_s: float,
        poll_s: float = 0.25,
    ) -> tuple[int | None, float | None, list[str]]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            new = winproc.new_processes(self.vissim_processes(pattern), before)
            if new:
                return winproc.choose_new_pid(new)
            if time.monotonic() >= deadline:
                return None, None, [winproc.PID_NOT_DETECTED_WARNING]
            time.sleep(0.01)

    def is_com_launched(self, pid: int) -> bool:
        with self._lock:
            process = self.table.get(pid)
            return process is not None and winproc.cmdline_is_com_launched(process.info.cmdline)

    def window_titles(self, pid: int | None) -> list[str]:
        with self._lock:
            process = self.table.get(pid) if pid is not None else None
            return list(process.titles) if process is not None and process.alive else []

    @staticmethod
    def looks_like_license_dialog(titles: Any) -> bool:
        return winproc.looks_like_license_dialog(titles)

    def pid_matches(self, pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
        return self._verified(pid, create_time, pattern) is not None

    def kill(self, pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
        with self._lock:
            self.kill_calls.append((pid, create_time))
            self._event("kill", pid)
            process = self._verified(pid, create_time, pattern)
            if process is None or process.refuses_end:
                return False
            self.killed.append(process.pid)
        if not process.survives_kill:
            process.end("kill")
        return True

    def terminate(self, pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
        with self._lock:
            self.terminate_calls.append((pid, create_time))
            self._event("terminate", pid)
            process = self._verified(pid, create_time, pattern)
            if process is None or process.refuses_end:
                return False
            self.terminated.append(process.pid)
        if not process.survives_terminate:
            process.end("terminate")
        return True

    def _verified(
        self, pid: int | None, create_time: float | None, pattern: re.Pattern[str]
    ) -> FakeProcess | None:
        if pid is None or create_time is None:
            return None
        with self._lock:
            process = self.table.get(pid)
            if process is None or not process.alive or not pattern.fullmatch(process.name):
                return None
            if not winproc.same_create_time(process.create_time, create_time):
                return None
            return process
