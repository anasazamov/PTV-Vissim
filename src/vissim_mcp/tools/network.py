"""Network editing tool (EXTENSION after M6): vissim_call_method.

The attribute tools can only change objects that already exist. Building or restructuring a network needs the
COM methods of the collections - ``Net.Links.AddLink(Key, WktLinestring, LaneWidths)``,
``Net.Links.AddConnector(...)``, ``Net.VehicleInputs.AddVehicleInput(Key, Link)``, ``RemoveLink(Link)``,
``DuplicateLink(Source, NewKey)`` ... - and ``Vissim.New()`` for an empty network. ``vissim_call_method``
calls one such method on the object at a path.

Policy (fail closed, like the attribute policy of PLAN 6.7):

* on the root object only ``New`` is allowed - never ``LoadNet``, ``SaveNetAs``, ``Import*``, ``Export*``,
  ``Exit`` ... (files go through the file tools and their path policy);
* elsewhere only methods whose name starts with one of :data:`ALLOWED_PREFIXES` and contains none of
  :data:`FILE_WORDS`;
* refused with ``JOB_RUNNING`` while a simulation job is running or paused;
* every call writes one audit record.

Arguments are JSON values; an object argument (a link, a lane, the object to remove) is written as
``{"ref": "<object path>"}`` and resolved on the COM worker right before the call. COM objects never leave the
worker: a returned object is described by its key and, when the method was called on a collection, its path.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from ..errors import (
    ErrorCode,
    VissimMcpError,
    from_com_error,
    is_com_error,
    is_connection_error,
)
from ..jobs import JobState, job_running_error
from ..resolver import Segment, format_path, is_com_object, parse_path
from ..resolver import resolve as resolve_path
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, FlagArg, StringArg, flag, register_tool
from .objects import _interface_name, _require_object, plain_value

#: Method name prefixes that edit the network (verified against the Vissim 2026 type library, M6).
#: Update* / Replace* are the editing methods of sub-collections: route.UpdateLinkSequence([...]),
#: distribution.SpeedDistrDatPts.ReplaceAll([...]).
ALLOWED_PREFIXES = (
    "Add",
    "Remove",
    "Duplicate",
    "Generate",
    "Recalculate",
    "Invert",
    "Move",
    "Update",
    "Replace",
)
#: An allowed prefix is not enough when the name says the method works on files (e.g. AddFromFile) or adds
#: code that Vissim would run (AddScript). "Dir" of the attribute policy is left out here:
#: GenerateOppositeDirection and InvertDirection are network edits. "Path" is matched as a whole word part
#: only through FILE_NAME_RE below, because Net.Paths.AddPath... are route paths, not file paths.
FILE_WORDS = ("file", "folder", "script")
#: Prefixes after which a method that returns no object must at least have changed the collection.
CREATING_PREFIXES = ("add", "duplicate")
#: The only method allowed on the root object: an empty network.
ROOT_METHODS = ("New",)
MAX_ARGS = 20
MAX_LIST_ITEMS = 10_000
MAX_TEXT_CHARS = 200_000  # a WKT line string of a long link
MAX_INT = 2**63 - 1
_ARG_DEPTH = 3
_METHOD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
#: A string argument that is a file system path (drive, UNC, rooted or dotted-relative, or any backslash).
#: WKT geometry, colours ("ff994400") and names never look like this.
_PATH_LIKE_RE = re.compile(r"^\s*(?:[A-Za-z]:[\\/]|\\\\|/|\.\.?[\\/])|\\")
NOTHING_CREATED_HINT = (
    "Vissim ignores an array argument of the wrong shape without an error. Array arguments are flat lists: "
    "data points [x1, y1, x2, y2, ...], function points [x1, y1, yMin1, yMax1, x2, ...], priority rule "
    'conflict markers [{"ref": lane}, pos, {"ref": lane}, pos, ...], compositions [{"ref": type}, '
    '{"ref": speed distribution}, ...]. Check the parameters with vissim_describe.'
)
ENUM_HINT = (
    "A parameter that is an enumeration takes its number, not its name (e.g. AddUserDefinedDataAttribute "
    "DataType: 1 Int, 2 Double, 3 Bool, 4 Text; AddVehicleRouteManagedLanes Type: 1 Managed, 2 General "
    'purpose). An object parameter takes {"ref": "<object path>"}.'
)
_KEY_ATTRIBUTE = "No"
#: Objects without a number: a lane is addressed by its Index (Net.Links[1].Lanes[2]).
_KEY_FALLBACKS = ("Index",)
METHOD_HINT = (
    "List the methods and their parameters with vissim_describe(path=...), e.g. "
    "vissim_describe(path='Net.Links') shows AddLink(Key, WktLinestring, LaneWidths)."
)
DENIED_HINT = (
    "vissim_call_method only edits the network: methods starting with "
    + ", ".join(ALLOWED_PREFIXES)
    + " on network objects, and New on the root. Use vissim_load_network / vissim_save_network for files and "
    "the simulation tools for runs."
)

ArgsArg = Annotated[Any, WithJsonSchema({"type": "array", "items": {}})]


@dataclass(frozen=True)
class Ref:
    """An object argument: the object at ``segments`` (resolved on the COM worker)."""

    segments: tuple[Segment, ...]

    @property
    def path(self) -> str:
        return format_path(self.segments)


def _invalid(message: str, hint: str = "") -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def method_name(value: Any) -> str:
    """``method``: a COM method name (letters, digits, ``_``; no leading ``_``)."""
    if not isinstance(value, str):
        raise _invalid(f"method must be a string, got {type(value).__name__}", METHOD_HINT)
    text = value.strip()
    if not _METHOD_RE.fullmatch(text):
        raise _invalid(f"method must be a COM method name such as 'AddLink', got {value!r}", METHOD_HINT)
    return text


def check_method_allowed(method: str, on_root: bool) -> None:
    """``ATTRIBUTE_DENIED`` unless ``method`` is an allowed network-editing method (see the module doc)."""
    folded = method.casefold()
    if on_root:
        if folded in {name.casefold() for name in ROOT_METHODS}:
            return
        raise VissimMcpError(
            ErrorCode.ATTRIBUTE_DENIED,
            f"calling {method!r} on the Vissim root object is not allowed (only: {', '.join(ROOT_METHODS)})",
            hint=DENIED_HINT,
            details={"method": method},
        )
    word = next((candidate for candidate in FILE_WORDS if candidate in folded), None)
    if word is None and folded.endswith(("frompath", "topath", "pathname", "filepath")):
        word = "path"
    if word is not None or not folded.startswith(tuple(prefix.casefold() for prefix in ALLOWED_PREFIXES)):
        reason = f"its name contains '{word}'" if word is not None else "it is not a network-editing method"
        raise VissimMcpError(
            ErrorCode.ATTRIBUTE_DENIED,
            f"calling method {method!r} is not allowed: {reason}",
            hint=DENIED_HINT,
            details={"method": method},
        )


def method_args(values: Any) -> list[Any]:
    """``args``: a JSON array of numbers, strings, booleans, null, nested arrays and ``{"ref": path}``."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise _invalid(
            'args must be an array, e.g. [0, "LINESTRING(0 0, 100 0)", [3.5, 3.5]]', hint=METHOD_HINT
        )
    if len(values) > MAX_ARGS:
        raise _invalid(f"at most {MAX_ARGS} arguments, got {len(values)}")
    return [_argument(value, f"args[{index}]", 0) for index, value in enumerate(values)]


