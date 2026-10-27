"""Signal tool (PLAN 7.5): vissim_set_signal_state.

Sets the state of one signal group (``Net.SignalControllers[sc_no].SGs[sg_no]``) through COM, or hands it back
to its signal controller. Writing ``SigState`` makes Vissim switch the group to COM control (``ContrByCOM``)
by itself; ``release_to_controller`` writes ``ContrByCOM = False``. Refused with ``JOB_RUNNING`` while a
simulation job is ``running`` - allowed when it is ``paused`` (at a ``pause_at`` point) or when there is no
active job. The check is repeated on the COM worker, right before the write. Every call writes one audit
record with the old and the new values (PLAN 6.8).
"""

from __future__ import annotations

import functools
import math
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
from ..jobs import JobState, job_running_error
from ..resolver import Segment, format_path
from ..resolver import resolve as resolve_path
from ..service import VissimService
from ..worker import WorkerContext
from ._common import AuditRecord, FlagArg, flag, register_tool
from .objects import _require_object, plain_value

SIG_STATE = "SigState"
CONTR_BY_COM = "ContrByCOM"
MAX_KEY = 2**31 - 1
MAX_STATE_CHARS = 32
STATE_HINT = (
    'Use a Vissim signal state name such as "RED", "GREEN", "AMBER", "REDAMBER" (or its number 1..11); '
    "check the current value with vissim_get_attributes."
)

IntArg = Annotated[Any, WithJsonSchema({"type": "integer"})]
StateArg = Annotated[
    Any, WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]})
]


def _invalid(message: str, hint: str = "") -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, message, hint=hint)


def object_number(value: Any, field: str) -> int:
    """``sc_no`` / ``sg_no``: an integer >= 1 (an integral float is accepted)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{field} must be an integer >= 1, got {value!r}")
    if isinstance(value, float):
        if not (math.isfinite(value) and value.is_integer()):
            raise _invalid(f"{field} must be an integer >= 1, got {value!r}")
        value = int(value)
    if not 1 <= value <= MAX_KEY:
        raise _invalid(f"{field} must be an integer between 1 and {MAX_KEY}, got {value}")
    return int(value)


def signal_state(value: Any) -> str | int | None:
    """``state``: None, a state name (upper-cased, blanks stripped) or an integer (passed through)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid(f"state must be a signal state name or number, got {value!r}", STATE_HINT)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().upper()
        if not text or len(text) > MAX_STATE_CHARS:
            raise _invalid(
                f"state must be a non-empty signal state name of at most {MAX_STATE_CHARS} characters",
                STATE_HINT,
            )
        return text
    raise _invalid(f"state must be a signal state name or number, got {value!r}", STATE_HINT)


def signal_group_path(sc_no: int, sg_no: int) -> list[Segment]:
    return [Segment("Net"), Segment("SignalControllers", sc_no), Segment("SGs", sg_no)]


def running_job(service: VissimService) -> Any | None:
    """The active job when it is ``running`` (a paused job allows the change), else None. Never raises."""
    job = service.active_job()
    if job is None:
        return None
    state = getattr(job, "state", None)
    return job if str(getattr(state, "value", state)) == JobState.RUNNING.value else None


# --- COM task (worker thread)


def _read(obj: Any, name: str, errors: dict[str, str]) -> Any:
    try:
        return plain_value(obj.AttValue(name))
    except Exception as exc:
        if is_connection_error(exc) or not is_com_error(exc):
            raise
        errors[name] = describe_exception(exc, 300)
        return None


def _write(obj: Any, name: str, value: Any, label: str, hint: str | None) -> None:
    try:
        obj.SetAttValue(name, value)
    except Exception as exc:
        if is_connection_error(exc) or not is_com_error(exc):
            raise
        error = from_com_error(exc)
        error.message = f"{label}: SetAttValue({name!r}, {value!r}) failed: {error.message}"
        if hint:
            error.hint = hint
        raise error from None


def set_signal_task(
    segments: list[Segment],
    state: str | int | None,
    release: bool,
    service: VissimService,
    progress: dict[str, Any],
    ctx: WorkerContext,
) -> dict[str, Any]:
    """Read SigState/ContrByCOM, write the state or release the group, read both back.

    ``progress`` (filled on this thread, read by the tool for the audit record) receives ``old``,
    ``written`` and ``new`` as they happen, so a failure or a timeout still shows what was done.
    """
    job = running_job(service)  # a job may have been resumed after the tool's own check
    if job is not None:
        raise job_running_error(job)
    label = format_path(segments)
    group = resolve_path(ctx.vissim, segments)
    _require_object(group, label)
    errors: dict[str, str] = {}
    progress["old"] = {
        SIG_STATE: _read(group, SIG_STATE, errors),
        CONTR_BY_COM: _read(group, CONTR_BY_COM, errors),
    }
    written: list[str] = []
    progress["written"] = written
    if state is not None:
        _write(group, SIG_STATE, state, label, STATE_HINT)
        written.append(SIG_STATE)
    if release:
        _write(group, CONTR_BY_COM, False, label, None)
        written.append(CONTR_BY_COM)
    after: dict[str, str] = {}
    progress["new"] = {
        SIG_STATE: _read(group, SIG_STATE, after),
        CONTR_BY_COM: _read(group, CONTR_BY_COM, after),
    }
    return {"path": label, "old": progress["old"], "new": progress["new"], "errors": {**errors, **after}}


