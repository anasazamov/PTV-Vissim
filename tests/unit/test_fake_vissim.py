"""The FakeVissim test double itself (PLAN 6.10): dispatch shape, collections, attributes, simulation
semantics (RunContinuous / SimBreakAt), files and fault injection. Later milestones rely on exactly these
behaviours."""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import psutil
import pytest
import pywintypes

from fake_vissim import (
    DISP_E_EXCEPTION,
    DISP_E_TYPEMISMATCH,
    E_FAIL,
    E_INVALIDARG,
    E_NOTIMPL,
    NET_COLLECTIONS,
    NOISE_MARKER,
    RPC_E_DISCONNECTED,
    RPC_E_WRONG_THREAD,
    RPC_S_CALL_FAILED,
    RPC_S_SERVER_UNAVAILABLE,
    SAVE_FORMAT,
    FakeVissim,
    options_from_env,
)


def hresult(exc: pytest.ExceptionInfo[BaseException]) -> int:
    return exc.value.args[0]


def description(exc: pytest.ExceptionInfo[BaseException]) -> str:
    excepinfo = exc.value.args[2]
    return excepinfo[2] if excepinfo else ""


def run_in_thread(fn: Any) -> Any:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # the test inspects it
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    return box


# --- dispatch shape


def test_properties_are_values_and_methods_are_bound(fake_vissim: FakeVissim) -> None:
    assert isinstance(fake_vissim.Net.Links.Count, int)
    assert isinstance(fake_vissim.Net.Links.ItemByKey, types.MethodType)
    assert isinstance(fake_vissim.LoadNet, types.MethodType)
    assert isinstance(fake_vissim.Simulation.RunContinuous, types.MethodType)
    assert not isinstance(fake_vissim.Net, types.MethodType)
    assert not isinstance(fake_vissim.Graphics.CurrentNetworkWindow, types.MethodType)
    with pytest.raises(AttributeError):
        _ = fake_vissim.Net.NoSuchCollection
    assert bool(fake_vissim.Net.Vehicles) is True  # like CDispatch: no implicit Count call


def test_calls_are_recorded_with_thread_names(fake_vissim: FakeVissim) -> None:
    _ = fake_vissim.Net.Links.Count
    members = [member for member, _thread in fake_vissim.calls]
    assert members[-3:] == ["Vissim.Net", "Net.Links", "Links.Count"]
    assert {thread for _member, thread in fake_vissim.calls} == {threading.current_thread().name}


# --- collections


def test_demo_network(fake_vissim: FakeVissim) -> None:
    net = fake_vissim.Net
    counts = {name: getattr(net, name).Count for name in NET_COLLECTIONS}
    assert counts == {
        "Links": 10,
        "Nodes": 4,
        "SignalControllers": 2,
        "SignalHeads": 4,
        "VehicleInputs": 3,
        "VehicleRoutingDecisionsStatic": 2,
        "DataCollectionMeasurements": 3,
        "VehicleTravelTimeMeasurements": 2,
        "QueueCounters": 2,
        "SimulationRuns": 0,
        "Vehicles": 0,
    }
    assert FakeVissim(network="empty").Net.Links.Count == 0


def test_item_by_key_and_exists(fake_vissim: FakeVissim) -> None:
    links = fake_vissim.Net.Links
    assert links.ItemByKey(5).AttValue("Name") == "Link 5"
    assert links.ItemByKey("5").AttValue("No") == 5  # VARIANT-like coercion
    assert links.ItemByKey(5.0).AttValue("No") == 5
    assert links.ItemKeyExists(5) is True
    assert links.ItemKeyExists(99) is False
    with pytest.raises(pywintypes.com_error) as info:
        links.ItemByKey(99)
    assert hresult(info) == DISP_E_EXCEPTION
    assert "does not exist" in description(info)
    assert info.value.args[2][5] == E_INVALIDARG
    with pytest.raises(pywintypes.com_error):
        links.ItemByKey(True)


def test_string_keys(fake_vissim: FakeVissim) -> None:
    fake_vissim.add_object("Nodes", "A1", Name="Center")
    assert fake_vissim.Net.Nodes.ItemByKey("A1").AttValue("Name") == "Center"
    assert fake_vissim.Net.Nodes.Count == 5


def test_get_all_iteration_and_len(fake_vissim: FakeVissim) -> None:
    links = fake_vissim.Net.Links
    assert len(links.GetAll()) == 10
    assert [link.AttValue("No") for link in links] == list(range(1, 11))
    assert len(links) == 10