def _argument(value: Any, field: str, depth: int) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_INT:
            raise _invalid(f"{field} is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(f"{field} must be a finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT_CHARS:
            raise _invalid(f"{field} is longer than {MAX_TEXT_CHARS} characters")
        return value
    if isinstance(value, (list, tuple)):
        if depth >= _ARG_DEPTH:
            raise _invalid(f"{field} is nested too deeply")
        if len(value) > MAX_LIST_ITEMS:
            raise _invalid(f"{field} has more than {MAX_LIST_ITEMS} items")
        return tuple(_argument(item, f"{field}[{index}]", depth + 1) for index, item in enumerate(value))
    if isinstance(value, dict):
        if set(value) != {"ref"} or not isinstance(value["ref"], str):
            raise _invalid(
                f'{field}: an object argument is written as {{"ref": "<object path>"}}, '
                'e.g. {"ref": "Net.Links[1].Lanes[1]"}'
            )
        segments = parse_path(value["ref"])
        if not segments:
            raise _invalid(f"{field}: the root object cannot be passed as an argument")
        return Ref(tuple(segments))
    raise _invalid(f"{field} has an unsupported type ({type(value).__name__})")


def check_path_arguments(args: list[Any], policy: Any) -> None:
    """``ATTRIBUTE_DENIED`` for a string argument that is a file path outside the allowed directories.

    A few editing methods take a file name (AddBackgroundImage(Key, PathFilename, ...), AddStatic3DModel,
    AddModel2D3D(Key, [model files])): such a file must be inside ``VISSIM_ALLOWED_DIRS`` like every other
    file this server touches."""

    def walk(value: Any, field: str) -> None:
        if isinstance(value, tuple):
            for index, item in enumerate(value):
                walk(item, f"{field}[{index}]")
        elif isinstance(value, str) and _PATH_LIKE_RE.search(value):
            try:
                policy.check_location(value)
            except VissimMcpError as err:
                raise VissimMcpError(
                    ErrorCode.ATTRIBUTE_DENIED,
                    f"{field} is a file path outside the allowed directories: {err.message}",
                    hint="A file given to a Vissim method must be inside VISSIM_ALLOWED_DIRS.",
                    details={"argument": field},
                ) from None

    for index, value in enumerate(args):
        walk(value, f"args[{index}]")


def blocking_job(service: VissimService, on_root: bool) -> Any | None:
    """The simulation job that forbids this call: a running job always; a paused one only for the root
    method New (objects such as vehicles are added while a simulation is paused - Vissim itself refuses
    network edits it cannot do during a run)."""
    job = service.active_job()
    if job is None:
        return None
    state = getattr(job, "state", None)
    running = str(getattr(state, "value", state)) == JobState.RUNNING.value
    return job if running or on_root else None


def echo_args(args: list[Any]) -> list[Any]:
    """``args`` as JSON values again (for the result and the audit record)."""

    def one(value: Any) -> Any:
        if isinstance(value, Ref):
            return {"ref": value.path}
        if isinstance(value, tuple):
            return [one(item) for item in value]
        return value

    return [one(value) for value in args]


# --- COM task (worker thread)


def _materialize(vissim: Any, value: Any) -> Any:
    if isinstance(value, Ref):
        obj = resolve_path(vissim, list(value.segments))
        _require_object(obj, value.path)
        return obj
    if isinstance(value, tuple):
        return tuple(_materialize(vissim, item) for item in value)
    return value


def _describe_result(value: Any, target_label: str, target_is_root: bool) -> dict[str, Any]:
    """What a method returned: a plain value, or the key (and path) of a returned object."""
    if not is_com_object(value):
        return {"value": plain_value(value)}
    described: dict[str, Any] = {"object": True, "interface": _interface_name(value)}
    key: Any = None
    for name in (_KEY_ATTRIBUTE, *_KEY_FALLBACKS):
        try:
            key = plain_value(value.AttValue(name))
        except Exception as exc:
            if is_connection_error(exc) or not is_com_error(exc):
                raise
            continue
        if key is not None:
            break
    described["key"] = key
    if key is not None and not target_is_root:
        shown = f'"{key}"' if isinstance(key, str) else str(key)
        described["path"] = f"{target_label}[{shown}]"
    return described


def _count(target: Any) -> int | None:
    try:
        return int(target.Count)
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return None


def _keys(target: Any) -> set[Any] | None:
    try:
        rows = target.GetMultipleAttributes((_KEY_ATTRIBUTE,))
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return None
    return {plain_value(row[0]) for row in rows or ()}


def _created_object(
    target: Any, label: str, method: str, count_before: int | None, keys_before: set[Any] | None
) -> dict[str, Any] | None:
    """For an Add*/Duplicate* call that returned no object: what it created, found by comparing the
    collection before and after. Raises when the collection did not change - real Vissim 2026 accepts e.g.
    AddSpeedDistribution(0, [[40, 0], [60, 1]]) (nested instead of flat) without an error and creates
    nothing (M6)."""
    count_after = _count(target)
    if count_before is None or count_after is None:
        return None
    if count_after <= count_before:
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT,
            f"Vissim accepted {label}.{method} without an error but created nothing "
            f"(the collection still has {count_after} objects)",
            hint=NOTHING_CREATED_HINT,
            details={"count": count_after},
        )
    keys_after = _keys(target) if keys_before is not None else None
    new_keys = (
        sorted(keys_after - keys_before, key=str)
        if keys_after is not None and keys_before is not None
        else []
    )
    if len(new_keys) != 1:
        return {"object": True, "interface": None, "key": None, "created": count_after - count_before}
    key = new_keys[0]
    shown = f'"{key}"' if isinstance(key, str) else str(key)
    return {"object": True, "interface": None, "key": key, "path": f"{label}[{shown}]"}


