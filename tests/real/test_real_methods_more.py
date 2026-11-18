"""vissim_call_method: the methods that need more than an empty network (real Vissim).

* functions whose data point format differs (curve speed, boarding delay, lateral drift), area behaviour
  elements;
* file arguments with real files inside the allowed directory (background image, static 3D model, 2D/3D
  model);
* pedestrians and public transport: build, simulate, add pedestrians and a PT vehicle to the paused run;
* dynamic assignment (PTV's "Path Addition & Assignment" example): paths and vehicles in parking lots/zones.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from real_helpers import LOAD_TIMEOUT_S, RealSession

pytestmark = pytest.mark.vissim

METHOD = "vissim_call_method"
VISSIM_EXE = Path(r"C:\Program Files\PTV Vision\PTV Vissim 2026\Exe")
EXAMPLES = Path(r"C:\Users\Public\Documents\PTV Vision\PTV Vissim 2026")
DTA_EXAMPLE = EXAMPLES / "Examples Training" / "Autonomous Vehicles (AV)" / "Path Addition & Assignment.PY"


def _ref(path: str) -> dict[str, str]:
    return {"ref": path}


def _call(session: RealSession, path: str, method: str, args: list[Any]) -> dict[str, Any]:
    return session.tool(METHOD, {"path": path, "method": method, "args": args}, timeout=120)


def _create(session: RealSession, path: str, method: str, args: list[Any]) -> dict[str, Any]:
    result = session.ok(METHOD, {"path": path, "method": method, "args": args}, timeout=120)
    assert result["returned"].get("object") is True, (path, method, result)
    return result["returned"]


def _two_links(session: RealSession) -> None:
    session.ok(METHOD, {"path": "Vissim", "method": "New"})
    for wkt in ("LINESTRING(0 0, 200 0)", "LINESTRING(220 0, 400 0)"):
        session.ok(METHOD, {"path": "Net.Links", "method": "AddLink", "args": [0, wkt, [3.5, 3.5]]})
    session.ok(
        METHOD,
        {
            "path": "Net.Links",
            "method": "AddConnector",
            "args": [
                0,
                _ref("Net.Links[1].Lanes[1]"),
                195,
                _ref("Net.Links[2].Lanes[1]"),
                5,
                2,
                "LINESTRING EMPTY",
            ],
        },
    )


def test_function_formats_and_area_behaviour(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()
    session.ok(METHOD, {"path": "Vissim", "method": "New"})

    # x, y, yMin, yMax per point
    curve = _create(session, "Net.CurvSpeedFuncs", "AddCurvSpeedFunc", [0, [10, 20, 18, 22, 100, 60, 55, 65]])
    points = session.ok(
        "vissim_list_objects",
        {"collection": f"{curve['path']}.CurvSpeedFuncDataPts", "attributes": ["X", "Y", "YMin", "YMax"]},
    )
    assert points["rows"] == [[10.0, 20.0, 18.0, 22.0], [100.0, 60.0, 55.0, 65.0]], points
    _create(session, "Net.BoardDelFuncs", "AddBoardDelFunc", [0, [0, 1, 1, 1, 10, 5, 5, 5]])
    # x (>= 1), y per point
    drift = _create(session, "Net.LatDriftSpeedFuncs", "AddLatDriftSpeedFunc", [0, [10, 0.5, 100, 1.5]])
    drift_points = session.ok(
        "vissim_list_objects",
        {"collection": f"{drift['path']}.LatDriftSpeedFuncDataPts", "attributes": ["X", "Y"]},
    )
    assert [row[0] for row in drift_points["rows"]] == [10.0, 100.0], drift_points

    # the wrong width per point is refused by Vissim itself (two values where four are needed)
    too_narrow = _call(session, "Net.CurvSpeedFuncs", "AddCurvSpeedFunc", [0, [10, 20, 100, 60]])
    below_minimum = _call(session, "Net.LatDriftSpeedFuncs", "AddLatDriftSpeedFunc", [0, [0, 0, 50, 1]])
    assert too_narrow["ok"] is False and below_minimum["ok"] is False
    record(
        refused={
            "curve": too_narrow["error"]["message"][:200],
            "drift": below_minimum["error"]["message"][:200],
        }
    )

    behaviour = _create(session, "Net.AreaBehaviorTypes", "AddAreaBehaviorType", [0])
    classes = session.ok("vissim_list_objects", {"collection": "Net.PedestrianClasses", "attributes": ["No"]})
    element = _create(
        session,
        f"{behaviour['path']}.AreaBehavTypeElements",
        "AddAreaBehaviorTypeElement",
        [_ref(f"Net.PedestrianClasses[{classes['rows'][0][0]}]")],
    )
    assert element["interface"] == "IAreaBehaviorTypeElement"


def test_file_arguments_with_real_files(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    model_source = next(iter(sorted(VISSIM_EXE.rglob("*.v3d"))), None)
    image_source = next(iter(sorted(EXAMPLES.rglob("*.jpg"))), None)
    if model_source is None or image_source is None:
        pytest.skip("no .v3d model or .jpg image found in the Vissim installation")
    session = real_session()
    session.connect()
    session.ok(METHOD, {"path": "Vissim", "method": "New"})
    files = session.projects / "files"
    files.mkdir(parents=True)
    image = files / "background.jpg"
    model = files / "model.v3d"
    shutil.copy2(image_source, image)
    shutil.copy2(model_source, model)

    corners = ["POINT(100 100)", "POINT(-100 -100)"]
    absolute = _create(session, "Net.BackgroundImages", "AddBackgroundImage", [0, str(image), *corners])
    relative = _create(
        session, "Net.BackgroundImages", "AddBackgroundImage", [0, "files/background.jpg", *corners]
    )
    static = _create(session, "Net.Static3DModels", "AddStatic3DModel", [0, str(model), "POINT(50 50 0)"])
    model_2d3d = _create(session, "Net.Models2D3D", "AddModel2D3D", [0, [str(model)]])
    stored = session.ok("vissim_get_attributes", {"path": absolute["path"], "attributes": ["PathFilename"]})[
        "values"
    ]
    record(
        files={"image": stored, "keys": [absolute["key"], relative["key"], static["key"], model_2d3d["key"]]}
    )
    assert "background.jpg" in str(stored["PathFilename"])

    # the same calls with files outside the allowed directories never reach Vissim
    for path, method, args in (
        ("Net.BackgroundImages", "AddBackgroundImage", [0, str(image_source), *corners]),
        ("Net.Static3DModels", "AddStatic3DModel", [0, str(model_source), "POINT(50 50 0)"]),
        ("Net.Models2D3D", "AddModel2D3D", [0, [str(model_source)]]),
    ):
        denied = _call(session, path, method, args)
        assert denied["ok"] is False and denied["error"]["code"] == "ATTRIBUTE_DENIED", (method, denied)
    images = session.ok("vissim_list_objects", {"collection": "Net.BackgroundImages", "attributes": ["No"]})
    assert images["total"] == 2
    session.ok("vissim_save_network", {"save_as": "built/with_files.inpx"}, timeout=660)


def test_pedestrians_and_public_transport(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()
    _two_links(session)

    # pedestrians: two adjoining areas, an input and a route from the first to the second
    area_1 = _create(session, "Net.Areas", "AddArea", [0, "POLYGON((500 500, 540 500, 540 520, 500 520))"])
    area_2 = _create(session, "Net.Areas", "AddArea", [0, "POLYGON((540 500, 600 500, 600 520, 540 520))"])
    ped_input = _create(session, "Net.PedestrianInputs", "AddPedestrianInput", [0, _ref(area_1["path"])])
    volume = session.ok("vissim_set_attributes", {"path": ped_input["path"], "values": {"Volume(1)": 600}})
    assert volume["failed"] == [], volume
    decision = _create(
        session,
        "Net.PedestrianRoutingDecisionsStatic",
        "AddPedestrianRoutingDecisionStatic",
        [0, _ref(area_1["path"])],
    )
    _create(
        session, f"{decision['path']}.PedRoutSta", "AddPedestrianRouteStaticOnArea", [0, _ref(area_2["path"])]
    )

    # public transport: a stop, a line over both links, three departures; plus private traffic
    _create(session, "Net.PTStops", "AddPTStop", [0, _ref("Net.Links[2].Lanes[1]"), 60])
    line = _create(session, "Net.PTLines", "AddPTLine", [0, _ref("Net.Links[1]"), _ref("Net.Links[2]"), 150])
    for departure in (10, 60, 120):
        _create(session, f"{line['path']}.DepTimes", "AddDepartureTime", [0, departure])
    vehicle_input = _create(session, "Net.VehicleInputs", "AddVehicleInput", [0, _ref("Net.Links[1]")])
    session.ok("vissim_set_attributes", {"path": vehicle_input["path"], "values": {"Volume(1)": 300}})
    bus_type = session.ok("vissim_get_attributes", {"path": line["path"], "attributes": ["VehType"]})[
        "values"
    ]["VehType"]

    session.ok("vissim_save_network", {"save_as": "built/ped_pt.inpx"}, timeout=660)
    session.ok("vissim_configure_simulation", {"period_s": 300, "seed": 4})
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 4, "pause_at": [100]})
    paused = session.wait_job(job["job_id"], timeout_s=300)
    assert paused["state"] == "paused", paused

    pedestrians = session.ok(
        "vissim_list_objects", {"collection": "Net.Pedestrians", "attributes": ["No", "PedType"]}
    )
    vehicles = session.ok(
        "vissim_list_objects", {"collection": "Net.Vehicles", "attributes": ["No", "VehType"]}
    )
    buses = [row for row in vehicles["rows"] if row[1] == str(bus_type)]
    assert pedestrians["total"] > 0, pedestrians  # the pedestrian input works
    assert buses, vehicles  # the PT line runs

    # objects added to the paused simulation
    ped_type = pedestrians["rows"][0][1]
    walker = _create(session, "Net.Pedestrians", "AddPedestrianOnArea", [int(ped_type), area_1["key"], 5])
    placed = _create(
        session,
        "Net.Pedestrians",
        "AddPedestrianOnAreaAtCoordinate",
        [int(ped_type), area_1["key"], 5, 5, 1, 0, 5, 1],
    )
    bus = _create(session, "Net.Vehicles", "AddVehicleToPTLine", [int(bus_type), line["key"]])
    after = session.ok("vissim_list_objects", {"collection": "Net.Pedestrians", "attributes": ["No"]})
    assert after["total"] == pedestrians["total"] + 2
    assert {walker["key"], placed["key"]} <= {row[0] for row in after["rows"]}
    removed = session.ok(
        METHOD, {"path": "Net.Pedestrians", "method": "RemovePedestrian", "args": [_ref(walker["path"])]}
    )
    assert removed["called"] is True
    record(
        ped_pt={
            "pedestrians_at_100s": pedestrians["total"],
            "buses_at_100s": len(buses),
            "added": [walker["key"], placed["key"], bus["key"]],
        }
    )

    session.ok("vissim_resume_simulation", {"job_id": job["job_id"]})
    assert session.wait_job(job["job_id"], timeout_s=300)["state"] == "finished"


def test_dynamic_assignment_paths_and_vehicles(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    if not DTA_EXAMPLE.is_dir():
        pytest.skip(f"example not installed: {DTA_EXAMPLE}")
    session = real_session()
    session.connect()
    folder = session.projects / "dta"
    shutil.copytree(DTA_EXAMPLE, folder)
    model = next(iter(folder.glob("*.inpx")))
    session.ok("vissim_load_network", {"inpx_path": str(model)}, timeout=LOAD_TIMEOUT_S)
    lots = session.ok("vissim_list_objects", {"collection": "Net.ParkingLots", "attributes": ["No"]})
    assert {1, 2, 3, 4} <= {row[0] for row in lots["rows"]}, lots

    session.ok("vissim_configure_simulation", {"period_s": 300, "seed": 4})
    job = session.ok("vissim_start_simulation", {"runs": 1, "seed": 4, "pause_at": [20]})
    paused = session.wait_job(job["job_id"], timeout_s=300)
    assert paused["state"] == "paused", paused

    # origin parking lot, destination parking lot, node list
    path = _create(session, "Net.Paths", "AddPath", [1, 3, [1, 2, 3]])
    by_reference = _create(
        session, "Net.Paths", "AddPath", [_ref("Net.ParkingLots[4]"), _ref("Net.ParkingLots[1]"), [4, 1]]
    )
    for_type = _create(session, "Net.Paths", "AddPathForVehicleType", [2, 3, [2, 3], 100])
    parked = _create(session, "Net.Vehicles", "AddVehicleInParkingLot", [100, 1])
    in_zone = _create(session, "Net.Vehicles", "AddVehicleInZone", [100, 1])
    for_vehicle = _create(session, "Net.Paths", "AddPathForVehicle", [parked["key"], 3, [1, 2, 3]])
    assert all(
        item["key"] is not None for item in (path, by_reference, for_type, parked, in_zone, for_vehicle)
    )
    vehicles = session.ok("vissim_list_objects", {"collection": "Net.Vehicles", "attributes": ["No"]})
    # the vehicle of the parking lot is in the network; the one of the zone enters it when it departs
    assert parked["key"] in {row[0] for row in vehicles["rows"]}
    record(
        dynamic_assignment={
            "paths": [path["key"], by_reference["key"], for_type["key"], for_vehicle["key"]],
            "vehicles": [parked["key"], in_zone["key"]],
        }
    )

    session.ok("vissim_resume_simulation", {"job_id": job["job_id"]})
    assert session.wait_job(job["job_id"], timeout_s=300)["state"] == "finished"
