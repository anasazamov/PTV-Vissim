"""Object tools (PLAN 7.3): vissim_describe, vissim_list_objects, vissim_get_attributes,
vissim_set_attributes, vissim_set_attribute_bulk.

Every tool parses its object path with :func:`~vissim_mcp.resolver.parse_path` and validates its arguments
*before* any COM work, then runs one task on the COM worker. Tasks return plain data only: values are passed
through :func:`plain_value` (a COM object somewhere in a value becomes a marker string). Per-attribute and
per-object failures are collected (``errors`` / ``failed``); a lost connection is always raised so that the
service marks the connection lost.

Writes (set, bulk) obey the attribute policy (``ATTRIBUTE_DENIED`` for file/path attributes) and write one
audit record per call (PLAN 6.8) - also for dry runs, refusals and failures. Their progress is collected on
the worker thread in append-only lists, so the audit record also shows what was written before a failure or
a timeout.
"""

from __future__ import annotations

import functools
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from ..errors import (
    ErrorCode,
    VissimMcpError,
    describe_exception,
    from_com_error,
    is_com_error,
    is_connection_error,
)
from ..output import dumps, paginate, to_jsonable
from ..policy import check_attribute_writable
from ..resolver import Segment, format_path, is_com_object, parse_path, resolve
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, FlagArg, StringArg, flag, register_tool

MAX_ATTRIBUTES = 200
MAX_ATTRIBUTE_NAME_CHARS = 256
MAX_BULK_ITEMS = 5000
MAX_TEXT_VALUE_CHARS = 10_000
MAX_INT_VALUE = 2**63 - 1
#: ``failed`` entries shown in a bulk result (all of them are in the audit record).
MAX_FAILED_SHOWN = 100
COM_OBJECT_MARKER = "<COM object>"
_VALUE_DEPTH = 8
#: Room kept free for warnings and bookkeeping keys when a list is cut to the response size.
_BUDGET_RESERVE_CHARS = 1500

_PATH_HINT = "Give the path of an object or a collection, e.g. Net.Links[1] or Net.Links."


# Input schemas of the write tools. Their arguments are validated in the tool body (inside the audit record),
# not by FastMCP: a call refused for the shape of its arguments is audited like any other refusal and gets
# the usual {"ok": false, "error": {"code": "INVALID_ARGUMENT"}} result (PLAN 6.8, contract section 2).
_VALUE_SCHEMA: dict[str, Any] = {
    "type": ["number", "string", "boolean"],
    "description": "New value of the attribute: a number, a string or a boolean",
}
_VALUES_SCHEMA: dict[str, Any] = {"type": "object", "additionalProperties": _VALUE_SCHEMA}
_ITEMS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "description": "One object of vissim_set_attribute_bulk",
        "properties": {
            "key": {
                "type": ["integer", "string"],
                "description": 'Key of the object in the collection (ItemByKey), e.g. 5 or "A1"',
            },
            "value": _VALUE_SCHEMA,
        },
        "required": ["key", "value"],
        "additionalProperties": False,
    },
}
ValuesArg = Annotated[Any, WithJsonSchema(_VALUES_SCHEMA)]
ItemsArg = Annotated[Any, WithJsonSchema(_ITEMS_SCHEMA)]

#: Wall-clock budget for naming the unreadable attributes after a failed GetMultipleAttributes: every probe
#: reads the whole collection, so probing 200 names of a large network could take 200 times the read.
PROBE_BUDGET_S = 3.0

Snapshot = tuple[list[tuple[Any, Any]], list[tuple[Any, Any]], list[Any], list[dict[str, Any]]]


