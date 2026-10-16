"""ComWorker: the single COM thread (PLAN 6.2).

Every COM call of the server runs on one daemon thread ``vissim-com``: it initialises COM as a
single-threaded apartment, creates the Vissim root object and executes the functions submitted by the service.
A submitted function receives the :class:`WorkerContext` and returns plain data; COM objects never leave the
thread (an accidentally returned COM object is turned into an ``INTERNAL`` error, and exception tracebacks -
whose frames could hold COM references - are dropped before an exception crosses to another thread).

Loop (PLAN 6.2)::

    loop:
        item = queue.get(timeout=0.05)
        if item: run it -> result/exception to its Future; continue      # drain the queue first
        backend.pump_messages()
        background_step(ctx)                                             # e.g. one simulation chunk

so all queued calls run between two background steps (fairness). Futures cancelled while queued are skipped.
The thread never dies from an exception: task errors go to the task's Future, background errors to
``on_background_error``.

``stop()`` ends the loop after the current item; ``abandon()`` additionally marks the worker as superseded by
a newer generation (``vissim_restart``) - its late results are ignored by the service. Either way queued
futures fail at once with ``NOT_CONNECTED`` (``SHUTTING_DOWN`` during server shutdown); when the current call
has returned, the COM root is dropped and COM is uninitialised on the worker thread. ``submit()`` on a stopped
worker returns an already failed Future.

This module never imports ``pythoncom`` / ``win32com`` (PLAN 4.3) - COM is reached only through the backend.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, InvalidStateError
from typing import Any

from .errors import ErrorCode, VissimMcpError, is_com_error

log = logging.getLogger(__name__)

THREAD_NAME = "vissim-com"
#: ``queue.get`` timeout of the loop (PLAN 6.2).
POLL_S = 0.05
DEFAULT_STOP_MESSAGE = "the Vissim COM worker has stopped"
ABANDON_MESSAGE = "Vissim was restarted; this COM call was discarded"

#: Upper bound of values inspected when checking a task result for leaked COM objects.
MAX_SCAN_NODES = 200_000

_WAKE = object()  # queued by stop()/abandon() so that a waiting get() returns at once
_PLAIN_TYPES = (str, int, float, bool, bytes)

TaskFn = Callable[["WorkerContext"], Any]


class WorkerContext:
    """State that lives on the worker thread. ``vissim`` (the COM root) must only be used there."""

    def __init__(self, worker: ComWorker, backend: Any, generation: int) -> None:
        self._worker = worker
        self.backend = backend
        self.generation = generation
        self.vissim: Any = None

    @property
    def stopping(self) -> bool:
        """True once the worker was asked to stop or was abandoned (long tasks may finish early)."""
        return self._worker.stopping

    @property
    def abandoned(self) -> bool:
        """True when a newer worker generation replaced this one."""
        return self._worker.abandoned

    @contextlib.contextmanager
    def task(self, name: str, *, background: bool = False) -> Iterator[None]:
        """Mark the worker busy with ``name`` (health shows it) - used by background steps doing real work."""
        self._worker._set_busy(name, background)
        try:
            yield
        finally:
            self._worker._clear_busy()


class ComWorker:
    """The COM thread of one connection generation."""

    def __init__(
        self,
        backend: Any,
        generation: int,
        background_step: Callable[[WorkerContext], None] | None = None,
        on_background_error: Callable[[BaseException], None] | None = None,
        *,
        thread_name: str = THREAD_NAME,
    ) -> None:
        self.backend = backend
        self.generation = int(generation)
        self._background_step = background_step
        self._on_background_error = on_background_error
        self._queue: queue.Queue[Any] = queue.Queue()
        self._queued = 0  # real items in the queue (without wake-up sentinels); guarded by _lock
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._started = False
        self._closed = False
        self._abandoned = False
        self._fast_exit = False
        self._stop_code = ErrorCode.NOT_CONNECTED
        self._stop_message = DEFAULT_STOP_MESSAGE
        self._init_error: BaseException | None = None
        self._busy_since: float | None = None
        self._current_task: str | None = None
        self._busy_background = False
        self._pump_failed = False
        self._context = WorkerContext(self, backend, self.generation)
        self._thread = threading.Thread(target=self._run, name=thread_name, daemon=True)

    # --- control (any thread)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread.start()

    def submit(self, fn: TaskFn, task_name: str) -> Future[Any]:
        """Queue ``fn(ctx)`` for the worker thread; the Future gets its result or exception."""
        future: Future[Any] = Future()
        with self._lock:
            if self._closed or self._stop_event.is_set():
                future.set_exception(self._closed_error())
                return future
            self._queue.put((fn, future, str(task_name)))
            self._queued += 1
        return future

    def stop(
        self, code: ErrorCode | str = ErrorCode.NOT_CONNECTED, message: str = DEFAULT_STOP_MESSAGE
    ) -> None:
        """End the loop after the current item (non-blocking).

        Queued futures fail with ``code`` at once - they could only run after the current call, which may be
        hung for good - and so does every later ``submit``. ``SHUTTING_DOWN`` selects the fast exit path (no
        garbage collection, no ``CoUninitialize``): the process is about to exit and must not wait for COM.
        """
        with self._lock:
            if not self._stop_event.is_set():
                self._stop_code = ErrorCode(code)
                self._stop_message = str(message)
                self._fast_exit = self._stop_code is ErrorCode.SHUTTING_DOWN
                self._stop_event.set()
            pending = self._drain_locked()
        self._fail_pending(pending)
        self._queue.put(_WAKE)

    def abandon(self, message: str = ABANDON_MESSAGE) -> None:
        """Mark this generation as superseded and stop it: queued futures fail with ``NOT_CONNECTED``
        (``message``) at once; when the current call returns, COM references are dropped and the thread
        exits (a late result still reaches that call's future - the service ignores it)."""
        with self._lock:
            self._abandoned = True
        self.stop(ErrorCode.NOT_CONNECTED, message)

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the thread; True when it has finished (or never started)."""
        if not self._started:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # --- state (read without locks; plain attribute reads are atomic under the GIL)

    @property
    def busy_since(self) -> float | None:
        """``time.monotonic()`` when the current task started, or None when idle."""
        return self._busy_since

    @property
    def busy_seconds(self) -> float:
        since = self._busy_since
        return 0.0 if since is None else max(0.0, time.monotonic() - since)

    @property
    def current_task(self) -> str | None:
        return self._current_task

    @property
    def busy_background(self) -> bool:
        """True while a background step (a simulation chunk) is running."""
        return self._busy_background and self._busy_since is not None

    @property
    def queue_depth(self) -> int:
        """Submitted calls waiting for the worker (cancelled ones count until the worker skips them)."""
        return self._queued

    @property
    def alive(self) -> bool:
        """True while the thread runs and accepts work."""
        return self._thread.is_alive() and not self._closed and not self._stop_event.is_set()

    @property
    def stopping(self) -> bool:
        return self._stop_event.is_set()

    @property
    def abandoned(self) -> bool:
        return self._abandoned

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    # --- the thread

    def _run(self) -> None:
        try:
            self.backend.co_initialize()
        except BaseException as exc:
            self._init_error = exc
            log.error(
                "COM initialisation failed on the worker thread (generation %d): %s", self.generation, exc
            )
            self._finish(initialized=False)
            return
        log.info("COM worker started (generation %d, backend %s)", self.generation, self._backend_name())
        try:
            self._loop()
        finally:
            self._finish(initialized=True)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                try:
                    item = self._queue.get(timeout=POLL_S)
                except queue.Empty:
                    item = None
                if item is _WAKE:
                    continue
                if item is not None:
                    with self._lock:
                        self._queued = max(0, self._queued - 1)
                    self._execute(item)
                    continue  # drain the queue before the next background step (PLAN 6.2)
                self._pump()
                if self._background_step is not None and not self._stop_event.is_set():
                    self._run_background()
            except BaseException:  # a bug in the loop itself must not kill the thread
                log.exception("unexpected error in the COM worker loop (generation %d)", self.generation)
                time.sleep(POLL_S)

    def _execute(self, item: tuple[TaskFn, Future[Any], str]) -> None:
        fn, future, task_name = item
        if not future.set_running_or_notify_cancel():
            return  # cancelled while queued (the caller timed out or went away)
        self._set_busy(task_name, False)
        result: Any = None
        error: BaseException | None = None
        try:
            result = fn(self._context)
            if _contains_com_object(result):
                result = None
                raise VissimMcpError(
                    ErrorCode.INTERNAL,
                    f"task {task_name!r} returned a COM object; tasks must return plain data",
                )
        except Exception as exc:
            error = exc
        except BaseException as exc:  # SystemExit, KeyboardInterrupt, ... must not leave the worker
            error = VissimMcpError(ErrorCode.INTERNAL, f"task {task_name!r} raised {type(exc).__name__}")
        finally:
            self._clear_busy()
        if error is not None:
            if not isinstance(error, VissimMcpError) and not is_com_error(error):
                log.error("COM task %r failed with an unexpected error", task_name, exc_info=error)
            _detach_tracebacks(error)
            _resolve(future, exception=error)
        else:
            _resolve(future, result=result)

    def _pump(self) -> None:
        try:
            self.backend.pump_messages()
        except Exception as exc:
            if not self._pump_failed:
                self._pump_failed = True
                log.warning("pumping COM messages failed (logged once): %s", exc)

    def _run_background(self) -> None:
        step = self._background_step
        if step is None:
            return
        try:
            step(self._context)
        except Exception as exc:
            handler = self._on_background_error
            if handler is None:
                log.exception("background step failed (generation %d)", self.generation)
            else:
                try:
                    handler(exc)
                except Exception:
                    log.exception("the background error handler failed")
        finally:
            self._clear_busy()

    def _drain_locked(self) -> list[Any]:
        """Remove every queued item (caller holds ``self._lock``)."""
        pending: list[Any] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except queue.Empty:
                self._queued = 0
                return pending

    def _fail_pending(self, pending: list[Any]) -> None:
        for item in pending:
            if item is _WAKE:
                continue
            _fn, future, _name = item
            if future.set_running_or_notify_cancel():
                _resolve(future, exception=self._closed_error())

    def _finish(self, *, initialized: bool) -> None:
        with self._lock:
            self._closed = True
            pending = self._drain_locked()
        self._fail_pending(pending)
        self._context.vissim = None
        if initialized and not self._fast_exit:
            try:
                gc.collect()  # release COM proxies held by garbage before CoUninitialize
            except Exception:
                pass
            try:
                self.backend.co_uninitialize()
            except BaseException:
                log.debug("CoUninitialize failed on the worker thread", exc_info=True)
        log.info(
            "COM worker stopped (generation %d%s)", self.generation, ", abandoned" if self._abandoned else ""
        )

    # --- helpers

    def _closed_error(self) -> VissimMcpError:
        init_error = self._init_error
        if isinstance(init_error, VissimMcpError):
            return VissimMcpError(
                init_error.code, init_error.message, init_error.hint or "", init_error.details
            )
        if init_error is not None:
            return VissimMcpError(
                ErrorCode.INTERNAL, f"COM initialisation failed: {type(init_error).__name__}: {init_error}"
            )
        return VissimMcpError(self._stop_code, self._stop_message)

    def _set_busy(self, name: str, background: bool) -> None:
        self._current_task = name
        self._busy_background = background
        self._busy_since = time.monotonic()  # last: a reader that sees busy_since also sees the task name

    def _clear_busy(self) -> None:
        self._busy_since = None
        self._current_task = None
        self._busy_background = False

    def _backend_name(self) -> str:
        return str(getattr(self.backend, "name", type(self.backend).__name__))


def _is_com_object(value: Any) -> bool:
    if value is None or isinstance(value, _PLAIN_TYPES):
        return False
    try:
        return hasattr(value, "_oleobj_")
    except Exception:
        return False


def _contains_com_object(value: Any) -> bool:
    """True when ``value`` is, or (in lists, tuples, sets and dict values) contains, a COM object.

    The scan is bounded (:data:`MAX_SCAN_NODES` values); what lies beyond is assumed to be plain data. Only
    attribute presence is checked, so no COM call is made.
    """
    stack: list[Any] = [value]
    budget = MAX_SCAN_NODES
    while stack and budget > 0:
        budget -= 1
        current = stack.pop()
        if isinstance(current, (list, tuple, set, frozenset)):
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif _is_com_object(current):
            return True
    return False


def _detach_tracebacks(error: BaseException) -> None:
    """Drop the tracebacks of ``error`` and its chain: their frames may reference COM objects, which must be
    released on the worker thread, not wherever the exception ends up."""
    seen: set[int] = set()
    current: BaseException | None = error
    stack: list[BaseException] = []
    while current is not None or stack:
        if current is None:
            current = stack.pop()
        if id(current) in seen:
            current = None
            continue
        seen.add(id(current))
        current.__traceback__ = None
        if current.__context__ is not None:
            stack.append(current.__context__)
        current = current.__cause__


def _resolve(future: Future[Any], *, result: Any = None, exception: BaseException | None = None) -> None:
    try:
        if exception is not None:
            future.set_exception(exception)
        else:
            future.set_result(result)
    except InvalidStateError:
        pass
