"""service.py (PLAN 6.2 - 6.4, 10.1 worker/service rows): connect, call watchdog, restart, close, shutdown.

Every test runs against FakeBackend/FakeVissim and FakeProcs (a fake process table instead of winproc), so no
real process is ever listed, killed or put into a job object.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from fake_procs import LICENSE_DIALOG_TITLE, MAIN_WINDOW_TITLE, FakeProcs
from fake_vissim import (
    CO_E_CLASSSTRING,
    CO_E_SERVER_EXEC_FAILURE,
    DISP_E_EXCEPTION,
    E_FAIL,
    E_INVALIDARG,
    RPC_E_DISCONNECTED,
    make_com_error,
)
from service_harness import Harness, StubJob, StubRegistry, eventually
from vissim_mcp.errors import DEFAULT_HINTS, HRESULT_HINTS, RESTART_HINT, ErrorCode, VissimMcpError
from vissim_mcp.winproc import PID_NOT_DETECTED_WARNING
from vissim_mcp.worker import THREAD_NAME, WorkerContext

MakeHarness = Callable[..., Harness]
CHUNK_TASK = "simulation chunk (job job-1)"


def count_links(ctx: WorkerContext) -> int:
    return int(ctx.vissim.Net.Links.Count)


async def raises(code: ErrorCode, awaitable: Any) -> VissimMcpError:
    with pytest.raises(VissimMcpError) as info:
        await awaitable
    assert info.value.code is code, info.value
    return info.value


# --- connect and PID detection (PLAN 6.4)


async def test_connect_detects_the_pid_and_uses_the_job_object(harness: MakeHarness) -> None:
    h = harness()
    user = h.procs.spawn(com=False, titles=["PTV Vissim 2026 - my_model.inpx"])  # the user's own Vissim
    result = await h.service.connect()
    (ours,) = [proc for proc in h.procs.table.values() if proc is not user]
    assert result["pid"] == ours.pid
    assert result["progid"] == "Vissim.Vissim"
    assert result["job_object"] is True
    assert result["generation"] == 1
    assert result["warnings"] == []
    assert h.procs.all_jobs_contain(ours.pid) and not h.procs.all_jobs_contain(user.pid)
    assert h.vissim.owner_thread_name == THREAD_NAME

    health = h.service.health()
    assert health["connected"] is True and health["connecting"] is False
    assert health["pid"] == ours.pid and health["vissim_alive"] is True
    assert health["job_object"] is True and health["generation"] == 1
    assert health["progid"] == "Vissim.Vissim"
    assert health["busy_seconds"] == 0.0 and health["current_task"] is None
    assert health["last_error"] is None and health["warnings"] == []

    # the user's Vissim is never a candidate: not on restart, close or shutdown
    await h.service.restart()
    await h.service.close()
    h.service.shutdown_fast()
    assert user.alive
    assert all(pid != user.pid for pid, _ in h.procs.kill_calls + h.procs.terminate_calls)


async def test_connect_twice_is_already_connected(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    error = await raises(ErrorCode.ALREADY_CONNECTED, h.service.connect())
    assert "vissim_restart" in (error.hint or "")
    assert h.service.generation == 1 and len(h.procs.table) == 1


async def test_connect_with_an_explicit_progid(harness: MakeHarness) -> None:
    h = harness({"VISSIM_PROGID": "Vissim.Vissim.2026"})
    result = await h.service.connect(" Vissim.Vissim-64.26 ")
    assert result["progid"] == "Vissim.Vissim-64.26" == h.vissim.progid
    await h.service.close()
    assert (await h.service.connect())["progid"] == "Vissim.Vissim.2026"


async def test_connect_with_an_invalid_progid(harness: MakeHarness) -> None:
    h = harness()
    await raises(ErrorCode.INVALID_ARGUMENT, h.service.connect("not a progid"))
    assert h.service.generation == 0 and h.backends.backends == []


async def test_unregistered_progid_is_a_com_error(harness: MakeHarness) -> None:
    h = harness()
    error = await raises(ErrorCode.COM_ERROR, h.service.connect("Excel.Application"))
    assert error.details is not None and error.details["hresult"] == CO_E_CLASSSTRING
    assert error.hint == HRESULT_HINTS[CO_E_CLASSSTRING]
    assert error.message.startswith("starting Vissim failed (Dispatch 'Excel.Application'): ")
    assert h.procs.kill_calls == [] and h.procs.table == {}
    health = h.service.health()
    assert health["connected"] is False and health["generation"] == 1 and health["pid"] is None
    assert health["last_error"]["code"] == "COM_ERROR" and health["last_error"]["task"] == "Dispatch"
    assert h.service.worker is None


async def test_no_new_process_means_no_pid_and_nothing_is_killed(harness: MakeHarness) -> None:
    h = harness({"VISSIM_PID_DETECT_S": "0.1"}, on_dispatch=lambda backend, progid: None)
    result = await h.service.connect()
    assert result["pid"] is None and result["job_object"] is False
    assert result["warnings"] == [PID_NOT_DETECTED_WARNING]
    assert h.procs.job_create_calls == 0
    health = h.service.health()
    assert health["pid"] is None and health["vissim_alive"] is None
    assert PID_NOT_DETECTED_WARNING in health["warnings"]

    restarted = await h.service.restart()
    assert restarted["killed"] is False and restarted["previous_pid"] is None
    assert any("was not detected, so it was not killed" in w for w in restarted["warnings"])
    h.service.shutdown_fast()
    assert h.procs.kill_calls == [] and h.procs.terminate_calls == []  # PLAN 6.4 rule 5


async def test_several_new_processes_choose_the_newest_with_a_warning(harness: MakeHarness) -> None:
    procs = FakeProcs()

    def spawn_two(backend: Any, progid: str) -> Any:
        procs.spawn()
        return procs.spawn()

    h = harness(procs=procs, on_dispatch=spawn_two)
    result = await h.service.connect()
    first, second = procs.table.values()
    assert result["pid"] == second.pid
    assert result["warnings"] == [
        f"several new Vissim processes appeared (PIDs {first.pid}, {second.pid}); "
        f"assuming the newest one, PID {second.pid}"
    ]


async def test_a_com_launched_process_is_preferred(harness: MakeHarness) -> None:
    procs = FakeProcs()

    def spawn_mixed(backend: Any, progid: str) -> Any:
        ours = procs.spawn()
        procs.spawn(com=False)  # newer, but started by a user
        return ours

    h = harness(procs=procs, on_dispatch=spawn_mixed)
    result = await h.service.connect()
    com, _user = procs.table.values()
    assert result["pid"] == com.pid
    assert result["warnings"][0].endswith(f"using the COM-launched one, PID {com.pid}")


async def test_job_object_failure_is_only_a_warning(harness: MakeHarness) -> None:
    h = harness()
    h.procs.assign_error = PermissionError(5, "Access is denied")
    result = await h.service.connect()
    assert result["pid"] is not None and result["job_object"] is False
    (warning,) = result["warnings"]
    assert f"could not put Vissim (PID {result['pid']}) into the kill-on-close job object" in warning
    assert "administrator" in warning
    assert h.service.health()["job_object"] is False


async def test_job_object_creation_failure_is_only_a_warning(harness: MakeHarness) -> None:
    h = harness()
    h.procs.job_create_fails = True
    result = await h.service.connect()
    assert result["job_object"] is False
    assert result["warnings"][0].startswith("could not create the kill-on-close job object")


async def test_no_job_object_without_kill_on_exit(harness: MakeHarness) -> None:
    h = harness({"VISSIM_KILL_ON_EXIT": "0"})
    result = await h.service.connect()
    assert result["job_object"] is False and h.procs.job_create_calls == 0
    h.service.shutdown_fast()
    assert h.procs.kill_calls == [] and h.process.alive  # PLAN 6.4: no kill on exit either


async def test_job_object_can_be_disabled_alone(harness: MakeHarness) -> None:
    h = harness({"VISSIM_USE_JOB_OBJECT": "0"})
    result = await h.service.connect()
    assert result["job_object"] is False and h.procs.job_create_calls == 0
    pid = result["pid"]
    h.service.shutdown_fast()
    assert h.procs.killed == [pid]  # kill on exit still applies


async def test_failed_process_listing_disables_detection_and_kills(harness: MakeHarness) -> None:
    h = harness()
    h.procs.list_error = OSError(13, "access denied")
    result = await h.service.connect()
    assert result["pid"] is None and result["job_object"] is False
    assert result["warnings"] == [
        "could not list the running processes before starting Vissim; PID detection, forced kill and the "
        "job object are disabled for this connection"
    ]
    h.service.shutdown_fast()
    assert h.procs.kill_calls == []


async def test_job_object_is_assigned_early_during_a_slow_start(harness: MakeHarness) -> None:
    h = harness(dispatch_delay_s=0.3)
    result = await h.service.connect()
    (job,) = h.procs.jobs
    assert job.assigned == [(result["pid"], h.service.pid_create_time)]  # once: early, then reused
    assert result["job_object"] is True


# --- start-up watchdog (EXTENSION): license dialog, timeout, failed Dispatch


async def test_license_dialog_closes_the_new_vissim(harness: MakeHarness) -> None:
    h = harness(spawn_options={"titles": [LICENSE_DIALOG_TITLE]}, dispatch_delay_s=30)
    user = h.procs.spawn(com=False)
    started = time.monotonic()
    error = await raises(ErrorCode.COM_ERROR, h.service.connect())
    assert time.monotonic() - started < 3
    (ours,) = [proc for proc in h.procs.table.values() if proc is not user]
    assert LICENSE_DIALOG_TITLE in error.message
    assert f"the COM-launched Vissim (PID {ours.pid}) was closed" in error.message
    assert error.hint == HRESULT_HINTS[CO_E_SERVER_EXEC_FAILURE]
    assert error.details == {"window_titles": [LICENSE_DIALOG_TITLE], "pid": ours.pid, "killed": True}
    assert ours.ended_by == "kill" and h.procs.killed == [ours.pid] and user.alive

    health = h.service.health()
    assert health["connected"] is False and health["connecting"] is False and health["pid"] is None
    assert health["last_error"]["code"] == "COM_ERROR"
    assert h.service.worker is None
    # like real COM, the abandoned Dispatch failed when the process died, and the old worker finished
    backend = h.backends.last
    await eventually(lambda: ("co_uninitialize", THREAD_NAME) in backend.events)
    assert backend.instances == []


async def test_license_dialog_with_two_new_processes_kills_nothing(harness: MakeHarness) -> None:
    procs = FakeProcs()

    def spawn_two(backend: Any, progid: str) -> Any:
        first = procs.spawn(titles=[LICENSE_DIALOG_TITLE])
        procs.spawn(titles=[LICENSE_DIALOG_TITLE])
        return first

    h = harness(procs=procs, on_dispatch=spawn_two, dispatch_delay_s=30)
    error = await raises(ErrorCode.COM_ERROR, h.service.connect())
    assert "2 new COM-launched Vissim processes exist, so none was killed" in error.message
    assert error.details is not None and error.details["killed"] is False
    assert procs.kill_calls == [] and all(proc.alive for proc in procs.table.values())


async def test_start_timeout_kills_the_single_new_com_process(harness: MakeHarness) -> None:
    h = harness({"VISSIM_CONNECT_TIMEOUT_S": "0.3"}, dispatch_delay_s=30)
    error = await raises(ErrorCode.TIMEOUT_HUNG, h.service.connect())
    (proc,) = h.procs.table.values()
    assert error.message == "Vissim did not finish starting within 0.3 s (VISSIM_CONNECT_TIMEOUT_S)"
    assert error.hint is not None and "license" in error.hint
    assert error.details == {
        "timeout_s": 0.3,
        "window_titles": {str(proc.pid): [MAIN_WINDOW_TITLE]},
        "killed_pid": proc.pid,
    }
    assert proc.ended_by == "kill"


async def test_failed_dispatch_kills_the_left_over_com_process(harness: MakeHarness) -> None:
    h = harness(dispatch_error=CO_E_SERVER_EXEC_FAILURE)
    error = await raises(ErrorCode.COM_ERROR, h.service.connect())
    (proc,) = h.procs.table.values()
    assert error.message.startswith("starting Vissim failed (Dispatch 'Vissim.Vissim'): ")
    assert error.hint == HRESULT_HINTS[CO_E_SERVER_EXEC_FAILURE]
    assert error.details is not None
    assert error.details["hresult_hex"] == "0x80080005" and error.details["killed_pid"] == proc.pid
    assert proc.ended_by == "kill"


async def test_connect_clean_up_never_kills_a_process_without_com_launch(harness: MakeHarness) -> None:
    procs = FakeProcs()
    h = harness(
        procs=procs,
        on_dispatch=lambda backend, progid: procs.spawn(com=False),
        dispatch_error=CO_E_SERVER_EXEC_FAILURE,
    )
    await raises(ErrorCode.COM_ERROR, h.service.connect())
    assert procs.kill_calls == [] and all(proc.alive for proc in procs.table.values())


async def test_health_and_calls_while_connecting(harness: MakeHarness) -> None:
    h = harness(dispatch_delay_s=30)
    connecting = asyncio.create_task(h.service.connect())
    await eventually(lambda: h.service.health()["current_task"] == "Dispatch")
    health = h.service.health()
    assert health["connecting"] is True and health["connected"] is False and health["generation"] == 1
    assert health["busy_seconds"] >= 0.0
    error = await raises(ErrorCode.NOT_CONNECTED, h.service.call(count_links, task_name="count"))
    assert "still starting" in error.message
    error = await raises(ErrorCode.ALREADY_CONNECTED, h.service.connect())
    assert "already in progress" in error.message
    h.backends.last.release_dispatch()
    result = await connecting
    assert result["pid"] == h.service.pid is not None


# --- call(): watchdog, error mapping, disconnects, cancellation (PLAN 6.3)


async def test_call_runs_on_the_worker_thread(harness: MakeHarness) -> None:
    h = harness()
    await raises(ErrorCode.NOT_CONNECTED, h.service.call(count_links, task_name="count"))
    await h.service.connect()

    def where(ctx: WorkerContext) -> tuple[str, int, int]:
        return threading.current_thread().name, ctx.vissim.Net.Links.Count, ctx.generation

    assert await h.service.call(where, task_name="where") == (THREAD_NAME, 10, 1)


async def test_timeout_is_timeout_hung_and_health_shows_the_busy_worker(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.vissim.hang_next_call(30)
    error = await raises(
        ErrorCode.TIMEOUT_HUNG, h.service.call(count_links, timeout_s=0.2, task_name="count links")
    )
    assert error.message == (
        "COM call 'count links' did not finish within 0.2 s; Vissim may have a modal dialog open - check "
        "its window, or call vissim_restart"
    )
    assert error.hint == DEFAULT_HINTS[ErrorCode.TIMEOUT_HUNG]
    assert "modal dialog" in error.hint and "vissim_restart" in error.hint
    assert error.details is not None and error.details["task"] == "count links"

    health = h.service.health()  # PLAN 10.1: after a timeout health.busy_seconds > 0
    assert health["busy_seconds"] > 0 and health["current_task"] == "count links"
    assert health["connected"] is True
    assert health["last_error"]["code"] == "TIMEOUT_HUNG" and health["last_error"]["task"] == "count links"

    ran: list[int] = []
    error = await raises(
        ErrorCode.TIMEOUT_HUNG, h.service.call(lambda ctx: ran.append(1), timeout_s=0.2, task_name="second")
    )
    assert "could not start within 0.2 s: the worker is still busy with 'count links'" in error.message

    h.vissim.release_hang()
    assert await h.service.call(count_links, task_name="again") == 10
    assert ran == []  # the timed-out call was still queued: it was cancelled, never run late
    await eventually(lambda: h.service.health()["busy_seconds"] == 0.0)


async def test_a_timeout_error_of_the_task_is_not_a_hang(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()

    def times_out(ctx: WorkerContext) -> None:
        raise TimeoutError("socket timed out")

    with pytest.raises(TimeoutError):
        await h.service.call(times_out, timeout_s=5, task_name="t")
    assert h.service.last_error is None


async def test_com_errors_are_mapped_by_hresult(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.vissim.raise_com_error_next(DISP_E_EXCEPTION, "AttValue failed: unknown attribute 'Foo'.", E_INVALIDARG)
    error = await raises(ErrorCode.COM_ERROR, h.service.call(count_links, task_name="count"))
    assert error.message == "AttValue failed: unknown attribute 'Foo'."
    assert error.details == {
        "hresult": DISP_E_EXCEPTION,
        "hresult_hex": "0x80020009",
        "scode": E_INVALIDARG,
        "scode_hex": "0x80070057",
        "source": "Vissim",
    }
    assert error.hint == HRESULT_HINTS[E_INVALIDARG]
    assert h.service.connected is True


async def test_errors_raised_by_the_task_pass_through(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()

    def resolve_failed(ctx: WorkerContext) -> None:
        raise VissimMcpError(ErrorCode.RESOLVE_FAILED, "Segment 'Nope' on 'Net': no such member")

    def buggy(ctx: WorkerContext) -> None:
        raise ZeroDivisionError("division by zero")

    error = await raises(ErrorCode.RESOLVE_FAILED, h.service.call(resolve_failed, task_name="resolve"))
    assert error.message == "Segment 'Nope' on 'Net': no such member"
    with pytest.raises(ZeroDivisionError):  # the tool wrapper reports it as INTERNAL
        await h.service.call(buggy, task_name="bug")
    assert h.service.connected is True


async def test_disconnect_marks_the_connection_lost(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    pid = h.service.pid
    h.vissim.crash()  # Vissim crashes; its process is gone
    error = await raises(ErrorCode.COM_ERROR, h.service.call(count_links, task_name="count"))
    assert error.hint == RESTART_HINT
    assert error.details is not None and error.details["disconnected"] is True
    assert h.service.connected is False

    again = await raises(ErrorCode.NOT_CONNECTED, h.service.call(count_links, task_name="count"))
    assert again.message == "the connection to Vissim was lost (Vissim was closed or crashed)"
    assert again.hint == RESTART_HINT
    health = h.service.health()
    assert health["connected"] is False and health["vissim_alive"] is False and health["pid"] == pid

    result = await h.service.connect()  # connect also recovers: the dead connection is dropped first
    assert result["generation"] == 2 and result["pid"] != pid
    assert h.procs.killed == []  # nothing alive to kill
    assert await h.service.call(count_links, task_name="count") == 10


async def test_a_vanished_vissim_process_counts_as_a_disconnect(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    process = h.process
    process.on_end.clear()  # the COM proxy does not notice yet ...
    process.end("crash")
    h.vissim.raise_com_error_next(E_FAIL)  # ... and reports a generic failure
    error = await raises(ErrorCode.COM_ERROR, h.service.call(count_links, task_name="count"))
    assert error.hint == RESTART_HINT and error.details is not None and error.details["disconnected"]
    assert h.service.connected is False


async def test_a_cancelled_call_that_is_still_queued_never_runs(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.vissim.hang_next_call(30)
    first = asyncio.create_task(h.service.call(count_links, timeout_s=10, task_name="hung"))
    await eventually(lambda: h.service.health()["current_task"] == "hung")
    ran: list[int] = []
    second = asyncio.create_task(h.service.call(lambda ctx: ran.append(1), timeout_s=10, task_name="queued"))
    await eventually(lambda: h.service.health()["queue_depth"] == 1)
    second.cancel()  # e.g. the client cancelled the tool call
    with pytest.raises(asyncio.CancelledError):
        await second
    h.vissim.release_hang()
    assert await first == 10
    assert await h.service.call(lambda ctx: "ok", task_name="after") == "ok"
    assert ran == []


# --- restart (PLAN 6.4)


async def test_restart_kills_vissim_bumps_the_generation_and_aborts_the_job(harness: MakeHarness) -> None:
    h = harness()
    first = await h.service.connect()
    job = StubJob(generation=h.service.generation)
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: job.ticks >= 2)
    assert job.threads == {THREAD_NAME}
    old_worker = h.service.worker
    assert old_worker is not None

    result = await h.service.restart()
    assert result["previous_pid"] == first["pid"] and result["killed"] is True
    assert result["aborted_job_id"] == "job-1"
    assert result["generation"] == 2 == h.service.generation
    assert result["pid"] is not None and result["pid"] != first["pid"]
    assert job.state == "aborted" and job.abort_reasons == ["Vissim was restarted"]
    assert h.procs.table[first["pid"]].ended_by == "kill"
    assert old_worker.abandoned and old_worker.join(5)
    assert h.service.worker is not old_worker

    ticks = job.ticks
    await asyncio.sleep(0.2)
    assert job.ticks == ticks  # the aborted job of generation 1 is ticked by nobody
    assert h.service.health()["job"]["state"] == "aborted"
    assert h.service.last_error is None


async def test_restart_recovers_from_a_hung_vissim(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.vissim.hang_next_call(30)
    await raises(ErrorCode.TIMEOUT_HUNG, h.service.call(count_links, timeout_s=0.2, task_name="hung"))
    old_worker = h.service.worker
    assert old_worker is not None

    result = await h.service.restart()
    assert result["killed"] is True and result["generation"] == 2
    assert old_worker.join(5)  # killing Vissim made the hung COM call fail; the old thread ended
    health = h.service.health()
    assert health["connected"] is True and health["busy_seconds"] == 0.0 and health["current_task"] is None
    assert await h.service.call(count_links, task_name="count") == 10


async def test_a_late_result_of_the_replaced_worker_is_discarded(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.vissim.hang_next_call(30)
    hung = asyncio.create_task(h.service.call(count_links, timeout_s=10, task_name="hung"))
    await eventually(lambda: h.service.health()["current_task"] == "hung")
    result = await h.service.restart()
    error = await raises(ErrorCode.NOT_CONNECTED, hung)
    assert error.message == (
        "Vissim was restarted or closed while 'hung' was running; its result was discarded"
    )
    assert result["generation"] == 2 and h.service.connected


async def test_restart_without_a_connection_just_connects(harness: MakeHarness) -> None:
    h = harness()
    result = await h.service.restart()
    assert result["generation"] == 1 and result["pid"] is not None
    assert result["previous_pid"] is None and result["killed"] is False
    assert result["aborted_job_id"] is None and result["warnings"] == []


async def test_restart_cancels_a_connect_in_progress(harness: MakeHarness) -> None:
    h = harness(dispatch_delay_s=30)
    connecting = asyncio.create_task(h.service.connect())
    await eventually(lambda: len(h.procs.table) == 1 and h.service.connecting)
    h.backends.backend_options["dispatch_delay_s"] = 0.0  # the next Vissim starts at once
    result = await h.service.restart()
    error = await raises(ErrorCode.NOT_CONNECTED, connecting)
    assert error.message == "vissim_connect was cancelled by vissim_restart"
    starting, fresh = h.procs.table.values()
    assert starting.ended_by == "kill"  # the single new COM-launched process of the cancelled start
    assert result["pid"] == fresh.pid and result["generation"] == 2 and h.service.connected


async def test_restart_during_shutdown(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    h.service.shutdown_fast()
    await raises(ErrorCode.SHUTTING_DOWN, h.service.restart())


# --- close (PLAN 6.4, 7.1)


async def test_close_calls_exit_and_waits_for_the_process(harness: MakeHarness) -> None:
    h = harness()
    connected = await h.service.connect()
    vissim = h.vissim
    result = await h.service.close()
    assert result == {"closed": True, "method": "exit", "pid": connected["pid"], "warnings": []}
    assert vissim.exited and h.procs.table[connected["pid"]].ended_by == "exit"
    assert h.procs.kill_calls == [] and h.procs.terminate_calls == []
    health = h.service.health()
    assert health["connected"] is False and health["pid"] is None and health["job_object"] is False
    assert h.service.worker is None
    error = await raises(ErrorCode.NOT_CONNECTED, h.service.call(count_links, task_name="count"))
    assert error.message == "Vissim is not connected"
    assert (await h.service.connect())["generation"] == 2


async def test_close_escalates_to_terminate(harness: MakeHarness) -> None:
    h = harness(exit_keeps_process=True)  # Exit() returns but the process stays
    pid = (await h.service.connect())["pid"]
    result = await h.service.close()
    assert result == {"closed": True, "method": "terminate", "pid": pid, "warnings": []}
    assert h.procs.table[pid].ended_by == "terminate"


async def test_close_escalates_to_kill(harness: MakeHarness) -> None:
    h = harness(exit_keeps_process=True, spawn_options={"survives_terminate": True})
    pid = (await h.service.connect())["pid"]
    result = await h.service.close()
    assert result["method"] == "kill" and result["closed"] is True
    assert h.procs.terminated == [pid] and h.procs.killed == [pid]


async def test_close_reports_a_process_that_cannot_be_ended(harness: MakeHarness) -> None:
    h = harness(exit_keeps_process=True, spawn_options={"survives_terminate": True, "survives_kill": True})
    pid = (await h.service.connect())["pid"]
    result = await h.service.close()
    assert result["closed"] is False and result["method"] is None
    assert result["warnings"] == [f"Vissim (PID {pid}) could not be ended; close it in Task Manager"]
    assert h.service.connected is False and h.service.worker is None


async def test_close_when_exit_hangs(harness: MakeHarness) -> None:
    h = harness()
    pid = (await h.service.connect())["pid"]
    h.service.close_exit_call_timeout_s = 0.2
    h.vissim.hang_on("Exit", 30)  # e.g. a "Save changes?" dialog
    result = await h.service.close()
    assert result["closed"] is True and result["method"] == "terminate" and result["pid"] == pid
    assert result["warnings"] == [
        "Exit() did not return within 0.2 s (Vissim may show a dialog); ending the process"
    ]


async def test_close_with_an_unknown_pid_only_calls_exit(harness: MakeHarness) -> None:
    h = harness({"VISSIM_PID_DETECT_S": "0.1"}, on_dispatch=lambda backend, progid: None)
    await h.service.connect()
    vissim = h.vissim
    result = await h.service.close()
    assert result == {
        "closed": True,
        "method": "exit",
        "pid": None,
        "warnings": ["the Vissim PID is unknown, so the server cannot verify that the process ended"],
    }
    assert vissim.exited and h.procs.kill_calls == [] and h.procs.terminate_calls == []


async def test_close_after_vissim_died(harness: MakeHarness) -> None:
    h = harness()
    pid = (await h.service.connect())["pid"]
    h.vissim.crash()
    result = await h.service.close()
    assert result == {
        "closed": True,
        "method": None,
        "pid": pid,
        "warnings": [f"Vissim (PID {pid}) had already exited"],
    }
    assert h.procs.kill_calls == [] and h.procs.terminate_calls == []


async def test_close_when_not_connected(harness: MakeHarness) -> None:
    h = harness()
    error = await raises(ErrorCode.NOT_CONNECTED, h.service.close())
    assert error.message == "Vissim is not connected; there is nothing to close"


async def test_close_aborts_the_running_job(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    job = StubJob(generation=h.service.generation)
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: job.ticks >= 1)
    result = await h.service.close()
    assert result["aborted_job_id"] == "job-1" and result["method"] == "exit"
    assert job.state == "aborted" and job.abort_reasons == ["Vissim was closed"]


async def test_close_cancels_a_connect_in_progress(harness: MakeHarness) -> None:
    h = harness(dispatch_delay_s=30)
    connecting = asyncio.create_task(h.service.connect())
    await eventually(lambda: len(h.procs.table) == 1 and h.service.connecting)
    result = await h.service.close()
    (proc,) = h.procs.table.values()
    assert result == {
        "closed": True,
        "method": "kill",
        "pid": proc.pid,
        "warnings": ["vissim_connect was still starting Vissim; the start was cancelled"],
    }
    error = await raises(ErrorCode.NOT_CONNECTED, connecting)
    assert error.message == "vissim_connect was cancelled by vissim_close"
    assert proc.ended_by == "kill" and h.service.worker is None


# --- shutdown_fast (PLAN 6.4)


async def test_shutdown_kills_vissim_before_stopping_the_worker(harness: MakeHarness) -> None:
    h = harness()
    pid = (await h.service.connect())["pid"]
    worker = h.service.worker
    assert worker is not None
    original_stop = worker.stop

    def recording_stop(*args: Any, **kwargs: Any) -> None:
        h.procs.events.append(("worker.stop", None))
        original_stop(*args, **kwargs)

    worker.stop = recording_stop  # type: ignore[method-assign]
    h.vissim.hang_next_call(30)  # the worker is stuck in a COM call
    hung = asyncio.create_task(h.service.call(count_links, timeout_s=10, task_name="hung"))
    await eventually(lambda: h.service.health()["current_task"] == "hung")

    started = time.perf_counter()
    h.service.shutdown_fast()
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2, f"shutdown took {elapsed * 1000:.0f} ms"
    names = [name for name, _pid in h.procs.events]
    assert ("kill", pid) in h.procs.events
    assert names.index("kill") < names.index("worker.stop")
    assert h.procs.table[pid].ended_by == "kill"
    await raises(ErrorCode.COM_ERROR, hung)  # the killed Vissim failed the hung call

    for attempt in (
        h.service.call(count_links, task_name="count"),
        h.service.connect(),
        h.service.restart(),
        h.service.close(),
    ):
        await raises(ErrorCode.SHUTTING_DOWN, attempt)
    assert h.service.health()["shutting_down"] is True
    h.service.shutdown_fast()  # idempotent
    assert names.count("kill") == 1 == [name for name, _ in h.procs.events].count("kill")
    error = worker.submit(count_links, "late").exception(0)
    assert isinstance(error, VissimMcpError) and error.code is ErrorCode.SHUTTING_DOWN


async def test_shutdown_without_kill_on_exit_leaves_vissim_running(harness: MakeHarness) -> None:
    h = harness({"VISSIM_KILL_ON_EXIT": "0"})
    await h.service.connect()
    process = h.process
    h.service.shutdown_fast()
    assert h.procs.kill_calls == [] and process.alive


async def test_shutdown_never_kills_a_reused_pid(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    pid, created = h.service.pid, h.service.pid_create_time
    assert pid is not None and created is not None
    process = h.process
    process.on_end.clear()
    process.end("crash")  # Vissim died unnoticed ...
    stranger = h.procs.spawn(pid=pid, create_time=created + 500.0, com=False)  # ... and its PID was reused
    h.service.shutdown_fast()
    assert h.procs.kill_calls == [(pid, created)]  # asked, but the identity check refused
    assert h.procs.killed == [] and stranger.alive


async def test_shutdown_during_a_slow_start_kills_the_starting_vissim(harness: MakeHarness) -> None:
    h = harness(dispatch_delay_s=30)
    connecting = asyncio.create_task(h.service.connect())
    await eventually(lambda: any(job.assigned for job in h.procs.jobs))  # the start-up candidate is known
    h.service.shutdown_fast()
    (proc,) = h.procs.table.values()
    assert proc.ended_by == "kill"
    with pytest.raises(VissimMcpError):
        await connecting


# --- background steps (simulation job hook)


async def test_background_step_ticks_only_running_jobs_of_this_generation(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    job = StubJob(generation=h.service.generation)
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: job.ticks >= 3)
    assert job.threads == {THREAD_NAME}
    assert h.service.health()["job"] == {
        "job_id": "job-1",
        "state": "running",
        "run": 1,
        "runs": 1,
        "simsec": 0.0,
        "percent": 0.0,
    }
    job.state = "paused"
    await asyncio.sleep(0.1)
    ticks = job.ticks
    await asyncio.sleep(0.2)
    assert job.ticks == ticks

    foreign = StubJob("job-2", generation=h.service.generation + 1)
    h.service.jobs = StubRegistry(foreign)
    await asyncio.sleep(0.2)
    assert foreign.ticks == 0


async def test_calls_are_served_between_chunks(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    chunk_done = threading.Event()
    job = StubJob(generation=h.service.generation, block=chunk_done)
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: h.service.health()["current_task"] == CHUNK_TASK)
    assert h.service.worker is not None and h.service.worker.busy_background
    calls = [
        asyncio.create_task(h.service.call(count_links, timeout_s=5, task_name=f"c{i}")) for i in range(3)
    ]
    await eventually(lambda: h.service.health()["queue_depth"] == 3)
    job.block = None  # the following chunks are quick
    chunk_done.set()
    assert await asyncio.gather(*calls) == [10, 10, 10]
    await eventually(lambda: job.ticks >= 3)


async def test_timeout_during_a_chunk_names_the_chunk(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    chunk_done = threading.Event()
    job = StubJob(generation=h.service.generation, block=chunk_done)
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: h.service.health()["current_task"] == CHUNK_TASK)
    try:
        probe = h.service.call(count_links, timeout_s=0.2, task_name="p")
        error = await raises(ErrorCode.TIMEOUT_HUNG, probe)
        assert f"busy with a simulation chunk ('{CHUNK_TASK}'" in error.message
        assert "vissim_stop_simulation" in error.message
    finally:
        job.state = "paused"
        chunk_done.set()


async def test_background_errors_are_reported_and_the_worker_survives(harness: MakeHarness) -> None:
    h = harness()
    await h.service.connect()
    job = StubJob(generation=h.service.generation, fail=RuntimeError("chunk exploded"))
    h.service.jobs = StubRegistry(job)
    await eventually(lambda: h.service.last_error is not None)
    last_error = h.service.last_error
    assert last_error is not None
    assert last_error["code"] == "INTERNAL" and "chunk exploded" in last_error["message"]
    assert last_error["task"] == "background step"
    assert await h.service.call(count_links, task_name="count") == 10
    assert h.service.connected

    job.fail = make_com_error(RPC_E_DISCONNECTED)  # Vissim went away during a chunk
    await eventually(lambda: not h.service.connected)
    assert h.service.last_error is not None and h.service.last_error["code"] == "COM_ERROR"
