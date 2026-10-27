"""Probe the Vissim COM names of docs/com-names.md on a REAL Vissim (PLAN 10.3 item 2, 11 row M6).

Needs a Vissim license WITH the COM module: without one, a COM-launched Vissim shows a license dialog (see
docs/preflight-report.md). The script therefore refuses to run unless ``--i-have-a-com-license`` is given.

What it does:

1. Starts its OWN Vissim through the vissim_mcp code paths (``VissimService``: ``Dispatch`` on the COM worker,
   PID detection by the ``-Embedding`` ownership rule, license-dialog watchdog, kill-on-close Job Object).
   An interactive Vissim (opened without ``-Embedding``) is never attached to, closed or killed.
2. Copies the network (default: the official "COM Basic Commands" example) and its layout into a temporary
   working directory and loads the COPY.
3. Probes every name of :data:`PROBES` - one list mirroring the tables of docs/com-names.md - by reading it;
   a value is written back only when that is safe and reversible (the same value is written, then re-read).
4. Runs the semantics experiments that M6 must answer: SimBreakAt=N pauses at N (and the role of
   IsSimBreakAtActive), SigState/ContrByCOM while paused and outside a run, a stale break below SimSec,
   SimBreakAt=VISSIM_BREAK_DISABLE_VALUE runs to the end, SimSec after a run and after Stop(), RunSingleStep,
   ItemByKey with a numeric string key, and whether Vissim's child processes live in the Job Object (a
   process that needs CREATE_BREAKAWAY_FROM_JOB would fail to start inside it).
5. Closes Vissim (Exit() -> wait -> terminate -> kill, only its own process), prints a table and writes the
   raw results as JSON into the working directory. With ``--update`` it also rewrites the status column of
   docs/com-names.md and appends the raw results to docs/test-report-v1.md.

Exit codes: 0 = every probed name works, 1 = some names failed (see the table), 2 = refused (missing flag,
test mode, bad arguments), 3 = the probe could not run (Vissim did not start, the model could not be loaded).

Example (PowerShell, repository root):
    .venv\\Scripts\\python.exe -X utf8 scripts\\probe_com_names.py --i-have-a-com-license
    .venv\\Scripts\\python.exe -X utf8 scripts\\probe_com_names.py --i-have-a-com-license --update
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:  # also works without an editable install
    sys.path.insert(0, str(REPO_ROOT / "src"))

from vissim_mcp.config import ENV_FORBID_REAL, forbid_real_enabled  # noqa: E402
from vissim_mcp.errors import VissimMcpError, describe_exception, is_connection_error  # noqa: E402
from vissim_mcp.resolver import resolve  # noqa: E402

COM_NAMES_MD = REPO_ROOT / "docs" / "com-names.md"
TEST_REPORT_MD = REPO_ROOT / "docs" / "test-report-v1.md"
DEFAULT_MODEL = Path(
    r"C:\Users\Public\Documents\PTV Vision\PTV Vissim 2026\Examples Training\COM\Basic Commands"
    r"\COM Basic Commands.inpx"
)
LICENSE_FLAG = "--i-have-a-com-license"

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_REFUSED = 2
EXIT_FAILED = 3

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
#: probe outcome -> status word in docs/com-names.md (skipped rows keep their status)
DOC_STATUS = {OK: "verified", FAILED: "rejected"}
_STATUS_WORD_RE = re.compile(r"\b(unverified|documented|verified|rejected)\b")
PROBE_MARK_START = "<!-- probe:auto:start -->"
PROBE_MARK_END = "<!-- probe:auto:end -->"
_MAX_TEXT = 300


# --- data


@dataclass
class Outcome:
    """Result of one probe: ``status`` is ok / failed / skipped; ``observed`` is a short human text."""

    status: str
    observed: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeState:
    """Everything the probes share. Mutated only by one probe at a time (on the COM worker thread)."""

    model: Path
    layout: Path | None
    workdir: Path
    period: int = 600
    break_at: int = 120
    break_disable_value: int | float = 0
    seed_attr: str = "RandSeed"
    simulate: bool = True
    first_link_key: Any = None
    sc_key: Any = None
    sg_key: Any = None
    paused_at_break: bool = False
    saved: dict[str, Any] = field(default_factory=dict)
    semantics: dict[str, Any] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    close_result: dict[str, Any] | None = None


ProbeFn = Callable[[Any, ProbeState], Outcome]


@dataclass(frozen=True)
class Probe:
    """One row of docs/com-names.md: ``doc_name`` is the row's first cell, exactly as written there.

    ``phase``: ``static`` (before any simulation), ``semantics`` (after the experiments, which it
    evaluates) or ``close`` (after Vissim was closed; ``fn`` then gets ``ctx=None`` and must not touch COM).
    """

    doc_name: str
    phase: str
    fn: ProbeFn


# --- small helpers (worker thread)


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _try(fn: Callable[[], Any]) -> Any:
    """``fn()`` or ``{"error": ...}``; a lost connection is raised (the probe cannot continue then)."""
    try:
        return fn()
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return {"error": describe_exception(exc, _MAX_TEXT)}


def _failed(value: Any) -> bool:
    return isinstance(value, dict) and "error" in value


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _step(sim: Any) -> float:
    res = _as_float(_try(lambda: sim.AttValue("SimRes"))) or 10.0
    return 1.0 / max(1.0, res)


def _runs_count(vissim: Any) -> Any:
    return _try(lambda: int(vissim.Net.SimulationRuns.Count))


def _last_run(vissim: Any) -> Any:
    def read() -> Any:
        rows = vissim.Net.SimulationRuns.GetMultipleAttributes(("No", "RandSeed", "SimEnd"))
        rows = [tuple(row) for row in rows]
        if not rows:
            return None
        last = max(rows, key=lambda row: row[0])
        return {"No": last[0], "RandSeed": last[1], "SimEnd": last[2]}

    return _try(read)


def _sim_state(vissim: Any) -> dict[str, Any]:
    sim = vissim.Simulation
    return {
        "SimSec": _try(lambda: sim.AttValue("SimSec")),
        "IsRunning": _try(lambda: sim.AttValue("IsRunning")),
        "SimulationRuns.Count": _runs_count(vissim),
    }


def _run_continuous(vissim: Any) -> dict[str, Any]:
    started = time.monotonic()
    vissim.Simulation.RunContinuous()
    return {"wall_s": round(time.monotonic() - started, 2), **_sim_state(vissim)}


def _stop_if_in_run(vissim: Any, runs_before: Any) -> dict[str, Any] | None:
    """Stop() when the current run has not ended (no new SimulationRuns row); returns what was seen."""
    if not _failed(runs_before) and _runs_count(vissim) != runs_before:
        return None
    _try(vissim.Simulation.Stop)
    return _sim_state(vissim)


# --- generic probes


def attribute_probe(
    doc_name: str,
    owner: str | Callable[[ProbeState], str | None],
    attributes: Sequence[str],
    *,
    writeback: bool,
) -> Probe:
    """Read ``attributes`` of the object at ``owner`` (a resolver path); with ``writeback`` write the value
    just read back and read it again. The row is ok when every attribute could be read."""

    def fn(ctx: Any, st: ProbeState) -> Outcome:
        path = owner(st) if callable(owner) else owner
        if path is None:
            return Outcome(SKIPPED, "the model has no such object")
        obj = resolve(ctx.vissim, path)
        parts: list[str] = []
        data: dict[str, Any] = {"path": path}
        status = OK
        for name in attributes:
            value = _try(lambda name=name: obj.AttValue(name))
            data[name] = value
            if _failed(value):
                status = FAILED
                parts.append(f"{name}: read failed: {value['error']}")
                continue
            text = f"{name}={_short(value)}"
            if writeback:
                written = _try(lambda name=name, value=value: obj.SetAttValue(name, value))
                again = _try(lambda name=name: obj.AttValue(name))
                if _failed(written):
                    text += f" (write-back failed: {written['error']})"
                elif again != value:
                    text += f" (write-back read {_short(again)})"
                else:
                    text += " (write-back ok)"
                data[f"{name}.writeback"] = {"write": written, "read_again": again}
            parts.append(text)
        return Outcome(status, "; ".join(parts), data)

    return Probe(doc_name, "static", fn)


def _sg_path(st: ProbeState) -> str | None:
    if st.sc_key is None or st.sg_key is None:
        return None
    return f"Net.SignalControllers[{_key_text(st.sc_key)}].SGs[{_key_text(st.sg_key)}]"


def _key_text(key: Any) -> str:
    return str(key) if isinstance(key, int) else json.dumps(str(key))


# --- method probes (static phase)


def probe_load_net(ctx: Any, st: ProbeState) -> Outcome:
    load = st.info.get("load", {})
    if load.get("LoadNet") == OK:
        return Outcome(
            OK, f"loaded {st.model.name} in {load.get('load_s')} s; Links.Count={load.get('links')}"
        )
    return Outcome(FAILED, f"LoadNet failed: {load.get('LoadNet_error')}")


def probe_load_layout(ctx: Any, st: ProbeState) -> Outcome:
    load = st.info.get("load", {})
    if st.layout is None:
        return Outcome(SKIPPED, "no .layx next to the model")
    if load.get("LoadLayout") == OK:
        return Outcome(OK, f"loaded {st.layout.name}")
    return Outcome(FAILED, f"LoadLayout failed: {load.get('LoadLayout_error')}")


def probe_save_net_as(ctx: Any, st: ProbeState) -> Outcome:
    target = st.workdir / "probe_saved.inpx"
    ctx.vissim.SaveNetAs(str(target))
    exists = target.is_file()
    size = target.stat().st_size if exists else None
    status = OK if exists and size else FAILED
    return Outcome(status, f"SaveNetAs -> file exists={exists}, size={size}", {"path": str(target)})


def probe_gui_update(ctx: Any, st: ProbeState) -> Outcome:
    ctx.vissim.SuspendUpdateGUI()
    ctx.vissim.ResumeUpdateGUI(True)
    return Outcome(OK, "SuspendUpdateGUI() and ResumeUpdateGUI(True) returned")


def probe_roots(ctx: Any, st: ProbeState) -> Outcome:
    found = {
        name: _try(lambda name=name: getattr(ctx.vissim, name) is not None)
        for name in ("Net", "Simulation", "Evaluation", "Graphics")
    }
    status = OK if all(value is True for value in found.values()) else FAILED
    st.info["version"] = _try(lambda: ctx.vissim.AttValue("Version"))
    return Outcome(status, ", ".join(f"{k}={v}" for k, v in found.items()), found)


def probe_network_window(ctx: Any, st: ProbeState) -> Outcome:
    window = ctx.vissim.Graphics.CurrentNetworkWindow
    return Outcome(
        OK if window is not None else FAILED, f"CurrentNetworkWindow={'object' if window else None}"
    )


def probe_collection_basics(ctx: Any, st: ProbeState) -> Outcome:
    links = ctx.vissim.Net.Links
    count = int(links.Count)
    if st.first_link_key is None:
        return Outcome(FAILED, f"Count={count}, but no link key could be read")
    key = st.first_link_key
    exists = links.ItemKeyExists(key)
    missing = _try(lambda: links.ItemKeyExists(987654321))
    number = links.ItemByKey(key).AttValue("No")
    ok = bool(exists) and number == key and missing is False
    return Outcome(
        OK if ok else FAILED,
        f"Links.Count={count}; ItemKeyExists({key})={exists}; ItemKeyExists(987654321)={_short(missing)}; "
        f"ItemByKey({key}).No={number}",
    )


INFO_COLLECTIONS = (
    "Links",
    "Nodes",
    "SignalControllers",
    "SignalHeads",
    "VehicleInputs",
    "VehicleRoutingDecisionsStatic",
    "DataCollectionMeasurements",
    "VehicleTravelTimeMeasurements",
    "QueueCounters",
)


def probe_net_collections(ctx: Any, st: ProbeState) -> Outcome:
    counts = {
        name: _try(lambda name=name: int(getattr(ctx.vissim.Net, name).Count)) for name in INFO_COLLECTIONS
    }
    bad = [name for name, value in counts.items() if _failed(value)]
    text = ", ".join(f"{k}={'ERR' if _failed(v) else v}" for k, v in counts.items())
    return Outcome(FAILED if bad else OK, text + (f"; failed: {bad}" if bad else ""), counts)


def probe_get_multiple(ctx: Any, st: ProbeState) -> Outcome:
    rows = ctx.vissim.Net.Links.GetMultipleAttributes(("No", "Name"))
    rows = [tuple(row) for row in rows]
    shape_ok = all(len(row) == 2 for row in rows)
    return Outcome(
        OK if rows and shape_ok else FAILED,
        f"{len(rows)} rows of (No, Name); first={_short(rows[0]) if rows else None}",
    )


def probe_att_value(ctx: Any, st: ProbeState) -> Outcome:
    if st.first_link_key is None:
        return Outcome(SKIPPED, "no link in the model")
    link = ctx.vissim.Net.Links.ItemByKey(st.first_link_key)
    name = link.AttValue("Name")
    link.SetAttValue("Name", name)
    again = link.AttValue("Name")
    return Outcome(
        OK if again == name else FAILED,
        f"Link {st.first_link_key} Name={_short(name)}; write-back read {_short(again)}",
    )


def probe_attributes_getall(ctx: Any, st: ProbeState) -> Outcome:
    infos = ctx.vissim.Net.Links.Attributes.GetAll()
    infos = list(infos)
    if not infos:
        return Outcome(FAILED, "Attributes.GetAll() returned nothing")
    first = infos[0]
    sample = {
        name: _try(lambda name=name: getattr(first, name))
        for name in ("Code", "Name", "ValueType", "Editable")
    }
    bad = [k for k, v in sample.items() if _failed(v)]
    return Outcome(FAILED if bad else OK, f"{len(infos)} attributes; first: {_short(sample)}", sample)


def probe_type_info(ctx: Any, st: ProbeState) -> Outcome:
    members = ctx.backend.type_info_members(ctx.vissim.Simulation)
    names = {m["name"] for m in members}
    expected = {"AttValue", "SetAttValue", "RunContinuous"}
    missing = sorted(expected - names)
    return Outcome(
        FAILED if missing else OK,
        f"Simulation: {len(members)} members"
        + (f"; missing {missing}" if missing else "; AttValue, SetAttValue, RunContinuous present"),
    )


def probe_sgs(ctx: Any, st: ProbeState) -> Outcome:
    path = _sg_path(st)
    if path is None:
        return Outcome(SKIPPED, "the model has no signal controller with a signal group")
    sg = resolve(ctx.vissim, path)
    return Outcome(OK, f"{path} -> No={sg.AttValue('No')}")


# --- semantics-phase probes (evaluate st.semantics)


def probe_run_methods(ctx: Any, st: ProbeState) -> Outcome:
    if not st.simulate:
        return Outcome(SKIPPED, "--no-simulation")
    sem = st.semantics
    errors = {
        k: v["error"]
        for k, v in sem.items()
        if isinstance(v, dict)
        and "error" in v
        and k in ("break_at_n", "break_disable_runs_to_end", "single_step_and_stop")
    }
    if errors:
        return Outcome(FAILED, f"experiments failed: {_short(errors)}")
    step = sem.get("single_step_and_stop", {})
    return Outcome(
        OK,
        f"RunContinuous/RunSingleStep/Stop returned; after one step SimSec="
        f"{_short(step.get('after_step', {}).get('SimSec'))}",
    )


def probe_simulation_runs(ctx: Any, st: ProbeState) -> Outcome:
    rows = ctx.vissim.Net.SimulationRuns.GetMultipleAttributes(("No", "RandSeed", "SimEnd"))
    rows = [tuple(row) for row in rows]
    return Outcome(OK, f"{len(rows)} runs; last={_short(rows[-1]) if rows else None}", {"rows": rows[-10:]})


def probe_break_disable(ctx: Any, st: ProbeState) -> Outcome:
    if not st.simulate:
        return Outcome(SKIPPED, "--no-simulation")
    result = st.semantics.get("break_disable_runs_to_end") or {}
    if "error" in result:
        return Outcome(FAILED, f"experiment failed: {result['error']}")
    ran = result.get("ran_to_end")
    return Outcome(
        OK if ran else FAILED,
        f"SimBreakAt={st.break_disable_value} then RunContinuous: ran_to_end={ran}, "
        f"SimSec after the run={_short(result.get('after', {}).get('SimSec'))}",
    )


def probe_seed_attr(ctx: Any, st: ProbeState) -> Outcome:
    sim = ctx.vissim.Simulation
    value = sim.AttValue(st.seed_attr)
    sim.SetAttValue(st.seed_attr, value)
    again = sim.AttValue(st.seed_attr)
    return Outcome(
        OK if again == value else FAILED, f"{st.seed_attr}={_short(value)} (write-back read {_short(again)})"
    )


# --- close-phase probe


def probe_exit(ctx: Any, st: ProbeState) -> Outcome:
    result = st.close_result
    if result is None:
        return Outcome(SKIPPED, "Vissim was not closed through the service")
    if "error" in result:
        return Outcome(FAILED, f"vissim_close failed: {result['error']}")
    method = result.get("method")
    text = f"closed={result.get('closed')}, method={method}, {result.get('elapsed_s')} s"
    if result.get("warnings"):
        text += f"; warnings={_short(result['warnings'])}"
    return Outcome(OK if method == "exit" else FAILED, text, dict(result))


#: One entry per row of docs/com-names.md (first cell exactly as written there), in table order.
PROBES: tuple[Probe, ...] = (
    # Metodlar va xossalar
    Probe("`Vissim.LoadNet(path, additive)`", "static", probe_load_net),
    Probe("`Vissim.LoadLayout(path)`", "static", probe_load_layout),
    Probe("`Vissim.SaveNetAs(path)`", "static", probe_save_net_as),
    Probe("`Vissim.Exit()`", "close", probe_exit),
    Probe("`Vissim.SuspendUpdateGUI()` / `ResumeUpdateGUI(bool)`", "static", probe_gui_update),
    Probe("`Vissim.Net`, `.Simulation`, `.Evaluation`, `.Graphics`", "static", probe_roots),
    Probe("`Graphics.CurrentNetworkWindow`", "static", probe_network_window),
    Probe("`Simulation.RunContinuous()` / `RunSingleStep()` / `Stop()`", "semantics", probe_run_methods),
    Probe(
        "`<collection>.Count`, `.ItemByKey(key)`, `.ItemKeyExists(key)`", "static", probe_collection_basics
    ),
    Probe(
        "`Net.Links`, `.Nodes`, `.SignalControllers`, `.SignalHeads`, `.VehicleInputs`, "
        "`.VehicleRoutingDecisionsStatic`, `.DataCollectionMeasurements`, `.VehicleTravelTimeMeasurements`, "
        "`.QueueCounters`",
        "static",
        probe_net_collections,
    ),
    Probe("`<collection>.GetMultipleAttributes(tuple)`", "static", probe_get_multiple),
    Probe("`<obj>.AttValue(name)` / `.SetAttValue(name, value)`", "static", probe_att_value),
    Probe(
        "`<collection>.Attributes.GetAll()` → `IAttribute.Code/Name/ValueType/Editable`",
        "static",
        probe_attributes_getall,
    ),
    Probe(
        "`_oleobj_.GetTypeInfo()` → `GetTypeAttr().cFuncs` → `GetFuncDesc(i)` → `GetNames(memid)`",
        "static",
        probe_type_info,
    ),
    Probe("`Net.SignalControllers[n].SGs[m]`", "static", probe_sgs),
    Probe(
        "`Net.SimulationRuns` (`GetMultipleAttributes(('No','RandSeed','SimEnd'))`)",
        "semantics",
        probe_simulation_runs,
    ),
    # Atributlar
    attribute_probe("`SimPeriod`", "Simulation", ["SimPeriod"], writeback=True),
    attribute_probe("`SimRes`", "Simulation", ["SimRes"], writeback=True),
    attribute_probe("`SimSec`", "Simulation", ["SimSec"], writeback=False),
    attribute_probe("`SimBreakAt`", "Simulation", ["SimBreakAt"], writeback=True),
    attribute_probe("`RandSeed`", "Simulation", ["RandSeed"], writeback=True),
    attribute_probe("`UseMaxSimSpeed`", "Simulation", ["UseMaxSimSpeed"], writeback=True),
    attribute_probe("`NumCores`", "Simulation", ["NumCores", "UseAllCores"], writeback=False),
    attribute_probe("`IsRunning`", "Simulation", ["IsRunning"], writeback=False),
    attribute_probe("`IsSimBreakAtActive`", "Simulation", ["IsSimBreakAtActive"], writeback=False),
    attribute_probe("`NumRuns`, `RandSeedIncr`", "Simulation", ["NumRuns", "RandSeedIncr"], writeback=True),
    attribute_probe("`QuickMode`", "Graphics.CurrentNetworkWindow", ["QuickMode"], writeback=True),
    attribute_probe("`KeepPrevResults`", "Evaluation", ["KeepPrevResults"], writeback=True),
    attribute_probe("`SigState`", _sg_path, ["SigState"], writeback=False),
    attribute_probe("`ContrByCOM`", _sg_path, ["ContrByCOM"], writeback=False),
    # Sozlanadigan qiymatlar
    Probe("`VISSIM_SEED_ATTR`", "semantics", probe_seed_attr),
    Probe("`VISSIM_BREAK_DISABLE_VALUE`", "semantics", probe_break_disable),
)


# --- discovery and semantics experiments (worker thread)


def load_model(ctx: Any, st: ProbeState) -> dict[str, Any]:
    vissim = ctx.vissim
    result: dict[str, Any] = {}
    started = time.monotonic()
    try:
        vissim.LoadNet(str(st.model), False)
    except Exception as exc:
        if is_connection_error(exc):
            raise
        result.update(LoadNet=FAILED, LoadNet_error=describe_exception(exc, _MAX_TEXT))
        return result
    result.update(LoadNet=OK, load_s=round(time.monotonic() - started, 2))
    if st.layout is not None:
        layout = _try(lambda: vissim.LoadLayout(str(st.layout)))
        result["LoadLayout"] = FAILED if _failed(layout) else OK
        if _failed(layout):
            result["LoadLayout_error"] = layout["error"]
    result["links"] = _try(lambda: int(vissim.Net.Links.Count))
    return result


def discover_keys(ctx: Any, st: ProbeState) -> dict[str, Any]:
    net = ctx.vissim.Net

    def first_key(collection: Any) -> Any:
        rows = [tuple(row) for row in collection.GetMultipleAttributes(("No",))]
        return min(row[0] for row in rows) if rows else None

    st.first_link_key = _try(lambda: first_key(net.Links))
    st.sc_key = _try(lambda: first_key(net.SignalControllers))
    for name in ("first_link_key", "sc_key"):
        if _failed(getattr(st, name)):
            setattr(st, name, None)
    if st.sc_key is not None:
        sg_key = _try(lambda: first_key(net.SignalControllers.ItemByKey(st.sc_key).SGs))
        st.sg_key = None if _failed(sg_key) else sg_key
    return {"first_link_key": st.first_link_key, "sc_key": st.sc_key, "sg_key": st.sg_key}


def exp_prepare(ctx: Any, st: ProbeState) -> dict[str, Any]:
    vissim = ctx.vissim
    sim = vissim.Simulation
    st.saved = {
        name: _try(lambda name=name: sim.AttValue(name))
        for name in ("SimPeriod", "SimBreakAt", "UseMaxSimSpeed", "IsSimBreakAtActive")
    }
    st.saved["QuickMode"] = _try(lambda: vissim.Graphics.CurrentNetworkWindow.AttValue("QuickMode"))
    _try(sim.Stop)  # no simulation from an earlier session may be pending
    sim.SetAttValue("SimPeriod", st.period)
    sim.SetAttValue("UseMaxSimSpeed", True)
    _try(lambda: vissim.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", 1))
    return {"saved": dict(st.saved), "idle": _sim_state(vissim)}


def exp_signal_outside_run(ctx: Any, st: ProbeState) -> dict[str, Any]:
    """SigState/ContrByCOM written while no simulation runs (M5 open issue); ContrByCOM is reset after."""
    path = _sg_path(st)
    if path is None:
        return {"skipped": "no signal group"}
    sg = resolve(ctx.vissim, path)
    before = {
        "SigState": _try(lambda: sg.AttValue("SigState")),
        "ContrByCOM": _try(lambda: sg.AttValue("ContrByCOM")),
    }
    written = _try(lambda: sg.SetAttValue("SigState", "RED"))
    after = {
        "SigState": _try(lambda: sg.AttValue("SigState")),
        "ContrByCOM": _try(lambda: sg.AttValue("ContrByCOM")),
    }
    released = _try(lambda: sg.SetAttValue("ContrByCOM", False))
    return {
        "path": path,
        "before": before,
        "write_RED": written,
        "after": after,
        "release": released,
        "after_release": {"ContrByCOM": _try(lambda: sg.AttValue("ContrByCOM"))},
    }


def exp_break_at_n(ctx: Any, st: ProbeState) -> dict[str, Any]:
    """SimBreakAt=N, RunContinuous -> does Vissim pause at N? Retries once with IsSimBreakAtActive=True."""
    vissim = ctx.vissim
    sim = vissim.Simulation
    step = _step(sim)
    data: dict[str, Any] = {"N": st.break_at, "time_step": step}
    data["IsSimBreakAtActive_before"] = _try(lambda: sim.AttValue("IsSimBreakAtActive"))
    sim.SetAttValue("SimBreakAt", st.break_at)
    data["IsSimBreakAtActive_after_set"] = _try(lambda: sim.AttValue("IsSimBreakAtActive"))
    runs_before = _runs_count(vissim)
    data["run"] = _run_continuous(vissim)
    simsec = _as_float(data["run"]["SimSec"])
    paused = simsec is not None and abs(simsec - st.break_at) <= step + 1e-6
    data["paused_at_N"] = paused
    if not paused and data["IsSimBreakAtActive_after_set"] is False:
        data["stopped_before_retry"] = _stop_if_in_run(vissim, runs_before)
        data["retry_set_IsSimBreakAtActive"] = _try(lambda: sim.SetAttValue("IsSimBreakAtActive", True))
        sim.SetAttValue("SimBreakAt", st.break_at)
        runs_before = _runs_count(vissim)
        data["retry_run"] = _run_continuous(vissim)
        simsec = _as_float(data["retry_run"]["SimSec"])
        paused = simsec is not None and abs(simsec - st.break_at) <= step + 1e-6
        data["paused_at_N_with_IsSimBreakAtActive"] = paused
    st.paused_at_break = paused
    data["runs_before"] = runs_before
    return data


def exp_signal_while_paused(ctx: Any, st: ProbeState) -> dict[str, Any]:
    """SigState "GREEN" (string) and 1 (int) while paused; ContrByCOM read back; release with False."""
    path = _sg_path(st)
    if path is None:
        return {"skipped": "no signal group"}
    if not st.paused_at_break:
        return {"skipped": "the simulation did not pause at SimBreakAt"}
    sg = resolve(ctx.vissim, path)

    def read() -> dict[str, Any]:
        return {
            "SigState": _try(lambda: sg.AttValue("SigState")),
            "ContrByCOM": _try(lambda: sg.AttValue("ContrByCOM")),
        }

    data: dict[str, Any] = {"path": path, "before": read()}
    data["write_GREEN"] = _try(lambda: sg.SetAttValue("SigState", "GREEN"))
    data["after_GREEN"] = read()
    data["write_int_1"] = _try(lambda: sg.SetAttValue("SigState", 1))
    data["after_int_1"] = read()
    data["release"] = _try(lambda: sg.SetAttValue("ContrByCOM", False))
    data["after_release"] = read()
    return data


def exp_stale_break(ctx: Any, st: ProbeState) -> dict[str, Any]:
    """While paused at N: SimBreakAt = N/2 (already passed), RunContinuous -> what happens?"""
    if not st.paused_at_break:
        return {"skipped": "the simulation did not pause at SimBreakAt"}
    vissim = ctx.vissim
    sim = vissim.Simulation
    stale = max(1, st.break_at // 2)
    runs_before = _runs_count(vissim)
    sim.SetAttValue("SimBreakAt", stale)
    data: dict[str, Any] = {"stale_break": stale, "simsec_before": st.break_at, "runs_before": runs_before}
    data["run"] = _run_continuous(vissim)
    runs_after = data["run"]["SimulationRuns.Count"]
    simsec = _as_float(data["run"]["SimSec"])
    if not _failed(runs_before) and runs_after == runs_before + 1:
        data["outcome"] = "ran_to_end"
    elif simsec is not None and abs(simsec - st.break_at) <= _step(sim) + 1e-6:
        data["outcome"] = "did_not_advance"
    else:
        data["outcome"] = f"paused_at_{simsec}"
    data["stop"] = _stop_if_in_run(vissim, runs_before)
    return data


def exp_break_disable(ctx: Any, st: ProbeState) -> dict[str, Any]:
    """A fresh run with SimBreakAt = VISSIM_BREAK_DISABLE_VALUE: does RunContinuous run to the end?"""
    vissim = ctx.vissim
    sim = vissim.Simulation
    runs_before = _runs_count(vissim)
    sim.SetAttValue("SimBreakAt", st.break_disable_value)
    data: dict[str, Any] = {
        "value": st.break_disable_value,
        "runs_before": runs_before,
        "SimBreakAt_read_back": _try(lambda: sim.AttValue("SimBreakAt")),
    }
    data["after"] = _run_continuous(vissim)
    data["last_run"] = _last_run(vissim)
    runs_after = data["after"]["SimulationRuns.Count"]
    sim_end = (
        _as_float((data["last_run"] or {}).get("SimEnd")) if isinstance(data["last_run"], dict) else None
    )
    new_row = not _failed(runs_before) and runs_after == runs_before + 1
    data["ran_to_end"] = bool(new_row and (sim_end is None or abs(sim_end - st.period) <= 1.0))
    if not data["ran_to_end"]:
        data["stop"] = _stop_if_in_run(vissim, runs_before)
    return data


def exp_single_step_and_stop(ctx: Any, st: ProbeState) -> dict[str, Any]:
    vissim = ctx.vissim
    sim = vissim.Simulation
    runs_before = _runs_count(vissim)
    sim.RunSingleStep()
    data: dict[str, Any] = {"time_step": _step(sim), "after_step": _sim_state(vissim)}
    sim.Stop()
    data["after_stop"] = _sim_state(vissim)
    data["runs_before"] = runs_before
    return data


def exp_item_by_key_string(ctx: Any, st: ProbeState) -> dict[str, Any]:
    if st.first_link_key is None:
        return {"skipped": "no link"}
    links = ctx.vissim.Net.Links
    key = st.first_link_key
    return {
        "key": key,
        "ItemKeyExists(str)": _try(lambda: links.ItemKeyExists(str(key))),
        "ItemByKey(str).No": _try(lambda: links.ItemByKey(str(key)).AttValue("No")),
    }


def exp_restore(ctx: Any, st: ProbeState) -> dict[str, Any]:
    vissim = ctx.vissim
    sim = vissim.Simulation
    _try(sim.Stop)
    done: dict[str, Any] = {}
    for name in ("SimPeriod", "SimBreakAt", "UseMaxSimSpeed", "IsSimBreakAtActive"):
        value = st.saved.get(name)
        if value is not None and not _failed(value):
            done[name] = _try(lambda name=name, value=value: sim.SetAttValue(name, value))
    quick = st.saved.get("QuickMode")
    if quick is not None and not _failed(quick):
        done["QuickMode"] = _try(lambda: vissim.Graphics.CurrentNetworkWindow.SetAttValue("QuickMode", quick))
    done["ResumeUpdateGUI"] = _try(lambda: vissim.ResumeUpdateGUI(True))
    return done


EXPERIMENTS: tuple[tuple[str, Callable[[Any, ProbeState], dict[str, Any]]], ...] = (
    ("prepare", exp_prepare),
    ("signal_outside_run", exp_signal_outside_run),
    ("break_at_n", exp_break_at_n),
    ("signal_while_paused", exp_signal_while_paused),
    ("stale_break", exp_stale_break),
    ("break_disable_runs_to_end", exp_break_disable),
    ("single_step_and_stop", exp_single_step_and_stop),
    ("item_by_key_string", exp_item_by_key_string),
    ("restore", exp_restore),
)


# --- process observations (event loop thread; never COM)


def process_report(service: Any) -> dict[str, Any]:
    """Vissim's PID, Job Object state and its child processes (and whether each is inside the job).

    A child outside the job was started with CREATE_BREAKAWAY_FROM_JOB or by another process (e.g. DCOM).
    Only read for the real process table; the fake one of the unit tests has no real children.
    """
    from vissim_mcp import winproc

    data: dict[str, Any] = {"pid": service.pid, "job_object": service.job_object_active}
    if service.pid is None or service.procs is not winproc:
        data["children"] = None
        return data
    try:
        import psutil

        job = getattr(service, "_job_object", None)
        children = []
        for child in psutil.Process(service.pid).children(recursive=True):
            try:
                name = child.name()
            except psutil.Error:
                name = None
            children.append(
                {
                    "pid": child.pid,
                    "name": name,
                    "in_job": job.contains(child.pid) if job is not None else None,
                }
            )
        data["children"] = children
        data["vissim_in_job"] = job.contains(service.pid) if job is not None else None
    except Exception as exc:
        data["children_error"] = describe_exception(exc, _MAX_TEXT)
    return data


# --- the run


class ProbeAborted(Exception):
    """The probe could not run at all (Vissim did not start, the model did not load)."""


async def run_probe(service: Any, st: ProbeState, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Connect, load the model copy, run every probe and experiment, close Vissim; returns the raw report.

    Never leaves its Vissim running: on any error the service is shut down (kill of its own process only).
    """
    report: dict[str, Any] = {
        "started": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "model": str(st.model),
        "layout": str(st.layout) if st.layout else None,
        "workdir": str(st.workdir),
        "settings": {
            "period": st.period,
            "break_at": st.break_at,
            "break_disable_value": st.break_disable_value,
            "seed_attr": st.seed_attr,
        },
    }
    outcomes: dict[str, Outcome] = {}
    timeout = service.settings.call_timeout_s
    try:
        log("starting Vissim through COM (this can take a minute) ...")
        try:
            report["connect"] = await service.connect()
        except VissimMcpError as err:
            raise ProbeAborted(f"vissim_connect failed: {err.code}: {err.message}") from err
        log(f"connected: PID {report['connect']['pid']}, job object {report['connect']['job_object']}")
        report["process_after_connect"] = process_report(service)

        async def call(fn: Callable[[Any, ProbeState], Any], name: str, timeout_s: float = timeout) -> Any:
            return await service.call(lambda ctx: fn(ctx, st), timeout_s=timeout_s, task_name=name)

        load = await call(load_model, "probe: load model", service.settings.load_timeout_s)
        st.info["load"] = load
        if load.get("LoadNet") != OK:
            raise ProbeAborted(f"loading {st.model} failed: {load.get('LoadNet_error')}")
        report["keys"] = await call(discover_keys, "probe: discover keys")

        async def run_phase(phase: str) -> None:
            for probe in PROBES:
                if probe.phase != phase:
                    continue
                if phase == "close":  # Vissim is gone: these only evaluate what was recorded
                    outcomes[probe.doc_name] = probe.fn(None, st)
                    continue
                try:
                    outcomes[probe.doc_name] = await call(probe.fn, f"probe: {probe.doc_name[:40]}")
                except VissimMcpError as err:
                    outcomes[probe.doc_name] = Outcome(FAILED, f"{err.code}: {err.message}"[:_MAX_TEXT])
                    if not service.connected:
                        raise ProbeAborted(f"the connection to Vissim was lost: {err.message}") from err

        await run_phase("static")
        if st.simulate:
            for name, experiment in EXPERIMENTS:
                log(f"experiment: {name}")
                try:
                    st.semantics[name] = await call(experiment, f"probe: {name}")
                except VissimMcpError as err:
                    st.semantics[name] = {"error": f"{err.code}: {err.message}"[:_MAX_TEXT]}
                    if not service.connected:
                        raise ProbeAborted(f"the connection to Vissim was lost: {err.message}") from err
        await run_phase("semantics")
        report["process_after_runs"] = process_report(service)

        started = time.monotonic()
        try:
            close = await service.close()
            st.close_result = {**close, "elapsed_s": round(time.monotonic() - started, 1)}
        except VissimMcpError as err:
            st.close_result = {"error": f"{err.code}: {err.message}"}
        await run_phase("close")
    except ProbeAborted as exc:
        report["aborted"] = str(exc)
    finally:
        if service.connected or service.pid is not None:
            service.shutdown_fast()  # kills only the Vissim this service started
    for probe in PROBES:
        outcomes.setdefault(probe.doc_name, Outcome(SKIPPED, "not reached"))
    report["probes"] = [{"name": p.doc_name, **asdict(outcomes[p.doc_name])} for p in PROBES]
    report["semantics"] = st.semantics
    report["info"] = {k: v for k, v in st.info.items()}
    report["close"] = st.close_result
    report["finished"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return report


# --- output and documents


def format_table(report: dict[str, Any]) -> str:
    rows = report.get("probes", [])
    width = min(70, max((len(r["name"]) for r in rows), default=10))
    lines = [f"{'name':<{width}}  {'status':<7}  observed", f"{'-' * width}  {'-' * 7}  {'-' * 40}"]
    for row in rows:
        name = row["name"] if len(row["name"]) <= width else row["name"][: width - 3] + "..."
        lines.append(f"{name:<{width}}  {row['status']:<7}  {row['observed']}")
    if report.get("aborted"):
        lines.append(f"\nABORTED: {report['aborted']}")
    return "\n".join(lines)


def summary_counts(report: dict[str, Any]) -> dict[str, int]:
    counts = {OK: 0, FAILED: 0, SKIPPED: 0}
    for row in report.get("probes", []):
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _split_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")) or len(stripped) < 2:
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _row_cells(line: str) -> list[str] | None:
    """Cells of a Markdown table row; ``|`` inside backticks does not split (com-names.md has none)."""
    return _split_row(line)


def table_rows(text: str) -> list[tuple[int, list[str], int | None]]:
    """``(line index, cells, index of the "Holat" column or None)`` for every body row of every table."""
    result: list[tuple[int, list[str], int | None]] = []
    lines = text.splitlines()
    status_col: int | None = None
    in_table = False
    for index, line in enumerate(lines):
        cells = _row_cells(line)
        if cells is None:
            in_table = False
            status_col = None
            continue
        if not in_table:
            in_table = True
            status_col = cells.index("Holat") if "Holat" in cells else None
            continue
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        result.append((index, cells, status_col))
    return result


def new_status_cell(cell: str, outcome_status: str) -> str:
    word = DOC_STATUS.get(outcome_status)
    if word is None:
        return cell
    if _STATUS_WORD_RE.search(cell):
        return _STATUS_WORD_RE.sub(word, cell, count=1)
    return word


def update_com_names(text: str, report: dict[str, Any], date: str) -> str:
    """Rewrite the status cells of the probed rows and the summary between the probe markers."""
    by_name = {row["name"]: row for row in report.get("probes", [])}
    lines = text.splitlines()
    for index, cells, status_col in table_rows(text):
        if status_col is None or not cells:
            continue
        row = by_name.get(cells[0])
        if row is None:
            continue
        cells = list(cells)
        cells[status_col] = new_status_cell(cells[status_col], row["status"])
        lines[index] = "| " + " | ".join(cells) + " |"
    counts = summary_counts(report)
    summary = (
        f"{PROBE_MARK_START}\n"
        f"> Oxirgi `scripts/probe_com_names.py` ishga tushirilishi: {date} — {counts[OK]} ta nom `verified`, "
        f"{counts[FAILED]} ta `rejected`, {counts[SKIPPED]} ta tekshirilmadi. Xom natijalar: "
        f"docs/test-report-v1.md.\n{PROBE_MARK_END}"
    )
    result = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    pattern = re.compile(re.escape(PROBE_MARK_START) + r".*?" + re.escape(PROBE_MARK_END), re.DOTALL)
    if pattern.search(result):
        result = pattern.sub(lambda _m: summary, result, count=1)
    else:
        result = result.rstrip("\n") + "\n\n" + summary + "\n"
    return result


def report_section(report: dict[str, Any]) -> str:
    counts = summary_counts(report)
    raw = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    return (
        f"\n## probe_com_names.py xom natijalari — {report.get('started')}\n\n"
        f"Natija: {counts[OK]} ok, {counts[FAILED]} failed, {counts[SKIPPED]} skipped"
        + (f"; TO'XTATILDI: {report['aborted']}" if report.get("aborted") else "")
        + "\n\n```text\n"
        + format_table(report)
        + "\n```\n\n"
        "<details><summary>JSON</summary>\n\n```json\n" + raw + "\n```\n\n</details>\n"
    )


def write_documents(
    report: dict[str, Any], com_names: Path = COM_NAMES_MD, test_report: Path = TEST_REPORT_MD
) -> None:
    date = str(report.get("started", ""))[:10]
    text = com_names.read_text(encoding="utf-8")
    com_names.write_text(update_com_names(text, report, date), encoding="utf-8", newline="\n")
    existing = test_report.read_text(encoding="utf-8") if test_report.exists() else "# Test hisoboti v1\n"
    test_report.write_text(
        existing.rstrip("\n") + "\n" + report_section(report), encoding="utf-8", newline="\n"
    )


# --- command line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="probe_com_names.py",
        description="Probe the COM names of docs/com-names.md on a real PTV Vissim (needs a license with the "
        "COM module). Starts its own Vissim, never touches an interactive one.",
    )
    parser.add_argument(
        LICENSE_FLAG,
        dest="licensed",
        action="store_true",
        help="required: confirms that a Vissim license WITH the COM module is active",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL,
        help="network to copy and load (default: the COM Basic Commands example)",
    )
    parser.add_argument(
        "--layout",
        type=Path,
        default=None,
        help="layout file (default: the .layx with the model's name, if it exists)",
    )
    parser.add_argument("--progid", default=None, help="COM ProgID (default VISSIM_PROGID or Vissim.Vissim)")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="working directory for the model copy, logs and results (default: a new temp dir)",
    )
    parser.add_argument("--period", type=int, default=600, help="simulation period of the experiments (s)")
    parser.add_argument("--break-at", type=int, default=120, help="SimBreakAt of the break experiment (s)")
    parser.add_argument("--no-simulation", action="store_true", help="only read/write names, run nothing")
    parser.add_argument(
        "--update",
        action="store_true",
        help="rewrite the status column of docs/com-names.md and append to docs/test-report-v1.md",
    )
    return parser


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def prepare_workdir(model: Path, layout: Path | None, workdir: Path) -> tuple[Path, Path | None]:
    """Copy the model (and layout) into ``workdir/model``; returns the copies."""
    target = workdir / "model"
    target.mkdir(parents=True, exist_ok=True)
    model_copy = target / model.name
    shutil.copy2(model, model_copy)
    layout_copy = None
    if layout is not None:
        layout_copy = target / layout.name
        shutil.copy2(layout, layout_copy)
    return model_copy, layout_copy


