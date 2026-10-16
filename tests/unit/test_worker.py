"""worker.py (PLAN 6.2): the single COM thread - loop, fairness, generation/abandon, futures, robustness."""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError
from pathlib import Path
from typing import Any

import pytest

from fake_backend import FakeBackend
from fake_vissim import RPC_E_DISCONNECTED, make_com_error
from vissim_mcp.errors import ErrorCode, VissimMcpError, is_com_error
from vissim_mcp.worker import THREAD_NAME, ComWorker, WorkerContext

SRC = Path(__file__).resolve().parents[2] / "src" / "vissim_mcp"
WAIT_S = 5.0


def dispatch(ctx: WorkerContext) -> str:
    ctx.vissim = ctx.backend.dispatch("Vissim.Vissim")
    return "dispatched"


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


@pytest.fixture
def make_worker() -> Iterator[Callable[..., ComWorker]]:
    """``make_worker(backend=None, generation=1, **kwargs)`` -> a started ComWorker (stopped at teardown)."""
    created: list[ComWorker] = []

    def factory(backend: Any = None, generation: int = 1, **kwargs: Any) -> ComWorker:
        worker = ComWorker(backend if backend is not None else FakeBackend(), generation, **kwargs)
        created.append(worker)
        worker.start()
        return worker

    yield factory
    for worker in created:
        worker.stop()
    for worker in created:
        worker.join(WAIT_S)


