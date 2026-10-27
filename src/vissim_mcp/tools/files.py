r"""File tools (PLAN 7.2): vissim_load_network and vissim_save_network.

Paths go through the service's :class:`~vissim_mcp.policy.PathPolicy` (in a worker thread of the event loop:
file system checks must not block the MCP session); the COM calls run on the COM worker with the load timeout
(``VISSIM_LOAD_TIMEOUT_S``). Both tools write one audit record per call (PLAN 6.8), whatever the outcome.

Saving never destroys data silently: an existing target - and the ``.layx`` layout of the same name, which
Vissim may rewrite with the network - is copied to ``<folder>\backups\`` first, and a file that was loaded in
this connection is only overwritten with ``overwrite_loaded=true``. That layout must pass the write policy
like the target itself (no hard link, no link into a read-only or foreign directory).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..errors import ErrorCode, VissimMcpError, describe_exception, is_com_error, is_connection_error
from ..policy import LOAD_LAYOUT_EXT, LOAD_NET_EXT, SAVE_EXT, PathPolicy, make_backup
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, FlagArg, OptionalStringArg, StringArg, flag, register_tool
from .lifecycle import collection_counts

LAYOUT_SUFFIX = ".layx"
READ_ONLY_LOAD_WARNING = (
    "the network was loaded from a read-only directory: save your work under a new name with "
    "vissim_save_network before simulating (Vissim may write result files next to the network file)"
)
_HASH_CHUNK = 1024 * 1024


# --- COM tasks (worker thread)


def load_network_task(inpx: str, layx: str | None, additive: bool, ctx: WorkerContext) -> dict[str, Any]:
    """``LoadNet(inpx, additive)``, then ``LoadLayout(layx)`` and the collection counts.

    A layout that cannot be loaded does not fail the call - the network *is* loaded by then - it is reported
    in ``layout_loaded``/``layout_error``. A lost connection is always raised.
    """
    vissim = ctx.vissim
    vissim.LoadNet(inpx, additive)
    result: dict[str, Any] = {"layout_loaded": None}
    if layx is not None:
        try:
            vissim.LoadLayout(layx)
            result["layout_loaded"] = True
        except Exception as exc:
            if is_connection_error(exc) or not is_com_error(exc):
                raise
            result["layout_loaded"] = False
            result["layout_error"] = describe_exception(exc, 300)
    errors: dict[str, str] = {}
    result["counts"] = collection_counts(vissim, errors)
    if errors:
        result["errors"] = errors
    return result


def save_network_task(target: str, ctx: WorkerContext) -> None:
    ctx.vissim.SaveNetAs(target)


# --- file helpers (run in a thread)


def file_digest(path: Path) -> tuple[str, int]:
    """``(sha256 hex digest, size in bytes)`` of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def backup_before_save(policy: PathPolicy, target: Path) -> dict[str, str]:
    """Back up the existing target and its same-named ``.layx`` layout; returns the backup paths by role."""
    backups: dict[str, str] = {}
    if target.is_file():
        backups["backup_path"] = str(make_backup(target, policy=policy))
    layout = target.with_suffix(LAYOUT_SUFFIX)
    if layout.is_file():
        backups["layout_backup_path"] = str(make_backup(layout, policy=policy))
    return backups


def check_layout_companion(policy: PathPolicy, target: Path) -> list[str]:
    """The write policy for ``<target>.layx``: Vissim may rewrite it together with the network, so a hard link
    or a symbolic link into a read-only or foreign directory must be refused like the target itself.

    Only checked when something exists under that name (``lexists``: a broken link is refused too). Returns
    the policy warnings; raises the policy's ``VissimMcpError`` with ``details.role = "layout"``.
    """
    layout = target.with_suffix(LAYOUT_SUFFIX)
    if not os.path.lexists(layout):
        return []
    try:
        return policy.check_write(layout, LOAD_LAYOUT_EXT)[1]
    except VissimMcpError as err:
        err.message = f"the layout file next to the target cannot be overwritten: {err.message}"
        err.details = {**(err.details or {}), "role": "layout", "layout_path": str(layout)}
        if not err.hint:
            err.hint = "Save under a new file name (a name without a .layx file next to it)."
        raise


