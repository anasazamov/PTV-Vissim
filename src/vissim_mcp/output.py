"""JSON conversion and response size limits (PLAN 6.9).

* :func:`to_jsonable` turns anything a COM call can return into JSON-safe data (NaN/Inf -> null with one
  warning, datetimes -> ISO strings, unknown objects -> ``str()`` cut to 200 characters, nesting limited to 20
  levels).
* :func:`paginate` and :func:`fit_payload` keep every tool result below ``VISSIM_MAX_RESULT_CHARS`` (Claude
  Code stores results longer than 50 000 characters in a file instead of showing them to the model).
* :func:`dumps` is the single JSON encoder of the server: compact, UTF-8 friendly (``ensure_ascii=False``),
  never emits NaN, and replaces lone surrogates (possible in malformed COM strings), which would otherwise
  make the MCP SDK's serializer fail and break the session.
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from mcp.types import CallToolResult, TextContent

from .errors import ErrorCode, VissimMcpError

MAX_DEPTH = 20
MAX_FALLBACK_STR = 200
MIN_KEPT_STR = 200
TRUNCATION_HINT = "use vissim_export_csv"
NONFINITE_WARNING = "non-finite numbers (NaN/Infinity) were replaced with null"
DEPTH_WARNING = f"values nested deeper than {MAX_DEPTH} levels were replaced with a placeholder"
STRINGS_SHORTENED_WARNING = (
    "long text values were shortened to fit the response size limit (VISSIM_MAX_RESULT_CHARS)"
)
DEPTH_PLACEHOLDER = "<nested too deep>"

_SURROGATES = re.compile("[\ud800-\udfff]")


# --- conversion


def to_jsonable(value: Any, warnings: list[str] | None = None) -> Any:
    """Convert ``value`` to data that ``json.dumps(..., allow_nan=False)`` always accepts.

    ``warnings`` (if given) receives each distinct warning once.
    """
    return _convert(value, warnings, 0)


def _convert(value: Any, warnings: list[str] | None, depth: int) -> Any:
    if value is None or value is True or value is False:
        return value
    if isinstance(value, enum.Enum):
        return _convert(value.value, warnings, depth)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return float(value)
        _warn(warnings, NONFINITE_WARNING)
        return None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, os.PathLike):
        return os.fsdecode(os.fspath(value))
    if isinstance(value, Mapping):
        if depth >= MAX_DEPTH:
            _warn(warnings, DEPTH_WARNING)
            return DEPTH_PLACEHOLDER
        return {_key(k): _convert(v, warnings, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            _warn(warnings, DEPTH_WARNING)
            return DEPTH_PLACEHOLDER
        return [_convert(v, warnings, depth + 1) for v in value]
    if isinstance(value, AbstractSet):
        if depth >= MAX_DEPTH:
            _warn(warnings, DEPTH_WARNING)
            return DEPTH_PLACEHOLDER
        items = [_convert(v, warnings, depth + 1) for v in value]
        try:
            return sorted(items)
        except TypeError:
            return items
    time_like = _time_like(value)
    if time_like is not None:
        return time_like
    return _short_str(value)


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None and message not in warnings:
        warnings.append(message)


def _key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key) if not isinstance(key, float) or math.isfinite(key) else str(key)
    return _short_str(key)


def _time_like(value: Any) -> str | None:
    """pywintypes.Time-like objects: ``isoformat()`` if available, else ``Format()``."""
    try:
        if not hasattr(value, "year"):
            return None
        iso = getattr(value, "isoformat", None)
        if callable(iso):
            return str(iso())
        fmt = getattr(value, "Format", None)
        if callable(fmt):
            return str(fmt("%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None
    return None


def _short_str(value: Any) -> str:
    try:
        text = str(value)
    except Exception:
        text = f"<{type(value).__name__}>"
    if len(text) > MAX_FALLBACK_STR:
        text = text[: MAX_FALLBACK_STR - 3] + "..."
    return text


# --- payloads


def dumps(payload: Any) -> str:
    """Compact JSON text of ``payload`` (UTF-8 friendly, no NaN, lone surrogates replaced by U+FFFD)."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = _SURROGATES.sub("�", text)
    return text


def ok(**fields: Any) -> dict[str, Any]:
    """Success payload ``{"ok": true, ...}``; an empty ``warnings`` list is dropped."""
    payload: dict[str, Any] = {"ok": True}
    payload.update(fields)
    payload["ok"] = True
    if not payload.get("warnings"):
        payload.pop("warnings", None)
    return payload


def error_payload(err: VissimMcpError, warnings: Sequence[str] | None = None) -> dict[str, Any]:
    """Failure payload ``{"ok": false, "error": {...}}`` (plus non-empty ``warnings``)."""
    payload: dict[str, Any] = {"ok": False, "error": err.to_dict()}
    if warnings:
        payload["warnings"] = list(warnings)
    return payload


