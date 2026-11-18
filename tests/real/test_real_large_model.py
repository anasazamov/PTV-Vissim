"""The whole workflow on the large example networks shipped with Vissim (not only the 34-link COM example).

Each model folder is copied into the test's project directory (a network needs its companion files: signal
programs, textures ...), then: load -> counts -> page through every link -> a bulk dry run -> a chunked
simulation with a pause (vehicles are listed, vissim_health stays fast) -> CSV export -> save under a new
name.
"""

from __future__ import annotations

import csv
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from real_helpers import LOAD_TIMEOUT_S, RealSession

pytestmark = pytest.mark.vissim

EXAMPLES = Path(r"C:\Users\Public\Documents\PTV Vision\PTV Vissim 2026")
MODELS = [
    pytest.param(
        EXAMPLES / "Examples Demo" / "Motorway Dyn Assign Mainz.DE" / "Mainz - Static Routes.inpx",
        id="mainz-motorway",
    ),
    pytest.param(
        EXAMPLES / "Examples Demo" / "3D - Complex Intersection Karlsruhe.DE" / "Karlsruhe 3D.inpx",
        id="karlsruhe-intersection",
    ),
    pytest.param(
        EXAMPLES / "Examples Demo" / "Signalization RBC Boise.US" / "Signalization Boise.inpx",
        id="boise-signals",
    ),
]
PERIOD = 900
PAUSE_AT = 600
PAGE = 500


def _all_rows(session: RealSession, collection: str, attributes: list[str]) -> tuple[list[list[Any]], int]:
    rows: list[list[Any]] = []
    pages = 0
    while True:
        page = session.ok(
            "vissim_list_objects",
            {"collection": collection, "attributes": attributes, "offset": len(rows), "limit": PAGE},
        )
        pages += 1
        rows.extend(page["rows"])
        if not page["truncated"] or not page["rows"]:
            assert len(rows) == page["total"], (len(rows), page["total"])
            return rows, pages


@pytest.mark.parametrize("source", MODELS)
def test_large_model_workflow(
    source: Path,
    real_session: Callable[..., RealSession],
    tmp_path: Path,
    record: Callable[..., None],
) -> None:
    if not source.is_file():
        pytest.skip(f"example network not installed: {source}")
    folder = tmp_path / "projects" / "model"
    shutil.copytree(source.parent, folder)
    model = folder / source.name
    layout = model.with_suffix(".layx")

    session = real_session()
    session.connect()
    args: dict[str, Any] = {"inpx_path": str(model)}
    if layout.is_file():
        args["layx_path"] = str(layout)
    loaded = session.tool("vissim_load_network", args, timeout=LOAD_TIMEOUT_S)
    if not loaded.get("ok"):
        # Vissim itself refuses some of the shipped networks under a restricted (Academic) license - e.g. more
        # than 20 signal controllers. That is not a server problem, but the error must say why.
        error = loaded["error"]
        record(load_refused={"model": source.name, "error": error})
        assert error["code"] == "COM_ERROR", loaded
        reported = (error.get("details") or {}).get("vissim_messages")
        assert reported, f"the error does not say why Vissim refused the network: {error}"
        assert session.ok("vissim_health")["connected"] is True
        pytest.skip(f"Vissim refused {source.name}: {reported[0]}")
    counts = loaded["counts"]
    assert counts["Links"] > 100, counts  # these are not toy networks

    started = time.monotonic()
    links, pages = _all_rows(session, "Net.Links", ["No", "Name", "NumLanes", "Length2D", "IsConn"])
    list_s = time.monotonic() - started
    assert len(links) == counts["Links"] and len({row[0] for row in links}) == len(links)

    sample = [row[0] for row in links[:200]]
    dry = session.ok(
        "vissim_set_attribute_bulk",
        {
            "collection": "Net.Links",
            "attribute": "Name",
            "items": [{"key": key, "value": f"L{key}"} for key in sample],
            "dry_run": True,
        },
    )
    assert dry["failed_total"] == 0 and dry["changes_total"] == len(sample), dry

    session.ok("vissim_configure_simulation", {"period_s": PERIOD, "seed": 21})
    job = session.ok(
        "vissim_start_simulation", {"runs": 1, "seed": 21, "chunk_sim_s": 60, "pause_at": [PAUSE_AT]}
    )
    slowest_health = 0.0
    polls = 0
    deadline = time.monotonic() + 1500
    status: dict[str, Any] = {}
    while time.monotonic() < deadline:
        before = time.monotonic()
        health = session.ok("vissim_health", timeout=30)
        slowest_health = max(slowest_health, time.monotonic() - before)
        assert health["connected"] is True
        status = session.ok("vissim_wait_simulation", {"job_id": job["job_id"], "max_wait_s": 20}, timeout=60)
        polls += 1
        if status["state"] != "running":
            break
    assert status["state"] == "paused" and abs(status["simsec"] - PAUSE_AT) <= 1, status
    assert slowest_health < 1.0, slowest_health

    vehicles = session.ok(
        "vissim_list_objects",
        {"collection": "Net.Vehicles", "attributes": ["No", "VehType", "Speed", "Lane"], "limit": PAGE},
    )
    assert vehicles["total"] > 0, vehicles
    exported = session.ok(
        "vissim_export_csv",
        {
            "collection": "Net.Vehicles",
            "attributes": ["No", "VehType", "Speed", "Pos", "Lane"],
            "csv_path": "out/vehicles.csv",
        },
    )
    assert exported["rows"] == vehicles["total"], (exported, vehicles["total"])
    with open(exported["path"], encoding="utf-8-sig", newline="") as handle:
        table = list(csv.reader(handle, delimiter=";"))
    assert table[0] == ["No", "VehType", "Speed", "Pos", "Lane"] and len(table) == exported["rows"] + 1

    session.ok("vissim_resume_simulation", {"job_id": job["job_id"]})
    final = session.wait_job(job["job_id"], timeout_s=1500)
    assert final["state"] == "finished", final

    links_csv = session.ok(
        "vissim_export_csv",
        {"collection": "Net.Links", "attributes": ["No", "Name", "Length2D"], "csv_path": "out/links.csv"},
    )
    assert links_csv["rows"] == counts["Links"]
    saved = session.ok("vissim_save_network", {"save_as": "work/large_copy.inpx"}, timeout=LOAD_TIMEOUT_S)
    assert saved["size_bytes"] > 100_000, saved

    record(
        large_model={
            "model": source.name,
            "inpx_bytes": source.stat().st_size,
            "load_s": loaded["elapsed_s"],
            "load_warnings": loaded.get("warnings", []),
            "counts": counts,
            "list_links_s": round(list_s, 2),
            "list_pages": pages,
            "vehicles_at_pause": vehicles["total"],
            "run_wall_s": final["run_wall_s"],
            "polls": polls,
            "slowest_health_s": round(slowest_health, 3),
            "saved_bytes": saved["size_bytes"],
            "job_warnings": final.get("warnings", []),
        }
    )
