"""resolver.py (PLAN 6.5, 10.1 row "resolver.parse_path"): COM-free parsing and resolving on FakeVissim."""

from __future__ import annotations

from typing import Any

import pytest

from fake_vissim import RPC_E_DISCONNECTED, FakeVissim, make_com_error
from vissim_mcp.errors import VissimMcpError, is_com_error
from vissim_mcp.resolver import MAX_SEGMENTS, Segment, format_path, is_com_object, parse_path, resolve


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Net", [Segment("Net")]),
        ("Net.Links", [Segment("Net"), Segment("Links")]),
        ("Net.Links[5]", [Segment("Net"), Segment("Links", 5)]),
        (
            "Net.SignalControllers[1].SGs[2]",
            [Segment("Net"), Segment("SignalControllers", 1), Segment("SGs", 2)],
        ),
        (
            "Net.VehicleRoutingDecisionsStatic[1].VehRoutSta[2]",
            [Segment("Net"), Segment("VehicleRoutingDecisionsStatic", 1), Segment("VehRoutSta", 2)],
        ),
        ('Net.Nodes["A1"]', [Segment("Net"), Segment("Nodes", "A1")]),
        ("Net.Nodes['A 1']", [Segment("Net"), Segment("Nodes", "A 1")]),
        ('Net.Nodes["a\\"b"]', [Segment("Net"), Segment("Nodes", 'a"b')]),
        ("Net.Links[ -3 ]", [Segment("Net"), Segment("Links", -3)]),
        ('Net.Nodes["5"]', [Segment("Net"), Segment("Nodes", "5")]),
        ("Graphics.CurrentNetworkWindow", [Segment("Graphics"), Segment("CurrentNetworkWindow")]),
        ("Simulation", [Segment("Simulation")]),
        ("Vissim.Net.Links[1]", [Segment("Net"), Segment("Links", 1)]),
        ("vissim.Simulation", [Segment("Simulation")]),
        ("  Net.Links  ", [Segment("Net"), Segment("Links")]),
        ("Vissim", []),
        ("", []),
        ("VissimX", [Segment("VissimX")]),
    ],
)
def test_parse_path_valid(text: str, expected: list[Segment]) -> None:
    assert parse_path(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Net..Links",
        "Net.",
        ".Net",
        "Net.Links[",
        "Net.Links[5",
        "Net.Links[]",
        "Net.Links[abc]",
        "Net.Links[1.5]",
        'Net.Nodes["A1]',
        "Net.Links[1]x",
        "Net Links",
        "Net.Links()",
        "Net.Links.ItemByKey(1)",
        "1Net",
        "Net.Links[99999999999999999999999]",
        "Net/Links",
        "Vissim.",
    ],
)
def test_parse_path_invalid(text: str) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        parse_path(text)
    assert excinfo.value.code.value == "INVALID_ARGUMENT"
    assert "invalid object path" in excinfo.value.message


@pytest.mark.parametrize("text", ["_oleobj_", "Net._oleobj_", "Net.__class__", "Net.Links[1]._dispobj_"])
def test_parse_path_rejects_underscore_names(text: str) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        parse_path(text)
    assert "'_'" in excinfo.value.message


def test_parse_path_limits() -> None:
    with pytest.raises(VissimMcpError):
        parse_path(".".join(["Net"] * (MAX_SEGMENTS + 1)))
    with pytest.raises(VissimMcpError):
        parse_path("N" * 600)
    with pytest.raises(VissimMcpError):
        parse_path(5)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text", ["Net.Links[5]", 'Net.Nodes["A\\"1"]', "Simulation", "Net.SignalControllers[1].SGs[2]"]
)
def test_format_path_round_trip(text: str) -> None:
    assert parse_path(format_path(parse_path(text))) == parse_path(text)
    assert format_path([]) == "Vissim"


# --- resolve on FakeVissim (created on this thread, so calls are allowed here)


@pytest.fixture
def vissim() -> FakeVissim:
    return FakeVissim()


def test_resolve_objects(vissim: FakeVissim) -> None:
    assert resolve(vissim, "") is vissim
    assert resolve(vissim, "Net.Links[5]").AttValue("No") == 5
    assert resolve(vissim, "Net.SignalControllers[2].SGs[3]").AttValue("Name") == "SG 2-3"
    assert resolve(vissim, "Net.VehicleRoutingDecisionsStatic[1].VehRoutSta[2]").AttValue("RelFlow(1)") == 2.0
    assert resolve(vissim, "Vissim.Simulation").AttValue("SimPeriod") == 3600
    assert is_com_object(resolve(vissim, "Graphics.CurrentNetworkWindow"))


def test_resolve_reports_the_failing_segment(vissim: FakeVissim) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        resolve(vissim, "Net.SignalControllers[1].Nope")
    err = excinfo.value
    assert err.code.value == "RESOLVE_FAILED"
    assert err.message.startswith("Segment 'Nope' on 'Net.SignalControllers[1]'")
    assert (
        err.details and err.details["segment"] == "Nope" and err.details["on"] == "Net.SignalControllers[1]"
    )

    with pytest.raises(VissimMcpError) as excinfo:
        resolve(vissim, "Net.SignalControllers[99].SGs")
    assert excinfo.value.code.value == "RESOLVE_FAILED"
    assert "Segment 'SignalControllers[99]' on 'Net'" in excinfo.value.message
    assert "ItemByKey(99)" in excinfo.value.message


def test_resolve_refuses_methods(vissim: FakeVissim) -> None:
    for path in ("Net.Links.ItemByKey", "Exit", "Net.Links.GetMultipleAttributes", "LoadNet"):
        with pytest.raises(VissimMcpError) as excinfo:
            resolve(vissim, path)
        assert excinfo.value.code.value == "RESOLVE_FAILED"
        assert "is a method" in excinfo.value.message
    assert not vissim.exited


def test_resolve_refuses_values_and_keys_on_non_collections(vissim: FakeVissim) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        resolve(vissim, "Net.Links.Count.Foo")
    assert "is a value" in excinfo.value.message
    with pytest.raises(VissimMcpError) as excinfo:
        resolve(vissim, "Net.Links.Count[1]")
    assert "not a collection" in excinfo.value.message
    with pytest.raises(VissimMcpError) as excinfo:
        resolve(vissim, "Simulation[1]")
    assert excinfo.value.code.value == "RESOLVE_FAILED"


def test_resolve_reraises_a_lost_connection(vissim: FakeVissim) -> None:
    vissim.crash(kill_process=False)
    with pytest.raises(Exception) as excinfo:
        resolve(vissim, "Net.Links[1]")
    assert is_com_error(excinfo.value)


def test_resolve_wraps_other_com_errors(vissim: FakeVissim) -> None:
    class Raising:
        _oleobj_ = object()

        def __getattr__(self, name: str) -> Any:
            raise make_com_error(-2147352567, "Vissim says no")

    with pytest.raises(VissimMcpError) as excinfo:
        resolve(Raising(), "Net")
    assert excinfo.value.code.value == "RESOLVE_FAILED"
    assert excinfo.value.details and excinfo.value.details.get("hresult") == -2147352567

    class Disconnected(Raising):
        def __getattr__(self, name: str) -> Any:
            raise make_com_error(RPC_E_DISCONNECTED)

    with pytest.raises(Exception) as caught:
        resolve(Disconnected(), "Net")
    assert is_com_error(caught.value)
