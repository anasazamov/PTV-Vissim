"""FakeVissim: an in-memory stand-in for the PTV Vissim COM object model (PLAN 6.10, contract section 5).

It behaves like a ``win32com.client.dynamic.Dispatch`` object: COM properties are plain attribute accesses
(``vissim.Net.Links.Count``), COM methods are bound methods (``vissim.Net.Links.ItemByKey(5)``). Every COM
member access goes through ``FakeVissim._on_call`` which records the call and applies fault injection. Errors
are pywin32-style ``com_error(hresult, text, excepinfo, argerr)``; Vissim's own errors are
``DISP_E_EXCEPTION`` with the description in ``excepinfo[2]``.

Simulation semantics (the part ``jobs.py`` depends on):

* ``RunContinuous()`` starts a new run if none is in progress (recording ``RandSeed`` for that run), then
  pauses at ``SimBreakAt`` when ``SimBreakAt > SimSec`` (and below ``SimPeriod``); otherwise it runs to the
  end. A completed run is appended to ``Net.SimulationRuns`` and the next call starts a new run.
* ``SimSec`` is read-only; after a run ends it reads 0 (``simsec_after_run="zero"``) or the period
  (``"period"``) - real Vissim's behaviour is unverified, so both are available.
* ``RunSingleStep()`` advances one time step (1 / ``SimRes``); ``Stop()`` ends the run in progress.
* ``SimPeriod``, ``SimRes`` and ``RandSeed`` are read-only while a run is in progress.

Fault injection (test API, not COM):

* ``hang_next_call(seconds)`` / ``hang_on(member, seconds)`` / ``release_hang()`` - a hung COM call;
* ``raise_com_error_next(hresult)`` - the next call fails;
* ``print_to_stdout_on_call`` - ``print()`` and ``os.write(1, ...)`` on every call (optionally a native
  ``WriteFile``);
* ``stale_break_at`` - an old break point that is still honoured once; ``ignore_break`` - ``SimBreakAt`` is
  ignored; ``stall_runs`` - ``RunContinuous`` returns without advancing;
* ``crash()`` / ``crash_after_calls`` - later calls raise ``RPC_E_DISCONNECTED``;
* ``dead_call_hresult`` - what a call in flight fails with when the process dies (default
  ``RPC_S_SERVER_UNAVAILABLE``; ``RPC_S_CALL_FAILED`` is the other common real-COM outcome);
* ``exit_keeps_process`` / ``exit_process_delay_s`` / ``exit_error`` - after ``Exit()`` the process stays,
  ends only after a delay, or ``Exit()`` itself fails (e.g. ``RPC_S_CALL_FAILED``: the server went away
  before its reply arrived).

COM objects are bound to the thread that created them (``RPC_E_WRONG_THREAD`` otherwise), like STA objects.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_pywintypes() -> ModuleType:
    """``pywintypes`` from sys.path (tests/fakes or real pywin32); otherwise the fake, by file path."""
    try:
        import pywintypes

        return pywintypes
    except ImportError:
        path = Path(__file__).resolve().parent / "fakes" / "pywintypes.py"
        spec = importlib.util.spec_from_file_location("pywintypes", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["pywintypes"] = module
        spec.loader.exec_module(module)
        return module


pywintypes = _load_pywintypes()
com_error = pywintypes.com_error

# HRESULTs (Windows SDK values; deliberately independent of vissim_mcp.errors)
E_NOTIMPL = -2147467263
E_FAIL = -2147467259
E_INVALIDARG = -2147024809
DISP_E_EXCEPTION = -2147352567
DISP_E_MEMBERNOTFOUND = -2147352573
DISP_E_UNKNOWNNAME = -2147352570
DISP_E_TYPEMISMATCH = -2147352571
DISP_E_BADPARAMCOUNT = -2147352562
TYPE_E_ELEMENTNOTFOUND = -2147319765
RPC_E_DISCONNECTED = -2147417848
RPC_E_WRONG_THREAD = -2147417842
RPC_S_SERVER_UNAVAILABLE = -2147023174
RPC_S_CALL_FAILED = -2147023170
CO_E_CLASSSTRING = -2147221005
CO_E_NOTINITIALIZED = -2147221008
CO_E_SERVER_EXEC_FAILURE = -2146959355

HRESULT_TEXT = {
    E_NOTIMPL: "Not implemented",
    E_FAIL: "Unspecified error",
    E_INVALIDARG: "The parameter is incorrect.",
    DISP_E_EXCEPTION: "Exception occurred.",
    DISP_E_MEMBERNOTFOUND: "Member not found.",
    DISP_E_UNKNOWNNAME: "Unknown name.",
    DISP_E_TYPEMISMATCH: "Type mismatch.",
    DISP_E_BADPARAMCOUNT: "Invalid number of parameters.",
    TYPE_E_ELEMENTNOTFOUND: "Element not found.",
    RPC_E_DISCONNECTED: "The object invoked has disconnected from its clients.",
    RPC_E_WRONG_THREAD: "The application called an interface that was marshalled for a different thread.",
    RPC_S_SERVER_UNAVAILABLE: "The RPC server is unavailable.",
    RPC_S_CALL_FAILED: "The remote procedure call failed.",
    CO_E_CLASSSTRING: "Invalid class string",
    CO_E_NOTINITIALIZED: "CoInitialize has not been called.",
    CO_E_SERVER_EXEC_FAILURE: "Server execution failed",
}

VISSIM_CLSID = "{A93F962D-8DE3-48FC-9F71-B12A0F730FA3}"
NOISE_MARKER = "FAKE-VISSIM-NOISE"
SAVE_FORMAT = "fake-vissim-network/1"

#: SignalizationState enum (1..11) as Vissim's string labels.
SIG_STATES = (
    "RED",
    "REDAMBER",
    "GREEN",
    "AMBER",
    "OFF",
    "UNDEFINED",
    "FLASHINGAMBER",
    "FLASHINGRED",
    "FLASHINGGREEN",
    "ALTERNATINGREDGREEN",
    "GREENAMBER",
)

INVOKE_FUNC = 1
INVOKE_PROPERTYGET = 2
INVOKE_PROPERTYPUT = 4
FUNCFLAG_FRESTRICTED = 0x1
PLUMBING_MEMBERS = (
    ("QueryInterface", 0x60000000),
    ("AddRef", 0x60000001),
    ("Release", 0x60000002),
    ("GetTypeInfoCount", 0x60010000),
    ("GetTypeInfo", 0x60010001),
    ("GetIDsOfNames", 0x60010002),
    ("Invoke", 0x60010003),
)
_KIND_TO_INVOKE = {
    "method": INVOKE_FUNC,
    "property_get": INVOKE_PROPERTYGET,
    "property_put": INVOKE_PROPERTYPUT,
}

_MISSING: Any = object()
_SUBATTR_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*\((.*)\)\s*$")


# --- errors


def make_com_error(
    hresult: int,
    message: str | None = None,
    *,
    description: str | None = None,
    scode: int | None = None,
    source: str = "Vissim",
) -> BaseException:
    """A pywin32-style ``com_error``; with ``description`` it carries EXCEPINFO (like Vissim errors)."""
    if description is not None:
        excepinfo = (0, source, description, None, 0, E_FAIL if scode is None else scode)
        return com_error(
            hresult, message or HRESULT_TEXT.get(hresult, "Exception occurred."), excepinfo, None
        )
    return com_error(hresult, message or HRESULT_TEXT.get(hresult, "Unspecified error"), None, None)


def vissim_error(description: str, scode: int = E_FAIL) -> BaseException:
    """An error raised by Vissim itself: DISP_E_EXCEPTION with ``description`` in EXCEPINFO."""
    return make_com_error(DISP_E_EXCEPTION, description=description, scode=scode)


# --- COM member markers


def com_method(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a fake COM method: the fault hook runs when it is *called* (attribute access is local)."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(self: FakeComObject, *args: Any, **kwargs: Any) -> Any:
        self._root._on_call(f"{self._com_label}.{name}")
        return fn(self, *args, **kwargs)

    wrapper._com_kinds = ("method",)  # type: ignore[attr-defined]
    return wrapper


def com_property(fn: Callable[[Any], Any]) -> property:
    """Mark a fake COM property getter: the fault hook runs on attribute access (a COM round trip)."""
    name = fn.__name__

    def getter(self: FakeComObject) -> Any:
        self._root._on_call(f"{self._com_label}.{name}")
        return fn(self)

    getter.__name__ = name
    getter.__doc__ = fn.__doc__
    getter._com_kinds = ("property_get",)  # type: ignore[attr-defined]
    return property(getter)


def com_members(cls: type) -> list[tuple[str, tuple[str, ...]]]:
    """``[(name, kinds)]`` of the fake COM members of ``cls`` (sorted by name)."""
    found: dict[str, tuple[str, ...]] = {}
    for klass in reversed(cls.__mro__):
        for name, value in vars(klass).items():
            if name.startswith("_"):
                continue
            target = value.fget if isinstance(value, property) else value
            kinds = getattr(target, "_com_kinds", None)
            if kinds:
                found[name] = tuple(kinds)
    return sorted(found.items())


# --- type info


@dataclass(frozen=True)
class FakeTypeAttr:
    cFuncs: int
    cVars: int = 0
    typekind: int = 4  # TKIND_DISPATCH
    guid: str = "{00000000-0000-0000-0000-000000000000}"


@dataclass(frozen=True)
class FakeFuncDesc:
    memid: int
    invkind: int
    funckind: int = 4  # FUNC_DISPATCH
    wFuncFlags: int = 0
    args: tuple[Any, ...] = ()
    cParamsOpt: int = 0


class FakeTypeInfo:
    """Enough of pywin32's PyITypeInfo for ``vissim_describe`` (GetTypeAttr / GetFuncDesc / GetNames)."""

    def __init__(self, interface_name: str, cls: type) -> None:
        self._name = interface_name
        self._entries: list[tuple[str, int, int, int]] = [
            (name, memid, INVOKE_FUNC, FUNCFLAG_FRESTRICTED) for name, memid in PLUMBING_MEMBERS
        ]
        for memid, (name, kinds) in enumerate(com_members(cls), start=1):
            for kind in kinds:
                self._entries.append((name, memid, _KIND_TO_INVOKE[kind], 0))

    def GetTypeAttr(self) -> FakeTypeAttr:
        return FakeTypeAttr(cFuncs=len(self._entries))

    def GetFuncDesc(self, index: int) -> FakeFuncDesc:
        name, memid, invkind, flags = self._entries[index]
        return FakeFuncDesc(memid=memid, invkind=invkind, wFuncFlags=flags)

    def GetNames(self, memid: int) -> tuple[str, ...]:
        for name, entry_memid, _invkind, _flags in self._entries:
            if entry_memid == memid:
                return (name,)
        raise make_com_error(TYPE_E_ELEMENTNOTFOUND)

    def GetDocumentation(self, memid: int) -> tuple[str, str | None, int, str | None]:
        if memid == -1:
            return (self._name, None, 0, None)
        return (self.GetNames(memid)[0], None, 0, None)