class Gate:
    """A task that blocks the worker until ``open()``; ``entered`` is set once it runs."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self._release = threading.Event()

    def __call__(self, ctx: WorkerContext) -> str:
        self.entered.set()
        self._release.wait(WAIT_S)
        return "gate"

    def open(self) -> None:
        self._release.set()


# --- basics


def test_tasks_run_on_one_initialised_sta_thread(make_worker: Callable[..., ComWorker]) -> None:
    backend = FakeBackend()
    worker = make_worker(backend, generation=7)
    assert worker.submit(dispatch, "Dispatch").result(WAIT_S) == "dispatched"

    def probe(ctx: WorkerContext) -> tuple[str, int, int, bool]:
        return threading.current_thread().name, ctx.vissim.Net.Links.Count, ctx.generation, ctx.stopping

    assert worker.submit(probe, "probe").result(WAIT_S) == (THREAD_NAME, 10, 7, False)
    assert backend.last is not None and backend.last.owner_thread_name == THREAD_NAME
    wait_until(lambda: backend.pump_count > 0)  # the idle loop pumps messages

    worker.stop()
    assert worker.join(WAIT_S)
    names = [name for name, _thread in backend.events]
    assert names[0] == "co_initialize" and names[-1] == "co_uninitialize"
    assert {thread for _name, thread in backend.events} == {THREAD_NAME}
    assert worker.thread.daemon is True


def test_com_objects_never_leave_the_thread(make_worker: Callable[..., ComWorker]) -> None:
    worker = make_worker()
    worker.submit(dispatch, "Dispatch").result(WAIT_S)
    leaks: list[Callable[[WorkerContext], Any]] = [
        lambda ctx: ctx.vissim.Net,
        lambda ctx: [1, {"links": ctx.vissim.Net.Links}],
        lambda ctx: (("deep", [ctx.vissim.Simulation]),),
    ]
    for leak in leaks:
        error = worker.submit(leak, "leak").exception(WAIT_S)
        assert isinstance(error, VissimMcpError) and error.code is ErrorCode.INTERNAL
        assert "returned a COM object" in error.message
    plain = worker.submit(lambda ctx: {"n": [ctx.vissim.Net.Links.Count, "x", 1.5, None]}, "plain")
    assert plain.result(WAIT_S) == {"n": [10, "x", 1.5, None]}


def test_task_errors_go_to_the_future_and_the_thread_survives(make_worker: Callable[..., ComWorker]) -> None:
    worker = make_worker()

    def value_error(ctx: WorkerContext) -> None:
        raise ValueError("bad value")

    def com_failure(ctx: WorkerContext) -> None:
        raise make_com_error(RPC_E_DISCONNECTED)

    def exits(ctx: WorkerContext) -> None:
        raise SystemExit(3)

    error = worker.submit(value_error, "value").exception(WAIT_S)
    assert isinstance(error, ValueError) and error.__traceback__ is None  # frames may hold COM refs
    assert is_com_error(worker.submit(com_failure, "com").exception(WAIT_S))
    fatal = worker.submit(exits, "exit").exception(WAIT_S)
    assert isinstance(fatal, VissimMcpError) and fatal.code is ErrorCode.INTERNAL
    assert worker.submit(lambda ctx: "still alive", "after").result(WAIT_S) == "still alive"
    assert worker.alive


def test_background_errors_go_to_the_handler(make_worker: Callable[..., ComWorker]) -> None:
    seen: list[BaseException] = []

    def step(ctx: WorkerContext) -> None:
        raise RuntimeError("chunk failed")

    def handler(exc: BaseException) -> None:
        seen.append(exc)
        raise KeyError("the handler fails too")

    worker = make_worker(background_step=step, on_background_error=handler)
    wait_until(lambda: len(seen) >= 3)
    assert all(isinstance(exc, RuntimeError) for exc in seen)
    assert worker.submit(lambda ctx: 42, "task").result(WAIT_S) == 42
    assert worker.alive


# --- fairness and busy state


def test_queue_is_drained_between_background_steps(make_worker: Callable[..., ComWorker]) -> None:
    events: list[str] = []
    first_step = threading.Event()
    release = threading.Event()

    def step(ctx: WorkerContext) -> None:
        events.append("step")
        if not release.is_set():  # the first step blocks until all tasks are queued
            first_step.set()
            release.wait(WAIT_S)

    worker = make_worker(background_step=step)
    assert first_step.wait(WAIT_S)
    futures = [worker.submit(lambda ctx, i=i: events.append(f"t{i}"), f"t{i}") for i in range(5)]
    assert worker.queue_depth == 5
    release.set()
    for future in futures:
        future.result(WAIT_S)
    wait_until(lambda: events.count("step") >= 3)
    first = events.index("step")
    assert events[first + 1 : first + 6] == ["t0", "t1", "t2", "t3", "t4"]
    assert worker.queue_depth == 0


def test_busy_state_of_tasks_and_background_steps(make_worker: Callable[..., ComWorker]) -> None:
    in_step = threading.Event()
    leave_step = threading.Event()

    def step(ctx: WorkerContext) -> None:
        if leave_step.is_set():
            return
        with ctx.task("simulation chunk (job j1)", background=True):
            in_step.set()
            leave_step.wait(WAIT_S)

    worker = make_worker(background_step=step)
    assert in_step.wait(WAIT_S)
    assert worker.current_task == "simulation chunk (job j1)" and worker.busy_background
    time.sleep(0.05)
    assert worker.busy_seconds >= 0.04
    leave_step.set()
    wait_until(lambda: worker.current_task is None)

    gate = Gate()
    future = worker.submit(gate, "LoadNet")
    assert gate.entered.wait(WAIT_S)
    assert worker.current_task == "LoadNet" and not worker.busy_background
    assert worker.busy_since is not None
    gate.open()
    future.result(WAIT_S)
    wait_until(lambda: worker.busy_since is None)
    assert worker.busy_seconds == 0.0


def test_cancelled_queued_futures_are_skipped(make_worker: Callable[..., ComWorker]) -> None:
    worker = make_worker()
    gate = Gate()
    ran: list[str] = []
    blocker = worker.submit(gate, "blocker")
    assert gate.entered.wait(WAIT_S)
    queued = worker.submit(lambda ctx: ran.append("queued"), "queued")
    assert queued.cancel()
    assert not blocker.cancel()  # a running call cannot be cancelled
    gate.open()
    assert blocker.result(WAIT_S) == "gate"
    assert worker.submit(lambda ctx: "next", "next").result(WAIT_S) == "next"
    assert ran == []
    with pytest.raises(CancelledError):
        queued.result(0)


# --- stop / abandon


def test_abandon_fails_queued_futures_and_releases_com(make_worker: Callable[..., ComWorker]) -> None:
    backend = FakeBackend()
    worker = make_worker(backend, generation=3)
    worker.submit(dispatch, "Dispatch").result(WAIT_S)
    gate = Gate()
    running = worker.submit(gate, "slow")
    assert gate.entered.wait(WAIT_S)
    queued = worker.submit(lambda ctx: "never", "queued")
    contexts: list[WorkerContext] = []
    worker.submit(lambda ctx: contexts.append(ctx), "spy")

    assert worker.queue_depth == 2
    worker.abandon("Vissim was restarted; this COM call was discarded")
    assert worker.abandoned and worker.stopping and not worker.alive
    assert worker.queue_depth == 0  # failed at once; the wake-up sentinel is not a queued call
    later = worker.submit(lambda ctx: "too late", "later")
    assert later.done()
    for future in (queued, later):
        error = future.exception(WAIT_S)
        assert isinstance(error, VissimMcpError) and error.code is ErrorCode.NOT_CONNECTED
        assert error.message == "Vissim was restarted; this COM call was discarded"

    gate.open()  # the hung call returns late: its future still gets the result (the service drops it)
    assert running.result(WAIT_S) == "gate"
    assert worker.join(WAIT_S)
    assert contexts == []  # queued work never ran after abandon()
    assert worker._context.vissim is None and worker._context.abandoned
    assert [name for name, _thread in backend.events][-1] == "co_uninitialize"


def test_stop_is_non_blocking_and_join_is_bounded(make_worker: Callable[..., ComWorker]) -> None:
    worker = make_worker()
    gate = Gate()
    worker.submit(gate, "hung")
    assert gate.entered.wait(WAIT_S)
    started = time.perf_counter()
    worker.stop()
    assert worker.join(0.1) is False
    assert time.perf_counter() - started < 0.5
    gate.open()
    assert worker.join(WAIT_S)


def test_submit_after_stop_fails_at_once(make_worker: Callable[..., ComWorker]) -> None:
    worker = make_worker()
    worker.stop()
    error = worker.submit(lambda ctx: 1, "x").exception(0)
    assert isinstance(error, VissimMcpError) and error.code is ErrorCode.NOT_CONNECTED


def test_shutting_down_stop_skips_com_uninitialize(make_worker: Callable[..., ComWorker]) -> None:
    backend = FakeBackend()
    worker = make_worker(backend)
    worker.submit(dispatch, "Dispatch").result(WAIT_S)
    worker.stop(ErrorCode.SHUTTING_DOWN, "the server is shutting down")
    error = worker.submit(lambda ctx: 1, "x").exception(0)
    assert isinstance(error, VissimMcpError) and error.code is ErrorCode.SHUTTING_DOWN
    assert worker.join(WAIT_S)
    assert "co_uninitialize" not in [name for name, _thread in backend.events]  # the process exits now


def test_failed_com_initialisation_fails_every_future() -> None:
    class BrokenBackend(FakeBackend):
        def co_initialize(self) -> None:
            raise OSError("CoInitializeEx failed")

    worker = ComWorker(BrokenBackend(), 1)
    queued = worker.submit(lambda ctx: 1, "queued before start")
    worker.start()
    assert worker.join(WAIT_S)
    for future in (queued, worker.submit(lambda ctx: 1, "after")):
        error = future.exception(WAIT_S)
        assert isinstance(error, VissimMcpError) and error.code is ErrorCode.INTERNAL
        assert "COM initialisation failed" in error.message


def test_join_of_a_worker_that_never_started() -> None:
    assert ComWorker(FakeBackend(), 1).join(0) is True


# --- PLAN 4.3: no pywin32 in the worker or the service


@pytest.mark.parametrize("module", ["worker.py", "service.py", "backend/base.py", "winproc.py"])
def test_module_does_not_import_pywin32(module: str) -> None:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert not imported & {"pythoncom", "pywintypes", "win32com", "win32api", "win32job"}


def test_real_backend_imports_pywin32_only_inside_methods() -> None:
    tree = ast.parse((SRC / "backend" / "real.py").read_text(encoding="utf-8"))
    top_level: set[str] = set()
    for node in tree.body:  # module level only; imports inside methods run on the COM worker thread
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])
    assert not top_level & {"pythoncom", "pywintypes", "win32com"}
