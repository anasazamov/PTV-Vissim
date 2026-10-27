"""PLAN 10.3 item 4 - stress: 3600 s x 5 runs; percent grows monotonically; vissim_health stays instant
while the job runs (the Claude session is not blocked); stop takes effect within one chunk."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import RealSession

pytestmark = pytest.mark.vissim

PERIOD = 3600
RUNS = 5
CHUNK = 60
SEED = 42
HEALTH_LIMIT_S = 1.0


def _prepared(real_session: Callable[..., RealSession], model: tuple[Path, Path | None]) -> RealSession:
    session = real_session({"VISSIM_CHUNK_SIM_S": str(CHUNK)})
    session.connect()
    session.load(*model)
    session.ok("vissim_configure_simulation", {"period_s": PERIOD, "seed": SEED})
    return session


def test_five_runs_of_an_hour(
    request: pytest.FixtureRequest,
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = _prepared(real_session, example_model)
    keep = session.ok("vissim_get_attributes", {"path": "Evaluation", "attributes": ["KeepPrevResults"]})
    if keep["values"].get("KeepPrevResults") != 3:  # 3 = KeepAll: every run stays in SimulationRuns
        session.ok("vissim_set_attributes", {"path": "Evaluation", "values": {"KeepPrevResults": 3}})

    job = session.ok("vissim_start_simulation", {"runs": RUNS, "seed": SEED, "seed_increment": 1})
    assert job["seeds"] == [SEED + k for k in range(RUNS)]
    started = time.monotonic()
    limit = float(request.config.getoption("vissim_stress_timeout"))
    percents: list[float] = []
    health_s: list[float] = []
    status: dict = {}
    while time.monotonic() - started < limit:
        t0 = time.perf_counter()
        health = session.ok("vissim_health", timeout=10)
        health_s.append(time.perf_counter() - t0)
        assert health["connected"] is True
        status = session.ok("vissim_wait_simulation", {"job_id": job["job_id"], "max_wait_s": 30}, timeout=60)
        percents.append(float(status["percent"]))
        if status["state"] != "running":
            break
    wall = time.monotonic() - started
    record(
        stress={
            "state": status.get("state"),
            "wall_s": round(wall, 1),
            "run_wall_s": status.get("run_wall_s"),
            "max_health_s": round(max(health_s), 3),
            "polls": len(percents),
            "warnings": status.get("warnings"),
            "error": status.get("error"),
        }
    )
    assert status.get("state") == "finished", status
    assert percents == sorted(percents), f"percent went backwards: {percents}"
    assert percents[-1] == 100.0
    assert max(health_s) < HEALTH_LIMIT_S, f"vissim_health blocked: {max(health_s):.2f} s"

    runs = session.ok(
        "vissim_list_objects",
        {"collection": "Net.SimulationRuns", "attributes": ["No", "RandSeed", "SimEnd"], "limit": 50},
    )
    seeds = [row[1] for row in runs["rows"]]
    record(simulation_runs=runs["rows"])
    for seed in job["seeds"]:
        assert seed in seeds, f"run with seed {seed} missing in SimulationRuns: {runs['rows']}"


def test_stop_takes_effect_within_one_chunk(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = _prepared(real_session, example_model)
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": SEED, "chunk_sim_s": CHUNK})
    deadline = time.monotonic() + 600
    status = session.ok("vissim_simulation_status", {"job_id": job["job_id"]})
    while float(status["simsec"]) < 10 * CHUNK and status["state"] == "running":
        assert time.monotonic() < deadline, status
        time.sleep(0.2)
        status = session.ok("vissim_simulation_status", {"job_id": job["job_id"]})
    assert status["state"] == "running", status

    requested = time.monotonic()
    stop = session.ok("vissim_stop_simulation", {"job_id": job["job_id"]})
    assert stop["stopping"] is True, stop
    at_request = float(session.ok("vissim_simulation_status", {"job_id": job["job_id"]})["simsec"])
    final = session.wait_job(job["job_id"], timeout_s=120)
    stop_wall = time.monotonic() - requested
    record(
        stop={
            "state": final["state"],
            "simsec_at_request": at_request,
            "final_simsec": final["simsec"],
            "stop_wall_s": round(stop_wall, 2),
        }
    )
    assert final["state"] == "stopped", final
    assert float(final["simsec"]) <= at_request + CHUNK + 1, final
    assert float(final["simsec"]) < PERIOD