def require_folder(target: Path) -> None:
    if not target.parent.is_dir():
        raise VissimMcpError(
            ErrorCode.FILE_NOT_FOUND,
            f"the folder '{target.parent}' does not exist",
            hint="Save into an existing folder (folders are not created automatically).",
            details={"path": str(target)},
        )


def _merge(warnings: list[str], more: list[str]) -> None:
    warnings.extend(warning for warning in more if warning not in warnings)


# --- tools


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings
    policy = service.policy

    async def vissim_load_network(
        inpx_path: StringArg, layx_path: OptionalStringArg = None, additive: FlagArg = False
    ) -> dict[str, Any]:
        """Load a Vissim network file (.inpx) into the connected Vissim, optionally with a layout (.layx).

        The path must be inside the allowed directories (VISSIM_ALLOWED_DIRS; vissim_health shows them); a
        relative path is relative to the first of them. Without additive the current network is REPLACED and
        unsaved changes are lost - save first with vissim_save_network. Refused while a simulation job is
        running or paused. A large network can take minutes to load (limit VISSIM_LOAD_TIMEOUT_S). Originals
        in a read-only folder (default <root>\\models\\original) can be loaded; save them under a new name.

        Parameters: inpx_path - the .inpx file; layx_path - optional .layx layout file, loaded after the
        network; additive - true merges the file into the current network instead of replacing it.
        Returns: loaded_file (the resolved path), layout_file, additive, counts (objects of the main
        collections; null when a count could not be read), errors (only for unreadable counts), layout_loaded
        and layout_error (when a layout was given), elapsed_s, warnings (e.g. a non-ASCII path).
        Errors: NOT_CONNECTED, JOB_RUNNING, PATH_DENIED, INVALID_ARGUMENT (file type, path syntax),
        FILE_NOT_FOUND, COM_ERROR (Vissim could not read the file), TIMEOUT_HUNG.

        Example: vissim_load_network(inpx_path="scenarios\\base.inpx")
        """
        args = {"inpx_path": inpx_path, "layx_path": layx_path, "additive": additive}
        async with AuditRecord(service, "vissim_load_network", args) as audit:
            additive = flag(additive, "additive")
            service.ensure_connected()
            job = service.active_job()
            if job is not None:
                job_id = getattr(job, "id", None)
                raise VissimMcpError(
                    ErrorCode.JOB_RUNNING,
                    f"simulation job {job_id} is {getattr(job, 'state', 'active')}; loading a network would "
                    "discard it",
                    details={"job_id": job_id},
                )
            inpx, warnings = await asyncio.to_thread(policy.check_read, inpx_path, LOAD_NET_EXT)
            layx: Path | None = None
            if layx_path is not None and str(layx_path).strip():
                layx, layout_warnings = await asyncio.to_thread(policy.check_read, layx_path, LOAD_LAYOUT_EXT)
                _merge(warnings, layout_warnings)
            audit.update(path=str(inpx), layout_path=str(layx) if layx is not None else None)
            if await asyncio.to_thread(policy.is_readonly, inpx):
                warnings.append(READ_ONLY_LOAD_WARNING)
            generation = service.generation
            started = time.monotonic()
            task = functools.partial(
                load_network_task, str(inpx), str(layx) if layx is not None else None, bool(additive)
            )
            try:
                data = await service.call(
                    task, timeout_s=settings.load_timeout_s, task_name="vissim_load_network"
                )
            except VissimMcpError:
                if service.generation == generation:
                    service.note_load_failed(str(inpx), additive=bool(additive))
                raise
            if service.generation == generation:
                service.remember_loaded(str(inpx), additive=bool(additive))
            if data.get("layout_loaded") is False:
                warnings.append(
                    f"the network was loaded, but the layout could not be loaded: {data.get('layout_error')}"
                )
            audit.update(counts=data.get("counts"), layout_loaded=data.get("layout_loaded"))
        result: dict[str, Any] = {
            "loaded_file": str(inpx),
            "layout_file": str(layx) if layx is not None else None,
            "additive": bool(additive),
            "counts": data.get("counts"),
        }
        for key in ("errors", "layout_loaded", "layout_error"):
            if data.get(key) is not None:
                result[key] = data[key]
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        result["warnings"] = [*warnings, *audit.warnings]
        return result

    async def vissim_save_network(save_as: StringArg, overwrite_loaded: FlagArg = False) -> dict[str, Any]:
        """Save the network in Vissim to a .inpx file (SaveNetAs); a file it replaces is backed up first.

        save_as must be inside the allowed directories and outside the read-only ones (default
        <root>\\models\\original); a relative path is relative to the first allowed directory and the folder
        must exist. An existing file is first copied to <folder>\\backups\\<name>_YYYYMMDD_HHMMSS.inpx (its
        .layx layout too). Overwriting a file loaded in this session - normally the original network - also
        needs overwrite_loaded=true. Nothing is saved automatically: call this after every change to keep.
        Every call is written to the audit log (path, backups, SHA-256).

        Parameters: save_as - the target .inpx path; overwrite_loaded - allow overwriting a file that was
        loaded in this session.
        Returns: saved_path, backup_path (null when no file was replaced), layout_backup_path (only when a
        layout was backed up), sha256 and size_bytes of the saved file, overwritten, warnings.
        Errors: NOT_CONNECTED, PATH_DENIED, READ_ONLY_PATH, INVALID_ARGUMENT (file type, the loaded file
        without overwrite_loaded), FILE_NOT_FOUND (the folder does not exist), COM_ERROR, TIMEOUT_HUNG.

        Example: vissim_save_network(save_as="scenarios\\base_v2.inpx")
        """
        args = {"save_as": save_as, "overwrite_loaded": overwrite_loaded}
        async with AuditRecord(service, "vissim_save_network", args) as audit:
            overwrite_loaded = flag(overwrite_loaded, "overwrite_loaded")
            service.ensure_connected()
            target, warnings = await asyncio.to_thread(policy.check_write, save_as, SAVE_EXT)
            audit.update(path=str(target))
            await asyncio.to_thread(require_folder, target)
            _merge(warnings, await asyncio.to_thread(check_layout_companion, policy, target))
            if await asyncio.to_thread(service.is_loaded_file, target) and not overwrite_loaded:
                raise VissimMcpError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"'{target}' is a network file loaded in this session; saving would overwrite it",
                    hint="Save under a new name, or pass overwrite_loaded=true (a backup is made first).",
                    details={"path": str(target)},
                )
            existed = target.exists()
            backups = await asyncio.to_thread(backup_before_save, policy, target)
            audit.update(overwritten=existed, **backups)
            try:
                await service.call(
                    functools.partial(save_network_task, str(target)),
                    timeout_s=settings.load_timeout_s,
                    task_name="vissim_save_network",
                )
            except VissimMcpError as err:
                if backups:
                    kept = backups.get("backup_path") or backups.get("layout_backup_path")
                    err.details = {**(err.details or {}), **backups}
                    err.message = f"{err.message} (the previous version is kept in {kept})"
                raise
            service.saved_file = str(target)
            digest: str | None = None
            size: int | None = None
            try:
                digest, size = await asyncio.to_thread(file_digest, target)
            except OSError as exc:
                warnings.append(
                    f"the saved file could not be read back for its SHA-256: {describe_exception(exc)}"
                )
            audit.update(sha256=digest, size_bytes=size)
        result: dict[str, Any] = {"saved_path": str(target), "backup_path": backups.get("backup_path")}
        if "layout_backup_path" in backups:
            result["layout_backup_path"] = backups["layout_backup_path"]
        result.update(
            sha256=digest, size_bytes=size, overwritten=existed, warnings=[*warnings, *audit.warnings]
        )
        return result

    register_tool(
        mcp,
        vissim_load_network,
        name="vissim_load_network",
        title="Load a Vissim network",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
    register_tool(
        mcp,
        vissim_save_network,
        name="vissim_save_network",
        title="Save the Vissim network",
        settings=settings,
        read_only=False,
        destructive=True,
        idempotent=False,
    )
