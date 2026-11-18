"""Signal tool (PLAN 7.5): vissim_set_signal_state.

Sets the state of one signal group (``Net.SignalControllers[sc_no].SGs[sg_no]``) through COM, or hands it back
to its signal controller. Writing ``SigState`` makes Vissim switch the group to COM control (``ContrByCOM``)
by itself; ``release_to_controller`` writes ``ContrByCOM = False``. Refused with ``JOB_RUNNING`` while a
simulation job is ``running`` - allowed when it is ``paused`` (at a ``pause_at`` point) or when there is no
active job (real Vissim then refuses the write itself unless a run is in progress: outside a run ``SigState``
reads as empty and cannot be changed). The check is repeated on the COM worker, right before the write.
Every call writes one audit record with the old and the new values (PLAN 6.8).
"""

from __future__ import annotations

import base64
import functools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any
from xml.sax.saxutils import quoteattr

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
#: Real Vissim (2026): outside a simulation run SigState reads as empty and neither SigState nor ContrByCOM
#: can be written ("the attribute cannot be changed").
NO_SIMULATION_HINT = (
    "Vissim accepts signal states only while a simulation run is in progress: start the simulation with "
    "vissim_start_simulation(pause_at=[...]), wait until the job is paused, set the state, then "
    "vissim_resume_simulation."
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


def _read_flag(obj: Any, name: str, errors: dict[str, str]) -> Any:
    """A boolean attribute as ``bool``: Vissim returns booleans through COM as the integers 1 and 0."""
    value = _read(obj, name, errors)
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return value


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
        CONTR_BY_COM: _read_flag(group, CONTR_BY_COM, errors),
    }
    # No simulation run in progress: SigState reads as empty and Vissim refuses both writes.
    idle = progress["old"][SIG_STATE] is None and SIG_STATE not in errors
    written: list[str] = []
    progress["written"] = written
    if state is not None:
        _write(group, SIG_STATE, state, label, NO_SIMULATION_HINT if idle else STATE_HINT)
        written.append(SIG_STATE)
    if release:
        _write(group, CONTR_BY_COM, False, label, NO_SIMULATION_HINT if idle else None)
        written.append(CONTR_BY_COM)
    after: dict[str, str] = {}
    progress["new"] = {
        SIG_STATE: _read(group, SIG_STATE, after),
        CONTR_BY_COM: _read_flag(group, CONTR_BY_COM, after),
    }
    return {"path": label, "old": progress["old"], "new": progress["new"], "errors": {**errors, **after}}

# --- fixed-time signal program (EXTENSION after M6): vissim_set_signal_program

#: Vissim keeps the program of a fixed-time controller as Vissig XML, base64-encoded, in this attribute (the
#: "internal supply data" - no .sig file needed; verified with real Vissim 2026).
SUPPLY_ATTR = "IntSupplyData"
PROGRAM_ATTR = "ProgNo"
PROGRAM_NO = 1
MAX_GROUPS = 200
MIN_CYCLE_S = 2.0
MAX_CYCLE_S = 3600.0
MAX_TRANSITION_S = 30.0
PROGRAM_HINT = (
    'E.g. vissim_set_signal_program(sc_no=1, cycle_s=60, groups=[{"sg_no": 1, "green_start_s": 0, '
    '"green_end_s": 25}, {"sg_no": 2, "green_start_s": 30, "green_end_s": 55}]). Times are seconds within '
    "the cycle; a green may wrap around the end of the cycle."
)

GroupsArg = Annotated[Any, WithJsonSchema({"type": "array", "items": {"type": "object"}})]
NumberArg = Annotated[Any, WithJsonSchema({"type": "number"})]

_DISPLAYS = """  <signaldisplays>
    <display id="1" name="Red" state="RED">
      <patterns>
        <pattern pattern="MINUS" color="#FF0000" isBold="true" />
      </patterns>
    </display>
    <display id="2" name="Red/Amber" state="REDAMBER">
      <patterns>
        <pattern pattern="FRAME" color="#CCCC00" isBold="true" />
        <pattern pattern="SLASH" color="#CC0000" isBold="false" />
        <pattern pattern="MINUS" color="#CC0000" isBold="false" />
      </patterns>
    </display>
    <display id="3" name="Green" state="GREEN">
      <patterns>
        <pattern pattern="FRAME" color="#00CC00" isBold="true" />
        <pattern pattern="SOLID" color="#00CC00" isBold="false" />
      </patterns>
    </display>
    <display id="4" name="Amber" state="AMBER">
      <patterns>
        <pattern pattern="FRAME" color="#CCCC00" isBold="true" />
        <pattern pattern="SLASH" color="#CCCC00" isBold="false" />
      </patterns>
    </display>
  </signaldisplays>
"""