def call_method_task(
    segments: list[Segment],
    method: str,
    args: list[Any],
    dry_run: bool,
    service: VissimService,
    progress: dict[str, Any],
    ctx: WorkerContext,
) -> dict[str, Any]:
    """Resolve the target and the object arguments, call the method, describe what it returned."""
    job = blocking_job(service, not segments)  # a job may have been started after the tool's own check
    if job is not None:
        raise job_running_error(job)
    label = format_path(segments) if segments else "Vissim"
    target = resolve_path(ctx.vissim, segments) if segments else ctx.vissim
    _require_object(target, label)
    try:
        member = getattr(target, method)
    except AttributeError:
        raise _invalid(f"'{label}' has no method {method!r}", METHOD_HINT) from None
    if not callable(member) or is_com_object(member):
        raise _invalid(f"'{label}.{method}' is a property, not a method", METHOD_HINT)
    resolved = [_materialize(ctx.vissim, value) for value in args]
    if dry_run:
        return {"called": False}
    creates = bool(segments) and method.casefold().startswith(CREATING_PREFIXES)
    count_before = _count(target) if creates else None
    keys_before = _keys(target) if count_before is not None else None
    progress["called"] = True
    try:
        value = member(*resolved)
    except (TypeError, ValueError) as exc:
        # pywin32 could not convert an argument to the parameter's COM type, e.g. the name of an
        # enumeration value ("NORMAL") where its number is expected. Nothing was sent to Vissim.
        progress["called"] = False
        raise _invalid(
            f"{label}.{method}: an argument has the wrong type ({type(exc).__name__}: {exc})",
            hint=f"{ENUM_HINT} {METHOD_HINT}",
        ) from None
    except Exception as exc:
        if is_connection_error(exc) or not is_com_error(exc):
            raise
        error = from_com_error(exc, context=f"{label}.{method}")
        if not error.hint or "vissim_describe" not in error.hint:
            error.hint = f"{error.hint} {METHOD_HINT}".strip()
        raise error from None
    returned = _describe_result(value, label, not segments)
    if creates and not is_com_object(value):
        found = _created_object(target, label, method, count_before, keys_before)
        if found is not None:
            returned = found
    return {"called": True, "returned": returned}