class FakeOleObject:
    """Stand-in for pywin32's PyIDispatch (``obj._oleobj_``)."""

    def __init__(self, owner: FakeComObject) -> None:
        self._owner = owner

    def GetTypeInfoCount(self) -> int:
        return 0 if self._unavailable() else 1

    def GetTypeInfo(self, index: int = 0, lcid: int = 0) -> FakeTypeInfo:
        owner = self._owner
        owner._root._on_call(f"{owner._com_label}.GetTypeInfo")
        if self._unavailable():
            raise make_com_error(E_NOTIMPL)
        return FakeTypeInfo(owner._interface_name, type(owner))

    def GetIDsOfNames(self, name: str, *_names: str) -> int:
        for memid, (member, _kinds) in enumerate(com_members(type(self._owner)), start=1):
            if member.lower() == str(name).lower():
                return memid
        raise make_com_error(DISP_E_UNKNOWNNAME)

    def _unavailable(self) -> bool:
        return self._owner._root.no_type_info or not self._owner._has_type_info


# --- base objects


class FakeComObject:
    """Base of every fake COM object (holds the root for the fault hook)."""

    _interface_name = "IDispatch"
    _has_type_info = True

    def __init__(self, root: FakeVissim | None, label: str) -> None:
        self._root: FakeVissim = root if root is not None else self  # type: ignore[assignment]
        self._com_label = label

    @property
    def _oleobj_(self) -> FakeOleObject:
        return FakeOleObject(self)

    def __bool__(self) -> bool:  # pywin32's CDispatch is always truthy (no implicit Count call)
        return True

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._com_label}>"


@dataclass
class AttrSpec:
    """One attribute of a fake object: value, value type, editability and range."""

    name: str
    value: Any = None
    vtype: str = "str"  # int | float | bool | str | filename | sigstate
    editable: bool = True
    sim_readonly: bool = False
    minimum: float | None = None
    maximum: float | None = None


class _TypeMismatch(Exception):
    pass


def coerce_value(spec: AttrSpec, value: Any) -> Any:
    """Convert ``value`` like COM VARIANT coercion would; raise COM-like errors on bad types or ranges."""
    try:
        result = _coerce(spec.vtype, value)
    except _TypeMismatch:
        raise make_com_error(DISP_E_TYPEMISMATCH) from None
    if spec.vtype in ("int", "float"):
        if (spec.minimum is not None and result < spec.minimum) or (
            spec.maximum is not None and result > spec.maximum
        ):
            raise vissim_error(
                f"SetAttValue failed: value {value!r} for attribute {spec.name} is out of range "
                f"[{_fmt(spec.minimum)}, {_fmt(spec.maximum)}]."
            )
    return result


def _fmt(bound: float | None) -> str:
    return "-" if bound is None else f"{bound:g}"