def test_get_multiple_attributes(fake_vissim: FakeVissim) -> None:
    rows = fake_vissim.Net.Links.GetMultipleAttributes(("No", "Name", "NumLanes"))
    assert len(rows) == 10
    assert rows[0] == (1, "Link 1", 2)
    with pytest.raises(pywintypes.com_error):
        fake_vissim.Net.Links.GetMultipleAttributes(("No", "Nope"))
    with pytest.raises(pywintypes.com_error) as info:
        fake_vissim.Net.Links.GetMultipleAttributes("No")
    assert hresult(info) == DISP_E_TYPEMISMATCH


def test_set_multiple_attributes_is_atomic(fake_vissim: FakeVissim) -> None:
    links = fake_vissim.Net.Links
    links.SetMultipleAttributes(("Name",), [(f"L{i}",) for i in range(1, 11)])
    assert links.ItemByKey(3).AttValue("Name") == "L3"
    bad = [("x", 1)] * 9 + [("y", 0)]  # NumLanes=0 is out of range for the last link
    with pytest.raises(pywintypes.com_error):
        links.SetMultipleAttributes(("Name", "NumLanes"), bad)
    assert links.ItemByKey(1).AttValue("Name") == "L1"  # nothing was applied
    with pytest.raises(pywintypes.com_error):
        links.SetMultipleAttributes(("Name",), [("only one row",)])


def test_multi_att_values_and_set_all(fake_vissim: FakeVissim) -> None:
    links = fake_vissim.Net.Links
    assert links.GetMultiAttValues("NumLanes")[0] == (1, 2)
    links.SetMultiAttValues("NumLanes", [(1, 3), (2, 4)])
    assert links.ItemByKey(2).AttValue("NumLanes") == 4
    links.SetAllAttValues("NumLanes", 1, True)
    assert links.ItemByKey(2).AttValue("NumLanes") == 5
    links.SetAllAttValues("Name", "same")
    assert {row[1] for row in links.GetMultiAttValues("Name")} == {"same"}


def test_attribute_container(fake_vissim: FakeVissim) -> None:
    attributes = fake_vissim.Net.Links.Attributes
    infos = {info.Code: info for info in attributes.GetAll()}
    assert attributes.Count == len(infos)
    assert infos["No"].Editable is False
    assert infos["Name"].Editable is True
    assert infos["NumLanes"].ValueType == "Integer"
    assert infos["NumLanes"].Name == "Num Lanes"
    assert attributes.ItemByKey("name").Code == "Name"
    measurement_codes = {info.Code for info in fake_vissim.Net.DataCollectionMeasurements.Attributes.GetAll()}
    assert {"Vehs", "Speed"} <= measurement_codes


def test_nested_collections(fake_vissim: FakeVissim) -> None:
    controller = fake_vissim.Net.SignalControllers.ItemByKey(1)
    assert controller.SGs.Count == 4
    assert controller.SGs.ItemByKey(2).AttValue("Name") == "SG 1-2"
    routes = fake_vissim.Net.VehicleRoutingDecisionsStatic.ItemByKey(1).VehRoutSta
    assert routes.ItemByKey(2).AttValue("RelFlow(1)") == 2.0
    assert fake_vissim.collection("SignalControllers[2].SGs").Count == 4


# --- attributes


def test_attribute_names_are_case_insensitive(fake_vissim: FakeVissim) -> None:
    link = fake_vissim.Net.Links.ItemByKey(1)
    assert link.AttValue("name") == link.AttValue("NAME") == "Link 1"


def test_read_only_and_unknown_attributes(fake_vissim: FakeVissim) -> None:
    link = fake_vissim.Net.Links.ItemByKey(1)
    with pytest.raises(pywintypes.com_error) as info:
        link.SetAttValue("No", 7)
    assert hresult(info) == DISP_E_EXCEPTION and "read-only" in description(info)
    with pytest.raises(pywintypes.com_error) as info:
        link.AttValue("Colour")
    assert "unknown attribute" in description(info)
    with pytest.raises(pywintypes.com_error):
        link.SetAttValue("Colour", "red")


def test_type_coercion_and_ranges(fake_vissim: FakeVissim) -> None:
    link = fake_vissim.Net.Links.ItemByKey(1)
    link.SetAttValue("NumLanes", "3")
    assert link.AttValue("NumLanes") == 3
    with pytest.raises(pywintypes.com_error) as info:
        link.SetAttValue("NumLanes", "many")
    assert hresult(info) == DISP_E_TYPEMISMATCH
    with pytest.raises(pywintypes.com_error) as info:
        link.SetAttValue("NumLanes", 0)
    assert "out of range" in description(info)
    link.SetAttValue("Name", 42)
    assert link.AttValue("Name") == "42"