def interactive_vissims() -> list[int]:
    """PIDs of Vissim processes NOT started by COM (they are never touched; listed as a warning only)."""
    from vissim_mcp import winproc
    from vissim_mcp.config import DEFAULT_EXE_PATTERN

    try:
        found = winproc.vissim_processes(re.compile(DEFAULT_EXE_PATTERN, re.IGNORECASE))
    except Exception:
        return []
    return sorted(pid for pid, info in found.items() if not winproc.cmdline_is_com_launched(info.cmdline))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.licensed:
        _log(
            "refused: this script starts a real Vissim through COM. Run it only when a Vissim license WITH "
            "the COM module is active (a Demo license shows a license dialog instead), and confirm that "
            f"with {LICENSE_FLAG}. See docs/m6-runbook.md."
        )
        return EXIT_REFUSED
    if forbid_real_enabled():
        _log(f"refused: {ENV_FORBID_REAL} is set (test mode); remove it from the environment to probe Vissim")
        return EXIT_REFUSED
    if sys.platform != "win32":
        _log("refused: Vissim COM is only available on Windows")
        return EXIT_REFUSED
    if not 0 < args.break_at < args.period:
        _log("refused: --break-at must be between 0 and --period")
        return EXIT_REFUSED
    model = args.model.expanduser().resolve()
    if not model.is_file():
        _log(f"model not found: {model}")
        return EXIT_REFUSED
    layout = args.layout
    if layout is None and model.with_suffix(".layx").is_file():
        layout = model.with_suffix(".layx")

    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="vissim-probe-"))).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    model_copy, layout_copy = prepare_workdir(model, layout, workdir)

    from vissim_mcp.config import load_settings
    from vissim_mcp.logging_setup import setup_logging
    from vissim_mcp.service import VissimService

    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("VISSIM_")}
    env.update(
        {
            "VISSIM_ALLOWED_DIRS": str(workdir),
            "VISSIM_LOG_DIR": str(workdir / "logs"),
            "VISSIM_AUDIT_DIR": str(workdir / "audit"),
            "VISSIM_KILL_ON_EXIT": "1",
            "VISSIM_USE_JOB_OBJECT": "1",
        }
    )
    for name in (
        "VISSIM_PROGID",
        "VISSIM_SEED_ATTR",
        "VISSIM_BREAK_DISABLE_VALUE",
        "VISSIM_CONNECT_TIMEOUT_S",
        "VISSIM_CALL_TIMEOUT_S",
        "VISSIM_LOAD_TIMEOUT_S",
    ):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if args.progid:
        env["VISSIM_PROGID"] = args.progid
    settings = load_settings(env)
    setup_logging(settings)

    interactive = interactive_vissims()
    if interactive:
        _log(
            f"note: interactive Vissim running (PID {interactive}); it is never touched. With a single-seat "
            "license it may hold the seat - close it yourself if the COM start shows a license dialog."
        )
    _log(f"working directory: {workdir}")

    state = ProbeState(
        model=model_copy,
        layout=layout_copy,
        workdir=workdir,
        period=args.period,
        break_at=args.break_at,
        break_disable_value=settings.break_disable_value,
        seed_attr=settings.seed_attr,
        simulate=not args.no_simulation,
    )
    service = VissimService(settings)
    report = asyncio.run(run_probe(service, state, log=_log))
    report["interactive_vissim_pids"] = interactive

    results = workdir / "probe-results.json"
    results.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(format_table(report))
    print(f"\nraw results: {results}")
    if args.update:
        write_documents(report)
        print(f"updated {COM_NAMES_MD} and {TEST_REPORT_MD}")
    if report.get("aborted"):
        return EXIT_FAILED
    return EXIT_FINDINGS if summary_counts(report)[FAILED] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
