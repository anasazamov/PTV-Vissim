"""vissim_call_method across the network-editing methods of real Vissim (beyond links and connectors).

One network is built object by object - distributions, functions, compositions, the routing decision kinds,
parking, public transport, signal control, detectors, pedestrian objects, sub-collections - with the argument
formats of PTV's "Network Objects Adding" example. Also: what Vissim silently ignores (an array of the wrong
shape) must be reported as an error, enumerations take numbers, files and scripts are refused by the policy,
and vehicles can be added to a paused simulation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from real_helpers import RealSession

pytestmark = pytest.mark.vissim

METHOD = "vissim_call_method"


def _ref(path: str) -> dict[str, str]:
    return {"ref": path}


LANE, LANE_B = _ref("Net.Links[1].Lanes[1]"), _ref("Net.Links[1].Lanes[2]")
LANE2, LANE2_B = _ref("Net.Links[2].Lanes[1]"), _ref("Net.Links[2].Lanes[2]")
LINK, LINK2 = _ref("Net.Links[1]"), _ref("Net.Links[2]")
ACCELERATION = [0, 3, 2, 4, 120, 6, 5, 7, 160, 0, 0, 0]  # x, y, yMin, yMax per point
DECELERATION = [0, -3, -4, -2, 120, -6, -7, -5, 160, 0, 0, 0]
AREA_1 = "POLYGON((500 500, 540 500, 540 540, 500 540))"
AREA_2 = "POLYGON((560 500, 600 500, 600 540, 560 540))"

#: (path, method, args) - every call must create an object that Vissim returns or that is found by its key.
CREATING: list[tuple[str, str, list[Any]]] = [
    # distributions and functions: data points are flat lists
    ("Net.SpeedDistributions", "AddSpeedDistribution", [0, [50, 0, 60, 0.1, 70, 0.9, 80, 1]]),
    ("Net.PowerDistributions", "AddPowerDistribution", [0, [60, 0, 75, 0.5, 100, 1]]),
    ("Net.WeightDistributions", "AddWeightDistribution", [0, [500, 0, 1000, 0.5, 2000, 1]]),
    ("Net.TimeDistributions", "AddTimeDistributionEmpirical", [0, [10, 0, 12, 0.75, 15, 1]]),
    ("Net.TimeDistributions", "AddTimeDistributionNormal", [0]),
    ("Net.TimeDistributions", "AddTimeDistribution", [0, 2, [10, 0, 15, 1]]),  # Type is an enumeration
    ("Net.LocationDistributions", "AddLocationDistribution", [0, [0, 0, 0.5, 0.3, 1, 1]]),
    ("Net.DistanceDistributions", "AddDistanceDistribution", [0, [50, 0, 90, 0.2, 150, 1]]),
    ("Net.OccupancyDistributions", "AddOccupancyDistributionEmpirical", [0, [1, 0, 2, 0.66, 5, 1]]),
    ("Net.FreeDistributions", "AddFreeDistributionEmpirical", [0, [0, 0, 10, 1]]),
    ("Net.ColorDistributions", "AddColorDistribution", [0, ["44556677", "ff000000"]]),
    ("Net.DesAccelerationFunctions", "AddDesAccelerationFunction", [0, ACCELERATION]),
    ("Net.DesDecelerationFunctions", "AddDesDecelerationFunction", [0, DECELERATION]),
    ("Net.MaxAccelerationFunctions", "AddMaxAccelerationFunction", [0, ACCELERATION]),
    ("Net.MaxDecelerationFunctions", "AddMaxDecelerationFunction", [0, DECELERATION]),
    # base data
    ("Net.VehicleTypes", "AddVehicleType", [0]),
    ("Net.VehicleClasses", "AddVehicleClass", [0]),
    ("Net.DrivingBehaviors", "AddDrivingBehavior", [0]),
    ("Net.LinkBehaviorTypes", "AddLinkBehaviorType", [0]),
    ("Net.DisplayTypes", "AddDisplayType", [0]),
    ("Net.Levels", "AddLevel", [0]),
    ("Net.Zones", "AddZone", [0]),
    ("Net.Matrices", "AddMatrix", [0]),
    ("Net.VehicleCompositions", "AddVehicleComposition", [0, []]),
    (
        "Net.UserDefinedAttributes",
        "AddUserDefinedDataAttribute",
        [0, "LINK", "MyUDA", "My attribute", 1, 0, True],
    ),
    (
        "Net.UserDefinedAttributes",
        "AddUserDefinedFormulaAttribute",
        [0, "LINK", "MyF", "My formula", 1, 0, "[NO]*2"],
    ),
    # geometry
    (
        "Net.Links",
        "AddConnectorEx",
        [0, [[LANE], [LANE_B]], 180, [[LANE2], [LANE2_B]], 10, "LINESTRING EMPTY"],
    ),
    ("Net.Links", "GenerateOppositeDirection", [LINK2, 1]),
    ("Net.Nodes", "AddNode", [0, "POLYGON((180 -20, 240 -20, 240 40, 180 40, 180 -20))"]),
    ("Net.Sections", "AddSection", [0, "POLYGON((-10 -10, 410 -10, 410 10, -10 10))"]),
    # control and speed
    ("Net.PriorityRules", "AddPriorityRule", [0, LANE, 100, [LANE2, 20]]),  # markers: lane, pos, lane, pos...
    ("Net.StopSigns", "AddStopSign", [0, LANE, 90]),
    ("Net.DesSpeedDecisions", "AddDesSpeedDecision", [0, LANE, 50]),
    ("Net.ReducedSpeedAreas", "AddReducedSpeedArea", [0, LANE, 70]),
    ("Net.PavementMarkings", "AddPavementMarking", [0, LANE, 40]),
    ("Net.SignalControllers", "AddSignalController", [0]),
    ("Net.SignalControllers", "AddSignalController", [0]),
    ("Net.SCCommunications", "AddSCCommunication", [0]),  # needs two signal controllers
    ("Net.Detectors", "AddDetector", [0, LANE, 60]),  # needs a signal controller
    ("Net.SignalHeads", "AddSignalHead", [0, LANE, 150]),
    # routing decisions of every kind and their routes
    ("Net.VehicleRoutingDecisionsStatic", "AddVehicleRoutingDecisionStatic", [0, LINK, 20]),
    ("Net.VehicleRoutingDecisionsStatic[1].VehRoutSta", "AddVehicleRouteStatic", [0, LINK2, 150]),
    ("Net.VehicleRoutingDecisionsPartial", "AddVehicleRoutingDecisionPartial", [0, LINK, 30, LINK2, 160]),
    ("Net.VehicleRoutingDecisionsPartial[1].VehRoutPart", "AddVehicleRoutePartial", [0]),
    ("Net.VehicleRoutingDecisionsClosure", "AddVehicleRoutingDecisionClosure", [0, LINK, 35]),
    ("Net.VehicleRoutingDecisionsClosure[1].VehRoutClos", "AddVehicleRouteClosure", [0, LINK2, 100]),
    (
        "Net.VehicleRoutingDecisionsManagedLanes",
        "AddVehicleRoutingDecisionManagedLanes",
        [0, LINK, 40, LINK2, 170],
    ),
    ("Net.VehicleRoutingDecisionsManagedLanes[1].VehRoutMngLns", "AddVehicleRouteManagedLanes", [0, 1]),
    ("Net.ParkingLots", "AddParkingLot", [1, LANE2, 120]),
    ("Net.ParkingLots", "AddAbstractParkingLot", [0, LINK2, 30, _ref("Net.Zones[1]")]),
    ("Net.VehicleRoutingDecisionsParking", "AddVehicleRoutingDecisionParking", [0, LINK, 45]),
    (
        "Net.VehicleRoutingDecisionsParking[1].VehRoutPark",
        "AddVehicleRouteParking",
        [0, _ref("Net.ParkingLots[1]")],
    ),
    ("Net.VehicleRoutingDecisionsDynamic", "AddVehicleRoutingDecisionDynamic", [0, LINK, 16]),
    # public transport
    ("Net.PTStops", "AddPTStop", [0, LANE2, 60]),
    ("Net.PTLines", "AddPTLine", [0, LINK, LINK2, 150]),
    # measurements
    ("Net.DataCollectionPoints", "AddDataCollectionPoint", [0, LANE2, 100]),
    ("Net.DataCollectionMeasurements", "AddDataCollectionMeasurement", [0]),
    ("Net.DelayMeasurements", "AddDelayMeasurement", [0]),
    ("Net.QueueCounters", "AddQueueCounter", [0, LINK, 120]),
    ("Net.VehicleTravelTimeMeasurements", "AddVehicleTravelTimeMeasurement", [0, LINK, 10, LINK2, 150]),
    # pedestrians
    ("Net.Areas", "AddArea", [0, AREA_1]),
    ("Net.Areas", "AddArea", [0, AREA_2]),
    ("Net.Obstacles", "AddObstacle", [0, "POLYGON((510 510, 515 510, 515 515, 510 515))"]),
    ("Net.Ramps", "AddRamp", [0, "POLYGON((700 500, 720 500, 720 540, 700 540))"]),
    ("Net.PedestrianTypes", "AddPedestrianType", [0]),
    ("Net.PedestrianClasses", "AddPedestrianClass", [0]),
    ("Net.WalkingBehaviors", "AddWalkingBehavior", [0]),
    ("Net.AreaBehaviorTypes", "AddAreaBehaviorType", [0]),
    ("Net.PedestrianCompositions", "AddPedestrianComposition", [0, []]),
    ("Net.PedestrianInputs", "AddPedestrianInput", [0, _ref("Net.Areas[1]")]),
    ("Net.PedestrianRoutingDecisionsStatic", "AddPedestrianRoutingDecisionStatic", [0, _ref("Net.Areas[1]")]),
    (
        "Net.PedestrianRoutingDecisionsStatic[1].PedRoutSta",
        "AddPedestrianRouteStaticOnArea",
        [0, _ref("Net.Areas[2]")],
    ),
    (
        "Net.PedestrianTravelTimeMeasurements",
        "AddPedestrianTravelTimeMeasurement",
        [0, _ref("Net.Areas[1]"), _ref("Net.Areas[2]")],
    ),
    # presentation
    ("Net.CameraPositions", "AddCameraPosition", [0, "POINT(10 10 20)"]),
    ("Net.Storyboards", "AddStoryboard", [0]),
]

#: Objects of sub-collections have no number of their own: Vissim returns the object, the result has no key
#: (lanes get their Index).
SUB_COLLECTIONS: list[tuple[str, str, list[Any]]] = [
    ("Net.Links[1].Lanes", "AddLane", [0, 3.5]),
    ("Net.PriorityRules[1].ConflictMarkers", "AddConflictMarker", [0, LANE2_B, 30]),
    ("Net.PTLines[1].DepTimes", "AddDepartureTime", [0, 123]),
    ("Net.Storyboards[1].Keyframes", "AddKeyframe", [0]),
    ("Net.TimeIntervalSets[1].TimeInts", "AddTimeInterval", [0]),
]


def _call(session: RealSession, path: str, method: str, args: list[Any]) -> dict[str, Any]:
    return session.tool(METHOD, {"path": path, "method": method, "args": args}, timeout=120)


def _base_network(session: RealSession) -> None:
    session.ok(METHOD, {"path": "Vissim", "method": "New"})
    for wkt in ("LINESTRING(0 0, 200 0)", "LINESTRING(220 0, 400 0)"):
        session.ok(METHOD, {"path": "Net.Links", "method": "AddLink", "args": [0, wkt, [3.5, 3.5]]})
    session.ok(
        METHOD,
        {
            "path": "Net.Links",
            "method": "AddConnector",
            "args": [0, LANE, 195, LANE2, 5, 2, "LINESTRING EMPTY"],
        },
    )


def test_network_editing_methods(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()
    _base_network(session)

    created: dict[str, Any] = {}
    failures: list[str] = []
    for path, method, args in CREATING:
        result = _call(session, path, method, args)
        returned = result.get("returned") or {}
        if not result.get("ok"):
            failures.append(f"{path}.{method}: {result['error']['code']}: {result['error']['message'][:200]}")
        elif not returned.get("object") or returned.get("key") is None or not returned.get("path"):
            failures.append(f"{path}.{method}: no key/path in {returned}")
        else:
            created[f"{path}.{method}"] = returned["key"]
    for path, method, args in SUB_COLLECTIONS:
        result = _call(session, path, method, args)
        if not result.get("ok") or not (result.get("returned") or {}).get("object"):
            failures.append(f"{path}.{method}: {result.get('error') or result.get('returned')}")
        else:
            created[f"{path}.{method}"] = result["returned"].get("key")
    record(created=created, failures=failures)
    assert not failures, "\n".join(failures)
    assert created["Net.Links[1].Lanes.AddLane"] == 3  # a lane is addressed by its Index
    lanes = session.ok("vissim_list_objects", {"collection": "Net.Links[1].Lanes", "attributes": ["Index"]})
    assert [row[0] for row in lanes["rows"]] == [1, 2, 3]

    # --- methods that change an object without creating one
    relative_flow = session.ok(
        METHOD,
        {
            "path": f"Net.VehicleCompositions[{created['Net.VehicleCompositions.AddVehicleComposition']}]"
            ".VehCompRelFlows",
            "method": "AddVehicleCompositionRelativeFlow",
            "args": [
                _ref("Net.VehicleTypes[100]"),
                _ref(f"Net.SpeedDistributions[{created['Net.SpeedDistributions.AddSpeedDistribution']}]"),
            ],
        },
    )
    assert relative_flow["called"] is True
    sequence = session.ok(
        METHOD,
        {
            "path": "Net.VehicleRoutingDecisionsStatic[1].VehRoutSta[1]",
            "method": "UpdateLinkSequence",
            "args": [[_ref("Net.Links[10000]"), 1]],
        },
    )
    assert sequence["returned"] == {"value": None}
    points = (
        f"Net.SpeedDistributions[{created['Net.SpeedDistributions.AddSpeedDistribution']}].SpeedDistrDatPts"
    )
    replaced = session.ok(METHOD, {"path": points, "method": "ReplaceAll", "args": [[30, 0, 50, 1]]})
    assert replaced["called"] is True
    after = session.ok("vissim_list_objects", {"collection": points, "attributes": ["X", "FX"]})
    assert after["rows"] == [[30.0, 0.0], [50.0, 1.0]], after

    # --- duplicate and remove
    copy = session.ok(
        METHOD,
        {"path": "Net.StopSigns", "method": "DuplicateStopSign", "args": [_ref("Net.StopSigns[1]"), 0]},
    )
    assert copy["returned"]["key"] == 2
    session.ok(
        METHOD, {"path": "Net.StopSigns", "method": "RemoveStopSign", "args": [_ref("Net.StopSigns[2]")]}
    )
    signs = session.ok("vissim_list_objects", {"collection": "Net.StopSigns", "attributes": ["No"]})
    assert [row[0] for row in signs["rows"]] == [1]

    # --- what Vissim would silently ignore, and what the policy refuses
    refused: dict[str, Any] = {}
    for label, path, method, args, code in (
        (
            "nested data points",
            "Net.SpeedDistributions",
            "AddSpeedDistribution",
            [0, [[50, 0], [80, 1]]],
            "INVALID_ARGUMENT",
        ),
        (
            "nested conflict markers",
            "Net.PriorityRules",
            "AddPriorityRule",
            [0, LANE, 110, [[LANE2, 20]]],
            "INVALID_ARGUMENT",
        ),
        (
            "enumeration by name",
            "Net.TimeDistributions",
            "AddTimeDistribution",
            [0, "EMPIRICAL", [10, 0, 15, 1]],
            "INVALID_ARGUMENT",
        ),
        ("too few data points", "Net.SpeedDistributions", "AddSpeedDistribution", [0, [50, 0]], None),
        ("script", "Net.Scripts", "AddScript", [0], "ATTRIBUTE_DENIED"),
        (
            "file outside the allowed directories",
            "Net.BackgroundImages",
            "AddBackgroundImage",
            [0, "C:/Windows/Web/Wallpaper/Windows/img0.jpg", "POINT(100 100)", "POINT(-100 -100)"],
            "ATTRIBUTE_DENIED",
        ),
        (
            "object that does not exist",
            "Net.StopSigns",
            "RemoveStopSign",
            [_ref("Net.StopSigns[99]")],
            "RESOLVE_FAILED",
        ),
    ):
        result = _call(session, path, method, args)
        refused[label] = result.get("error", {}).get("code"), result.get("error", {}).get("message", "")[:160]
        assert result["ok"] is False, (label, result)
        if code is not None:
            assert result["error"]["code"] == code, (label, result)
    record(refused=refused)
    distributions = session.ok(
        "vissim_list_objects", {"collection": "Net.PriorityRules", "attributes": ["No"]}
    )
    assert [row[0] for row in distributions["rows"]] == [1]  # the refused calls created nothing
    assert session.ok("vissim_health")["connected"] is True

    saved = session.ok("vissim_save_network", {"save_as": "built/all_objects.inpx"}, timeout=660)
    assert saved["size_bytes"] > 50_000


def test_vehicles_are_added_to_a_paused_simulation(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()
    session.ok(METHOD, {"path": "Vissim", "method": "New"})
    session.ok(
        METHOD, {"path": "Net.Links", "method": "AddLink", "args": [0, "LINESTRING(0 0, 300 0)", [3.5, 3.5]]}
    )
    session.ok(METHOD, {"path": "Net.VehicleInputs", "method": "AddVehicleInput", "args": [0, LINK]})
    session.ok("vissim_set_attributes", {"path": "Net.VehicleInputs[1]", "values": {"Volume(1)": 600}})
    vehicle_args = [100, 1, 1, 50, 50, True]  # vehicle type, link, lane, position, desired speed, interaction

    outside = _call(session, "Net.Vehicles", "AddVehicleAtLinkPosition", vehicle_args)
    assert outside["ok"] is False and outside["error"]["code"] == "COM_ERROR", outside  # no run in progress

    session.ok("vissim_save_network", {"save_as": "built/vehicles.inpx"}, timeout=660)
    session.ok("vissim_configure_simulation", {"period_s": 300, "seed": 3})
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 3, "pause_at": [30]})
    paused = session.wait_job(job["job_id"], timeout_s=300)
    assert paused["state"] == "paused", paused
    before = session.ok("vissim_list_objects", {"collection": "Net.Vehicles", "attributes": ["No"]})["total"]

    added = session.ok(
        METHOD, {"path": "Net.Vehicles", "method": "AddVehicleAtLinkPosition", "args": vehicle_args}
    )
    vehicle = added["returned"]
    assert vehicle["object"] is True and vehicle["path"] == f"Net.Vehicles[{vehicle['key']}]", added
    values = session.ok(
        "vissim_get_attributes", {"path": vehicle["path"], "attributes": ["VehType", "Pos", "Lane", "Speed"]}
    )["values"]
    assert values == {"VehType": "100", "Pos": 50.0, "Lane": "1-1", "Speed": 50.0}, values
    assert (
        session.ok("vissim_list_objects", {"collection": "Net.Vehicles", "attributes": ["No"]})["total"]
        == before + 1
    )

    removed = session.ok(
        METHOD, {"path": "Net.Vehicles", "method": "RemoveVehicle", "args": [_ref(vehicle["path"])]}
    )
    assert removed["called"] is True
    assert (
        session.ok("vissim_list_objects", {"collection": "Net.Vehicles", "attributes": ["No"]})["total"]
        == before
    )

    # the network itself cannot be edited during a run (Vissim refuses), New not while a job is active
    edit = _call(session, "Net.Links", "AddLink", [0, "LINESTRING(0 50, 100 50)", [3.5]])
    assert edit["ok"] is False and edit["error"]["code"] == "COM_ERROR", edit
    new = _call(session, "Vissim", "New", [])
    assert new["ok"] is False and new["error"]["code"] == "JOB_RUNNING", new
    record(paused_simulation={"vehicle": values, "edit_refused": edit["error"]["message"][:200]})

    session.ok("vissim_resume_simulation", {"job_id": job["job_id"]})
    running = _call(session, "Net.Vehicles", "AddVehicleAtLinkPosition", vehicle_args)
    final = session.wait_job(job["job_id"], timeout_s=300)
    assert final["state"] == "finished", final
    if not running["ok"]:  # the run may already be over on a fast machine
        assert running["error"]["code"] in ("JOB_RUNNING", "COM_ERROR"), running