def _coerce(vtype: str, value: Any) -> Any:
    if vtype == "int":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*[-+]?\d+\s*", value):
            return int(value)
        raise _TypeMismatch
    if vtype == "float":
        if isinstance(value, (bool, int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                raise _TypeMismatch from None
        else:
            raise _TypeMismatch
        if not math.isfinite(number):
            raise _TypeMismatch
        return number
    if vtype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (-1, 0, 1):
            return value != 0
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return value.strip().lower() in ("true", "1")
        raise _TypeMismatch
    if vtype == "filename":
        if not isinstance(value, str):
            raise _TypeMismatch
        return value
    if vtype == "str":
        if value is None or isinstance(value, (list, tuple, dict)):
            raise _TypeMismatch
        return value if isinstance(value, str) else str(value)
    if vtype == "sigstate":
        if isinstance(value, bool):
            raise _TypeMismatch
        if isinstance(value, int):
            if 1 <= value <= len(SIG_STATES):
                return SIG_STATES[value - 1]
            raise vissim_error(
                f"SetAttValue failed: {value} is not a valid signal state (1..{len(SIG_STATES)})."
            )
        if isinstance(value, str):
            label = value.strip().upper().replace("_", "").replace(" ", "")
            if label in SIG_STATES:
                return label
            raise vissim_error(f"SetAttValue failed: {value!r} is not a valid signal state.")
        raise _TypeMismatch
    raise _TypeMismatch


def _attr_arg(attribute: Any) -> str:
    if not isinstance(attribute, str) or not attribute.strip():
        raise make_com_error(DISP_E_TYPEMISMATCH)
    return attribute.strip()


class FakeAttributeObject(FakeComObject):
    """An object with ``AttValue`` / ``SetAttValue`` (attribute names are case-insensitive)."""

    def __init__(
        self,
        root: FakeVissim | None,
        label: str,
        specs: Sequence[AttrSpec] = (),
        results: Sequence[str] = (),
    ) -> None:
        super().__init__(root, label)
        self._attrs: dict[str, AttrSpec] = {spec.name.lower(): spec for spec in specs}
        self._result_bases: dict[str, str] = {name.lower(): name for name in results}

    # -- internal access (no COM hook) ------------------------------------------------------------------

    def _describe(self) -> str:
        return self._com_label

    def _computed(self, key: str) -> Any:
        """Override for read-only attributes computed on the fly (return ``_MISSING`` if not handled)."""
        return _MISSING

    def _computed_names(self) -> tuple[str, ...]:
        return ()

    def _get(self, name: str) -> Any:
        key = name.lower()
        value = self._computed(key)
        if value is not _MISSING:
            return value
        spec = self._attrs.get(key)
        if spec is not None:
            return spec.value
        match = _SUBATTR_RE.match(name)
        if match and match.group(1).lower() in self._result_bases:
            base = self._result_bases[match.group(1).lower()]
            args = [part.strip() for part in match.group(2).split(",")] if match.group(2).strip() else []
            return self._root._result_value(self, base, args)
        raise vissim_error(f"AttValue failed: {self._describe()}: unknown attribute '{name}'.", E_INVALIDARG)

    def _set(self, name: str, value: Any) -> None:
        key = name.lower()
        spec = self._attrs.get(key)
        if spec is None:
            match = _SUBATTR_RE.match(name)
            is_result = bool(match and match.group(1).lower() in self._result_bases)
            if is_result or key in {n.lower() for n in self._computed_names()}:
                raise vissim_error(
                    f"SetAttValue failed: {self._describe()}: attribute '{name}' is read-only."
                )
            raise vissim_error(
                f"SetAttValue failed: {self._describe()}: unknown attribute '{name}'.", E_INVALIDARG
            )
        if not spec.editable:
            raise vissim_error(
                f"SetAttValue failed: {self._describe()}: attribute '{spec.name}' is read-only."
            )
        if spec.sim_readonly and self._root._sim_active:
            raise vissim_error(
                f"SetAttValue failed: {self._describe()}: attribute '{spec.name}' cannot be changed while a "
                "simulation run is in progress."
            )
        spec.value = coerce_value(spec, value)
        self._after_set(spec)

    def _after_set(self, spec: AttrSpec) -> None:
        """Override for side effects of writing an attribute."""

    def _attribute_infos(self) -> list[tuple[str, str, bool]]:
        infos = [(spec.name, spec.vtype, spec.editable) for spec in self._attrs.values()]
        infos.extend((name, "float", False) for name in self._computed_names())
        infos.extend((name, "result", False) for name in self._result_bases.values())
        return infos

    # -- COM members ----------------------------------------------------------------------------------------

    @com_method
    def AttValue(self, attribute: str) -> Any:
        return self._get(_attr_arg(attribute))

    AttValue._com_kinds = ("property_get", "property_put")  # type: ignore[attr-defined]

    @com_method
    def SetAttValue(self, attribute: str, value: Any) -> None:
        self._set(_attr_arg(attribute), value)


class FakeNetObject(FakeAttributeObject):
    """A network object (Link, Signal group, ...) identified by its key."""

    _interface_name = "INetObject"

    def __init__(
        self,
        root: FakeVissim,
        item_label: str,
        key: Any,
        specs: Sequence[AttrSpec],
        results: Sequence[str] = (),
    ) -> None:
        super().__init__(root, item_label, specs, results)
        self._key = key
        self._children: dict[str, FakeCollection] = {}

    def _describe(self) -> str:
        return f"{self._com_label} {self._key}"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._com_label} {self._key!r}>"


class FakeSignalController(FakeNetObject):
    @com_property
    def SGs(self) -> FakeCollection:
        return self._children["SGs"]


class FakeSignalGroup(FakeNetObject):
    def _after_set(self, spec: AttrSpec) -> None:
        if spec.name.lower() == "sigstate":
            contr = self._attrs.get("contrbycom")
            if contr is not None:
                contr.value = True  # documented: writing SigState switches the group to COM control


class FakeRoutingDecision(FakeNetObject):
    @com_property
    def VehRoutSta(self) -> FakeCollection:
        return self._children["VehRoutSta"]


# --- collections


@dataclass(frozen=True)
class CollectionTemplate:
    label: str
    item_label: str
    attrs: tuple[AttrSpec, ...]
    results: tuple[str, ...] = ()
    item_class: type[FakeNetObject] = FakeNetObject
    children: tuple[str, ...] = ()


def _spec(name: str, value: Any, vtype: str, editable: bool = True, **kwargs: Any) -> AttrSpec:
    return AttrSpec(name=name, value=value, vtype=vtype, editable=editable, **kwargs)


TEMPLATES: dict[str, CollectionTemplate] = {
    "Links": CollectionTemplate(
        "Links",
        "Link",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("Length2D", 100.0, "float", False),
            _spec("NumLanes", 1, "int", minimum=1, maximum=20),
            _spec("LinkBehavType", 1, "int", minimum=1),
            _spec("DisplayType", 1, "int", minimum=1),
            _spec("IsConn", False, "bool", False),
        ),
    ),
    "Nodes": CollectionTemplate("Nodes", "Node", (_spec("No", 0, "int", False), _spec("Name", "", "str"))),
    "SignalControllers": CollectionTemplate(
        "SignalControllers",
        "SignalController",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("Type", "FixedTime", "str", False),
            _spec("ProgNo", 1, "int", minimum=1),
            _spec("SupplyFile1", "", "filename"),
        ),
        item_class=FakeSignalController,
        children=("SGs",),
    ),
    "SGs": CollectionTemplate(
        "SGs",
        "SignalGroup",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("SigState", "RED", "sigstate"),
            _spec("ContrByCOM", False, "bool"),
        ),
        item_class=FakeSignalGroup,
    ),
    "SignalHeads": CollectionTemplate(
        "SignalHeads",
        "SignalHead",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("SG", "", "str", False),
            _spec("Lane", "", "str", False),
        ),
    ),
    "VehicleInputs": CollectionTemplate(
        "VehicleInputs",
        "VehicleInput",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("Link", 0, "int", False),
            _spec("Volume(1)", 500.0, "float", minimum=0),
            _spec("VehComp(1)", 1, "int", minimum=1),
        ),
    ),
    "VehicleRoutingDecisionsStatic": CollectionTemplate(
        "VehicleRoutingDecisionsStatic",
        "VehicleRoutingDecisionStatic",
        (
            _spec("No", 0, "int", False),
            _spec("Name", "", "str"),
            _spec("Link", 0, "int", False),
            _spec("Pos", 10.0, "float", minimum=0),
        ),
        item_class=FakeRoutingDecision,
        children=("VehRoutSta",),
    ),
    "VehRoutSta": CollectionTemplate(
        "VehRoutSta",
        "VehicleRouteStatic",
        (
            _spec("No", 0, "int", False),
            _spec("RelFlow(1)", 1.0, "float", minimum=0),
            _spec("DestLink", 0, "int", False),
        ),
    ),
    "DataCollectionMeasurements": CollectionTemplate(
        "DataCollectionMeasurements",
        "DataCollectionMeasurement",
        (_spec("No", 0, "int", False), _spec("Name", "", "str")),
        results=("Vehs", "Speed", "OccupRate"),
    ),
    "VehicleTravelTimeMeasurements": CollectionTemplate(
        "VehicleTravelTimeMeasurements",
        "VehicleTravelTimeMeasurement",
        (_spec("No", 0, "int", False), _spec("Name", "", "str")),
        results=("TravTm", "Vehs", "DistTrav"),
    ),
    "QueueCounters": CollectionTemplate(
        "QueueCounters",
        "QueueCounter",
        (_spec("No", 0, "int", False), _spec("Name", "", "str"), _spec("Link", 0, "int", False)),
        results=("QLen", "QLenMax", "QStops"),
    ),
    "SimulationRuns": CollectionTemplate(
        "SimulationRuns",
        "SimulationRun",
        (
            _spec("No", 0, "int", False),
            _spec("RandSeed", 0, "int", False),
            _spec("SimEnd", 0.0, "float", False),
            _spec("Timestamp", "", "str", False),
            _spec("Comment", "", "str"),
        ),
    ),
    "Vehicles": CollectionTemplate(
        "Vehicles",
        "Vehicle",
        (
            _spec("No", 0, "int", False),
            _spec("VehType", "100", "str", False),
            _spec("Lane", "1-1", "str", False),
            _spec("Pos", 0.0, "float", False),
            _spec("Speed", 0.0, "float", False),
        ),
    ),
}

NET_COLLECTIONS = (
    "Links",
    "Nodes",
    "SignalControllers",
    "SignalHeads",
    "VehicleInputs",
    "VehicleRoutingDecisionsStatic",
    "DataCollectionMeasurements",
    "VehicleTravelTimeMeasurements",
    "QueueCounters",
    "SimulationRuns",
    "Vehicles",
)
_NOT_SAVED = frozenset({"SimulationRuns", "Vehicles"})


