"""Build a network from nothing through the MCP tools (vissim_call_method), save, simulate, read results.

This is the "whole project through MCP" path: New -> links -> connectors -> vehicle input -> static routes ->
measurements -> save -> simulate -> results -> a signal controller without a supply file (Vissim refuses the
run and the job error carries Vissim's own message).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from real_helpers import RealSession

pytestmark = pytest.mark.vissim

METHOD = "vissim_call_method"
PERIOD = 600


def _add(session: RealSession, path: str, method: str, args: list[Any]) -> dict[str, Any]:
    result = session.ok(METHOD, {"path": path, "method": method, "args": args})
    assert result["called"] is True and result["returned"].get("object") is True, result
    assert result["returned"]["key"] is not None, result
    return result["returned"]


def _ref(path: str) -> dict[str, str]:
    return {"ref": path}


def test_build_a_network_from_scratch_and_simulate(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()

    new = session.ok(METHOD, {"path": "Vissim", "method": "New"})
    assert new["called"] is True and session.ok("vissim_info")["counts"]["Links"] == 0

    members = {m["name"]: m for m in session.ok("vissim_describe", {"path": "Net.Links"})["members"]}
    assert members["AddLink"]["params"] == ["Key", "WktLinestring", "LaneWidths"], members["AddLink"]
    assert members["AddConnector"]["params"][:3] == ["Key", "FromLane", "FromPos"], members["AddConnector"]
    assert members["RemoveLink"]["params"] == ["Link"]

    # --- geometry: a two-lane approach that splits into a through link and a turn
    approach = _add(session, "Net.Links", "AddLink", [0, "LINESTRING(0 0, 200 0)", [3.5, 3.5]])
    through = _add(session, "Net.Links", "AddLink", [0, "LINESTRING(220 0, 400 0)", [3.5, 3.5]])
    turn = _add(session, "Net.Links", "AddLink", [7, "LINESTRING(220 20, 220 200)", [3.5]])
    assert turn["key"] == 7 and turn["path"] == "Net.Links[7]"
    a, t, n = approach["key"], through["key"], turn["key"]
    to_through = _add(
        session,
        "Net.Links",
        "AddConnector",
        [
            0,
            _ref(f"Net.Links[{a}].Lanes[1]"),
            195,
            _ref(f"Net.Links[{t}].Lanes[1]"),
            5,
            2,
            "LINESTRING EMPTY",
        ],
    )
    to_turn = _add(
        session,
        "Net.Links",
        "AddConnector",
        [
            0,
            _ref(f"Net.Links[{a}].Lanes[2]"),
            190,
            _ref(f"Net.Links[{n}].Lanes[1]"),
            5,
            1,
            "LINESTRING EMPTY",
        ],
    )
    links = session.ok(
        "vissim_list_objects",
        {"collection": "Net.Links", "attributes": ["No", "NumLanes", "IsConn", "FromLink", "ToLink"]},
    )
    by_no = {row[0]: row for row in links["rows"]}
    assert by_no[a][1] == 2 and by_no[n][1] == 1
    assert by_no[to_through["key"]][2:] == [1, str(a), str(t)]
    assert by_no[to_turn["key"]][2:] == [1, str(a), str(n)]

    # --- demand and routes
    vehicle_input = _add(session, "Net.VehicleInputs", "AddVehicleInput", [0, _ref(f"Net.Links[{a}]")])
    volume = session.ok(
        "vissim_set_attributes", {"path": vehicle_input["path"], "values": {"Volume(1)": 1200}}
    )
    assert volume["failed"] == [] and volume["new"]["Volume(1)"] == 1200
    decision = _add(
        session,
        "Net.VehicleRoutingDecisionsStatic",
        "AddVehicleRoutingDecisionStatic",
        [0, _ref(f"Net.Links[{a}]"), 20],
    )
    routes_path = f"{decision['path']}.VehRoutSta"
    route_through = _add(session, routes_path, "AddVehicleRouteStatic", [0, _ref(f"Net.Links[{t}]"), 150])
    route_turn = _add(session, routes_path, "AddVehicleRouteStatic", [0, _ref(f"Net.Links[{n}]"), 150])
    for route, share in ((route_through, 3), (route_turn, 1)):
        flow = session.ok("vissim_set_attributes", {"path": route["path"], "values": {"RelFlow(1)": share}})
        assert flow["failed"] == [], flow

    # --- measurements
    point = _add(
        session,
        "Net.DataCollectionPoints",
        "AddDataCollectionPoint",
        [0, _ref(f"Net.Links[{t}].Lanes[1]"), 100],
    )
    measurement = _add(session, "Net.DataCollectionMeasurements", "AddDataCollectionMeasurement", [0])
    linked = session.ok(
        "vissim_set_attributes",
        {"path": measurement["path"], "values": {"DataCollectionPoints": str(point["key"])}},
    )
    assert linked["failed"] == [], linked
    travel_time = _add(
        session,
        "Net.VehicleTravelTimeMeasurements",
        "AddVehicleTravelTimeMeasurement",
        [0, _ref(f"Net.Links[{a}]"), 10, _ref(f"Net.Links[{t}]"), 150],
    )
    evaluation = session.ok(
        "vissim_set_attributes",
        {"path": "Evaluation", "values": {"DataCollCollectData": True, "VehTravTmsCollectData": True}},
    )
    assert evaluation["failed"] == [], evaluation

    # --- a network without a file: refused before Vissim can open its 'Save as' dialog
    unsaved = session.tool("vissim_start_simulation", {"runs": 1, "seed": 5})
    record(unsaved_start=unsaved)
    assert unsaved["ok"] is False and unsaved["error"]["code"] == "INVALID_ARGUMENT", unsaved
    assert "vissim_save_network" in unsaved["error"]["hint"]

    saved = session.ok("vissim_save_network", {"save_as": "built/crossing.inpx"}, timeout=660)
    assert saved["created_folder"].endswith("built"), saved

    # --- simulate and read results
    session.ok("vissim_configure_simulation", {"period_s": PERIOD, "seed": 5})
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 5})
    status = session.wait_job(job["job_id"], timeout_s=300)
    assert status["state"] == "finished", status
    counted = session.ok(
        "vissim_get_attributes", {"path": measurement["path"], "attributes": ["Vehs(Current,Total,All)"]}
    )
    travelled = session.ok(
        "vissim_get_attributes",
        {"path": travel_time["path"], "attributes": ["Vehs(Current,Total,All)", "TravTm(Current,Total,All)"]},
    )
    record(
        built={
            "links": links["rows"],
            "run_wall_s": status["run_wall_s"],
            "data_collection": counted["values"],
            "travel_time": travelled["values"],
        }
    )
    vehicles = counted["values"]["Vehs(Current,Total,All)"]
    assert isinstance(vehicles, (int, float)) and vehicles > 20, counted  # 1200 veh/h, 3/4 go through
    assert travelled["values"]["Vehs(Current,Total,All)"] > 20, travelled
    assert 5 < travelled["values"]["TravTm(Current,Total,All)"] < 120, travelled

    # --- a fixed-time controller without a supply file: Vissim refuses the run and says why
    controller = _add(session, "Net.SignalControllers", "AddSignalController", [0])
    group = _add(session, f"{controller['path']}.SGs", "AddSignalGroup", [0])
    head = _add(session, "Net.SignalHeads", "AddSignalHead", [0, _ref(f"Net.Links[{a}].Lanes[1]"), 150])
    assigned = session.ok(
        "vissim_set_attributes",
        {"path": head["path"], "values": {"SG": f"{controller['key']}-{group['key']}"}},
    )
    assert assigned["failed"] == [], assigned
    session.ok("vissim_save_network", {"save_as": "built/crossing_signal.inpx"}, timeout=660)
    failing = session.ok("vissim_start_simulation", {"runs": 1, "seed": 5})
    failed = session.wait_job(failing["job_id"], timeout_s=300)
    record(run_refused_by_vissim={"state": failed["state"], "error": failed["error"]})
    assert failed["state"] == "error", failed
    messages = failed["error"]["details"].get("vissim_messages")
    assert messages, failed["error"]  # the lines of Vissim's .err file
    assert "Vissim reported" in failed["error"]["message"]
    assert "vissim_load_network" in failed["error"]["hint"], failed["error"]

    # --- give the controller a fixed-time program (cycle 60 s, green 10-30 s): now the run starts and the
    # --- signal group follows the program: red/amber before green, amber after it
    program = session.ok(
        "vissim_set_signal_program",
        {
            "sc_no": controller["key"],
            "cycle_s": 60,
            "groups": [{"sg_no": group["key"], "green_start_s": 10, "green_end_s": 30}],
        },
    )
    assert program["stored"] is True and program["created_groups"] == [], program
    session.ok("vissim_save_network", {"save_as": "built/crossing_program.inpx"}, timeout=660)
    # after a failed start Vissim cannot start another run until the network is loaded again (M6)
    session.ok("vissim_load_network", {"inpx_path": "built/crossing_program.inpx"}, timeout=660)
    session.ok("vissim_configure_simulation", {"period_s": PERIOD, "seed": 5})
    expected = {5: "RED", 9: "REDAMBER", 20: "GREEN", 31: "AMBER", 34: "RED", 80: "GREEN", 100: "RED"}
    signalled = session.ok("vissim_start_simulation", {"runs": 1, "seed": 5, "pause_at": sorted(expected)})
    observed: dict[int, Any] = {}
    for second in sorted(expected):
        paused = session.wait_job(signalled["job_id"], timeout_s=300)
        assert paused["state"] == "paused" and abs(paused["simsec"] - second) <= 1, paused
        state = session.ok(
            "vissim_get_attributes",
            {"path": f"{controller['path']}.SGs[{group['key']}]", "attributes": ["SigState"]},
        )
        observed[second] = state["values"]["SigState"]
        session.ok("vissim_resume_simulation", {"job_id": signalled["job_id"]})
    with_program = session.wait_job(signalled["job_id"], timeout_s=300)
    record(signal_program={"program": program, "observed": observed, "state": with_program["state"]})
    assert with_program["state"] == "finished", with_program
    assert observed == expected, observed

    # --- remove the controller again: the network still runs
    removed_head = session.ok(
        METHOD, {"path": "Net.SignalHeads", "method": "RemoveSignalHead", "args": [_ref(head["path"])]}
    )
    removed = session.ok(
        METHOD,
        {
            "path": "Net.SignalControllers",
            "method": "RemoveSignalController",
            "args": [_ref(controller["path"])],
        },
    )
    assert removed_head["returned"] == {"value": None} and removed["returned"] == {"value": None}
    assert session.ok("vissim_info")["counts"]["SignalControllers"] == 0
    session.ok("vissim_save_network", {"save_as": "built/crossing_fixed.inpx"}, timeout=660)
    again = session.ok("vissim_start_simulation", {"runs": 1, "seed": 5})
    rerun = session.wait_job(again["job_id"], timeout_s=300)
    record(rerun_after_removing_the_controller={"state": rerun["state"], "error": rerun["error"]})
    assert rerun["state"] == "finished", rerun

    # --- policy: files and runs stay with their own tools
    denied = session.tool(METHOD, {"path": "Vissim", "method": "LoadNet", "args": ["x.inpx"]})
    assert denied["error"]["code"] == "ATTRIBUTE_DENIED", denied
    removed_link = session.ok(
        METHOD, {"path": "Net.Links", "method": "RemoveLink", "args": [_ref(f"Net.Links[{n}]")]}
    )
    assert removed_link["called"] is True
    remaining = session.ok("vissim_list_objects", {"collection": "Net.Links", "attributes": ["No"]})
    assert n not in [row[0] for row in remaining["rows"]]
    record(after_remove=[row[0] for row in remaining["rows"]])
