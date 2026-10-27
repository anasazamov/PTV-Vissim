"""PLAN 10.3 item 6 - encoding: a network in a non-ASCII directory (Uzbek Latin apostrophe + Cyrillic):
load, save and CSV export work; the policy warns about the non-ASCII path."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import RealSession, copy_model

pytestmark = pytest.mark.vissim

NON_ASCII_DIR = "Samarqand_ko'cha_Самарқанд"
NON_ASCII_STEM = "tarmoq_ўзбек"


def test_non_ascii_path_round_trip(
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = real_session()
    session.connect()
    ascii_counts = session.load(*example_model)["counts"]

    target = session.projects / NON_ASCII_DIR
    model, layout = copy_model(example_model[0], target, NON_ASCII_STEM)
    loaded = session.tool(
        "vissim_load_network",
        {"inpx_path": str(model), **({"layx_path": str(layout)} if layout else {})},
        timeout=660,
    )
    record(
        non_ascii_load={"ok": loaded["ok"], "warnings": loaded.get("warnings"), "error": loaded.get("error")}
    )
    assert loaded["ok"] is True, f"Vissim could not load the non-ASCII path: {loaded}"
    assert any("non-ASCII" in w for w in loaded["warnings"]), loaded["warnings"]
    assert loaded["counts"]["Links"] == ascii_counts["Links"]

    saved_path = target / "saqlangan_ўзбек.inpx"
    saved = session.tool("vissim_save_network", {"save_as": str(saved_path)}, timeout=660)
    record(non_ascii_save={"ok": saved["ok"], "error": saved.get("error")})
    assert saved["ok"] is True, saved
    assert saved_path.is_file() and saved_path.stat().st_size > 0

    csv_path = target / "havolalar_ўзбек.csv"
    exported = session.ok(
        "vissim_export_csv",
        {"collection": "Net.Links", "attributes": ["No", "Name"], "csv_path": str(csv_path)},
    )
    text = csv_path.read_text(encoding="utf-8-sig")
    record(non_ascii_csv={"rows": exported["rows"]})
    assert exported["rows"] == ascii_counts["Links"]
    assert text.splitlines()[0] == "No;Name"