class FakeAttributeInfo(FakeComObject):
    """``IAttribute``: description of one attribute of a collection's objects."""

    _interface_name = "IAttribute"

    def __init__(self, root: FakeVissim, code: str, vtype: str, editable: bool) -> None:
        super().__init__(root, "Attribute")
        self._code = code
        self._vtype = vtype
        self._editable = editable

    @com_property
    def Code(self) -> str:
        return self._code

    @com_property
    def Name(self) -> str:
        return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", self._code)

    @com_property
    def ValueType(self) -> str:
        return {
            "int": "Integer",
            "float": "Floating-point number",
            "bool": "Boolean",
            "str": "Text",
            "filename": "File name",
            "sigstate": "SignalizationState",
            "result": "Floating-point number",
        }.get(self._vtype, "Text")

    @com_property
    def Editable(self) -> bool:
        return self._editable


class FakeAttributeContainer(FakeComObject):
    """``IAttributeContainer`` (``<collection>.Attributes``)."""

    _interface_name = "IAttributeContainer"

    def __init__(self, root: FakeVissim, infos: Sequence[FakeAttributeInfo]) -> None:
        super().__init__(root, "Attributes")
        self._infos = {info._code.lower(): info for info in infos}

    @com_property
    def Count(self) -> int:
        return len(self._infos)

    @com_method
    def GetAll(self) -> tuple[FakeAttributeInfo, ...]:
        return tuple(self._infos.values())

    @com_method
    def ItemByKey(self, code: str) -> FakeAttributeInfo:
        info = self._infos.get(str(code).lower())
        if info is None:
            raise vissim_error(f"ItemByKey failed: attribute {code!r} does not exist.", E_INVALIDARG)
        return info

    @com_method
    def ItemKeyExists(self, code: str) -> bool:
        return str(code).lower() in self._infos


class FakeCollection(FakeComObject):
    """A keyed collection (``Net.Links``, ``SignalController.SGs``, ...)."""

    _interface_name = "IObjectContainer"

    def __init__(self, root: FakeVissim, template: CollectionTemplate) -> None:
        super().__init__(root, template.label)
        self._template = template
        self._items: dict[Any, FakeNetObject] = {}

    # -- internal ---------------------------------------------------------------------------------------

    def _all(self) -> dict[Any, FakeNetObject]:
        return self._items

    def _find(self, key: Any) -> FakeNetObject | None:
        """Exact key, then VARIANT-like coercion (``"5"`` <-> ``5``, ``5.0`` -> ``5``); bools never match."""
        if isinstance(key, bool) or not isinstance(key, (int, float, str)):
            return None
        items = self._all()
        if key in items:
            return items[key]
        if isinstance(key, str) and re.fullmatch(r"\s*[-+]?\d+\s*", key):
            return items.get(int(key))
        if isinstance(key, float) and key.is_integer():
            return items.get(int(key))
        if isinstance(key, int):
            return items.get(str(key))
        return None

    def _require(self, key: Any) -> FakeNetObject:
        found = self._find(key)
        if found is None:
            raise vissim_error(
                f"ItemByKey failed: {self._template.item_label} with key {key!r} does not exist.",
                E_INVALIDARG,
            )
        return found

    def _add(self, key: Any, values: Mapping[str, Any] | None = None) -> FakeNetObject:
        template = self._template
        specs = [replace(spec) for spec in template.attrs]
        obj = template.item_class(self._root, template.item_label, key, specs, template.results)
        if "no" in obj._attrs:
            obj._attrs["no"].value = key
        for name, value in (values or {}).items():
            spec = obj._attrs.get(name.lower())
            if spec is None:
                spec = AttrSpec(name=name, value=value, vtype=_infer_vtype(value))
                obj._attrs[name.lower()] = spec
            else:
                spec.value = value
        for child in template.children:
            obj._children[child] = FakeCollection(self._root, TEMPLATES[child])
        self._items[key] = obj
        return obj

    # -- COM members ----------------------------------------------------------------------------------------

    @com_property
    def Count(self) -> int:
        return len(self._all())

    @com_method
    def ItemByKey(self, key: Any) -> FakeNetObject:
        return self._require(key)

    @com_method
    def ItemKeyExists(self, key: Any) -> bool:
        return self._find(key) is not None

    @com_method
    def GetAll(self) -> tuple[FakeNetObject, ...]:
        return tuple(self._all().values())

    @com_method
    def GetMultipleAttributes(self, attributes: Sequence[str]) -> tuple[tuple[Any, ...], ...]:
        names = _attribute_list(attributes)
        return tuple(tuple(obj._get(name) for name in names) for obj in self._all().values())

    @com_method
    def SetMultipleAttributes(self, attributes: Sequence[str], values: Sequence[Sequence[Any]]) -> None:
        names = _attribute_list(attributes)
        objects = list(self._all().values())
        rows = list(values) if isinstance(values, (list, tuple)) else None
        if (
            rows is None
            or len(rows) != len(objects)
            or any(not isinstance(row, (list, tuple)) or len(row) != len(names) for row in rows)
        ):
            raise make_com_error(E_INVALIDARG)
        changes = [
            (obj, name, row[index])
            for obj, row in zip(objects, rows, strict=True)
            for index, name in enumerate(names)
        ]
        _apply_atomically(changes)

    @com_method
    def GetMultiAttValues(self, attribute: str) -> tuple[tuple[Any, Any], ...]:
        name = _attr_arg(attribute)
        return tuple((key, obj._get(name)) for key, obj in self._all().items())

    @com_method
    def SetMultiAttValues(self, attribute: str, values: Sequence[Sequence[Any]]) -> None:
        name = _attr_arg(attribute)
        changes = []
        for pair in values:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise make_com_error(E_INVALIDARG)
            changes.append((self._require(pair[0]), name, pair[1]))
        _apply_atomically(changes)

    @com_method
    def SetAllAttValues(self, attribute: str, value: Any, add: bool = False) -> None:
        name = _attr_arg(attribute)
        changes = []
        for obj in self._all().values():
            new = value
            if add:
                current = obj._get(name)
                if not isinstance(current, (int, float)) or isinstance(current, bool):
                    raise make_com_error(DISP_E_TYPEMISMATCH)
                new = current + value
            changes.append((obj, name, new))
        _apply_atomically(changes)

    @com_property
    def Attributes(self) -> FakeAttributeContainer:
        sample = next(iter(self._all().values()), None)
        if sample is not None:
            infos = sample._attribute_infos()
        else:
            template = self._template
            infos = [(s.name, s.vtype, s.editable) for s in template.attrs]
            infos += [(r, "result", False) for r in template.results]
        return FakeAttributeContainer(
            self._root,
            [FakeAttributeInfo(self._root, code, vtype, editable) for code, vtype, editable in infos],
        )

    def __iter__(self) -> Iterator[FakeNetObject]:
        self._root._on_call(f"{self._com_label}._NewEnum")
        return iter(tuple(self._all().values()))

    def __len__(self) -> int:
        self._root._on_call(f"{self._com_label}.Count")
        return len(self._all())


def _attribute_list(attributes: Any) -> list[str]:
    if isinstance(attributes, str) or not isinstance(attributes, (list, tuple)):
        raise make_com_error(DISP_E_TYPEMISMATCH)
    return [_attr_arg(name) for name in attributes]


def _apply_atomically(changes: Sequence[tuple[FakeAttributeObject, str, Any]]) -> None:
    """Validate every change first (like a single COM call), then apply them all."""
    for obj, name, value in changes:
        spec = obj._attrs.get(name.lower())
        if spec is None or not spec.editable or (spec.sim_readonly and obj._root._sim_active):
            obj._set(name, value)  # raises the matching COM error (unknown / read-only attribute)
            continue
        coerce_value(spec, value)
    for obj, name, value in changes:
        obj._set(name, value)


