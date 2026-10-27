"""jobs.py (PLAN 6.6, 10.1 "jobs"): the simulation state machine driven directly against FakeVissim.

The job is prepared and ticked on the test thread (FakeVissim is bound to its creating thread), so every case
is deterministic: one ``tick`` = one chunk.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from fake_vissim import DISP_E_EXCEPTION, RPC_E_DISCONNECTED, FakeVissim
from vissim_mcp.config import Settings
from vissim_mcp.errors import ErrorCode, VissimMcpError, is_disconnect_error
from vissim_mcp.jobs import (
    HISTORY_SIZE,
    MAX_EARLY_PAUSES,
    MULTI_RUN_WARNING,
    STALE_BREAK_WARNING,
    JobRegistry,
    JobState,
    SimulationJob,
    StartRequest,
    compute_seeds,
    prepare_job,
)


class Ctx:
    def __init__(self, vissim: FakeVissim, generation: int = 1) -> None:
        self.vissim = vissim
        self.generation = generation


def make_vissim(period: int = 600, **options: Any) -> FakeVissim:
    vissim = FakeVissim(**options)
    vissim.set_simulation_attribute("SimPeriod", period)
    return vissim


def start(
    vissim: FakeVissim,
    settings: Settings,
    registry: JobRegistry | None = None,
    *,
    chunk: int = 60,
    **request: Any,
) -> tuple[SimulationJob, dict[str, Any]]:
    registry = registry if registry is not None else JobRegistry()
    data = prepare_job(
        registry, StartRequest(chunk_sim_s=chunk, default_chunk=60, **request), settings, Ctx(vissim)
    )
    assert registry.current is not None
    return registry.current, data


def drive(job: SimulationJob, vissim: FakeVissim, settings: Settings, limit: int = 500) -> list[float]:
    """Tick while the job is running; returns the percent after every tick."""
    percents: list[float] = []
    for _ in range(limit):
        if job.state is not JobState.RUNNING:
            break
        job.tick(vissim, settings)
        percents.append(job.snapshot()["percent"])
    return percents


# --- happy paths


def test_one_run_finishes(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, data = start(vissim, settings)
    assert data["state"] == "running" and data["runs"] == 1 and data["seeds"] == [None]
    assert data["period"] == 600 and data["chunk"] == 60 and data["warnings"] == []
    percents = drive(job, vissim, settings)
    assert job.state is JobState.FINISHED
    assert len(percents) == 10  # 9 chunks with a break + the final chunk
    assert len(vissim.run_records) == 1 and vissim.run_records[0]["SimEnd"] == 600
    assert vissim.run_records[0]["stopped"] is False
    snapshot = job.snapshot()
    assert snapshot["percent"] == 100.0 and snapshot["eta_s"] == 0.0 and snapshot["simsec"] == 600
    assert len(snapshot["run_wall_s"]) == 1 and snapshot["error"] is None


def test_three_runs_use_distinct_precomputed_seeds(settings: Settings) -> None:
    vissim = make_vissim(300)
    job, data = start(vissim, settings, runs=3, seed=7, seed_increment=3)
    assert data["seeds"] == [7, 10, 13]
    assert MULTI_RUN_WARNING in data["warnings"]
    assert vissim.simulation_attribute("RandSeed") == 7  # the first seed is applied at start
    percents = drive(job, vissim, settings)
    assert job.state is JobState.FINISHED and job.run == 3
    assert [record["RandSeed"] for record in vissim.run_records] == [7, 10, 13]
    assert percents == sorted(percents) and percents[-1] == 100.0
    assert len(job.snapshot()["run_wall_s"]) == 3


def test_seed_none_with_several_runs_reads_the_current_seed(settings: Settings) -> None:
    vissim = make_vissim(120)
    vissim.set_simulation_attribute("RandSeed", 99)
    job, data = start(vissim, settings, runs=2)
    assert data["seeds"] == [99, 100]
    assert any("current RandSeed 99" in warning for warning in data["warnings"])
    drive(job, vissim, settings)
    assert [record["RandSeed"] for record in vissim.run_records] == [99, 100]


def test_seed_none_with_several_runs_is_rejected_when_unreadable(settings: Settings) -> None:
    vissim = make_vissim(120)
    registry = JobRegistry()
    odd = dataclasses.replace(settings, seed_attr="NoSuchSeedAttribute")
    with pytest.raises(VissimMcpError) as caught:
        prepare_job(registry, StartRequest(runs=2, default_chunk=60), odd, Ctx(vissim))
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT
    assert caught.value.message == "seed required for runs>1"
    assert registry.current is None


def test_single_run_without_seed_keeps_vissims_seed(settings: Settings) -> None:
    vissim = make_vissim(120)
    vissim.set_simulation_attribute("RandSeed", 5)
    job, data = start(vissim, settings)
    assert data["seeds"] == [None]
    drive(job, vissim, settings)
    assert vissim.run_records[0]["RandSeed"] == 5


def test_chunk_not_smaller_than_the_period_runs_each_run_in_one_tick(settings: Settings) -> None:
    vissim = make_vissim(300)
    job, _ = start(vissim, settings, chunk=300, runs=2, seed=1)
    percents = drive(job, vissim, settings)
    assert percents == [50.0, 100.0] and job.state is JobState.FINISHED
    job2, _ = start(vissim, settings, chunk=10_000)
    assert drive(job2, vissim, settings) == [100.0]


def test_percent_is_monotonic_and_ends_at_100(settings: Settings) -> None:
    vissim = make_vissim(250)
    job, _ = start(vissim, settings, chunk=40, runs=3, seed=11, pause_at=[100])
    percents: list[float] = []
    for _ in range(200):
        if job.state is JobState.PAUSED:
            job.resume()
        if job.state is not JobState.RUNNING:
            break
        job.tick(vissim, settings)
        percents.append(job.snapshot()["percent"])
    assert job.state is JobState.FINISHED
    assert percents == sorted(percents) and percents[-1] == 100.0
    assert 0 < percents[0] < 100


def test_snapshot_fields_mid_run(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings, runs=2, seed=3)
    for _ in range(3):
        job.tick(vissim, settings)
    snapshot = job.snapshot()
    assert snapshot["state"] == "running" and snapshot["run"] == 1 and snapshot["simsec"] == 180
    assert snapshot["percent"] == 15.0  # 180 / (2 * 600)
    assert snapshot["eta_s"] is not None and snapshot["paused_at"] is None
    assert set(snapshot) >= {
        "job_id", "state", "run", "runs", "simsec", "period", "percent", "elapsed_s", "eta_s", "seeds",
        "paused_at", "error", "warnings",
    }  # fmt: skip


# --- stale breaks, early pauses


def test_stale_simbreakat_in_a_chunk_is_accepted_with_one_warning(settings: Settings) -> None:
    vissim = make_vissim(600, stale_break_at=[90, 100])
    job, _ = start(vissim, settings)
    drive(job, vissim, settings)
    assert job.state is JobState.FINISHED
    assert job.warnings.count(STALE_BREAK_WARNING) == 1
    assert len(vissim.run_records) == 1 and vissim.run_records[0]["SimEnd"] == 600


@pytest.mark.parametrize("simsec_after_run", ["zero", "period"])
def test_stale_break_in_the_final_branch_still_finishes(settings: Settings, simsec_after_run: str) -> None:
    vissim = make_vissim(600, stale_break_at=[100, 400], simsec_after_run=simsec_after_run)
    job, _ = start(vissim, settings, chunk=1000, runs=2, seed=1)
    drive(job, vissim, settings)
    assert job.state is JobState.FINISHED
    assert [record["SimEnd"] for record in vissim.run_records] == [600, 600]
    assert STALE_BREAK_WARNING in job.warnings


def test_too_many_early_pauses_in_the_final_branch_fail_the_job(settings: Settings) -> None:
    points = [50 * (k + 1) for k in range(MAX_EARLY_PAUSES + 1)]
    vissim = make_vissim(600, stale_break_at=points)
    job, _ = start(vissim, settings, chunk=1000)
    drive(job, vissim, settings)
    assert job.state is JobState.ERROR
    assert job.error is not None and "keeps pausing" in job.error["message"]


def test_exactly_max_early_pauses_are_tolerated(settings: Settings) -> None:
    vissim = make_vissim(600, stale_break_at=[50 * (k + 1) for k in range(MAX_EARLY_PAUSES)])
    job, _ = start(vissim, settings, chunk=1000)
    drive(job, vissim, settings)
    assert job.state is JobState.FINISHED


# --- pause_at, resume, stop


def test_pause_at_pauses_every_run_and_resume_continues(settings: Settings) -> None:
    vissim = make_vissim(300)
    job, data = start(vissim, settings, chunk=60, runs=2, seed=4, pause_at=[250, 100, 100])
    assert data["pause_at"] == [100, 250]  # sorted, deduplicated
    pauses: list[tuple[int, Any]] = []
    for _ in range(100):
        if job.state is JobState.FINISHED:
            break
        if job.state is JobState.PAUSED:
            snapshot = job.snapshot()
            pauses.append((snapshot["run"], snapshot["paused_at"]))
            assert vissim.sim_active and vissim.sim_sec == snapshot["paused_at"]
            job.tick(vissim, settings)  # a paused job does nothing
            assert job.state is JobState.PAUSED
            job.resume()
            continue
        job.tick(vissim, settings)
    assert pauses == [(1, 100), (1, 250), (2, 100), (2, 250)]
    assert job.state is JobState.FINISHED


def test_resume_of_a_job_that_is_not_paused_is_invalid(settings: Settings) -> None:
    vissim = make_vissim(300)
    job, _ = start(vissim, settings)
    with pytest.raises(VissimMcpError) as caught:
        job.resume()
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_stop_of_a_running_job_happens_at_the_next_chunk(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings)
    job.tick(vissim, settings)
    job.request_stop()
    assert job.state is JobState.RUNNING and job.snapshot()["stop_requested"] is True
    job.tick(vissim, settings)
    assert job.state is JobState.STOPPED
    assert vissim.run_records[-1]["stopped"] is True and vissim.run_records[-1]["SimEnd"] == 60
    assert not vissim.sim_active


def test_stop_of_a_paused_job(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings, pause_at=[30])
    job.tick(vissim, settings)
    assert job.state is JobState.PAUSED
    job.request_stop()  # ignored for a paused job (the tool calls stop_paused on the worker)
    assert job.stop_requested is False
    assert job.stop_paused(vissim) == "stopped"
    assert vissim.run_records[-1]["stopped"] is True and not vissim.sim_active
    assert job.stop_paused(vissim) == "stopped"  # idempotent, no second Stop()


# --- errors


def test_com_error_fails_the_job_with_code_message_hint_details(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings)
    job.tick(vissim, settings)
    vissim.raise_com_error_next(DISP_E_EXCEPTION, "RunContinuous failed: no network.")
    job.tick(vissim, settings)
    assert job.state is JobState.ERROR
    error = job.snapshot()["error"]
    assert error["code"] == "COM_ERROR"
    assert "RunContinuous failed: no network." in error["message"] and job.id in error["message"]
    assert error["hint"] and error["details"]["hresult"] == DISP_E_EXCEPTION
    job.tick(vissim, settings)  # an ended job is never ticked again
    assert job.state is JobState.ERROR


def test_a_disconnect_is_raised_to_the_service(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings)
    vissim.raise_com_error_next(RPC_E_DISCONNECTED)
    with pytest.raises(Exception) as caught:
        job.tick(vissim, settings)
    assert is_disconnect_error(caught.value)
    job.fail({"code": "COM_ERROR", "message": "gone"})  # what the service's safety net does
    assert job.state is JobState.ERROR


def test_did_not_advance_in_a_chunk(settings: Settings) -> None:
    vissim = make_vissim(600, stall_runs=1)
    job, _ = start(vissim, settings)
    job.tick(vissim, settings)
    assert job.state is JobState.ERROR
    assert job.error is not None and job.error["message"].startswith("simulation did not advance")


def test_did_not_advance_in_the_final_chunk(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings, chunk=500)
    job.tick(vissim, settings)
    assert job.simsec == 500
    vissim.stall_runs = 1
    job.tick(vissim, settings)
    assert job.state is JobState.ERROR
    assert job.error is not None and job.error["message"].startswith("simulation did not advance")


@pytest.mark.parametrize(
    ("simsec_after_run", "message"),
    [("zero", "simulation did not advance"), ("period", "simulation did not pause at SimBreakAt")],
)
def test_an_ignored_break_fails_the_job(settings: Settings, simsec_after_run: str, message: str) -> None:
    vissim = make_vissim(600, ignore_break=True, simsec_after_run=simsec_after_run)
    job, _ = start(vissim, settings)
    job.tick(vissim, settings)
    assert job.state is JobState.ERROR
    assert job.error is not None and job.error["message"].startswith(message)


def test_abort_is_final(settings: Settings) -> None:
    vissim = make_vissim(600)
    job, _ = start(vissim, settings)
    job.abort("Vissim was restarted")
    job.abort("again")
    job.fail({"code": "COM_ERROR", "message": "late"})
    job.tick(vissim, settings)
    snapshot = job.snapshot()
    assert snapshot["state"] == "aborted" and snapshot["abort_reason"] == "Vissim was restarted"
    assert snapshot["error"] is None


# --- start validation


def test_start_stops_a_run_in_progress_first(settings: Settings) -> None:
    vissim = make_vissim(600)
    vissim.Simulation.RunSingleStep()
    assert vissim.sim_active
    job, data = start(vissim, settings, runs=1, seed=8)
    assert any("it was stopped first" in warning for warning in data["warnings"])
    assert vissim.simulation_attribute("RandSeed") == 8
    drive(job, vissim, settings)
    assert job.state is JobState.FINISHED


def test_start_rejects_a_second_active_job(settings: Settings) -> None:
    vissim = make_vissim(600)
    registry = JobRegistry()
    first, _ = start(vissim, settings, registry)
    with pytest.raises(VissimMcpError) as caught:
        start(vissim, settings, registry)
    assert caught.value.code is ErrorCode.JOB_RUNNING and first.id in caught.value.message
    first.abort("test")
    second, _ = start(vissim, settings, registry)
    assert registry.get() is second and registry.get(first.id) is first


def test_start_rejects_a_non_positive_period(settings: Settings) -> None:
    vissim = make_vissim(600)
    vissim.Simulation._attrs["simperiod"].value = 0
    with pytest.raises(VissimMcpError) as caught:
        start(vissim, settings)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT and "SimPeriod" in caught.value.message


@pytest.mark.parametrize("pause_at", [[600], [700], [0]])
def test_pause_at_must_lie_inside_the_period(settings: Settings, pause_at: list[int]) -> None:
    vissim = make_vissim(600)
    with pytest.raises(VissimMcpError) as caught:
        start(vissim, settings, pause_at=pause_at)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"runs": 0},
        {"runs": 1001},
        {"runs": True},
        {"runs": 1.5},
        {"seed": 0},
        {"seed": "7"},
        {"seed_increment": 0},
        {"chunk_sim_s": 0},
        {"pause_at": "100"},
        {"pause_at": [1.5]},
    ],
)
def test_start_request_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(VissimMcpError) as caught:
        StartRequest(default_chunk=60, **kwargs)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENT


def test_compute_seeds() -> None:
    assert compute_seeds(None, 1, 1) == [None]
    assert compute_seeds(5, 4, 10) == [5, 15, 25, 35]
    with pytest.raises(VissimMcpError):
        compute_seeds(2**31 - 2, 3, 1)


# --- registry


def test_registry_keeps_the_current_and_the_last_jobs(settings: Settings) -> None:
    registry = JobRegistry()
    with pytest.raises(VissimMcpError) as caught:
        registry.get()
    assert caught.value.code is ErrorCode.JOB_NOT_FOUND
    assert registry.active() is None
    vissim = make_vissim(60)
    ids: list[str] = []
    for _ in range(HISTORY_SIZE + 2):
        job, _ = start(vissim, settings, registry)
        ids.append(job.id)
        assert registry.active() is job
        drive(job, vissim, settings)
        assert registry.active() is None
    assert registry.get().id == ids[-1]
    assert registry.get(ids[-HISTORY_SIZE - 1]).id == ids[-HISTORY_SIZE - 1]
    with pytest.raises(VissimMcpError) as caught:
        registry.get(ids[0])
    assert caught.value.code is ErrorCode.JOB_NOT_FOUND
    assert len(registry.jobs()) == HISTORY_SIZE + 1