@dataclass(frozen=True)
class GroupTimes:
    """Green of one signal group within the cycle, in milliseconds."""

    no: int
    green_start_ms: int
    green_end_ms: int


def _milliseconds(value: Any, field: str, low: float, high: float) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid(f"{field} must be a number of seconds, got {value!r}", PROGRAM_HINT)
    if not low <= value <= high:
        raise _invalid(f"{field} must be between {low:g} and {high:g} s, got {value:g}", PROGRAM_HINT)
    return int(round(float(value) * 1000))


def program_groups(groups: Any, cycle_ms: int, amber_ms: int, red_amber_ms: int) -> list[GroupTimes]:
    """``groups`` of vissim_set_signal_program, validated against the cycle."""
    if not isinstance(groups, (list, tuple)) or not groups:
        raise _invalid(
            "groups must be a non-empty array of {sg_no, green_start_s, green_end_s}", PROGRAM_HINT
        )
    if len(groups) > MAX_GROUPS:
        raise _invalid(f"at most {MAX_GROUPS} signal groups, got {len(groups)}")
    cycle_s = cycle_ms / 1000
    parsed: list[GroupTimes] = []
    seen: set[int] = set()
    for index, item in enumerate(groups):
        field = f"groups[{index}]"
        if not isinstance(item, dict) or set(item) != {"sg_no", "green_start_s", "green_end_s"}:
            raise _invalid(
                f"{field} must be an object with sg_no, green_start_s and green_end_s", PROGRAM_HINT
            )
        number = object_number(item["sg_no"], f"{field}.sg_no")
        if number in seen:
            raise _invalid(f"{field}: signal group {number} is listed twice")
        seen.add(number)
        start = _milliseconds(item["green_start_s"], f"{field}.green_start_s", 0, cycle_s)
        end = _milliseconds(item["green_end_s"], f"{field}.green_end_s", 0, cycle_s)
        start, end = start % cycle_ms, end % cycle_ms
        green = (end - start) % cycle_ms
        if green == 0:
            raise _invalid(f"{field}: green_start_s and green_end_s are the same moment of the cycle")
        if green + amber_ms + red_amber_ms >= cycle_ms:
            raise _invalid(
                f"{field}: green ({green / 1000:g} s) plus amber and red/amber leaves no red in a cycle of "
                f"{cycle_s:g} s"
            )
        parsed.append(GroupTimes(number, start, end))
    return sorted(parsed, key=lambda group: group.no)


def fixed_time_program_xml(
    sc_no: int,
    sc_name: str,
    cycle_ms: int,
    groups: Sequence[GroupTimes],
    names: Mapping[int, str],
    amber_ms: int,
    red_amber_ms: int,
) -> str:
    """Vissig XML of one fixed-time program (id 1): red -> red/amber -> green -> amber -> red per group.

    Layout taken from a network saved by Vissim 2026 and verified by running it (M6): in ``<cmds>`` the green
    command is the moment green begins (red/amber is shown for ``red_amber_ms`` before it) and the red
    command the moment red begins - amber is shown for ``amber_ms`` before that, i.e. from ``green_end``.
    """
    group_list = "".join(
        f'    <sg id="{group.no}" name={quoteattr(names.get(group.no, ""))} defaultSignalSequence="3" '
        'underEPICSControl="true">\n      <defaultDurations />\n      <EPICSTrafficDemands />\n    </sg>\n'
        for group in groups
    )
    program = "".join(
        f'        <sg sg_id="{group.no}" signal_sequence="3">\n'
        "          <cmds>\n"
        f'            <cmd display="3" begin="{group.green_start_ms}" />\n'
        f'            <cmd display="1" begin="{(group.green_end_ms + amber_ms) % cycle_ms}" />\n'
        "          </cmds>\n"
        "          <fixedstates>\n"
        f'            <fixedstate display="2" duration="{red_amber_ms}" />\n'
        f'            <fixedstate display="4" duration="{amber_ms}" />\n'
        "          </fixedstates>\n"
        "        </sg>\n"
        for group in groups
    )
    sequence = (
        '    <signalsequence id="3" name="Red-Red/Amber-Green-Amber">\n'
        '      <state display="1" isFixedDuration="false" isClosed="true" defaultDuration="1000" />\n'
        '      <state display="2" isFixedDuration="true" isClosed="true" '
        f'defaultDuration="{red_amber_ms}" />\n'
        '      <state display="3" isFixedDuration="false" isClosed="false" defaultDuration="5000" />\n'
        f'      <state display="4" isFixedDuration="true" isClosed="true" defaultDuration="{amber_ms}" />\n'
        "    </signalsequence>\n"
    )
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<sc id="{sc_no}" name={quoteattr(sc_name)} frequency="1" steps="0" defaultIntergreenMatrix="0">\n'
        f"{_DISPLAYS}"
        f"  <signalsequences>\n{sequence}  </signalsequences>\n"
        f"  <sgs>\n{group_list}  </sgs>\n"
        "  <dets />\n  <messagePointPairs />\n  <intergreenmatrices />\n"
        "  <progs>\n"
        f'    <prog id="{PROGRAM_NO}" cycletime="{cycle_ms}" switchpoint="0" offset="0" intergreens="0" '
        f'fitness="0.000000" vehicleCount="0" name="Program {PROGRAM_NO} ({cycle_ms / 1000:g} s)">\n'
        f"      <sgs>\n{program}      </sgs>\n"
        "    </prog>\n"
        "  </progs>\n"
        "  <stages />\n  <interstageProgs />\n  <stageProgs />\n  <dailyProgLists />\n"
        "</sc>\n"
    )
    return text.replace("\n", "\r\n")