# --- tool


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_set_signal_state(
        sc_no: IntArg, sg_no: IntArg, state: StateArg = None, release_to_controller: FlagArg = False
    ) -> dict[str, Any]:
        """Set the state of one signal group through COM, or hand the group back to its signal controller.

        Allowed only when no simulation job is running: with no job, or while a job is paused (start the
        simulation with pause_at, wait until state is paused, set the signals, then vissim_resume_simulation).
        Writing a state switches the group to COM control - Vissim sets ContrByCOM=true itself and the group
        keeps this state until it is released. release_to_controller=true writes ContrByCOM=false so the
        signal controller drives the group again. Give either state or release_to_controller=true. Written to
        the audit log with the old and new values.

        Parameters: sc_no - signal controller number; sg_no - signal group number within it; state - signal
        state name ("RED", "GREEN", "AMBER", "REDAMBER", ...; case-insensitive) or its number (1..11);
        release_to_controller - true to give control back to the signal controller.
        Returns: sc_no, sg_no, path, SigState and ContrByCOM (read back after the change), old ({SigState,
        ContrByCOM} before), errors (values that could not be read), warnings.
        Errors: JOB_RUNNING (a job is running - wait for a pause or stop it), INVALID_ARGUMENT (bad numbers,
        neither or both of state and release_to_controller), RESOLVE_FAILED (no such signal controller or
        group), NOT_CONNECTED, COM_ERROR (e.g. Vissim refused the state), TIMEOUT_HUNG.

        Example: vissim_set_signal_state(sc_no=1, sg_no=2, state="GREEN")
        """
        args = {
            "sc_no": sc_no,
            "sg_no": sg_no,
            "state": state,
            "release_to_controller": release_to_controller,
        }
        progress: dict[str, Any] = {}
        async with AuditRecord(service, "vissim_set_signal_state", args) as audit:
            sc = object_number(sc_no, "sc_no")
            sg = object_number(sg_no, "sg_no")
            new_state = signal_state(state)
            release = flag(release_to_controller, "release_to_controller")
            if new_state is None and not release:
                raise _invalid(
                    "give a state or release_to_controller=true",
                    hint='E.g. vissim_set_signal_state(sc_no=1, sg_no=2, state="RED").',
                )
            if new_state is not None and release:
                raise _invalid(
                    "give either a state or release_to_controller=true, not both: writing a state puts the "
                    "group under COM control, releasing it gives control back to the signal controller"
                )
            segments = signal_group_path(sc, sg)
            label = format_path(segments)
            audit.update(path=label)
            service.ensure_connected()
            job = running_job(service)
            if job is not None:
                raise job_running_error(job)
            task = functools.partial(set_signal_task, segments, new_state, release, service, progress)
            try:
                data = await service.call(task, task_name="vissim_set_signal_state")
            finally:
                audit.update(
                    old=progress.get("old"),
                    new=progress.get("new"),
                    written=list(progress.get("written", [])),
                )
        new = data["new"]
        warnings: list[str] = []
        if new_state is not None and new.get(CONTR_BY_COM) is not True:
            warnings.append(
                f"ContrByCOM read back as {new.get(CONTR_BY_COM)!r} after writing SigState; the signal "
                "controller may override the state"
            )
        if release and new.get(CONTR_BY_COM) is not False:
            warnings.append(f"ContrByCOM read back as {new.get(CONTR_BY_COM)!r} after releasing the group")
        result: dict[str, Any] = {
            "sc_no": sc,
            "sg_no": sg,
            "path": data["path"],
            SIG_STATE: new.get(SIG_STATE),
            CONTR_BY_COM: new.get(CONTR_BY_COM),
            "old": data["old"],
        }
        if data["errors"]:
            result["errors"] = data["errors"]
        result["warnings"] = [*warnings, *audit.warnings]
        return result

    register_tool(
        mcp,
        vissim_set_signal_state,
        name="vissim_set_signal_state",
        title="Set a signal group state",
        settings=settings,
        read_only=False,
        destructive=False,
        idempotent=True,
    )