# --- tool


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_call_method(
        path: StringArg, method: StringArg, args: ArgsArg = None, dry_run: FlagArg = False
    ) -> dict[str, Any]:
        """Call a network-editing COM method: add, remove or duplicate network objects, or start an empty
        network. This is how a network is built or restructured; attributes of the new object are then set
        with vissim_set_attributes.

        Find methods and their parameter names with vissim_describe (e.g. path="Net.Links"). Typical calls
        (Key 0 = Vissim picks the next free number; positions in metres; WKT coordinates in metres):
        - path="Vissim", method="New" - empty network (the current one is discarded; save it first)
        - path="Net.Links", method="AddLink", args=[0, "LINESTRING(0 0, 200 0)", [3.5, 3.5]] - 2 lanes
        - path="Net.Links", method="AddConnector", args=[0, {"ref": "Net.Links[1].Lanes[1]"}, 195,
          {"ref": "Net.Links[2].Lanes[1]"}, 5, 1, "LINESTRING EMPTY"] - from lane/pos, to lane/pos, lanes
        - path="Net.VehicleInputs", method="AddVehicleInput", args=[0, {"ref": "Net.Links[1]"}]
        - path="Net.VehicleRoutingDecisionsStatic", method="AddVehicleRoutingDecisionStatic",
          args=[0, {"ref": "Net.Links[1]"}, 20], then on "...[1].VehRoutSta": "AddVehicleRouteStatic",
          args=[0, {"ref": "Net.Links[2]"}, 150]
        - path="Net.Links", method="RemoveLink", args=[{"ref": "Net.Links[7]"}]
        - path="Net.DesSpeedDistributions", method="AddDesSpeedDistribution", args=[0, [40, 0, 60, 1]]
        An object argument is {"ref": "<object path>"}. An array argument is a FLAT JSON array: data points
        [x1, y1, x2, y2, ...], priority rule conflict markers [{"ref": lane}, pos, {"ref": lane}, pos],
        compositions [{"ref": vehicle type}, {"ref": speed distribution}, ...]. An enumeration parameter
        takes its number. Objects inside objects have their own collections (Net.Links[1].Lanes: AddLane;
        a route: UpdateLinkSequence; a distribution's data points: ReplaceAll).

        Allowed: methods starting with Add, Remove, Duplicate, Generate, Recalculate, Invert, Move, Update
        or Replace on network objects, and New on the root; not methods that take script code, and a file
        argument must be inside the allowed directories. Refused while a simulation job is running; while a
        job is paused only what Vissim itself permits during a run works (e.g. Net.Vehicles:
        AddVehicleAtLinkPosition). An Add/Duplicate call that creates nothing is an error. A new network
        has no file: save it with vissim_save_network before starting a simulation. Nothing is saved
        automatically. Written to the audit log.

        Parameters: path - the object that has the method ("Vissim" or "" is the root); method - its name;
        args - positional arguments; dry_run - true only checks that the object, the method and the
        referenced objects exist.
        Returns: path, method, args, dry_run, called, and returned: {value} for a plain result or
        {object: true, key, interface, path} for a new object (key = its No; path when it can be addressed).
        Errors: ATTRIBUTE_DENIED (method not allowed), JOB_RUNNING, INVALID_ARGUMENT (no such method, bad
        arguments), RESOLVE_FAILED (path or a ref does not exist), COM_ERROR (Vissim refused the call - see
        the message), NOT_CONNECTED, TIMEOUT_HUNG.

        Example: vissim_call_method(path="Net.Links", method="AddLink",
        args=[0, "LINESTRING(0 0, 150 0)", [3.5]])
        """
        audit_args = {"path": path, "method": method, "args": args, "dry_run": dry_run}
        progress: dict[str, Any] = {}
        async with AuditRecord(service, "vissim_call_method", audit_args) as audit:
            audit.update(called=False)
            if not isinstance(path, str):
                raise _invalid("path must be a string", hint='E.g. "Net.Links" (or "Vissim" for the root).')
            segments = parse_path(path)
            name = method_name(method)
            parsed = method_args(args)
            dry = flag(dry_run, "dry_run")
            check_method_allowed(name, on_root=not segments)
            label = format_path(segments) if segments else "Vissim"
            audit.update(path=label, method=name, dry_run=dry)
            check_path_arguments(parsed, service.policy)
            service.ensure_connected()
            job = blocking_job(service, not segments)
            if job is not None:
                raise job_running_error(job)
            task = functools.partial(call_method_task, segments, name, parsed, dry, service, progress)
            try:
                data = await service.call(task, task_name="vissim_call_method")
            finally:
                audit.update(called=bool(progress.get("called")))
            if data["called"] and not segments:
                service.network_replaced()  # Vissim.New(): no file is loaded any more
            audit.update(returned=data.get("returned"))
        result: dict[str, Any] = {
            "path": label,
            "method": name,
            "args": echo_args(parsed),
            "dry_run": dry,
            "called": data["called"],
        }
        if "returned" in data:
            result["returned"] = data["returned"]
        warnings = list(audit.warnings)
        if data["called"] and not segments:
            warnings.insert(
                0,
                "the network is new and has no file: save it with vissim_save_network before "
                "vissim_start_simulation",
            )
        result["warnings"] = warnings
        return result

    register_tool(
        mcp,
        vissim_call_method,
        name="vissim_call_method",
        title="Call a network-editing method",
        settings=settings,
        read_only=False,
        destructive=True,  # Remove* deletes objects, New discards the network
        idempotent=False,
    )