def set_program_task(
    sc_no: int,
    cycle_ms: int,
    groups: list[GroupTimes],
    amber_ms: int,
    red_amber_ms: int,
    dry_run: bool,
    service: VissimService,
    progress: dict[str, Any],
    ctx: WorkerContext,
) -> dict[str, Any]:
    """Create missing signal groups, build the program and write it to the controller."""
    job = service.active_job()
    if job is not None:
        raise job_running_error(job)
    segments = [Segment("Net"), Segment("SignalControllers", sc_no)]
    label = format_path(segments)
    controller = resolve_path(ctx.vissim, segments)
    _require_object(controller, label)
    collection = controller.SGs
    rows = collection.GetMultipleAttributes(("No", "Name"))
    names = {int(row[0]): str(row[1] or "") for row in rows or ()}
    wanted = [group.no for group in groups]
    extra = sorted(set(names) - set(wanted))
    if extra:
        raise _invalid(
            f"signal group(s) {extra} of {label} are not in groups: Vissim refuses a program that does not "
            "cover every signal group of the controller",
            hint="Add green times for them, or remove them with vissim_call_method (RemoveSignalGroup).",
        )
    missing = [number for number in wanted if number not in names]
    errors: dict[str, str] = {}
    old_supply = _read(controller, SUPPLY_ATTR, errors)
    progress["old"] = {
        PROGRAM_ATTR: _read(controller, PROGRAM_ATTR, errors),
        "program_chars": len(old_supply) if isinstance(old_supply, str) else None,
    }
    controller_name = _read(controller, "Name", errors)
    xml = fixed_time_program_xml(
        sc_no,
        controller_name if isinstance(controller_name, str) else "",
        cycle_ms,
        groups,
        names,
        amber_ms,
        red_amber_ms,
    )
    encoded = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    result: dict[str, Any] = {
        "path": label,
        "created_groups": missing,
        "controller_type": _read(controller, "Type", errors),
        "program_chars": len(encoded),
    }
    if dry_run:
        return result
    for number in missing:
        try:
            collection.AddSignalGroup(number)
        except Exception as exc:
            if is_connection_error(exc) or not is_com_error(exc):
                raise
            raise from_com_error(exc, context=f"{label}.SGs.AddSignalGroup({number})") from None
        progress.setdefault("created_groups", []).append(number)
    _write(controller, SUPPLY_ATTR, encoded, label, None)
    progress.setdefault("written", []).append(SUPPLY_ATTR)
    _write(controller, PROGRAM_ATTR, PROGRAM_NO, label, None)
    progress["written"].append(PROGRAM_ATTR)
    after: dict[str, str] = {}
    written_back = _read(controller, SUPPLY_ATTR, after)
    result["stored"] = isinstance(written_back, str) and written_back == encoded
    result[PROGRAM_ATTR] = _read(controller, PROGRAM_ATTR, after)
    return result


# --- tool