class WriteProgress:
    """What a write task did so far. Filled on the worker thread and only ever appended to (entries are tuples
    or finished dicts), so the event loop thread can copy it at any time - also while a timed-out task still
    runs.

    When the tool gives up on the task (timeout, cancellation, any error) it calls :meth:`abandon`: the task
    stops before its next ``SetAttValue`` - the client has been told that the call failed - and if it still
    changes something after that moment (the hung call itself returning), :meth:`task_finished` reports the
    final state through the callback (a follow-up audit record).
    """

    def __init__(self) -> None:
        self.old: list[tuple[Any, Any]] = []  # (attribute or key, value before)
        self.new: list[tuple[Any, Any]] = []  # (attribute or key, value read back - or planned for a dry run)
        self.written: list[Any] = []  # attributes or keys whose SetAttValue succeeded
        self.failed: list[dict[str, Any]] = []  # {attribute|key, stage: read|write|verify, error}
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._finished = False
        self._late: Callable[[Snapshot], None] | None = None
        self._abandoned_at: Snapshot | None = None

    def snapshot(self) -> Snapshot:
        return list(self.old), list(self.new), list(self.written), list(self.failed)

    def abandon(self, late: Callable[[Snapshot], None] | None = None) -> Snapshot:
        """Stop the task before its next write; returns the snapshot the tool reports and audits. ``late`` is
        called (on the worker thread) with the final snapshot if the task changes anything after this."""
        with self._lock:
            self.cancelled.set()
            snapshot = self.snapshot()
            if not self._finished:
                self._late = late
                self._abandoned_at = snapshot
            return snapshot

    def task_finished(self) -> None:
        """Called by the task when it ends, however it ends."""
        with self._lock:
            self._finished = True
            late, before = self._late, self._abandoned_at
            self._late = None
        if late is None:
            return
        final = self.snapshot()
        if final != before:
            try:
                late(final)
            except Exception:  # bookkeeping must never break the worker
                pass


# --- value helpers