def paginate(rows: Sequence[Any], offset: int, limit: int, max_rows: int) -> tuple[list[Any], dict[str, Any]]:
    """Slice ``rows`` to one page; ``limit`` is clamped to ``max_rows`` (VISSIM_MAX_ROWS).

    Returns ``(page, meta)`` with ``meta = {total, offset, returned, truncated}``; ``truncated`` is true when
    rows exist after this page.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise VissimMcpError(ErrorCode.INVALID_ARGUMENT, f"offset must be an integer >= 0, got {offset!r}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise VissimMcpError(ErrorCode.INVALID_ARGUMENT, f"limit must be an integer >= 1, got {limit!r}")
    effective = min(limit, max(1, max_rows))
    total = len(rows)
    page = list(rows[offset : offset + effective])
    meta = {
        "total": total,
        "offset": offset,
        "returned": len(page),
        "truncated": offset + len(page) < total,
    }
    return page, meta


def fit_payload(payload: dict[str, Any], max_chars: int, rows_key: str = "rows") -> dict[str, Any]:
    """Make the JSON text of ``payload`` at most ``max_chars`` characters long.

    1. Drop rows from the end of ``payload[rows_key]`` (``truncated: true``, ``returned``, ``hint``).
    2. If that is not enough (or there are no rows), shorten the longest strings and add a warning.
    3. Last resort: an ``INTERNAL`` error payload saying the result was too large.

    ``payload`` must already be JSON-able (see :func:`to_jsonable`); it is not modified.
    """
    if _size(payload) <= max_chars:
        return payload
    work = payload
    rows = payload.get(rows_key)
    if isinstance(rows, list) and rows:
        fitted = _fit_rows(payload, rows_key, rows, max_chars)
        if fitted is not None:
            return fitted
        work = _with_rows(payload, rows_key, rows, 0)
    shortened = _shorten_strings(work, max_chars)
    if shortened is not None:
        return shortened
    return error_payload(
        VissimMcpError(
            ErrorCode.INTERNAL,
            f"result too large to return ({_size(payload)} characters, limit {max_chars}) "
            "even after truncation",
            hint="Request fewer rows or attributes, or use vissim_export_csv.",
        )
    )


def to_tool_result(payload: Mapping[str, Any]) -> CallToolResult:
    """Wrap a payload as an MCP tool result (one text block with compact JSON; ``isError`` mirrors ``ok``)."""
    return CallToolResult(
        content=[TextContent(type="text", text=dumps(payload))],
        isError=not bool(payload.get("ok")),
    )


def _size(payload: Any) -> int:
    return len(dumps(payload))


def _with_rows(payload: dict[str, Any], rows_key: str, rows: list[Any], count: int) -> dict[str, Any]:
    candidate = dict(payload)
    candidate[rows_key] = rows[:count]
    candidate["returned"] = count
    candidate["truncated"] = True
    candidate["hint"] = TRUNCATION_HINT
    return candidate


def _fit_rows(
    payload: dict[str, Any], rows_key: str, rows: list[Any], max_chars: int
) -> dict[str, Any] | None:
    low, high = 0, len(rows) - 1
    best: dict[str, Any] | None = None
    while low <= high:
        mid = (low + high) // 2
        candidate = _with_rows(payload, rows_key, rows, mid)
        if _size(candidate) <= max_chars:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _shorten_strings(payload: dict[str, Any], max_chars: int) -> dict[str, Any] | None:
    work = json.loads(dumps(payload))  # deep copy of plain JSON data
    warnings = work.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    if STRINGS_SHORTENED_WARNING not in warnings:
        warnings.append(STRINGS_SHORTENED_WARNING)
    work["warnings"] = warnings

    for _ in range(10_000):
        size = _size(work)
        if size <= max_chars:
            return work
        leaves = _string_leaves(work)
        if not leaves:
            return None
        container, key, text = max(leaves, key=lambda leaf: len(leaf[2]))
        if len(text) <= MIN_KEPT_STR:
            return None
        excess = size - max_chars
        marker_room = 40
        keep = max(MIN_KEPT_STR, len(text) - excess - marker_room)
        if keep >= len(text):
            keep = max(MIN_KEPT_STR, len(text) // 2)
        removed = len(text) - keep
        container[key] = f"{text[:keep]}...[{removed} chars truncated]"
    return None


def _string_leaves(node: Any) -> list[tuple[Any, Any, str]]:
    leaves: list[tuple[Any, Any, str]] = []
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            items: Any = current.items()
        elif isinstance(current, list):
            items = enumerate(current)
        else:
            continue
        for key, value in items:
            if isinstance(value, str):
                if value != STRINGS_SHORTENED_WARNING:
                    leaves.append((current, key, value))
            elif isinstance(value, (dict, list)):
                stack.append(value)
    return leaves