def register(mcp: FastMCP, service: VissimService) -> None:
    settings = service.settings

    async def vissim_set_signal_state(
        sc_no: IntArg, sg_no: IntArg, state: StateArg = None, release_to_controller: FlagArg = False
    ) -> dict[str, Any]:
        """Set the state of one signal group through COM, or hand the group back to its signal controller.

        Use it while a simulation job is paused: start the simulation with pause_at, wait until state is
        paused, set the signals, then vissim_resume_simulation. Refused with JOB_RUNNING while a job is
        running; without a simulation run in progress Vissim itself refuses the change (COM_ERROR).
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
        group), NOT_CONNECTED, COM_ERROR (Vissim refused the state, or no simulation run is in progress),
        TIMEOUT_HUNG.

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

    async def vissim_set_signal_program(
        sc_no: IntArg,
        cycle_s: NumberArg,
        groups: GroupsArg,
        amber_s: NumberArg = 3,
        red_amber_s: NumberArg = 1,
        dry_run: FlagArg = False,
    ) -> dict[str, Any]:
        """Give a fixed-time signal controller its signal program: the cycle time and the green of every
        signal group. A new signal controller needs this before a simulation can run (without a program
        Vissim refuses the run: "signal groups ... differ from those in the external data files").

        The program replaces the controller's current one and is stored inside the network (no .sig file).
        Each group is red, shows red/amber for red_amber_s before its green, is green from green_start_s to
        green_end_s and then amber for amber_s. Signal groups that do not exist yet are created; every
        existing signal group of the controller must be listed. Assign signal heads to groups with
        vissim_set_attributes (attribute SG = "<controller no>-<group no>"). Conflicting greens are not
        checked - choose the times so that conflicting movements are never green together, with intergreen
        time between them. Not allowed while a simulation job is running or paused. Written to the audit log.

        Parameters: sc_no - signal controller number; cycle_s - cycle time in seconds; groups -
        [{sg_no, green_start_s, green_end_s}], seconds within the cycle (a green may wrap around the end of
        the cycle); amber_s (default 3) and red_amber_s (default 1) - transition times, 0 = none; dry_run -
        true only validates and reports what would be created.
        Returns: sc_no, path, cycle_s, groups, amber_s, red_amber_s, dry_run, created_groups, ProgNo,
        stored (the program was read back unchanged), program_chars, warnings.
        Errors: INVALID_ARGUMENT (times outside the cycle, a group missing or listed twice), JOB_RUNNING,
        RESOLVE_FAILED (no such controller), COM_ERROR, NOT_CONNECTED, TIMEOUT_HUNG.

        Example: vissim_set_signal_program(sc_no=1, cycle_s=60, groups=[{"sg_no": 1, "green_start_s": 0,
        "green_end_s": 25}, {"sg_no": 2, "green_start_s": 30, "green_end_s": 55}])
        """
        args = {
            "sc_no": sc_no,
            "cycle_s": cycle_s,
            "groups": groups,
            "amber_s": amber_s,
            "red_amber_s": red_amber_s,
            "dry_run": dry_run,
        }
        progress: dict[str, Any] = {}
        async with AuditRecord(service, "vissim_set_signal_program", args) as audit:
            sc = object_number(sc_no, "sc_no")
            cycle_ms = _milliseconds(cycle_s, "cycle_s", MIN_CYCLE_S, MAX_CYCLE_S)
            amber_ms = _milliseconds(amber_s, "amber_s", 0, MAX_TRANSITION_S)
            red_amber_ms = _milliseconds(red_amber_s, "red_amber_s", 0, MAX_TRANSITION_S)
            times = program_groups(groups, cycle_ms, amber_ms, red_amber_ms)
            dry = flag(dry_run, "dry_run")
            audit.update(path=f"Net.SignalControllers[{sc}]", dry_run=dry)
            service.ensure_connected()
            job = service.active_job()
            if job is not None:
                raise job_running_error(job)
            task = functools.partial(
                set_program_task, sc, cycle_ms, times, amber_ms, red_amber_ms, dry, service, progress
            )
            try:
                data = await service.call(task, task_name="vissim_set_signal_program")
            finally:
                audit.update(
                    old=progress.get("old"),
                    written=list(progress.get("written", [])),
                    created_groups=list(progress.get("created_groups", [])),
                )
        warnings: list[str] = []
        controller_type = data.get("controller_type")
        if isinstance(controller_type, str) and controller_type.replace(" ", "").upper() != "FIXEDTIME":
            warnings.append(
                f"the controller type is {controller_type}; a fixed-time program is only used by a "
                "controller of type FIXEDTIME"
            )
        if not dry and data.get("stored") is False:
            warnings.append("the program read back from Vissim differs from the one written")
        result: dict[str, Any] = {
            "sc_no": sc,
            "path": data["path"],
            "cycle_s": cycle_ms / 1000,
            "groups": [
                {
                    "sg_no": group.no,
                    "green_start_s": group.green_start_ms / 1000,
                    "green_end_s": group.green_end_ms / 1000,
                }
                for group in times
            ],
            "amber_s": amber_ms / 1000,
            "red_amber_s": red_amber_ms / 1000,
            "dry_run": dry,
            "created_groups": data["created_groups"],
            "program_chars": data["program_chars"],
        }
        if not dry:
            result[PROGRAM_ATTR] = data.get(PROGRAM_ATTR)
            result["stored"] = data.get("stored")
        result["warnings"] = [*warnings, *audit.warnings]
        return result

    register_tool(
        mcp,
        vissim_set_signal_program,
        name="vissim_set_signal_program",
        title="Set a fixed-time signal program",
        settings=settings,
        read_only=False,
        destructive=True,  # replaces the controller's signal program
        idempotent=True,
    )
