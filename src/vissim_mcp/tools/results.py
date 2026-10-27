"""Result tools (PLAN 7.5): vissim_export_csv.

The table is read with one ``GetMultipleAttributes`` call on the COM worker and written - off the event loop -
as a ``;``-separated, ``utf-8-sig`` CSV (Excel opens it directly). The target passes the write policy
(allowed roots, not read-only, ``.csv`` only); the file is written to a temporary name first and then moved
into place, so a failed export never leaves half a file. Every call writes one audit record (PLAN 6.8).
"""

from __future__ import annotations

import asyncio
import csv
import functools
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from ..errors import ErrorCode, VissimMcpError, describe_exception, is_com_error, is_connection_error
from ..policy import EXPORT_EXT
from ..resolver import Segment, format_path, parse_path, resolve
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, StringArg, register_tool
from .files import require_folder
from .objects import (
    COM_OBJECT_MARKER,
    _member,
    _require_object,
    _unreadable_attributes,
    attribute_names,
    plain_value,
)

CSV_DELIMITER = ";"
CSV_ENCODING = "utf-8-sig"
AttributesArg = Annotated[Any, WithJsonSchema({"type": "array", "items": {"type": "string"}})]


def export_rows_task(segments: list[Segment], attributes: list[str], ctx: WorkerContext) -> list[list[Any]]:
    """``GetMultipleAttributes(attributes)`` of the collection at ``segments`` as plain rows."""
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
    return [
        plain_value(list(row)) if isinstance(row, (list, tuple)) else [plain_value(row)] for row in rows or ()
    ]


def csv_cell(value: Any) -> Any:
    """One CSV cell: null -> empty, booleans as 1/0 (like Vissim's own lists), nested values as text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    if isinstance(value, list):
        return ",".join(str(csv_cell(item)) for item in value)
    text = str(value)
    return text if text else COM_OBJECT_MARKER


def write_csv(target: Path, header: list[str], rows: list[list[Any]]) -> tuple[str, int]:
    """Write the CSV atomically (temporary file + replace); returns ``(sha256, size in bytes)``."""
    handle, temp_name = tempfile.mkstemp(prefix=f".{target.stem}_", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding=CSV_ENCODING, newline="") as stream:
            writer = csv.writer(stream, delimiter=CSV_DELIMITER, lineterminator="\r\n")
            writer.writerow(header)
            for row in rows:
                writer.writerow([csv_cell(value) for value in row])
        data = temp.read_bytes()
        os.replace(temp, target)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return hashlib.sha256(data).hexdigest(), len(data)


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings
    policy = service.policy

    async def vissim_export_csv(
        collection: StringArg, attributes: AttributesArg, csv_path: StringArg
    ) -> dict[str, Any]:
        """Export attributes of all objects of a collection to a CSV file (for tables of more than ~200 rows).

        One GetMultipleAttributes call reads the whole collection; the file is ';'-separated, UTF-8 with BOM
        (opens directly in Excel), one header row with the attribute names, then one row per object in
        collection order. csv_path must be a .csv inside the allowed directories and outside the read-only
        ones; a relative path is relative to the first allowed directory and the folder must exist. An
        existing file is replaced. Written to the audit log.

        Parameters: collection - collection path (e.g. "Net.Links", "Net.DataCollectionMeasurements");
        attributes - 1..200 attribute names, result attributes too (e.g. ["No", "Vehs(Current,Last,All)"]);
        csv_path - the target file.
        Returns: path, rows (objects written), columns, sha256, size_bytes, overwritten, warnings.
        Errors: PATH_DENIED, READ_ONLY_PATH, INVALID_ARGUMENT, FILE_NOT_FOUND (folder missing),
        RESOLVE_FAILED, COM_ERROR (details.invalid_attributes), NOT_CONNECTED, TIMEOUT_HUNG.

        Example: vissim_export_csv(collection="Net.Links", attributes=["No", "Name"], csv_path="links.csv")
        """
        args = {"collection": collection, "attributes": attributes, "csv_path": csv_path}
        async with AuditRecord(service, "vissim_export_csv", args) as audit:
            if not isinstance(collection, str):
                raise VissimMcpError(ErrorCode.INVALID_ARGUMENT, "collection must be a string", hint="")
            segments = parse_path(collection)
            names = attribute_names(attributes)
            service.ensure_connected()
            target, warnings = await asyncio.to_thread(policy.check_write, csv_path, EXPORT_EXT)
            audit.update(path=str(target))
            await asyncio.to_thread(require_folder, target)
            task = functools.partial(export_rows_task, segments, names)
            rows = await service.call(task, task_name="vissim_export_csv")
            existed = await asyncio.to_thread(target.exists)
            try:
                digest, size = await asyncio.to_thread(write_csv, target, names, rows)
            except OSError as exc:
                raise VissimMcpError(
                    ErrorCode.INTERNAL,
                    f"writing '{target}' failed: {describe_exception(exc)}",
                    hint="Check that the file is not open in Excel and that the folder is writable.",
                    details={"path": str(target)},
                ) from None
            audit.update(rows=len(rows), columns=names, sha256=digest, size_bytes=size, overwritten=existed)
        return {
            "path": str(target),
            "rows": len(rows),
            "columns": names,
            "sha256": digest,
            "size_bytes": size,
            "overwritten": existed,
            "warnings": [*warnings, *audit.warnings],
        }

    register_tool(
        mcp,
        vissim_export_csv,
        name="vissim_export_csv",
        title="Export a collection to CSV",
        settings=settings,
        read_only=False,
        destructive=True,  # an existing CSV at csv_path is replaced without a backup
        idempotent=True,
    )