def test_signal_state_switches_to_com_control(fake_vissim: FakeVissim) -> None:
    group = fake_vissim.Net.SignalControllers.ItemByKey(1).SGs.ItemByKey(1)
    assert group.AttValue("SigState") == "RED"
    assert group.AttValue("ContrByCOM") is False
    group.SetAttValue("SigState", "green")
    assert group.AttValue("SigState") == "GREEN"
    assert group.AttValue("ContrByCOM") is True
    group.SetAttValue("SigState", 4)
    assert group.AttValue("SigState") == "AMBER"
    group.SetAttValue("ContrByCOM", False)
    assert group.AttValue("ContrByCOM") is False
    for bad in ("BLUE", 12, 0):
        with pytest.raises(pywintypes.com_error):
            group.SetAttValue("SigState", bad)


def test_file_attributes_exist_for_policy_tests(fake_vissim: FakeVissim) -> None:
    controller = fake_vissim.Net.SignalControllers.ItemByKey(1)
    controller.SetAttValue("SupplyFile1", "D:\\x\\sc.sig")
    assert controller.AttValue("SupplyFile1") == "D:\\x\\sc.sig"
    assert fake_vissim.Evaluation.AttValue("EvalOutDir") == ""


def test_result_attributes(fake_vissim: FakeVissim) -> None:
    measurement = fake_vissim.Net.DataCollectionMeasurements.ItemByKey(1)
    assert measurement.AttValue("Vehs(Current,Last,All)") is None  # no simulation yet
    fake_vissim.Simulation.SetAttValue("SimPeriod", 60)
    fake_vissim.Simulation.RunContinuous()
    first = measurement.AttValue("Vehs(Current,Last,All)")
    assert isinstance(first, int)
    assert measurement.AttValue("Vehs(Current,Last,All)") == first  # deterministic
    assert measurement.AttValue("Vehs(1,Last,All)") == first
    assert measurement.AttValue("Vehs(9,Last,All)") is None
    fake_vissim.Simulation.SetAttValue("RandSeed", 43)
    fake_vissim.Simulation.RunContinuous()
    second = measurement.AttValue("Vehs(Current,Last,All)")
    assert second != first  # another seed gives other results
    assert measurement.AttValue("Vehs(Avg,Last,All)") == round((first + second) / 2, 3)
    with pytest.raises(pywintypes.com_error):
        measurement.SetAttValue("Vehs(Current,Last,All)", 1)


# --- simulation semantics


@pytest.fixture
def sim(fake_vissim: FakeVissim) -> Any:
    fake_vissim.Simulation.SetAttValue("SimPeriod", 600)
    return fake_vissim.Simulation


def test_simulation_defaults(fake_vissim: FakeVissim) -> None:
    simulation = fake_vissim.Simulation
    assert simulation.AttValue("SimPeriod") == 3600
    assert simulation.AttValue("SimRes") == 10
    assert simulation.AttValue("SimBreakAt") == 0.0
    assert simulation.AttValue("SimSec") == 0.0
    assert simulation.AttValue("RandSeed") == 42
    assert simulation.AttValue("IsRunning") is False


