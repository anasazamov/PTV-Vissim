"""PLAN 10.3 item 3 - break semantics on a real Vissim.

* raw COM (through vissim_exec_python, enabled only for this server): SimBreakAt=N pauses at N;
  SimBreakAt=VISSIM_BREAK_DISABLE_VALUE (0) runs to the end; SimSec after the run is recorded;
* a stale SimBreakAt (below SimSec) left in a model paused mid-run: what a raw RunContinuous does with it,
  and that a job started on top of that state finishes (once with the probe chunk + final branch, once
  chunked over two runs);
* pause_at -> paused -> vissim_set_signal_state (SigState, ContrByCOM) -> release -> resume -> finished.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import RealSession

pytestmark = pytest.mark.vissim

PERIOD = 600
BREAK_AT = 120
EXEC_ENV = {"VISSIM_MCP_ALLOW_EXEC": "1", "VISSIM_CALL_TIMEOUT_S": "300"}

#: "Run ended" is detected by a new SimulationRuns row, which needs KeepPrevResults = 3 (KeepAll).
KEEP_ALL_CODE = """
evaluation = Vissim.Evaluation
keep_before = evaluation.AttValue("KeepPrevResults")
if keep_before != 3:
    evaluation.SetAttValue("KeepPrevResults", 3)
"""

RAW_BREAK_CODE = (
    KEEP_ALL_CODE
    + f"""
sim = Vissim.Simulation
runs = Vissim.Net.SimulationRuns
step = 1.0 / float(sim.AttValue("SimRes"))
sim.SetAttValue("SimPeriod", {PERIOD})
sim.SetAttValue("UseMaxSimSpeed", True)
active_before = sim.AttValue("IsSimBreakAtActive")
sim.SetAttValue("SimBreakAt", {BREAK_AT})
active_after = sim.AttValue("IsSimBreakAtActive")
runs_before = runs.Count
sim.RunContinuous()
at_break = sim.AttValue("SimSec")
running_at_break = sim.AttValue("IsRunning")
sim.SetAttValue("SimBreakAt", 0)
sim.RunContinuous()
after_run = sim.AttValue("SimSec")
running_after_run = sim.AttValue("IsRunning")
runs_after = runs.Count
result = {{
    "step": step,
    "KeepPrevResults_before": keep_before,
    "IsSimBreakAtActive_before": active_before,
    "IsSimBreakAtActive_after_set": active_after,
    "SimSec_at_break": at_break,
    "IsRunning_at_break": running_at_break,
    "SimSec_after_run": after_run,
    "IsRunning_after_run": running_after_run,
    "runs_before": runs_before,
    "runs_after": runs_after,
}}
"""
)

STALE = BREAK_AT // 2

#: Leaves the model paused at BREAK_AT with SimBreakAt = STALE (already passed). Before that it measures what
#: a raw RunContinuous does with such a stale break, and leaves the model paused mid-run again afterwards.
STALE_BREAK_CODE = (
    KEEP_ALL_CODE
    + f"""
sim = Vissim.Simulation
runs = Vissim.Net.SimulationRuns
step = 1.0 / float(sim.AttValue("SimRes"))
sim.Stop()
sim.SetAttValue("SimPeriod", {PERIOD})
sim.SetAttValue("UseMaxSimSpeed", True)

def pause_at_break():
    sim.SetAttValue("SimBreakAt", {BREAK_AT})
    sim.RunContinuous()
    return sim.AttValue("SimSec")

at_break = pause_at_break()
sim.SetAttValue("SimBreakAt", {STALE})
runs_before = runs.Count
sim.RunContinuous()
after_stale_run = sim.AttValue("SimSec")
runs_after = runs.Count
if runs_after == runs_before + 1:
    outcome = "ran_to_end"
elif abs(float(after_stale_run) - float(at_break)) <= step + 1e-6:
    outcome = "did_not_advance"
else:
    outcome = "paused_at_" + str(after_stale_run)
if outcome != "did_not_advance":  # back to a mid-run pause at BREAK_AT
    sim.Stop()
    at_break = pause_at_break()