def plain_value(value: Any, depth: int = 0) -> Any:
    """A COM result as plain data: tuples become lists, COM objects become :data:`COM_OBJECT_MARKER`.

    Other values (numbers, strings, times) are kept; the tool wrapper turns them into JSON.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        if depth >= _VALUE_DEPTH:
            return str(value)[:200]
        return [plain_value(item, depth + 1) for item in value]
    if is_com_object(value):
        return COM_OBJECT_MARKER
    return value


def attribute_name(value: Any, field: str = "attribute") -> str:
    """A validated attribute name: ``Name``, ``Volume(1)``, ``Vehs(Current,Last,All)``, ``Lane\\Link\\No``."""
    if not isinstance(value, str):
        raise _invalid(f"{field} must be a string, got {type(value).__name__}")
    name = value.strip()
    if not name:
        raise _invalid(f"{field} must not be empty")
    if len(name) > MAX_ATTRIBUTE_NAME_CHARS:
        raise _invalid(f"{field} is longer than {MAX_ATTRIBUTE_NAME_CHARS} characters")
    if any(ord(char) < 32 or char == "\x7f" for char in name):
        raise _invalid(f"{field} {name!r} contains control characters")
    return name


def attribute_names(values: Any, field: str = "attributes") -> list[str]:
    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        raise _invalid(f"{field} must be a non-empty list of attribute names")
    if len(values) > MAX_ATTRIBUTES:
        raise _invalid(f"at most {MAX_ATTRIBUTES} {field} per call, got {len(values)}")
    return [attribute_name(item, f"{field}[{index}]") for index, item in enumerate(values)]


def attribute_value(value: Any, field: str) -> Any:
    """A value Vissim can take through COM: bool, int (64-bit), finite float, string (<= 10 000 chars)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_INT_VALUE:
            raise _invalid(f"{field}: the integer {value} is out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(f"{field}: NaN and infinity cannot be written")
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT_VALUE_CHARS:
            raise _invalid(f"{field}: text longer than {MAX_TEXT_VALUE_CHARS} characters")
        return value
    kind = "null" if value is None else type(value).__name__
    raise _invalid(f"{field} must be a number, a string or a boolean, got {kind}")


def write_values(values: Any, allow_path_attrs: bool) -> list[tuple[str, Any]]:
    """``values`` of vissim_set_attributes as validated ``(name, value)`` pairs (attribute policy applied)."""
    if not isinstance(values, Mapping) or not values:
        raise _invalid("values must be a non-empty object {attribute: new value}")
    if len(values) > MAX_ATTRIBUTES:
        raise _invalid(f"at most {MAX_ATTRIBUTES} attributes per call, got {len(values)}")
    pairs: list[tuple[str, Any]] = []
    seen: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = attribute_name(raw_name, "attribute name")
        folded = name.casefold()
        if folded in seen:
            raise _invalid(
                f"attributes {seen[folded]!r} and {name!r} are the same (names are case-insensitive)"
            )
        seen[folded] = name
        check_attribute_writable(name, allow_path_attrs)
        pairs.append((name, attribute_value(raw_value, f"value of {name!r}")))
    return pairs


def bulk_items(items: Any) -> list[tuple[int | str, Any]]:
    """``items`` of vissim_set_attribute_bulk as validated ``(key, value)`` pairs with unique keys."""
    if not isinstance(items, Sequence) or isinstance(items, str) or not items:
        raise _invalid("items must be a non-empty list of {key, value}")
    if len(items) > MAX_BULK_ITEMS:
        raise _invalid(
            f"at most {MAX_BULK_ITEMS} items per call, got {len(items)}",
            hint="Split the items into several calls.",
        )
    pairs: list[tuple[int | str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if isinstance(item, Mapping) and set(item) == {"key", "value"}:
            key, value = item["key"], item["value"]
        else:
            raise _invalid(f"items[{index}] must be an object {{key, value}}")
        if isinstance(key, bool) or not isinstance(key, (int, str)):
            raise _invalid(f"items[{index}].key must be an integer or a string")
        if isinstance(key, int) and abs(key) > MAX_INT_VALUE:
            raise _invalid(f"items[{index}].key is out of range")
        if isinstance(key, str) and (not key.strip() or len(key) > MAX_ATTRIBUTE_NAME_CHARS):
            raise _invalid(
                f"items[{index}].key must be a non-empty string of at most {MAX_ATTRIBUTE_NAME_CHARS} "
                "characters"
            )
        # 5 and "5" count as the same key (fail-closed). Whether Vissim's ItemByKey resolves a string key of
        # an integer-keyed collection is NOT verified (docs/com-names.md, M6 list); keys are passed unchanged.
        marker = str(key).strip()
        if marker in seen:
            raise _invalid(f"duplicate key {key!r} (items[{index}]); every object may appear only once")
        seen.add(marker)
        pairs.append((key, attribute_value(value, f"items[{index}].value")))
    return pairs


def _invalid(message: str, hint: str = "") -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def _require_object(value: Any, label: str) -> None:
    if not is_com_object(value):
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT,
            f"'{label}' is a value ({type(value).__name__}), not an object",
            hint=f"{_PATH_HINT} Read plain values with vissim_get_attributes on the object that has them.",
        )


def _member(obj: Any, name: str, label: str, what: str) -> Any:
    """``obj.<name>`` (a COM method); a missing member is ``INVALID_ARGUMENT`` saying what it is not."""
    try:
        return getattr(obj, name)
    except AttributeError:
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT, f"'{label}' is not {what} (it has no {name})", hint=_PATH_HINT
        ) from None


def _read_property(obj: Any, name: str) -> Any:
    try:
        return plain_value(getattr(obj, name))
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return None


# --- COM tasks (worker thread)


def describe_task(
    segments: list[Segment], include_attributes: bool, max_attributes: int, ctx: WorkerContext
) -> dict[str, Any]:
    """Type-info members (and with ``include_attributes`` the attribute list) of the object at a path."""
    label = format_path(segments)
    obj = resolve(ctx.vissim, segments)
    _require_object(obj, label)
    result: dict[str, Any] = {"path": label}
    try:
        result["members"] = ctx.backend.type_info_members(obj)
        result["interface"] = _interface_name(obj)
    except VissimMcpError as err:
        if err.code is not ErrorCode.INFO_UNAVAILABLE:
            raise
        unavailable = VissimMcpError(
            ErrorCode.INFO_UNAVAILABLE, f"'{label}' exposes no COM type information", details=err.details
        )
        if not include_attributes:
            raise unavailable from None
        result["members"] = None
        result["members_error"] = unavailable.to_dict()
    if include_attributes:
        result.update(_attribute_infos(obj, label, max_attributes))
    return result


def _interface_name(obj: Any) -> str | None:
    try:
        name = obj._oleobj_.GetTypeInfo().GetDocumentation(-1)[0]
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return None
    return str(name) if name else None


def _attribute_infos(obj: Any, label: str, limit: int) -> dict[str, Any]:
    try:
        infos = tuple(obj.Attributes.GetAll())
    except AttributeError:
        return {
            "attributes": None,
            "attributes_error": f"'{label}' has no attribute list; use include_attributes on a collection "
            "such as Net.Links",
        }
    except Exception as exc:
        if is_connection_error(exc):
            raise
        return {"attributes": None, "attributes_error": describe_exception(exc, 300)}
    rows = [
        {
            "code": _read_property(info, "Code"),
            "name": _read_property(info, "Name"),
            "value_type": _read_property(info, "ValueType"),
            "editable": _read_property(info, "Editable"),
        }
        for info in infos[: max(0, limit)]
    ]
    return {"attributes": rows, "attributes_total": len(infos)}


def list_objects_task(
    segments: list[Segment], attributes: list[str], offset: int, limit: int, max_rows: int, ctx: WorkerContext
) -> dict[str, Any]:
    """One ``GetMultipleAttributes`` call; the requested page of its rows plus the paging metadata."""
    label = format_path(segments)
    collection = resolve(ctx.vissim, segments)
    _require_object(collection, label)
    get_multiple = _member(collection, "GetMultipleAttributes", label, "a collection")
    try:
        rows = get_multiple(tuple(attributes))
    except Exception as exc:
        if is_connection_error(exc) or not is_com_error(exc):
            raise
        raise _unreadable_attributes(exc, get_multiple, attributes, label, ctx) from None
    page, meta = paginate(list(rows or ()), offset, limit, max_rows)
    return {
        "rows": [plain_value(row) if isinstance(row, (list, tuple)) else [plain_value(row)] for row in page],
        **meta,
    }


def _unreadable_attributes(
    exc: BaseException,
    get_multiple: Any,
    attributes: list[str],
    label: str,
    ctx: WorkerContext | None = None,
    budget_s: float | None = None,
) -> VissimMcpError:
    """COM_ERROR for a failed GetMultipleAttributes, naming the attributes that cannot be read (each one is
    probed on its own - only after a failure). Every probe reads the whole collection, so probing stops
    after ``budget_s`` (default :data:`PROBE_BUDGET_S`); names not probed go to
    ``details.unchecked_attributes``.
    """
    error = from_com_error(exc, context=f"GetMultipleAttributes on '{label}'")
    invalid: list[str] = []
    unchecked: list[str] = []
    if len(attributes) == 1:
        invalid = list(attributes)
    else:
        deadline = time.monotonic() + (PROBE_BUDGET_S if budget_s is None else budget_s)
        for index, name in enumerate(attributes):
            if index and (time.monotonic() >= deadline or (ctx is not None and ctx.stopping)):
                unchecked = list(attributes[index:])
                break
            try:
                get_multiple((name,))
            except Exception as probe:
                if is_connection_error(probe):
                    raise
                invalid.append(name)
    details: dict[str, Any] = {}
    if invalid:
        details["invalid_attributes"] = invalid
        error.message = f"{error.message} (cannot read: {', '.join(invalid)})"
    if unchecked:
        details["unchecked_attributes"] = unchecked
        error.message = f"{error.message} ({len(unchecked)} attribute(s) not checked: time limit)"
    if details:
        error.details = {**(error.details or {}), **details}
    return error


def get_attributes_task(segments: list[Segment], attributes: list[str], ctx: WorkerContext) -> dict[str, Any]:
    """``AttValue`` per attribute; a failing attribute goes to ``errors``, the others are still read."""
    label = format_path(segments)
    obj = resolve(ctx.vissim, segments)
    _require_object(obj, label)
    att_value = _member(obj, "AttValue", label, "an object with attributes")
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name in attributes:
        try:
            values[name] = plain_value(att_value(name))
        except Exception as exc:
            if is_connection_error(exc):
                raise
            errors[name] = describe_exception(exc, 300)
    return {"path": label, "values": values, "errors": errors}


def set_attributes_task(
    segments: list[Segment],
    values: list[tuple[str, Any]],
    dry_run: bool,
    progress: WriteProgress,
    ctx: WorkerContext,
) -> None:
    """Read all old values, then (unless ``dry_run``) write them, then read them back (PLAN 7.3).

    An attribute whose old value cannot be read is not written. Nothing is rolled back when a later write
    fails; ``progress`` records exactly what happened. Stops before the next write once the tool has
    abandoned the task (``progress.cancelled``).
    """
    try:
        _set_attributes(segments, values, dry_run, progress, ctx)
    finally:
        progress.task_finished()


def _set_attributes(
    segments: list[Segment],
    values: list[tuple[str, Any]],
    dry_run: bool,
    progress: WriteProgress,
    ctx: WorkerContext,
) -> None:
    label = format_path(segments)
    obj = resolve(ctx.vissim, segments)
    _require_object(obj, label)
    att_value = _member(obj, "AttValue", label, "an object with attributes")
    set_att_value = (
        None if dry_run else _member(obj, "SetAttValue", label, "an object with writable attributes")
    )
    readable: list[tuple[str, Any]] = []
    for name, value in values:
        try:
            before = plain_value(att_value(name))
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append(
                {"attribute": name, "stage": "read", "error": describe_exception(exc, 300)}
            )
            continue
        progress.old.append((name, before))
        readable.append((name, value))
    if set_att_value is None:
        progress.new.extend(readable)  # the planned values
        return
    for name, value in readable:
        if ctx.stopping or progress.cancelled.is_set():
            return  # the connection was replaced, or the tool has already reported a failure
        try:
            set_att_value(name, value)
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append(
                {"attribute": name, "stage": "write", "error": describe_exception(exc, 300)}
            )
            continue
        progress.written.append(name)
    for name, _value in readable:
        if ctx.stopping or progress.cancelled.is_set():
            return
        try:
            progress.new.append((name, plain_value(att_value(name))))
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append(
                {"attribute": name, "stage": "verify", "error": describe_exception(exc, 300)}
            )


def bulk_task(
    segments: list[Segment],
    attribute: str,
    items: list[tuple[int | str, Any]],
    dry_run: bool,
    progress: WriteProgress,
    ctx: WorkerContext,
) -> None:
    """One COM task: ``ItemByKey(key)`` -> old value -> (unless ``dry_run``) write -> read back, per item.

    Stops before the next item or write once the tool has abandoned the task (``progress.cancelled``).
    """
    try:
        _bulk(segments, attribute, items, dry_run, progress, ctx)
    finally:
        progress.task_finished()


def _bulk(
    segments: list[Segment],
    attribute: str,
    items: list[tuple[int | str, Any]],
    dry_run: bool,
    progress: WriteProgress,
    ctx: WorkerContext,
) -> None:
    label = format_path(segments)
    collection = resolve(ctx.vissim, segments)
    _require_object(collection, label)
    item_by_key = _member(collection, "ItemByKey", label, "a collection")
    for key, value in items:
        if ctx.stopping or progress.cancelled.is_set():
            return
        try:
            obj = item_by_key(key)
            before = plain_value(obj.AttValue(attribute))
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append({"key": key, "stage": "read", "error": describe_exception(exc, 300)})
            continue
        progress.old.append((key, before))
        if dry_run:
            progress.new.append((key, value))
            continue
        if progress.cancelled.is_set():
            return
        try:
            obj.SetAttValue(attribute, value)
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append({"key": key, "stage": "write", "error": describe_exception(exc, 300)})
            continue
        progress.written.append(key)
        try:
            progress.new.append((key, plain_value(obj.AttValue(attribute))))
        except Exception as exc:
            if is_connection_error(exc):
                raise
            progress.failed.append({"key": key, "stage": "verify", "error": describe_exception(exc, 300)})


# --- result helpers


def fit_list(payload: dict[str, Any], key: str, max_chars: int) -> bool:
    """Cut ``payload[key]`` (a list) from its end so the payload stays below ``max_chars``; True if cut."""
    items = payload.get(key)
    if not isinstance(items, list) or not items:
        return False
    budget = max_chars - len(dumps(to_jsonable({**payload, key: []}))) - _BUDGET_RESERVE_CHARS
    used = 0
    kept = 0
    for item in items:
        size = len(dumps(to_jsonable(item))) + 1
        if used + size > budget:
            break
        used += size
        kept += 1
    if kept == len(items):
        return False
    payload[key] = items[:kept]
    return True


def _audit_map(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    """``[(key, value)]`` as a JSON object for the audit record (keys as text)."""
    return {key if isinstance(key, str) else str(key): value for key, value in pairs}


def _item_pair(item: Any) -> list[Any]:
    """``[key, value]`` of a bulk item as requested (for the audit record), whatever its shape."""
    if isinstance(item, Mapping):
        return [item.get("key"), item.get("value")]
    return [None, item]


def late_audit(
    service: VissimService, tool: str, fields: Mapping[str, Any], key_map: bool
) -> Callable[[Snapshot], None]:
    """Callback for :meth:`WriteProgress.abandon`: a follow-up audit record (``late_completion: true``) with
    the final ``old``/``new``/``written``/``failed`` of a task that changed the model after its tool had
    already returned an error (PLAN 6.8: every write is audited)."""

    def write(snapshot: Snapshot) -> None:
        old, new, written, failed = snapshot
        as_map = _audit_map if key_map else dict
        service.write_audit(
            tool,
            {
                **fields,
                "late_completion": True,
                "old": as_map(old),
                "new": as_map(new),
                "written": written,
                "failed": failed,
            },
        )

    return write


# --- tools


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_describe(path: str, include_attributes: bool = False) -> dict[str, Any]:
        """List the COM members (methods, properties) of a Vissim object - and optionally its attributes.

        Use it to find exact names before reading or writing (names differ between Vissim versions; never
        guess them). Object paths: "Net", "Net.Links", "Net.Links[5]", "Net.SignalControllers[1].SGs[2]",
        'Net.Nodes["A1"]', "Net.VehicleRoutingDecisionsStatic[1].VehRoutSta[2]", "Simulation", "Evaluation",
        "Graphics.CurrentNetworkWindow"; "" or "Vissim" is the root. Keys in [] are integers or quoted
        strings.

        Parameters: path - object path; include_attributes - also list the attributes of the objects of a
        collection (Attributes.GetAll: code, name, value_type, editable; at most VISSIM_MAX_ROWS).
        Returns: path, interface, members ([{name, kind}], kind = method | property_get | property_put),
        and with include_attributes: attributes, attributes_total, attributes_truncated (or
        attributes_error). Without type information but with include_attributes, members is null and
        members_error explains why.
        Errors: INFO_UNAVAILABLE (the object has no COM type information - use vissim_get_attributes with
        known names), RESOLVE_FAILED (a path segment does not exist), INVALID_ARGUMENT (path syntax),
        NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_describe(path="Net.Links", include_attributes=true)
        """
        segments = parse_path(path)
        task = functools.partial(describe_task, segments, bool(include_attributes), settings.max_rows)
        data: dict[str, Any] = await service.call(task, task_name="vissim_describe")
        if include_attributes and isinstance(data.get("attributes"), list):
            fit_list(data, "attributes", settings.max_result_chars)
            data["attributes_truncated"] = data.get("attributes_total", 0) > len(data["attributes"])
        return data

    async def vissim_list_objects(
        collection: str, attributes: list[str], offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Read attributes of all objects of a collection as a table (one GetMultipleAttributes call).

        collection is an object path ("Net.Links", "Net.SignalControllers[1].SGs", "Net.VehicleInputs");
        attributes are Vissim attribute names - result attributes with sub-attributes too, e.g.
        "Vehs(Current,Last,All)" or "QLen(Avg,Avg)". At most VISSIM_MAX_ROWS rows per call (a larger limit is
        reduced, with a warning). While truncated is true, call again with offset = offset + returned. For
        more than about 200 rows prefer vissim_export_csv.

        Parameters: collection; attributes (1..200 names); offset (first row, default 0); limit (rows,
        default 100).
        Returns: collection, columns (= attributes), rows (one list of values per object, in collection
        order), total, offset, returned, truncated; hint "use vissim_export_csv" when rows were cut to fit
        the response size.
        Errors: INVALID_ARGUMENT, RESOLVE_FAILED, COM_ERROR (details.invalid_attributes names the unknown
        attributes), NOT_CONNECTED, TIMEOUT_HUNG.

        Example: vissim_list_objects(collection="Net.Links", attributes=["No", "Name", "NumLanes"], limit=50)
        """
        segments = parse_path(collection)
        names = attribute_names(attributes)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise _invalid(f"offset must be an integer >= 0, got {offset!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise _invalid(f"limit must be an integer >= 1, got {limit!r}")
        warnings: list[str] = []
        if limit > settings.max_rows:
            warnings.append(f"limit {limit} was reduced to {settings.max_rows} (VISSIM_MAX_ROWS)")
            limit = settings.max_rows
        task = functools.partial(list_objects_task, segments, names, offset, limit, settings.max_rows)
        data = await service.call(task, task_name="vissim_list_objects")
        return {
            "collection": format_path(segments),
            "columns": names,
            "rows": data["rows"],
            "total": data["total"],
            "offset": data["offset"],
            "returned": data["returned"],
            "truncated": data["truncated"],
            "warnings": warnings,
        }

    async def vissim_get_attributes(path: str, attributes: list[str]) -> dict[str, Any]:
        """Read attribute values of one object. Each attribute is read on its own: a wrong name only shows up
        in errors, the other values are still returned.

        Parameters: path - object path (e.g. "Net.Links[5]", "Simulation", "Net.SignalControllers[1].SGs[2]",
        "Evaluation"); attributes - 1..200 attribute names (e.g. ["Name", "NumLanes"] or
        ["SimPeriod", "SimRes"]).
        Returns: path, values ({attribute: value}), errors ({attribute: message}; empty when all were read).
        Errors: RESOLVE_FAILED, INVALID_ARGUMENT, NOT_CONNECTED, COM_ERROR (a lost connection), TIMEOUT_HUNG.

        Example: vissim_get_attributes(path="Net.Links[1]", attributes=["Name", "NumLanes"])
        """
        segments = parse_path(path)
        names = attribute_names(attributes)
        task = functools.partial(get_attributes_task, segments, names)
        result: dict[str, Any] = await service.call(task, task_name="vissim_get_attributes")
        return result

    async def vissim_set_attributes(
        path: StringArg, values: ValuesArg, dry_run: FlagArg = False
    ) -> dict[str, Any]:
        """Write attributes of one object: read the old values, write the new ones, read them back.

        Always call it first with dry_run=true, show the old and new values to the user and get approval.
        Attributes whose name contains File, Path, Folder or Dir are refused (VISSIM_ALLOW_PATH_ATTRS=0). An
        attribute whose old value cannot be read is not written; earlier writes are not rolled back when a
        later one fails. Every call - dry runs and refusals too - is written to the audit log. Changes stay in
        Vissim only: save them with vissim_save_network.

        Parameters: path - object path (e.g. "Net.Links[5]", "Simulation"); values - {attribute: new value}
        (numbers, strings or booleans; at most 200); dry_run - only read the current values.
        Returns: path, dry_run, old ({attribute: value before}), new ({attribute: value read back after the
        write; for a dry run the requested value}), failed ([{attribute, stage: read|write|verify, error}]),
        warnings.
        Errors: ATTRIBUTE_DENIED, INVALID_ARGUMENT, RESOLVE_FAILED, NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_set_attributes(path="Net.Links[5]", values={"Name": "Main street"}, dry_run=true)
        """
        args = {"path": path, "values": values, "dry_run": dry_run}
        progress = WriteProgress()
        async with AuditRecord(service, "vissim_set_attributes", args) as audit:
            dry_run = flag(dry_run, "dry_run")
            audit.update(dry_run=dry_run)
            segments = parse_path(path)
            pairs = write_values(values, settings.allow_path_attrs)
            label = format_path(segments)
            audit.update(path=label)
            task = functools.partial(set_attributes_task, segments, pairs, dry_run, progress)
            snapshot: Snapshot | None = None
            try:
                await service.call(task, task_name="vissim_set_attributes")
            except BaseException:
                fields = {"args": args, "dry_run": dry_run, "path": label}
                snapshot = progress.abandon(late_audit(service, "vissim_set_attributes", fields, False))
                raise
            finally:
                old, new, written, failed = snapshot or progress.snapshot()
                audit.update(old=dict(old), new=dict(new), written=written, failed=failed)
        warnings: list[str] = []
        if failed:
            warnings.append(f"{len(failed)} of {len(pairs)} attribute operation(s) failed; see failed")
        return {
            "path": label,
            "dry_run": dry_run,
            "old": dict(old),
            "new": dict(new),
            "failed": failed,
            "warnings": [*warnings, *audit.warnings],
        }

    async def vissim_set_attribute_bulk(
        collection: StringArg, attribute: StringArg, items: ItemsArg, dry_run: FlagArg = False
    ) -> dict[str, Any]:
        """Write one attribute of many objects of a collection in a single COM task (ItemByKey loop).

        For example new volumes for all vehicle inputs, or new names for many links. Always run it first with
        dry_run=true and get approval. At most 5000 items per call; every key at most once. The attribute
        policy and the audit log of vissim_set_attributes apply. Changes stay in Vissim only: save them with
        vissim_save_network.

        Parameters: collection - collection path (e.g. "Net.VehicleInputs", "Net.SignalControllers[1].SGs");
        attribute - attribute name (e.g. "Volume(1)"); items - [{key, value}] (key: the object key, an integer
        or a string); dry_run - only read the current values.
        Returns: collection, attribute, dry_run, total, updated (objects written; 0 for a dry run), failed
        ([{key, stage, error}], the first 100) and failed_total, changes ([{key, old, new}], at most
        VISSIM_MAX_ROWS), changes_total, changes_truncated, warnings.
        Errors: ATTRIBUTE_DENIED, INVALID_ARGUMENT (too many items, duplicate keys, bad values),
        RESOLVE_FAILED, NOT_CONNECTED, COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_set_attribute_bulk(collection="Net.VehicleInputs", attribute="Volume(1)",
        items=[{"key": 1, "value": 800}], dry_run=true)
        """
        listed = list(items) if isinstance(items, Sequence) and not isinstance(items, str) else []
        args = {
            "collection": collection,
            "attribute": attribute,
            "items": [_item_pair(item) for item in listed[:MAX_BULK_ITEMS]],
            "items_total": len(listed),
            "dry_run": dry_run,
        }
        progress = WriteProgress()
        async with AuditRecord(service, "vissim_set_attribute_bulk", args) as audit:
            dry_run = flag(dry_run, "dry_run")
            audit.update(dry_run=dry_run)
            segments = parse_path(collection)
            name = attribute_name(attribute)
            check_attribute_writable(name, settings.allow_path_attrs)
            pairs = bulk_items(items)
            label = format_path(segments)
            audit.update(path=label, attribute=name)
            task = functools.partial(bulk_task, segments, name, pairs, dry_run, progress)
            snapshot: Snapshot | None = None
            try:
                await service.call(task, task_name="vissim_set_attribute_bulk")
            except BaseException:
                fields = {"args": args, "dry_run": dry_run, "path": label, "attribute": name}
                snapshot = progress.abandon(late_audit(service, "vissim_set_attribute_bulk", fields, True))
                raise
            finally:
                old, new, written, failed = snapshot or progress.snapshot()
                audit.update(old=_audit_map(old), new=_audit_map(new), written=written, failed=failed)
        new_by_key = dict(new)
        changes = [
            {"key": key, "old": value, "new": new_by_key[key]} for key, value in old if key in new_by_key
        ]
        result: dict[str, Any] = {
            "collection": label,
            "attribute": name,
            "dry_run": dry_run,
            "total": len(pairs),
            "updated": len(written),
            "failed": failed[:MAX_FAILED_SHOWN],
            "failed_total": len(failed),
            "changes": changes[: settings.max_rows],
            "changes_total": len(changes),
        }
        warnings: list[str] = []
        if failed:
            warnings.append(f"{len(failed)} of {len(pairs)} item(s) failed; see failed")
        warnings.extend(audit.warnings)
        fit_list(result, "changes", settings.max_result_chars - len(dumps(warnings)))
        result["changes_truncated"] = len(result["changes"]) < len(changes)
        result["warnings"] = warnings
        return result

    register_tool(
        mcp,
        vissim_describe,
        name="vissim_describe",
        title="Describe a Vissim object",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_list_objects,
        name="vissim_list_objects",
        title="List objects of a collection",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_get_attributes,
        name="vissim_get_attributes",
        title="Read attributes of an object",
        settings=settings,
        read_only=True,
        idempotent=True,
    )
    register_tool(
        mcp,
        vissim_set_attributes,
        name="vissim_set_attributes",
        title="Write attributes of an object",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_set_attribute_bulk,
        name="vissim_set_attribute_bulk",
        title="Write one attribute of many objects",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