def test_run_continuous_without_break_runs_the_whole_run(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.RunContinuous()
    assert fake_vissim.sim_active is False
    assert sim.AttValue("SimSec") == 0.0
    assert [(r["No"], r["RandSeed"], r["SimEnd"], r["stopped"]) for r in fake_vissim.run_records] == [
        (1, 42, 600.0, False)
    ]
    runs = fake_vissim.Net.SimulationRuns
    assert runs.Count == 1
    assert runs.ItemByKey(1).AttValue("RandSeed") == 42
    assert runs.ItemByKey(1).AttValue("SimEnd") == 600.0


def test_break_greater_than_simsec_pauses_there(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.SetAttValue("SimBreakAt", 100)
    sim.RunContinuous()
    assert sim.AttValue("SimSec") == 100.0
    assert fake_vissim.sim_active is True
    sim.SetAttValue("SimBreakAt", 250)  # editable while paused
    sim.RunContinuous()
    assert sim.AttValue("SimSec") == 250.0
    assert fake_vissim.run_records == []


@pytest.mark.parametrize("stale_value", [100, 50])
def test_break_not_greater_than_simsec_runs_to_the_end(
    fake_vissim: FakeVissim, sim: Any, stale_value: int
) -> None:
    sim.SetAttValue("SimBreakAt", 100)
    sim.RunContinuous()
    sim.SetAttValue("SimBreakAt", stale_value)  # equal to or below SimSec: no break any more
    sim.RunContinuous()
    assert fake_vissim.sim_active is False
    assert fake_vissim.run_records[-1]["SimEnd"] == 600.0


@pytest.mark.parametrize("value", [0, 600, 601])
def test_zero_or_period_break_means_no_break(fake_vissim: FakeVissim, sim: Any, value: int) -> None:
    sim.SetAttValue("SimBreakAt", value)
    sim.RunContinuous()
    assert fake_vissim.sim_active is False
    assert len(fake_vissim.run_records) == 1


def test_next_call_starts_a_new_run_with_the_current_seed(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.RunContinuous()
    sim.SetAttValue("RandSeed", 43)
    sim.SetAttValue("SimBreakAt", 300)
    sim.RunContinuous()
    assert fake_vissim.run_no == 2 and sim.AttValue("SimSec") == 300.0
    sim.SetAttValue("SimBreakAt", 0)
    sim.RunContinuous()
    assert [r["RandSeed"] for r in fake_vissim.run_records] == [42, 43]
    assert [row[0] for row in fake_vissim.Net.SimulationRuns.GetMultipleAttributes(("No",))] == [1, 2]


def test_simsec_after_a_run_is_configurable() -> None:
    for mode, expected in (("zero", 0.0), ("period", 600.0)):
        fake = FakeVissim(simsec_after_run=mode)
        fake.Simulation.SetAttValue("SimPeriod", 600)
        fake.Simulation.RunContinuous()
        assert fake.Simulation.AttValue("SimSec") == expected


def test_parameters_are_read_only_during_a_run(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.SetAttValue("SimBreakAt", 100)
    sim.RunContinuous()
    for name, value in (("SimPeriod", 10), ("RandSeed", 7), ("SimRes", 5)):
        with pytest.raises(pywintypes.com_error) as info:
            sim.SetAttValue(name, value)
        assert "in progress" in description(info)
    with pytest.raises(pywintypes.com_error) as info:
        sim.SetAttValue("SimSec", 5)
    assert "read-only" in description(info)
    sim.SetAttValue("UseMaxSimSpeed", True)  # ordinary attributes stay editable


def test_rand_seed_minimum_is_one(fake_vissim: FakeVissim) -> None:
    with pytest.raises(pywintypes.com_error) as info:
        fake_vissim.Simulation.SetAttValue("RandSeed", 0)
    assert "out of range" in description(info)


def test_break_is_aligned_to_the_time_step(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.SetAttValue("SimRes", 4)
    sim.SetAttValue("SimBreakAt", 10.1)
    sim.RunContinuous()
    assert sim.AttValue("SimSec") == 10.25


def test_run_single_step(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.RunSingleStep()
    assert sim.AttValue("SimSec") == pytest.approx(0.1)
    assert fake_vissim.sim_active is True


def test_stop_ends_the_run(fake_vissim: FakeVissim, sim: Any) -> None:
    sim.SetAttValue("SimBreakAt", 100)
    sim.RunContinuous()
    sim.Stop()
    assert fake_vissim.sim_active is False
    assert fake_vissim.run_records[-1]["stopped"] is True
    assert fake_vissim.run_records[-1]["SimEnd"] == 100.0
    sim.Stop()  # no run in progress: no-op
    sim.SetAttValue("SimBreakAt", 0)
    sim.RunContinuous()
    assert fake_vissim.run_no == 2


def test_stale_break_pauses_once() -> None:
    fake = FakeVissim(stale_break_at=150)
    simulation = fake.Simulation
    simulation.SetAttValue("SimPeriod", 600)
    simulation.RunContinuous()  # SimBreakAt=0 ("no break"), but the stale break is still honoured
    assert simulation.AttValue("SimSec") == 150.0
    assert fake.sim_active is True
    simulation.RunContinuous()
    assert fake.sim_active is False and fake.run_records[-1]["SimEnd"] == 600.0
    assert fake.stale_break_at == []


def test_stale_break_before_the_chunk_target() -> None:
    fake = FakeVissim(stale_break_at=[50])
    simulation = fake.Simulation
    simulation.SetAttValue("SimPeriod", 600)
    simulation.SetAttValue("SimBreakAt", 100)
    simulation.RunContinuous()
    assert simulation.AttValue("SimSec") == 50.0  # early pause
    simulation.RunContinuous()
    assert simulation.AttValue("SimSec") == 100.0


def test_ignore_break_runs_through() -> None:
    fake = FakeVissim(ignore_break=True)
    fake.Simulation.SetAttValue("SimPeriod", 600)
    fake.Simulation.SetAttValue("SimBreakAt", 100)
    fake.Simulation.RunContinuous()
    assert fake.run_records[-1]["SimEnd"] == 600.0


def test_stalled_run_does_not_advance() -> None:
    fake = FakeVissim(stall_runs=1)
    fake.Simulation.SetAttValue("SimBreakAt", 100)
    fake.Simulation.RunContinuous()
    assert fake.sim_active is True and fake.Simulation.AttValue("SimSec") == 0.0
    fake.Simulation.RunContinuous()
    assert fake.Simulation.AttValue("SimSec") == 100.0


def test_wall_clock_pacing() -> None:
    fake = FakeVissim(wall_s_per_sim_s=0.001)
    fake.Simulation.SetAttValue("SimBreakAt", 100)
    started = time.perf_counter()
    fake.Simulation.RunContinuous()
    assert time.perf_counter() - started >= 0.09


def test_vehicles_exist_only_during_a_run(fake_vissim: FakeVissim, sim: Any) -> None:
    assert fake_vissim.Net.Vehicles.Count == 0
    sim.SetAttValue("SimBreakAt", 100)
    sim.RunContinuous()
    vehicles = fake_vissim.Net.Vehicles
    assert vehicles.Count == 20
    rows = vehicles.GetMultipleAttributes(("No", "Speed"))
    assert rows[0][0] == 1 and isinstance(rows[0][1], float)
    sim.Stop()
    assert fake_vissim.Net.Vehicles.Count == 0


# --- fault injection


def test_raise_com_error_next_is_one_shot(fake_vissim: FakeVissim) -> None:
    fake_vissim.raise_com_error_next(E_FAIL)
    with pytest.raises(pywintypes.com_error) as info:
        _ = fake_vissim.Net
    assert hresult(info) == E_FAIL and info.value.args[2] is None
    assert fake_vissim.Net.Links.Count == 10


def test_raised_disp_exception_carries_excepinfo(fake_vissim: FakeVissim) -> None:
    fake_vissim.raise_com_error_next(DISP_E_EXCEPTION, "custom failure", E_INVALIDARG, count=2)
    for _ in range(2):
        with pytest.raises(pywintypes.com_error) as info:
            fake_vissim.Simulation.RunContinuous()
        assert description(info) == "custom failure"
        assert info.value.args[2][5] == E_INVALIDARG


def test_hang_next_call_blocks_then_proceeds(fake_vissim: FakeVissim) -> None:
    fake_vissim.hang_next_call(0.2)
    started = time.perf_counter()
    assert fake_vissim.Net.Links.Count == 10
    assert time.perf_counter() - started >= 0.18


def test_release_hang_ends_the_wait_early(fake_vissim: FakeVissim) -> None:
    fake_vissim.hang_next_call(30)
    threading.Timer(0.1, fake_vissim.release_hang).start()
    started = time.perf_counter()
    _ = fake_vissim.Net
    assert time.perf_counter() - started < 5


def test_hang_on_a_specific_member(fake_vissim: FakeVissim) -> None:
    fake_vissim.hang_on("RunContinuous", 0.2)
    started = time.perf_counter()
    simulation = fake_vissim.Simulation
    assert time.perf_counter() - started < 0.15
    simulation.RunContinuous()
    assert time.perf_counter() - started >= 0.18
    started = time.perf_counter()
    simulation.RunContinuous()  # only once
    assert time.perf_counter() - started < 0.15


def test_crash_disconnects_every_later_call(fake_vissim: FakeVissim) -> None:
    simulation = fake_vissim.Simulation
    fake_vissim.crash()
    assert fake_vissim.crashed
    for call in (lambda: fake_vissim.Net, simulation.RunContinuous, lambda: simulation.AttValue("SimSec")):
        with pytest.raises(pywintypes.com_error) as info:
            call()
        assert hresult(info) == RPC_E_DISCONNECTED


def test_crash_after_calls() -> None:
    fake = FakeVissim(crash_after_calls=2)
    _ = fake.Net
    _ = fake.Simulation
    with pytest.raises(pywintypes.com_error) as info:
        _ = fake.Evaluation
    assert hresult(info) == RPC_E_DISCONNECTED


def test_exit_disconnects(fake_vissim: FakeVissim) -> None:
    fake_vissim.Exit()
    assert fake_vissim.exited
    with pytest.raises(pywintypes.com_error) as info:
        _ = fake_vissim.Net
    assert hresult(info) == RPC_E_DISCONNECTED


def test_objects_are_bound_to_their_creating_thread(fake_vissim: FakeVissim) -> None:
    error = run_in_thread(lambda: fake_vissim.Net.Links.Count)["error"]
    assert isinstance(error, pywintypes.com_error) and error.args[0] == RPC_E_WRONG_THREAD
    inspection = run_in_thread(lambda: fake_vissim.attribute("Links", 1, "Name"))
    assert inspection["value"] == "Link 1"  # the inspection API works from any thread
    relaxed = FakeVissim(enforce_thread=False)
    assert run_in_thread(lambda: relaxed.Net.Links.Count)["value"] == 10


def test_print_to_stdout_on_call(fake_vissim: FakeVissim, capfd: pytest.CaptureFixture[str]) -> None:
    fake_vissim.print_to_stdout_on_call = True
    _ = fake_vissim.Net
    out = capfd.readouterr().out
    assert f"{NOISE_MARKER} print() in Vissim.Net" in out
    assert f"{NOISE_MARKER} os.write(1) in Vissim.Net" in out


# --- files & network


def test_load_net_requires_an_existing_file(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    with pytest.raises(pywintypes.com_error) as info:
        fake_vissim.LoadNet(str(tmp_path / "missing.inpx"), False)
    assert "does not exist" in description(info)
    with pytest.raises(pywintypes.com_error):
        fake_vissim.LoadNet("", False)


def test_load_generic_file_gives_the_demo_network(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    model = tmp_path / "model.inpx"
    model.write_text("<network/>", encoding="utf-8")
    fake_vissim.Net.Links.ItemByKey(1).SetAttValue("Name", "changed")
    fake_vissim.Simulation.RunContinuous()
    fake_vissim.LoadNet(str(model), False)
    assert fake_vissim.Net.Links.ItemByKey(1).AttValue("Name") == "Link 1"
    assert fake_vissim.Net.SimulationRuns.Count == 0
    assert fake_vissim.loaded_net_path == str(model)
    assert fake_vissim.load_count == 1
    assert fake_vissim.last_load_additive is False


def test_save_and_reload_round_trip(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    fake_vissim.Net.Links.ItemByKey(1).SetAttValue("Name", "Main street")
    fake_vissim.add_object("Nodes", "A1", Name="Center")
    fake_vissim.Simulation.SetAttValue("SimPeriod", 900)
    target = tmp_path / "saved.inpx"
    fake_vissim.SaveNetAs(str(target))
    assert json.loads(target.read_text(encoding="utf-8"))["format"] == SAVE_FORMAT
    assert fake_vissim.saved_paths == [str(target)]
    fake_vissim.New()
    assert fake_vissim.Net.Links.Count == 0
    fake_vissim.LoadNet(str(target), False)
    assert fake_vissim.Net.Links.ItemByKey(1).AttValue("Name") == "Main street"
    assert fake_vissim.Net.Nodes.ItemByKey("A1").AttValue("Name") == "Center"
    assert fake_vissim.Net.SignalControllers.ItemByKey(2).SGs.Count == 4
    assert fake_vissim.Simulation.AttValue("SimPeriod") == 900


def test_object_identity_survives_load_net(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    model = tmp_path / "m.inpx"
    model.write_text("x", encoding="utf-8")
    simulation, net, evaluation = fake_vissim.Simulation, fake_vissim.Net, fake_vissim.Evaluation
    simulation.SetAttValue("SimPeriod", 77)
    fake_vissim.LoadNet(str(model), False)
    assert (
        fake_vissim.Simulation is simulation
        and fake_vissim.Net is net
        and fake_vissim.Evaluation is evaluation
    )
    assert simulation.AttValue("SimPeriod") == 3600  # parameters come from the loaded file (defaults here)


def test_load_net_aborts_a_paused_run(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    model = tmp_path / "m.inpx"
    model.write_text("x", encoding="utf-8")
    fake_vissim.Simulation.SetAttValue("SimBreakAt", 100)
    fake_vissim.Simulation.RunContinuous()
    fake_vissim.LoadNet(str(model), False)
    assert fake_vissim.sim_active is False


def test_additive_load_keeps_the_network(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    model = tmp_path / "m.inpx"
    model.write_text("x", encoding="utf-8")
    fake_vissim.Net.Links.ItemByKey(1).SetAttValue("Name", "kept")
    fake_vissim.LoadNet(str(model), True)
    assert fake_vissim.Net.Links.ItemByKey(1).AttValue("Name") == "kept"
    assert fake_vissim.last_load_additive is True


def test_save_errors(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    with pytest.raises(pywintypes.com_error):
        fake_vissim.SaveNetAs(str(tmp_path / "no" / "such" / "dir" / "x.inpx"))
    with pytest.raises(pywintypes.com_error):
        fake_vissim.SaveNet()  # no file name yet


def test_load_layout(fake_vissim: FakeVissim, tmp_path: Path) -> None:
    with pytest.raises(pywintypes.com_error):
        fake_vissim.LoadLayout(str(tmp_path / "missing.layx"))
    layout = tmp_path / "view.layx"
    layout.write_text("<layout/>", encoding="utf-8")
    fake_vissim.LoadLayout(str(layout))
    assert fake_vissim.layout_path == str(layout)


def test_non_ascii_paths(tmp_path: Path) -> None:
    folder = tmp_path / "Samarqand_ko'cha_Самарканд"
    folder.mkdir()
    target = folder / "tarmoq.inpx"
    fake = FakeVissim()
    fake.SaveNetAs(str(target))
    fake.LoadNet(str(target), False)
    assert fake.loaded_net_path == str(target)
    strict = FakeVissim(reject_non_ascii_paths=True)
    with pytest.raises(pywintypes.com_error):
        strict.LoadNet(str(target), False)


def test_gui_and_quick_mode(fake_vissim: FakeVissim) -> None:
    fake_vissim.SuspendUpdateGUI()
    assert fake_vissim.gui_suspended is True
    fake_vissim.ResumeUpdateGUI(True)
    assert fake_vissim.gui_suspended is False
    window = fake_vissim.Graphics.CurrentNetworkWindow
    window.SetAttValue("QuickMode", True)
    assert window.AttValue("QuickMode") == 1
    with pytest.raises(pywintypes.com_error):
        window.SetAttValue("QuickMode", 2)


# --- type info


def test_type_info_lists_com_members_and_plumbing(fake_vissim: FakeVissim) -> None:
    type_info = fake_vissim._oleobj_.GetTypeInfo()
    names = set()
    for index in range(type_info.GetTypeAttr().cFuncs):
        desc = type_info.GetFuncDesc(index)
        names.add(type_info.GetNames(desc.memid)[0])
    assert {"QueryInterface", "Invoke", "LoadNet", "SaveNetAs", "Net", "Simulation", "Exit"} <= names
    assert "hang_next_call" not in names and "collection" not in names
    assert type_info.GetDocumentation(-1)[0] == "IVissim"
    assert fake_vissim._oleobj_.GetTypeInfoCount() == 1


def test_type_info_can_be_unavailable() -> None:
    fake = FakeVissim(no_type_info=True)
    assert fake._oleobj_.GetTypeInfoCount() == 0
    with pytest.raises(pywintypes.com_error) as info:
        fake._oleobj_.GetTypeInfo()
    assert hresult(info) == E_NOTIMPL


def test_type_info_goes_through_the_fault_hook(fake_vissim: FakeVissim) -> None:
    fake_vissim.crash()
    with pytest.raises(pywintypes.com_error) as info:
        fake_vissim._oleobj_.GetTypeInfo()
    assert hresult(info) == RPC_E_DISCONNECTED


# --- env configuration


def test_options_from_env() -> None:
    options = options_from_env(
        {
            "FAKE_VISSIM_PRINT_STDOUT": "1",
            "FAKE_VISSIM_NETWORK": "empty",
            "FAKE_VISSIM_SIMSEC_AFTER_RUN": "period",
            "FAKE_VISSIM_STALE_BREAK_AT": "100, 200",
            "FAKE_VISSIM_IGNORE_BREAK": "yes",
            "FAKE_VISSIM_STALL_RUNS": "2",
            "FAKE_VISSIM_CRASH_AFTER_CALLS": "10",
            "FAKE_VISSIM_WALL_S_PER_SIM_S": "0.01",
            "FAKE_VISSIM_NO_TYPE_INFO": "1",
            "FAKE_VISSIM_REJECT_NON_ASCII": "1",
            "FAKE_VISSIM_EXIT_KEEPS_PROCESS": "1",
            "FAKE_VISSIM_HANG_ON": "LoadNet:30; Simulation.RunContinuous:5",
            "FAKE_VISSIM_SPAWN": '["a.exe", "-Embedding"]',
            "FAKE_VISSIM_SPAWN_ENV": '{"STANDIN_MODE": "license", "PYTHONPATH": ""}',
        }
    )
    assert options == {
        "print_to_stdout_on_call": True,
        "native_noise": True,
        "network": "empty",
        "simsec_after_run": "period",
        "stale_break_at": [100.0, 200.0],
        "ignore_break": True,
        "stall_runs": 2,
        "crash_after_calls": 10,
        "wall_s_per_sim_s": 0.01,
        "no_type_info": True,
        "reject_non_ascii_paths": True,
        "exit_keeps_process": True,
        "hang_on": {"LoadNet": 30.0, "Simulation.RunContinuous": 5.0},
        "spawn_cmd": ["a.exe", "-Embedding"],
        "spawn_env": {"STANDIN_MODE": "license", "PYTHONPATH": ""},
    }
    assert options_from_env({}) == {}
    with pytest.raises(ValueError):
        options_from_env({"FAKE_VISSIM_SPAWN": '"not a list"'})
    with pytest.raises(ValueError):
        options_from_env({"FAKE_VISSIM_SPAWN_ENV": '{"N": 1}'})


def test_from_env_builds_a_configured_fake() -> None:
    fake = FakeVissim.from_env(
        "Vissim.Vissim.2026", {"FAKE_VISSIM_NETWORK": "empty", "FAKE_VISSIM_STALL_RUNS": "1"}
    )
    assert fake.progid == "Vissim.Vissim.2026"
    assert fake.Net.Links.Count == 0
    assert fake.stall_runs == 1


# --- stand-in process


def sleeper_command() -> list[str]:
    # The base interpreter, not the venv launcher (killing a launcher would orphan its child interpreter).
    executable = getattr(sys, "_base_executable", None) or sys.executable
    return [executable, "-c", "import time; time.sleep(30)"]


def test_spawned_process_ends_on_exit() -> None:
    fake = FakeVissim(spawn_cmd=sleeper_command())
    try:
        assert fake.pid is not None and psutil.pid_exists(fake.pid)
        fake.Exit()
        assert fake.process is not None
        fake.process.wait(10)
    finally:
        fake.kill_process()


def test_crash_kills_the_spawned_process() -> None:
    fake = FakeVissim(spawn_cmd=sleeper_command())
    try:
        fake.crash()
        assert fake.process is not None
        fake.process.wait(10)
    finally:
        fake.kill_process()


def test_process_end_fails_a_hung_call_and_disconnects() -> None:
    """Like real COM when the server process is killed: the pending call fails at once, later calls too."""
    fake = FakeVissim(enforce_thread=False)
    fake.hang_next_call(30)
    box: dict[str, Any] = {}

    def hung_call() -> None:
        try:
            box["value"] = fake.Net.Links.Count
        except Exception as exc:  # inspected below
            box["error"] = exc

    thread = threading.Thread(target=hung_call)
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    fake.process_ended("kill")
    thread.join(5)
    assert time.monotonic() - started < 2
    assert box["error"].hresult == RPC_S_SERVER_UNAVAILABLE
    with pytest.raises(pywintypes.com_error) as info:
        _ = fake.Net
    assert info.value.hresult == RPC_E_DISCONNECTED and fake.crashed


def test_process_end_after_exit_is_not_a_crash() -> None:
    fake = FakeVissim()
    fake.Exit()
    fake.process_ended("exit")
    assert fake.exited and not fake.crashed


def test_exit_can_leave_the_process_running() -> None:
    fake = FakeVissim(spawn_cmd=sleeper_command(), exit_keeps_process=True)
    try:
        fake.Exit()
        time.sleep(0.2)
        assert fake.process is not None and fake.process.poll() is None
    finally:
        fake.kill_process()
        if fake.process is not None:
            fake.process.wait(10)


def test_a_dying_process_can_fail_the_call_in_flight_with_rpc_s_call_failed() -> None:
    fake = FakeVissim(enforce_thread=False, dead_call_hresult=RPC_S_CALL_FAILED)
    fake.hang_next_call(30)
    box: dict[str, Any] = {}

    def hung_call() -> None:
        try:
            box["value"] = fake.Net.Links.Count
        except Exception as exc:  # inspected below
            box["error"] = exc

    thread = threading.Thread(target=hung_call)
    thread.start()
    time.sleep(0.1)
    fake.process_ended("crash")
    thread.join(5)
    assert box["error"].hresult == RPC_S_CALL_FAILED


def test_exit_can_end_the_process_later_and_lose_its_reply() -> None:
    """``exit_process_delay_s``: Vissim needs a moment to shut down; ``exit_error``: the reply of Exit() is
    lost because the server goes away first (the shutdown still happens)."""
    fake = FakeVissim(spawn_cmd=sleeper_command(), exit_process_delay_s=0.3, exit_error=RPC_S_CALL_FAILED)
    try:
        with pytest.raises(pywintypes.com_error) as info:
            fake.Exit()
        assert info.value.hresult == RPC_S_CALL_FAILED and fake.exited
        assert fake.process is not None and fake.process.poll() is None  # still shutting down
        fake.process.wait(10)
    finally:
        fake.kill_process()
        if fake.process is not None:
            fake.process.wait(10)
