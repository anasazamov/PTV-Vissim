"""PLAN 10.3 item 1 - smoke: connect -> load the COM Basic Commands network -> Links.Count -> 60 s
simulation -> close; no Vissim process may remain."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import RealSession, wait_gone

pytestmark = pytest.mark.vissim


def test_smoke_connect_load_simulate_close(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = real_session()
    connected = session.connect()
    own = session.current()
    record(
        step="connect",
        pid=connected["pid"],
        job_object=connected["job_object"],
        elapsed_s=connected["elapsed_s"],
        warnings=connected["warnings"],
    )
    assert connected["job_object"] is True, connected

    model, layout = example_model
    loaded = session.load(model, layout)
    links = loaded["counts"]["Links"]
    record(
        step="load",
        counts=loaded["counts"],
        layout_loaded=loaded.get("layout_loaded"),
        elapsed_s=loaded["elapsed_s"],
        warnings=loaded["warnings"],
    )
    assert isinstance(links, int) and links > 0

    info = session.ok("vissim_info")
    assert info["counts"]["Links"] == links
    assert info["loaded_file"] == loaded["loaded_file"]

    configured = session.ok("vissim_configure_simulation", {"period_s": 60, "seed": 42})
    assert configured["period_s"] == 60, configured
    started = session.ok("vissim_start_simulation", {"runs": 1, "seed": 42})
    status = session.wait_job(started["job_id"], timeout_s=300)
    record(
        step="simulate_60s",
        state=status["state"],
        simsec=status["simsec"],
        run_wall_s=status["run_wall_s"],
        warnings=status["warnings"],
    )
    assert status["state"] == "finished", status
    assert status["percent"] == 100.0

    closed = session.ok("vissim_close", timeout=120)
    record(step="close", method=closed["method"], warnings=closed.get("warnings"))
    assert closed["closed"] is True
    assert wait_gone(own), f"Vissim PID {own.pid} still running after vissim_close"
    health = session.ok("vissim_health")
    assert health["connected"] is False