sim.SetAttValue("SimBreakAt", {STALE})
result = {{
    "step": step,
    "KeepPrevResults_before": keep_before,
    "stale_break": {STALE},
    "raw_outcome": outcome,
    "SimSec_after_stale_run": after_stale_run,
    "runs_before": runs_before,
    "runs_after": runs_after,
    "left_paused_at": sim.AttValue("SimSec"),
    "left_SimBreakAt": sim.AttValue("SimBreakAt"),
}}
"""
)


def _prepared(
    real_session: Callable[..., RealSession],
    model: tuple[Path, Path | None],
    env: dict[str, str] | None = None,
) -> RealSession:
    session = real_session(env)
    session.connect()
    session.load(*model)
    return session


def test_raw_break_at_and_break_disable_value(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = _prepared(real_session, example_model, EXEC_ENV)
    payload = session.ok("vissim_exec_python", {"code": RAW_BREAK_CODE}, timeout=330)
    raw = payload["result"]
    record(raw_break_semantics=raw)
    assert abs(float(raw["SimSec_at_break"]) - BREAK_AT) <= raw["step"] + 1e-6, (
        f"RunContinuous did not pause at SimBreakAt={BREAK_AT}: {raw} - check IsSimBreakAtActive"
    )
    assert raw["runs_after"] == raw["runs_before"] + 1, (
        f"SimBreakAt=0 did not run to the end of the run: {raw} - VISSIM_BREAK_DISABLE_VALUE must change"
    )


def test_stale_break_does_not_break_a_job(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    """A model left paused mid-run (SimSec = BREAK_AT) with SimBreakAt = STALE < SimSec: the job stops that
    run first (start warning) and then always writes its own SimBreakAt - it must finish either way."""
    session = _prepared(real_session, example_model, EXEC_ENV)

    def leave_stale_break() -> dict:
        raw = session.ok("vissim_exec_python", {"code": STALE_BREAK_CODE}, timeout=330)["result"]
        assert abs(float(raw["left_paused_at"]) - BREAK_AT) <= raw["step"] + 1e-6, raw
        assert float(raw["left_SimBreakAt"]) < float(raw["left_paused_at"]), raw  # genuinely stale
        return raw

    raw = leave_stale_break()
    # chunk >= period: probe chunk to probe_point(PERIOD), then the final branch.
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 7, "chunk_sim_s": 1000})
    status = session.wait_job(job["job_id"], timeout_s=300)
    record(
        stale_break_raw=raw,
        stale_break_final_branch={
            "start_warnings": job.get("warnings"),
            "state": status["state"],
            "simsec": status["simsec"],
            "warnings": status["warnings"],
            "error": status["error"],
        },
    )
    assert status["state"] == "finished", status

    # The regular chunked path, over two runs, from the same stale state.
    leave_stale_break()
    job = session.ok("vissim_start_simulation", {"runs": 2, "seed": 7, "chunk_sim_s": 60})
    status = session.wait_job(job["job_id"], timeout_s=600)
    record(
        stale_break_chunked={
            "start_warnings": job.get("warnings"),
            "state": status["state"],
            "warnings": status["warnings"],
            "run_wall_s": status["run_wall_s"],
            "error": status["error"],
        }
    )
    assert status["state"] == "finished", status
    assert status["run"] == 2 and status["percent"] == 100.0


def test_pause_at_signal_state_and_resume(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = _prepared(real_session, example_model)
    sc_no = session.first_key("Net.SignalControllers")
    sg_no = session.first_key(f"Net.SignalControllers[{sc_no}].SGs")
    session.ok("vissim_configure_simulation", {"period_s": PERIOD, "seed": 11})
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 11, "chunk_sim_s": 60, "pause_at": [200]})
    status = session.wait_job(job["job_id"], timeout_s=300)
    assert status["state"] == "paused", status
    assert abs(float(status["simsec"]) - 200) <= 1, status

    green = session.ok("vissim_set_signal_state", {"sc_no": sc_no, "sg_no": sg_no, "state": "GREEN"})
    by_int = session.ok("vissim_set_signal_state", {"sc_no": sc_no, "sg_no": sg_no, "state": 1})
    released = session.ok(
        "vissim_set_signal_state", {"sc_no": sc_no, "sg_no": sg_no, "release_to_controller": True}
    )
    record(signal_while_paused={"green": green, "int_1": by_int, "released": released})
    assert green["ContrByCOM"] is True, green
    assert released["ContrByCOM"] is False, released

    resumed = session.ok("vissim_resume_simulation", {"job_id": job["job_id"]})
    assert resumed["state"] in ("running", "finished"), resumed
    status = session.wait_job(job["job_id"], timeout_s=300)
    assert status["state"] == "finished", status