def _infer_vtype(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


class FakeVehicles(FakeCollection):
    """``Net.Vehicles``: vehicles exist only while a simulation run is in progress."""

    MAX_VEHICLES = 200

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(root, TEMPLATES["Vehicles"])
        self._snapshot_key: tuple[int, float] | None = None

    def _all(self) -> dict[Any, FakeNetObject]:
        root = self._root
        state = (root._run_no, root._simsec) if root._sim_active else None
        if state != self._snapshot_key:
            self._items = {}
            if state is not None:
                count = min(self.MAX_VEHICLES, int(root._simsec // 5))
                for number in range(1, count + 1):
                    pos = round((number * 37.5 + root._simsec) % 500.0, 2)
                    speed = round(30.0 + (zlib.crc32(f"{root._run_no}|{number}".encode()) % 300) / 10, 1)
                    self._add(number, {"Pos": pos, "Speed": speed, "Lane": f"{1 + number % 3}-1"})
            self._snapshot_key = state
        return self._items


# --- top-level objects


def _collection_property(name: str) -> property:
    def getter(self: FakeNet) -> FakeCollection:
        self._root._on_call(f"Net.{name}")
        return self._collections[name]

    getter.__name__ = name
    getter._com_kinds = ("property_get",)  # type: ignore[attr-defined]
    return property(getter)


class FakeNet(FakeAttributeObject):
    """``Vissim.Net`` with the collections of ``NET_COLLECTIONS``."""

    _interface_name = "INet"

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(root, "Net", [_spec("Name", "", "str"), _spec("NorthDir", 0.0, "float")])
        self._collections: dict[str, FakeCollection] = {}
        for name in NET_COLLECTIONS:
            self._collections[name] = (
                FakeVehicles(root) if name == "Vehicles" else FakeCollection(root, TEMPLATES[name])
            )


for _name in NET_COLLECTIONS:
    setattr(FakeNet, _name, _collection_property(_name))
del _name


class FakeSimulation(FakeAttributeObject):
    """``Vissim.Simulation`` (the run itself is driven by the root, see ``FakeVissim._run_continuous``)."""

    _interface_name = "ISimulation"

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(
            root,
            "Simulation",
            [
                _spec("SimPeriod", 3600, "int", sim_readonly=True, minimum=1, maximum=2_678_400),
                _spec("SimRes", 10, "int", sim_readonly=True, minimum=1, maximum=1000),
                _spec("SimBreakAt", 0.0, "float", minimum=0),
                _spec("RandSeed", 42, "int", sim_readonly=True, minimum=1),
                _spec("RandSeedIncr", 1, "int", minimum=1),
                _spec("NumRuns", 1, "int", minimum=1),
                _spec("UseMaxSimSpeed", False, "bool"),
                _spec("SimSpeed", 1.0, "float", minimum=0.1),
                _spec("UseAllCores", True, "bool"),
                _spec("NumCores", 1, "int", minimum=1),
                _spec("IsSimBreakAtActive", False, "bool"),
            ],
        )

    def _computed(self, key: str) -> Any:
        if key == "simsec":
            return self._root._displayed_simsec()
        if key == "isrunning":
            return self._root._in_run_call
        return _MISSING

    def _computed_names(self) -> tuple[str, ...]:
        return ("SimSec", "IsRunning")

    @com_method
    def RunContinuous(self) -> None:
        self._root._run_continuous()

    @com_method
    def RunSingleStep(self) -> None:
        self._root._run_single_step()

    @com_method
    def Stop(self) -> None:
        self._root._stop_run()


class FakeEvaluation(FakeAttributeObject):
    _interface_name = "IEvaluation"

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(
            root,
            "Evaluation",
            [
                _spec("KeepPrevResults", 3, "int", minimum=1, maximum=3),
                _spec("EvalOutDir", "", "filename"),
                _spec("DataCollCollectData", False, "bool"),
                _spec("VehTravTmCollectData", False, "bool"),
                _spec("QueuesCollectData", False, "bool"),
            ],
        )


class FakeNetworkWindow(FakeAttributeObject):
    _interface_name = "INetworkWindow"

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(root, "NetworkWindow", [_spec("QuickMode", 0, "int", minimum=0, maximum=1)])


class FakeGraphics(FakeComObject):
    _interface_name = "IGraphics"

    def __init__(self, root: FakeVissim) -> None:
        super().__init__(root, "Graphics")
        self._window = FakeNetworkWindow(root)

    @com_property
    def CurrentNetworkWindow(self) -> FakeNetworkWindow:
        return self._window


# --- the root


class FakeVissim(FakeComObject):
    """The ``Vissim.Vissim`` COM root object."""

    _interface_name = "IVissim"

    def __init__(
        self,
        progid: str = "Vissim.Vissim",
        *,
        network: str = "demo",
        print_to_stdout_on_call: bool = False,
        native_noise: bool = False,
        enforce_thread: bool = True,
        simsec_after_run: str = "zero",
        stale_break_at: float | Sequence[float] | None = None,
        ignore_break: bool = False,
        stall_runs: int = 0,
        crash_after_calls: int | None = None,
        wall_s_per_sim_s: float = 0.0,
        no_type_info: bool = False,
        reject_non_ascii_paths: bool = False,
        exit_keeps_process: bool = False,
        exit_process_delay_s: float = 0.0,
        exit_error: int | None = None,
        dead_call_hresult: int = RPC_S_SERVER_UNAVAILABLE,
        spawn_cmd: Sequence[str] | None = None,
        spawn_env: Mapping[str, str] | None = None,
        hang_on: Mapping[str, float] | None = None,
    ) -> None:
        super().__init__(None, "Vissim")
        if simsec_after_run not in ("zero", "period"):
            raise ValueError("simsec_after_run must be 'zero' or 'period'")
        if network not in ("demo", "empty"):
            raise ValueError("network must be 'demo' or 'empty'")
        self.progid = progid
        self.print_to_stdout_on_call = print_to_stdout_on_call
        self.native_noise = native_noise
        self.enforce_thread = enforce_thread
        self.simsec_after_run = simsec_after_run
        self.ignore_break = ignore_break
        self.stall_runs = stall_runs
        self.crash_after_calls = crash_after_calls
        self.wall_s_per_sim_s = wall_s_per_sim_s
        self.no_type_info = no_type_info
        self.reject_non_ascii_paths = reject_non_ascii_paths
        self.exit_keeps_process = exit_keeps_process
        self.exit_process_delay_s = float(exit_process_delay_s)
        self.exit_error = exit_error
        self.dead_call_hresult = dead_call_hresult

        self._lock = threading.Lock()
        self._owner_thread = threading.get_ident()
        self.owner_thread_name = threading.current_thread().name
        self.calls: deque[tuple[str, str]] = deque(maxlen=10_000)
        self.call_count = 0
        self._pending_errors: list[BaseException] = []
        self._pending_hang: float | None = None
        self._hang_rules: dict[str, list[float]] = {}
        self._hang_release = threading.Event()
        self._crashed = False
        self._exited = False

        # simulation state
        self._sim_active = False
        self._in_run_call = False
        self._simsec = 0.0
        self._run_no = 0
        self._run_seed: int | None = None
        self._last_run_end: float | None = None
        self._stale_breaks: list[float] = []
        self.stale_break_at = stale_break_at
        self.run_records: list[dict[str, Any]] = []

        # files and GUI
        self.loaded_net_path: str | None = None
        self.layout_path: str | None = None
        self.saved_paths: list[str] = []
        self.load_count = 0
        self.last_load_additive: bool | None = None
        self.gui_suspended = False

        self._simulation = FakeSimulation(self)
        self._evaluation = FakeEvaluation(self)
        self._graphics = FakeGraphics(self)
        self._net = FakeNet(self)
        if network == "demo":
            populate_demo_network(self)

        for member, seconds in (hang_on or {}).items():
            self.hang_on(member, seconds)

        self.process: Any = None  # subprocess.Popen (or a Popen-like test double)
        if spawn_cmd:
            self.process = spawn_standin(spawn_cmd, spawn_env)

    # --- construction from env

    @classmethod
    def from_env(cls, progid: str = "Vissim.Vissim", environ: Mapping[str, str] | None = None) -> FakeVissim:
        """Create a FakeVissim configured by ``FAKE_VISSIM_*`` variables (used by the fake ``Dispatch``)."""
        return cls(progid, **options_from_env(os.environ if environ is None else environ))

    # --- the fault hook

    def _on_call(self, member: str) -> None:
        """Runs at the start of every COM member access."""
        with self._lock:
            self.call_count += 1
            self.calls.append((member, threading.current_thread().name))
            if self.crash_after_calls is not None and self.call_count > self.crash_after_calls:
                self._crashed = True
            pending_hang, self._pending_hang = self._pending_hang, None
            rule_hang = self._take_hang_rule(member)
            pending_error = self._pending_errors.pop(0) if self._pending_errors else None
            disconnected = self._crashed or self._exited
        if self.enforce_thread and threading.get_ident() != self._owner_thread:
            raise make_com_error(RPC_E_WRONG_THREAD)
        if self.print_to_stdout_on_call:
            self._make_noise(member)
        if disconnected:
            raise make_com_error(RPC_E_DISCONNECTED)
        for seconds in (pending_hang, rule_hang):
            if seconds:
                self._hang_release.wait(seconds)
                if self._crashed:  # the process died during the hung call (e.g. it was killed)
                    raise make_com_error(self.dead_call_hresult)
        if pending_error is not None:
            raise pending_error

    def _take_hang_rule(self, member: str) -> float | None:
        short = member.rsplit(".", 1)[-1]
        for key in (member, short):
            queue = self._hang_rules.get(key)
            if queue:
                seconds = queue.pop(0)
                if not queue:
                    del self._hang_rules[key]
                return seconds
        return None

    def _make_noise(self, member: str) -> None:
        print(f"{NOISE_MARKER} print() in {member}")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os.write(1, f"{NOISE_MARKER} os.write(1) in {member}\n".encode())
        if self.native_noise:
            native_stdout_write(f"{NOISE_MARKER} native WriteFile in {member}\n".encode())

    # --- fault injection API

    def hang_next_call(self, seconds: float) -> None:
        """The next COM call blocks for ``seconds`` (or until ``release_hang()``), then proceeds normally."""
        with self._lock:
            self._hang_release.clear()
            self._pending_hang = float(seconds)

    def hang_on(self, member: str, seconds: float, times: int = 1) -> None:
        """Calls of ``member`` (``"LoadNet"`` or ``"Simulation.RunContinuous"``) block ``times`` times."""
        with self._lock:
            self._hang_release.clear()
            self._hang_rules.setdefault(member, []).extend([float(seconds)] * times)

    def release_hang(self) -> None:
        """End every current and armed hang immediately."""
        with self._lock:
            self._pending_hang = None
            self._hang_rules.clear()
        self._hang_release.set()

    def raise_com_error_next(
        self, hresult: int, description: str | None = None, scode: int | None = None, *, count: int = 1
    ) -> None:
        """The next ``count`` COM calls raise ``com_error(hresult)`` (with EXCEPINFO if ``description``)."""
        if hresult == DISP_E_EXCEPTION and description is None:
            description = "Simulated Vissim error."
        with self._lock:
            for _ in range(count):
                self._pending_errors.append(make_com_error(hresult, description=description, scode=scode))

    def crash(self, kill_process: bool = True) -> None:
        """Simulate a Vissim crash: every later call raises RPC_E_DISCONNECTED (and the process dies)."""
        with self._lock:
            self._crashed = True
            self._sim_active = False
        if kill_process:
            self.kill_process()

    def process_ended(self, how: str) -> None:
        """The stand-in process ended (``how`` = exit, terminate, kill, crash).

        Except after ``Exit()`` this is a lost server: later calls raise ``RPC_E_DISCONNECTED`` and a hung
        call fails at once with ``dead_call_hresult`` - like real COM when the Vissim process is killed.
        """
        if how != "exit" or not self._exited:
            with self._lock:
                self._crashed = True
                self._sim_active = False
        self._hang_release.set()

    @property
    def crashed(self) -> bool:
        return self._crashed

    @property
    def exited(self) -> bool:
        return self._exited

    @property
    def stale_break_at(self) -> list[float]:
        return list(self._stale_breaks)

    @stale_break_at.setter
    def stale_break_at(self, value: float | Sequence[float] | None) -> None:
        if value is None:
            points: list[float] = []
        elif isinstance(value, (int, float)):
            points = [float(value)]
        else:
            points = [float(v) for v in value]
        self._stale_breaks = sorted(p for p in points if p > 0)

    # --- inspection API

    @property
    def sim_active(self) -> bool:
        return self._sim_active

    @property
    def sim_sec(self) -> float:
        return self._simsec

    @property
    def run_no(self) -> int:
        return self._run_no

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def collection(self, path: str) -> FakeCollection:
        """Collection by path without COM side effects: ``"Links"``, ``"SignalControllers[1].SGs"``."""
        current: Any = self._net._collections
        collection: FakeCollection | None = None
        for part in path.split("."):
            match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)(?:\[(.+)\])?", part.strip())
            if not match:
                raise KeyError(path)
            name, key = match.group(1), match.group(2)
            if isinstance(current, dict):
                collection = current[name]
            else:
                collection = current._children[name]
            if key is not None:
                current = collection._find(_parse_key(key))
                if current is None:
                    raise KeyError(path)
        if collection is None:
            raise KeyError(path)
        return collection

    def add_object(self, path: str, key: Any, **values: Any) -> FakeNetObject:
        """Add an object (any key type) to the collection at ``path``."""
        return self.collection(path)._add(key, values)

    def attribute(self, path: str, key: Any, name: str) -> Any:
        """Read an attribute without COM side effects (for assertions from any thread)."""
        found = self.collection(path)._find(key)
        if found is None:
            raise KeyError(f"{path}[{key!r}]")
        return found._get(name)

    def simulation_attribute(self, name: str) -> Any:
        return self._simulation._get(name)

    def set_simulation_attribute(self, name: str, value: Any) -> None:
        """Write a Simulation attribute directly (bypasses the run-in-progress check and the COM hook)."""
        spec = self._simulation._attrs[name.lower()]
        spec.value = coerce_value(spec, value)

    # --- COM properties

    @com_property
    def Net(self) -> FakeNet:
        return self._net

    @com_property
    def Simulation(self) -> FakeSimulation:
        return self._simulation

    @com_property
    def Evaluation(self) -> FakeEvaluation:
        return self._evaluation

    @com_property
    def Graphics(self) -> FakeGraphics:
        return self._graphics

    # --- COM methods

    @com_method
    def LoadNet(self, path: str | None = None, additive: bool = False) -> None:
        file = self._existing_file(path, "LoadNet")
        self._abort_run()
        data = _read_saved_network(file)
        if not additive:
            self._reset_model(populate=data is None)
        if data is not None:
            self._restore_network(data)
        self.loaded_net_path = str(file)
        self.layout_path = None
        self.load_count += 1
        self.last_load_additive = bool(additive)

    @com_method
    def LoadLayout(self, path: str | None = None) -> None:
        file = self._existing_file(path, "LoadLayout")
        self.layout_path = str(file)

    @com_method
    def SaveNetAs(self, path: str | None = None) -> None:
        if not isinstance(path, str) or not path.strip():
            raise vissim_error("SaveNetAs failed: no file name given.", E_INVALIDARG)
        file = Path(path)
        if self.reject_non_ascii_paths and not path.isascii():
            raise vissim_error(f"SaveNetAs failed: cannot write {path}.")
        if not file.parent.is_dir():
            raise vissim_error(f"SaveNetAs failed: directory {file.parent} does not exist.")
        payload = {"format": SAVE_FORMAT, "saved_at": datetime.now().isoformat(), **self._serialize_network()}
        try:
            file.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:
            raise vissim_error(f"SaveNetAs failed: {exc}") from None
        self.saved_paths.append(str(file))
        self.loaded_net_path = str(file)

    @com_method
    def SaveNet(self) -> None:
        if not self.loaded_net_path:
            raise vissim_error("SaveNet failed: the network has no file name yet; use SaveNetAs.")
        path = self.loaded_net_path
        payload = {"format": SAVE_FORMAT, "saved_at": datetime.now().isoformat(), **self._serialize_network()}
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        self.saved_paths.append(path)

    @com_method
    def New(self) -> None:
        self._abort_run()
        self._reset_model(populate=False)
        self.loaded_net_path = None
        self.layout_path = None

    @com_method
    def Exit(self) -> None:
        with self._lock:
            self._exited = True
            self._sim_active = False
        if not self.exit_keeps_process:
            if self.exit_process_delay_s > 0:  # Vissim needs a moment to shut down
                timer = threading.Timer(self.exit_process_delay_s, self.terminate_process)
                timer.daemon = True
                timer.start()
            else:
                self.terminate_process()
        if self.exit_error is not None:  # the server went away before its reply arrived
            raise make_com_error(self.exit_error)

    @com_method
    def SuspendUpdateGUI(self) -> None:
        self.gui_suspended = True

    @com_method
    def ResumeUpdateGUI(self, update: bool = True) -> None:
        self.gui_suspended = False

    # --- simulation engine

    def _period(self) -> float:
        return float(self._simulation._attrs["simperiod"].value)

    def _resolution(self) -> int:
        return int(self._simulation._attrs["simres"].value)

    def _displayed_simsec(self) -> float:
        if self._sim_active:
            return self._simsec
        if self.simsec_after_run == "period" and self._last_run_end is not None:
            return self._last_run_end
        return 0.0

    def _start_run(self) -> None:
        self._run_no += 1
        self._run_seed = int(self._simulation._attrs["randseed"].value)
        self._simsec = 0.0
        self._sim_active = True
        self._last_run_end = None

    def _align(self, target: float, period: float) -> float:
        resolution = self._resolution()
        aligned = math.ceil(target * resolution - 1e-9) / resolution
        return min(aligned, period)

    def _advance_to(self, target: float) -> None:
        if self.wall_s_per_sim_s > 0:
            time.sleep(max(0.0, target - self._simsec) * self.wall_s_per_sim_s)
        self._simsec = target
        self._stale_breaks = [p for p in self._stale_breaks if p > target + 1e-9]

    def _run_continuous(self) -> None:
        self._in_run_call = True
        try:
            if not self._sim_active:
                self._start_run()
            if self.stall_runs > 0:
                self.stall_runs -= 1
                return
            period = self._period()
            start = self._simsec
            candidates = [period]
            brk = float(self._simulation._attrs["simbreakat"].value)
            if not self.ignore_break and start + 1e-9 < brk < period:
                candidates.append(brk)
            stale = [p for p in self._stale_breaks if start + 1e-9 < p < period]
            if stale:
                candidates.append(min(stale))
            target = self._align(min(candidates), period)
            self._advance_to(target)
            if target >= period - 1e-9:
                self._finish_run(period)
        finally:
            self._in_run_call = False

    def _run_single_step(self) -> None:
        if not self._sim_active:
            self._start_run()
        period = self._period()
        target = min(period, round(self._simsec + 1.0 / self._resolution(), 9))
        self._advance_to(target)
        if target >= period - 1e-9:
            self._finish_run(period)

    def _stop_run(self) -> None:
        if self._sim_active:
            self._finish_run(self._simsec, stopped=True)

    def _abort_run(self) -> None:
        self._sim_active = False
        self._simsec = 0.0

    def _finish_run(self, end: float, stopped: bool = False) -> None:
        record = {
            "No": self._run_no,
            "RandSeed": self._run_seed,
            "SimEnd": end,
            "Timestamp": datetime.now().isoformat(timespec="seconds"),
            "stopped": stopped,
        }
        self.run_records.append(record)
        self._net._collections["SimulationRuns"]._add(
            self._run_no,
            {"RandSeed": record["RandSeed"], "SimEnd": float(end), "Timestamp": record["Timestamp"]},
        )
        self._sim_active = False
        self._simsec = 0.0
        self._last_run_end = float(end)
        self._stale_breaks = [p for p in self._stale_breaks if p > self._period() + 1e-9]

    def _result_value(self, obj: FakeAttributeObject, base: str, args: list[str]) -> Any:
        """Deterministic pseudo results; None when no run has produced data yet."""
        runs = [r for r in self.run_records if not r["stopped"]]
        if self._sim_active:
            runs = [*runs, {"No": self._run_no, "RandSeed": self._run_seed}]
        if not runs:
            return None
        selector = args[0].lower() if args else "current"
        rest = ",".join(args[1:])
        key = getattr(obj, "_key", obj._com_label)
        if selector in ("avg", "mean", "all"):
            values = [_pseudo(base, key, run["RandSeed"], rest) for run in runs]
            return round(sum(values) / len(values), 3)
        if selector.isdigit():
            chosen = next((run for run in runs if run["No"] == int(selector)), None)
            if chosen is None:
                return None
            return _pseudo(base, key, chosen["RandSeed"], rest)
        return _pseudo(base, key, runs[-1]["RandSeed"], rest)

    # --- files & network

    def _reset_model(self, populate: bool) -> None:
        """Empty the network and reset Simulation/Evaluation attributes, keeping the COM object identities
        (``vissim.Net``, ``vissim.Simulation`` and ``vissim.Evaluation`` stay valid across ``LoadNet``)."""
        fresh_net = FakeNet(self)
        self._net._attrs = fresh_net._attrs
        for name, collection in self._net._collections.items():
            collection._items = fresh_net._collections[name]._items
            if isinstance(collection, FakeVehicles):
                collection._snapshot_key = None
        self._simulation._attrs = FakeSimulation(self)._attrs
        self._evaluation._attrs = FakeEvaluation(self)._attrs
        self._run_no = 0
        self._last_run_end = None
        self.run_records = []
        if populate:
            populate_demo_network(self)

    def _existing_file(self, path: Any, method: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise vissim_error(f"{method} failed: no file name given.", E_INVALIDARG)
        if self.reject_non_ascii_paths and not path.isascii():
            raise vissim_error(f"{method} failed: cannot open {path}.")
        file = Path(path)
        if not file.is_file():
            raise vissim_error(f"{method} failed: file {path} does not exist.")
        return file

    def _serialize_network(self) -> dict[str, Any]:
        def item(obj: FakeNetObject) -> dict[str, Any]:
            out: dict[str, Any] = {
                "key": obj._key,
                "values": {spec.name: spec.value for spec in obj._attrs.values()},
            }
            if obj._children:
                out["children"] = {
                    name: [item(child) for child in coll._items.values()]
                    for name, coll in obj._children.items()
                }
            return out

        return {
            "net": {spec.name: spec.value for spec in self._net._attrs.values()},
            "collections": {
                name: [item(obj) for obj in coll._items.values()]
                for name, coll in self._net._collections.items()
                if name not in _NOT_SAVED
            },
            "simulation": {spec.name: spec.value for spec in self._simulation._attrs.values()},
            "evaluation": {spec.name: spec.value for spec in self._evaluation._attrs.values()},
        }

    def _restore_network(self, data: Mapping[str, Any]) -> None:
        def add_items(collection: FakeCollection, items: Sequence[Mapping[str, Any]]) -> None:
            for entry in items:
                obj = collection._add(entry["key"], entry.get("values"))
                for child_name, child_items in (entry.get("children") or {}).items():
                    if child_name in obj._children:
                        add_items(obj._children[child_name], child_items)

        for name, value in (data.get("net") or {}).items():
            if name.lower() in self._net._attrs:
                self._net._attrs[name.lower()].value = value
        for name, items in (data.get("collections") or {}).items():
            if name in self._net._collections:
                add_items(self._net._collections[name], items)
        for target, values in (
            (self._simulation, data.get("simulation")),
            (self._evaluation, data.get("evaluation")),
        ):
            for name, value in (values or {}).items():
                if name.lower() in target._attrs:
                    target._attrs[name.lower()].value = value

    # --- the process

    def terminate_process(self) -> None:
        """Ask the spawned stand-in process to exit (like Vissim after ``Exit()``)."""
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except OSError:
                pass

    def kill_process(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.kill()
            except OSError:
                pass


# --- helpers


def spawn_standin(cmd: Sequence[str], extra_env: Mapping[str, str] | None = None) -> subprocess.Popen[bytes]:
    """Start the stand-in process of a fake Vissim (detached from our console; ``extra_env`` added to the
    environment - an empty value removes the variable)."""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if extra_env:
        env = dict(os.environ)
        for name, value in extra_env.items():
            if value:
                env[name] = value
            else:
                env.pop(name, None)
        kwargs["env"] = env
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(cmd), **kwargs)


def _pseudo(base: str, key: Any, seed: Any, extra: str) -> float | int:
    digest = zlib.crc32(f"{base}|{key}|{seed}|{extra}".encode())
    lowered = base.lower()
    if lowered in ("vehs", "qstops"):
        return 50 + digest % 450
    if lowered == "speed":
        return round(20 + (digest % 600) / 10, 2)
    if lowered == "travtm":
        return round(30 + (digest % 1200) / 10, 2)
    if lowered in ("qlen", "qlenmax"):
        return round((digest % 800) / 10, 2)
    return round((digest % 1000) / 10, 2)


def _parse_key(text: str) -> Any:
    text = text.strip()
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _read_saved_network(file: Path) -> dict[str, Any] | None:
    """The content of a file written by ``SaveNetAs``; None for any other file (a "real" .inpx)."""
    try:
        if file.stat().st_size > 50_000_000:
            return None
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if isinstance(data, dict) and data.get("format") == SAVE_FORMAT:
        return data
    return None


def populate_demo_network(vissim: FakeVissim) -> None:
    """A small deterministic network: 10 links, 4 nodes, 2 signal controllers with 4 groups each, ..."""
    collections = vissim._net._collections
    for number in range(1, 11):
        collections["Links"]._add(
            number,
            {
                "Name": f"Link {number}",
                "Length2D": round(50.0 + number * 12.5, 3),
                "NumLanes": 1 + number % 3,
                "IsConn": number > 8,
            },
        )
    for number in range(1, 5):
        collections["Nodes"]._add(number, {"Name": f"Node {number}"})
    for sc_no in (1, 2):
        controller = collections["SignalControllers"]._add(sc_no, {"Name": f"SC {sc_no}"})
        for sg_no in range(1, 5):
            controller._children["SGs"]._add(sg_no, {"Name": f"SG {sc_no}-{sg_no}"})
    for number in range(1, 5):
        collections["SignalHeads"]._add(
            number, {"Name": f"Head {number}", "SG": f"1-{number}", "Lane": f"{number}-1"}
        )
    for number in range(1, 4):
        collections["VehicleInputs"]._add(
            number, {"Name": f"Input {number}", "Link": number, "Volume(1)": 400.0 + 100 * number}
        )
    for number in (1, 2):
        decision = collections["VehicleRoutingDecisionsStatic"]._add(
            number, {"Name": f"Decision {number}", "Link": number}
        )
        for route in (1, 2):
            decision._children["VehRoutSta"]._add(route, {"RelFlow(1)": float(route), "DestLink": 4 + route})
    for number in range(1, 4):
        collections["DataCollectionMeasurements"]._add(number, {"Name": f"DC {number}"})
    for number in (1, 2):
        collections["VehicleTravelTimeMeasurements"]._add(number, {"Name": f"TT {number}"})
        collections["QueueCounters"]._add(number, {"Name": f"Queue {number}", "Link": number})
    vissim._net._attrs["name"].value = "Fake demo network"


def native_stdout_write(data: bytes) -> None:
    """Write below the C runtime: ``WriteFile(GetStdHandle(STD_OUTPUT_HANDLE))`` / libc ``write(1)``."""
    import ctypes

    if sys.platform == "win32":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_std_handle.argtypes = (wintypes.DWORD,)
        get_std_handle.restype = wintypes.HANDLE
        write_file = kernel32.WriteFile
        write_file.argtypes = (
            wintypes.HANDLE,
            ctypes.c_char_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )
        write_file.restype = wintypes.BOOL
        handle = get_std_handle(-11 & 0xFFFFFFFF)
        written = wintypes.DWORD(0)
        write_file(handle, data, len(data), ctypes.byref(written), None)
    else:
        libc = ctypes.CDLL(None)
        libc.write(1, data, len(data))


# --- env configuration


def _env_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def options_from_env(environ: Mapping[str, str]) -> dict[str, Any]:
    """FakeVissim keyword arguments from ``FAKE_VISSIM_*`` variables (see
    tests/fakes/win32com/client/dynamic.py).

    ``FAKE_VISSIM_PRINT_STDOUT=1`` (print, os.write and native WriteFile on every call),
    ``FAKE_VISSIM_NETWORK=demo|empty``, ``FAKE_VISSIM_SIMSEC_AFTER_RUN=zero|period``,
    ``FAKE_VISSIM_STALE_BREAK_AT=100[,200]``, ``FAKE_VISSIM_IGNORE_BREAK=1``, ``FAKE_VISSIM_STALL_RUNS=n``,
    ``FAKE_VISSIM_CRASH_AFTER_CALLS=n``, ``FAKE_VISSIM_WALL_S_PER_SIM_S=0.001``,
    ``FAKE_VISSIM_NO_TYPE_INFO=1``, ``FAKE_VISSIM_REJECT_NON_ASCII=1``, ``FAKE_VISSIM_EXIT_KEEPS_PROCESS=1``,
    ``FAKE_VISSIM_HANG_ON=LoadNet:30;Simulation.RunContinuous:5``, ``FAKE_VISSIM_SPAWN=["exe", "arg"]``
    (JSON), ``FAKE_VISSIM_SPAWN_ENV={"NAME": "value"}`` (JSON; extra environment of the spawned process).
    """
    options: dict[str, Any] = {}
    if _env_bool(environ.get("FAKE_VISSIM_PRINT_STDOUT")):
        options["print_to_stdout_on_call"] = True
        options["native_noise"] = True
    if environ.get("FAKE_VISSIM_NETWORK"):
        options["network"] = environ["FAKE_VISSIM_NETWORK"].strip().lower()
    if environ.get("FAKE_VISSIM_SIMSEC_AFTER_RUN"):
        options["simsec_after_run"] = environ["FAKE_VISSIM_SIMSEC_AFTER_RUN"].strip().lower()
    if environ.get("FAKE_VISSIM_STALE_BREAK_AT"):
        options["stale_break_at"] = [
            float(v) for v in environ["FAKE_VISSIM_STALE_BREAK_AT"].split(",") if v.strip()
        ]
    if _env_bool(environ.get("FAKE_VISSIM_IGNORE_BREAK")):
        options["ignore_break"] = True
    if environ.get("FAKE_VISSIM_STALL_RUNS"):
        options["stall_runs"] = int(environ["FAKE_VISSIM_STALL_RUNS"])
    if environ.get("FAKE_VISSIM_CRASH_AFTER_CALLS"):
        options["crash_after_calls"] = int(environ["FAKE_VISSIM_CRASH_AFTER_CALLS"])
    if environ.get("FAKE_VISSIM_WALL_S_PER_SIM_S"):
        options["wall_s_per_sim_s"] = float(environ["FAKE_VISSIM_WALL_S_PER_SIM_S"])
    if _env_bool(environ.get("FAKE_VISSIM_NO_TYPE_INFO")):
        options["no_type_info"] = True
    if _env_bool(environ.get("FAKE_VISSIM_REJECT_NON_ASCII")):
        options["reject_non_ascii_paths"] = True
    if _env_bool(environ.get("FAKE_VISSIM_EXIT_KEEPS_PROCESS")):
        options["exit_keeps_process"] = True
    if environ.get("FAKE_VISSIM_HANG_ON"):
        rules: dict[str, float] = {}
        for item in environ["FAKE_VISSIM_HANG_ON"].split(";"):
            if item.strip():
                member, _, seconds = item.rpartition(":")
                rules[member.strip()] = float(seconds)
        options["hang_on"] = rules
    if environ.get("FAKE_VISSIM_SPAWN"):
        command = json.loads(environ["FAKE_VISSIM_SPAWN"])
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError("FAKE_VISSIM_SPAWN must be a JSON list of strings")
        options["spawn_cmd"] = command
    if environ.get("FAKE_VISSIM_SPAWN_ENV"):
        extra = json.loads(environ["FAKE_VISSIM_SPAWN_ENV"])
        if not isinstance(extra, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in extra.items()
        ):
            raise ValueError("FAKE_VISSIM_SPAWN_ENV must be a JSON object of strings")
        options["spawn_env"] = extra
    return options


def is_registered_progid(progid: str) -> bool:
    """ProgIDs the fake COM registry knows: ``Vissim.Vissim*`` and Vissim's CLSID."""
    text = progid.strip()
    return text.lower().startswith("vissim.vissim") or text.upper() == VISSIM_CLSID
