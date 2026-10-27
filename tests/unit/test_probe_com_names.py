"""Tests of scripts/probe_com_names.py without a real Vissim (PLAN 10.3 item 2 preparation).

The probe runs here against ``FakeVissim`` through the same ``VissimService`` code path it uses for real
Vissim (FakeBackend + FakeProcs instead of pywin32 + winproc). The command-line guard (``--help``, refusal
without ``--i-have-a-com-license``) is checked in a subprocess, which must not import pywin32 at all.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

from service_harness import Harness

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "probe_com_names.py"
COM_NAMES = REPO_ROOT / "docs" / "com-names.md"


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("probe_com_names", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _script_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("VISSIM_", "FAKE_VISSIM_"))}
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONPATH", None)  # neither the fakes nor anything else: the script must not need pywin32
    return env


def _run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_script_env(),
        cwd=str(REPO_ROOT),
        timeout=60,
    )


# --- command line


def test_help_works() -> None:
    result = _run_script("--help")
    assert result.returncode == 0, result.stderr
    assert "--i-have-a-com-license" in result.stdout
    assert "--update" in result.stdout


def test_refuses_without_the_license_flag_and_imports_no_pywin32() -> None:
    code = (
        "import runpy, sys\n"
        f"sys.argv = [{str(SCRIPT)!r}]\n"
        "try:\n"
        f"    runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    rc = exc.code\n"
        "loaded = sorted(m for m in ('pythoncom', 'pywintypes', 'win32com') if m in sys.modules)\n"
        "print('RESULT', rc, loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_script_env(),
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    assert "RESULT 2 []" in result.stdout, result.stdout + result.stderr
    assert "--i-have-a-com-license" in result.stderr
    assert "refused" in result.stderr


def test_refuses_in_test_mode_even_with_the_flag(capsys: pytest.CaptureFixture[str]) -> None:
    # tests/conftest.py sets VISSIM_MCP_FORBID_REAL=1: the probe must not start anything.
    assert probe.main(["--i-have-a-com-license"]) == probe.EXIT_REFUSED
    assert "VISSIM_MCP_FORBID_REAL" in capsys.readouterr().err


def test_refuses_a_bad_break_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("VISSIM_MCP_FORBID_REAL")
    assert (
        probe.main(["--i-have-a-com-license", "--period", "100", "--break-at", "100"]) == probe.EXIT_REFUSED
    )
    assert "--break-at" in capsys.readouterr().err


# --- the probe list mirrors docs/com-names.md


def test_probe_list_mirrors_com_names() -> None:
    text = COM_NAMES.read_text(encoding="utf-8")
    rows = probe.table_rows(text)
    status_rows = {cells[0] for _, cells, col in rows if col is not None and col > 0}
    names = [p.doc_name for p in probe.PROBES]
    assert len(names) == len(set(names)), "duplicate probe names"
    missing_in_doc = [name for name in names if name not in status_rows]
    assert not missing_in_doc, f"probes without a docs/com-names.md row: {missing_in_doc}"
    # Every name row of the three name tables (methods, attributes, configurable values) has a probe.
    name_rows = {
        cells[0]
        for _, cells, col in rows
        if col is not None and col > 0 and cells[0].startswith("`") and cells[0] not in {"`unverified`"}
    }
    assert name_rows - set(names) == set(), f"rows without a probe: {sorted(name_rows - set(names))}"
    for p in probe.PROBES:
        assert p.phase in {"static", "semantics", "close"}


def test_probes_cover_the_m6_questions() -> None:
    names = {name for name, _ in probe.EXPERIMENTS}
    assert {
        "break_at_n",
        "stale_break",
        "break_disable_runs_to_end",
        "single_step_and_stop",
        "signal_while_paused",
        "signal_outside_run",
        "item_by_key_string",
    } <= names
    joined = " ".join(p.doc_name for p in probe.PROBES)
    for name in (
        "IsSimBreakAtActive",
        "IsRunning",
        "KeepPrevResults",
        "QuickMode",
        "SigState",
        "ContrByCOM",
        "RandSeed",
        "SimBreakAt",
        "SimSec",
    ):
        assert name in joined


# --- documents


def _fake_report(statuses: dict[str, str]) -> dict:
    return {
        "started": "2026-11-02T10:00:00+05:00",
        "probes": [
            {
                "name": p.doc_name,
                "status": statuses.get(p.doc_name, probe.SKIPPED),
                "observed": "x",
                "data": {},
            }
            for p in probe.PROBES
        ],
    }


def test_update_com_names_rewrites_only_the_status_cells() -> None:
    text = COM_NAMES.read_text(encoding="utf-8")
    report = _fake_report(
        {
            "`SimPeriod`": probe.OK,
            "`IsSimBreakAtActive`": probe.FAILED,
            "`RandSeed`": probe.OK,
            "`VISSIM_BREAK_DISABLE_VALUE`": probe.OK,
            "`Vissim.Exit()`": probe.OK,
        }
    )
    updated = probe.update_com_names(text, report, "2026-11-02")
    rows = {cells[0]: (cells, col) for _, cells, col in probe.table_rows(updated) if col}
    assert rows["`SimPeriod`"][0][rows["`SimPeriod`"][1]] == "verified"
    assert rows["`IsSimBreakAtActive`"][0][rows["`IsSimBreakAtActive`"][1]] == "rejected"
    assert rows["`RandSeed`"][0][rows["`RandSeed`"][1]] == "verified → configurable"
    assert rows["`VISSIM_BREAK_DISABLE_VALUE`"][0][rows["`VISSIM_BREAK_DISABLE_VALUE`"][1]] == (
        "configurable (verified)"
    )
    assert rows["`Vissim.Exit()`"][0][rows["`Vissim.Exit()`"][1]] == "verified"
    assert rows["`SimRes`"][0][rows["`SimRes`"][1]] == "documented"  # skipped rows keep their status
    assert "2026-11-02 — 5 ta nom `verified`" not in updated  # 4 ok + 1 failed
    assert "2026-11-02 — 4 ta nom `verified`, 1 ta `rejected`" in updated
    assert updated.count(probe.PROBE_MARK_START) == 1
    # Everything outside the probed rows and the marker block is unchanged.
    before = [line for line in text.splitlines() if "probe" not in line and "`SimPeriod`" not in line]
    after_lines = set(updated.splitlines())
    changed = [line for line in before if line not in after_lines]
    assert all(line.startswith("|") for line in changed), changed
    # Idempotent.
    assert probe.update_com_names(updated, report, "2026-11-02") == updated


def test_write_documents_appends_raw_results(tmp_path: Path) -> None:
    com_names = tmp_path / "com-names.md"
    com_names.write_text(COM_NAMES.read_text(encoding="utf-8"), encoding="utf-8")
    test_report = tmp_path / "test-report-v1.md"
    test_report.write_text("# Hisobot\n\nmavjud matn\n", encoding="utf-8")
    report = _fake_report({"`SimSec`": probe.OK})
    probe.write_documents(report, com_names, test_report)
    probe.write_documents(report, com_names, test_report)
    content = test_report.read_text(encoding="utf-8")
    assert content.startswith("# Hisobot\n\nmavjud matn\n")
    assert content.count("## probe_com_names.py xom natijalari") == 2
    assert '"`SimSec`"' in content


# --- the probe against FakeVissim


def _state(tmp_path: Path, **kwargs: object) -> object:
    model_dir = tmp_path / "src_model"
    model_dir.mkdir()
    (model_dir / "COM Basic Commands.inpx").write_text("<network/>", encoding="utf-8")
    (model_dir / "COM Basic Commands.layx").write_text("<layout/>", encoding="utf-8")
    workdir = tmp_path / "work"
    model, layout = probe.prepare_workdir(
        model_dir / "COM Basic Commands.inpx", model_dir / "COM Basic Commands.layx", workdir
    )
    return probe.ProbeState(model=model, layout=layout, workdir=workdir, **kwargs)


def test_probe_runs_against_the_fake(harness: Callable[..., Harness], tmp_path: Path) -> None:
    h = harness()
    st = _state(tmp_path)
    report = asyncio.run(probe.run_probe(h.service, st, log=lambda _m: None))
    assert "aborted" not in report, report.get("aborted")
    by_name = {row["name"]: row for row in report["probes"]}
    assert set(by_name) == {p.doc_name for p in probe.PROBES}
    failed = {name: row["observed"] for name, row in by_name.items() if row["status"] != probe.OK}
    assert failed == {}, failed
    assert report["semantics"]["break_at_n"]["paused_at_N"] is True
    assert report["semantics"]["break_disable_runs_to_end"]["ran_to_end"] is True
    assert report["semantics"]["stale_break"]["outcome"] in {"ran_to_end", "did_not_advance"} or report[
        "semantics"
    ]["stale_break"]["outcome"].startswith("paused_at_")
    assert report["semantics"]["signal_while_paused"]["after_GREEN"]["ContrByCOM"] is True
    assert report["close"]["method"] == "exit"
    assert not h.service.connected
    assert h.procs.alive_pids() == set()  # its own Vissim is gone
    assert (st.workdir / "probe_saved.inpx").is_file()
    assert report["process_after_connect"]["children"] is None  # fake process table: no real children
    json.dumps(report, default=str)  # serialisable for probe-results.json


def test_probe_without_simulation_skips_the_semantics_rows(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    report = asyncio.run(probe.run_probe(h.service, _state(tmp_path, simulate=False), log=lambda _m: None))
    by_name = {row["name"]: row["status"] for row in report["probes"]}
    assert by_name["`VISSIM_BREAK_DISABLE_VALUE`"] == probe.SKIPPED
    assert by_name["`SimPeriod`"] == probe.OK
    assert report["semantics"] == {}


def test_probe_reports_a_failed_start_and_starts_nothing_else(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness(dispatch_error=-2147221005)  # CO_E_CLASSSTRING: COM server not registered
    report = asyncio.run(probe.run_probe(h.service, _state(tmp_path), log=lambda _m: None))
    assert "vissim_connect failed" in report["aborted"]
    assert all(row["status"] == probe.SKIPPED for row in report["probes"])
    assert h.procs.alive_pids() == set()


def test_probe_records_a_missing_attribute_as_failed(harness: Callable[..., Harness], tmp_path: Path) -> None:
    h = harness()
    st = _state(tmp_path)

    async def scenario() -> dict:
        original = probe.PROBES
        try:
            probe.PROBES = (
                *original,
                probe.attribute_probe("`NoSuchAttr`", "Simulation", ["NoSuchAttr"], writeback=False),
            )
            return await probe.run_probe(h.service, st, log=lambda _m: None)
        finally:
            probe.PROBES = original

    report = asyncio.run(scenario())
    row = next(r for r in report["probes"] if r["name"] == "`NoSuchAttr`")
    assert row["status"] == probe.FAILED and "read failed" in row["observed"]
    assert probe.summary_counts(report)[probe.FAILED] == 1
    assert "NoSuchAttr" in probe.format_table(report)


# --- inconclusive outcomes, partial reports, experiment state


def test_error_outcome_needs_evidence_against_the_name() -> None:
    from vissim_mcp.errors import ErrorCode, VissimMcpError

    assert probe.error_outcome(VissimMcpError(ErrorCode.COM_ERROR, "bad name")).status == probe.FAILED
    assert probe.error_outcome(VissimMcpError(ErrorCode.RESOLVE_FAILED, "no such")).status == probe.FAILED
    for code in (
        ErrorCode.TIMEOUT_HUNG,
        ErrorCode.NOT_CONNECTED,
        ErrorCode.SHUTTING_DOWN,
        ErrorCode.INTERNAL,
    ):
        assert probe.error_outcome(VissimMcpError(code, "x")).status == probe.INCONCLUSIVE, code
    python_error = probe.error_outcome(TypeError("'CDispatch' object is not iterable"))
    assert python_error.status == probe.INCONCLUSIVE and "TypeError" in python_error.observed
    hung = probe.experiment_error(VissimMcpError(ErrorCode.TIMEOUT_HUNG, "busy"))
    assert hung["inconclusive"] is True and hung["error_code"] == "TIMEOUT_HUNG"
    assert probe.experiment_error(VissimMcpError(ErrorCode.COM_ERROR, "x"))["inconclusive"] is False


def test_update_leaves_inconclusive_rows_unchanged_and_lists_them() -> None:
    text = COM_NAMES.read_text(encoding="utf-8")
    report = _fake_report({"`SimPeriod`": probe.INCONCLUSIVE, "`SimRes`": probe.OK})
    updated = probe.update_com_names(text, report, "2026-11-02")
    rows = {cells[0]: (cells, col) for _, cells, col in probe.table_rows(updated) if col}
    assert rows["`SimPeriod`"][0][rows["`SimPeriod`"][1]] == "documented"
    assert rows["`SimRes`"][0][rows["`SimRes`"][1]] == "verified"
    assert "1 ta aniq natija bermadi (holati o'zgartirilmadi: `SimPeriod`)" in updated
    assert "1 inconclusive" in probe.report_section(report)
    assert probe.summary_counts(report)[probe.INCONCLUSIVE] == 1


def test_a_python_error_in_a_probe_or_experiment_does_not_lose_the_report(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    st = _state(tmp_path)

    def broken_probe(ctx: object, st: object) -> object:
        return list(ctx.vissim.Net)  # type: ignore[attr-defined]  # a COM object is not iterable

    def broken_experiment(ctx: object, st: object) -> dict:
        raise IndexError("rows[0] of an empty result")

    async def scenario() -> dict:
        probes, experiments = probe.PROBES, probe.EXPERIMENTS
        try:
            probe.PROBES = (probe.Probe("`Broken`", "static", broken_probe), *probes)
            probe.EXPERIMENTS = (
                *experiments[:-1],
                ("broken", broken_experiment),
                experiments[-1],
            )
            return await probe.run_probe(h.service, st, log=lambda _m: None)
        finally:
            probe.PROBES, probe.EXPERIMENTS = probes, experiments

    report = asyncio.run(scenario())
    assert "aborted" not in report, report.get("aborted")
    statuses = {row["name"]: row["status"] for row in report["probes"]}
    assert statuses.pop("`Broken`") == probe.INCONCLUSIVE
    assert set(statuses.values()) == {probe.OK}  # every other row was still probed
    assert report["semantics"]["broken"]["inconclusive"] is True
    assert "IndexError" in report["semantics"]["broken"]["error"]
    assert "restore" in report["semantics"] and report["close"]["method"] == "exit"
    assert h.procs.alive_pids() == set()


def test_timeouts_make_the_semantics_rows_inconclusive_not_rejected() -> None:
    st = probe.ProbeState(model=Path("m.inpx"), layout=None, workdir=Path("."))
    hung = {"error": "TIMEOUT_HUNG: busy", "error_code": "TIMEOUT_HUNG", "inconclusive": True}
    st.semantics = {"break_at_n": hung, "break_disable_runs_to_end": hung, "single_step_and_stop": hung}
    assert probe.probe_run_methods(None, st).status == probe.INCONCLUSIVE
    assert probe.probe_break_disable(None, st).status == probe.INCONCLUSIVE
    com = {"error": "COM_ERROR: x", "error_code": "COM_ERROR", "inconclusive": False}
    st.semantics["break_disable_runs_to_end"] = com
    assert probe.probe_run_methods(None, st).status == probe.FAILED
    assert probe.probe_break_disable(None, st).status == probe.FAILED


def test_exit_is_judged_only_on_a_saved_network() -> None:
    st = probe.ProbeState(model=Path("m.inpx"), layout=None, workdir=Path("."))
    st.close_result = {"closed": True, "method": "terminate", "elapsed_s": 30.0}
    st.info["final_save"] = {"error": "COM_ERROR: disk full", "inconclusive": False}
    assert probe.probe_exit(None, st).status == probe.INCONCLUSIVE
    st.info["final_save"] = {"saved": True}
    assert probe.probe_exit(None, st).status == probe.FAILED
    st.close_result = {"closed": True, "method": "exit", "elapsed_s": 1.0}
    assert probe.probe_exit(None, st).status == probe.OK


def test_the_probe_saves_before_close_and_restores_keep_prev_results(
    harness: Callable[..., Harness], tmp_path: Path
) -> None:
    h = harness()
    st = _state(tmp_path)
    report = asyncio.run(probe.run_probe(h.service, st, log=lambda _m: None))
    assert report["info"]["final_save"]["saved"] is True
    assert (st.workdir / "probe_final.inpx").is_file()
    prepare = report["semantics"]["prepare"]
    assert prepare["KeepPrevResults"] == probe.KEEP_ALL and "KeepPrevResults" in prepare["saved"]
    assert "KeepPrevResults" in report["semantics"]["restore"]


class _StubSimulation:
    """Just enough of Simulation for exp_break_disable: records every write."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {"IsSimBreakAtActive": True, "SimBreakAt": 120, "SimSec": 0.0}
        self.writes: list[tuple[str, object]] = []

    def AttValue(self, name: str) -> object:  # noqa: N802 - COM name
        return self.values.get(name, 0)

    def SetAttValue(self, name: str, value: object) -> None:  # noqa: N802 - COM name
        self.writes.append((name, value))
        self.values[name] = value

    def RunContinuous(self) -> None:  # noqa: N802 - COM name
        self.values["SimSec"] = 0.0

    def Stop(self) -> None:  # noqa: N802 - COM name
        pass


def test_break_disable_is_measured_without_the_forced_is_sim_break_at_active(tmp_path: Path) -> None:
    sim = _StubSimulation()

    class Vissim:
        Simulation = sim

        class Net:
            class SimulationRuns:
                Count = 0

    class Ctx:
        vissim = Vissim

    st = probe.ProbeState(model=tmp_path / "m.inpx", layout=None, workdir=tmp_path)
    st.saved = {"IsSimBreakAtActive": False}
    st.break_active_forced = True
    data = probe.exp_break_disable(Ctx, st)
    assert sim.writes[0] == ("IsSimBreakAtActive", False)  # restored before the break-disable run
    assert data["IsSimBreakAtActive"] is False and data["IsSimBreakAtActive_forced_by_probe"] is False
    assert st.break_active_forced is False
