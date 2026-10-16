r"""Vissim process tracking: PID detection, identity-checked kill, window titles, Job Object (PLAN 6.4).

Vissim is started by DCOM, so it is *not* a child of the server and Claude Code never kills it. This module
gives the server the means to find and end exactly its own instance - and nothing else.

Safety rules (PLAN 6.4, contract section 6.5):

* A process is "ours" only when it appeared after ``Dispatch`` started (PID diff against a snapshot taken
  before), its name fully matches ``VISSIM_EXE_PATTERN`` (case-insensitive) and its create time equals the
  recorded one (protection against PID reuse). A new process whose readable command line has no COM switch
  was started by someone else and is never adopted; one whose command line cannot be read is adopted with a
  warning (:func:`choose_new_pid`). :func:`kill` / :func:`terminate` re-check name and create time right
  before acting; without a known PID and create time nothing is ever killed.
* Connect clean-up (license dialog, timeout, failed ``Dispatch``) additionally requires a COM launch command
  line (``-Embedding`` / ``/Automation``), see :func:`cmdline_is_com_launched`.
* Off Windows every function is a safe no-op: empty results, nothing killed, no job object.
* Test mode (``VISSIM_MCP_FORBID_REAL`` set): a process named like real Vissim (``^vissim\d*\.exe$``) is
  never killed, terminated or put into a job object, whatever the configured pattern.

All OS access goes through small module-level functions (``_is_windows``, ``_process_iter``,
``_get_process``, ``_window_titles_native`` and the ``_k32_*`` wrappers) so that tests can monkeypatch them.
The pure decision helpers (:func:`new_processes`, :func:`choose_new_pid`, :func:`cmdline_is_com_launched`,
:func:`looks_like_license_dialog`) are shared with the test doubles.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psutil

from .config import ENV_FORBID_REAL, forbid_real_enabled

log = logging.getLogger(__name__)

PID_NOT_DETECTED_WARNING = "PID not detected; forced kill and job object disabled"
#: A new Vissim whose readable command line has no COM switch was started by someone else (M0: a COM-launched
# Vissim always has ``-Embedding``), so it is never adopted - and therefore never killed or put into the job.
NOT_ADOPTED_WARNING = "a new Vissim process without -Embedding appeared during connect; it was not adopted"
#: Two create times closer than this belong to the same process (psutil and GetProcessTimes both use
# FILETIME).
CREATE_TIME_TOLERANCE_S = 0.05
#: Case-insensitive title fragments of a license dialog (English/German/Russian/Japanese/Chinese UIs).
LICENSE_KEYWORDS = ("licen", "lizenz", "лиценз", "ライセンス", "许可", "許可")
#: Command line switches COM uses to start a local server (``-Embedding``; ``/Automation`` for old servers).
COM_LAUNCH_SWITCHES = frozenset({"embedding", "automation"})
#: Names of real Vissim executables - protected from kill/terminate/job while VISSIM_MCP_FORBID_REAL is set.
REAL_VISSIM_EXE_RE = re.compile(r"^vissim\d*\.exe$", re.IGNORECASE)

# Win32 constants
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9  # JobObjectExtendedLimitInformation
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
#: Exactly KILL_ON_JOB_CLOSE (PLAN 6.4): every process Vissim starts stays in the job and ends with it; a
# child started with CREATE_BREAKAWAY_FROM_JOB is refused by Windows (M6 checks that real Vissim never needs
# that, docs/com-names.md).
JOB_LIMIT_FLAGS = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
_EPOCH_AS_FILETIME = 116_444_736_000_000_000  # 1970-01-01 in 100 ns units since 1601-01-01
_TITLE_BUFFER_CHARS = 512


# --- data


@dataclass(frozen=True)
class ProcInfo:
    """A running process that matched ``VISSIM_EXE_PATTERN``."""

    pid: int
    name: str
    create_time: float | None
    cmdline: tuple[str, ...] | None = None

    @property
    def com_launched(self) -> bool:
        """True when the command line shows a COM launch (``-Embedding`` / ``/Automation``)."""
        return cmdline_is_com_launched(self.cmdline)


# --- pure helpers (no OS access)


def cmdline_is_com_launched(cmdline: Sequence[str] | None) -> bool:
    """True when an argument after the executable is ``-Embedding``/``/Embedding``/``-Automation``/...

    Only whole switches count - a folder named ``automation`` in the executable path does not.
    """
    if not cmdline:
        return False
    args = [str(part) for part in cmdline]
    if len(args) == 1:  # an unparsed command line string
        args = args[0].split()
    for arg in args[1:]:
        token = arg.strip().strip('"').lower()
        if token[:1] in ("-", "/") and token[1:] in COM_LAUNCH_SWITCHES:
            return True
    return False


def looks_like_license_dialog(titles: Iterable[str] | None) -> bool:
    """True when any window title contains a license keyword (e.g. ``Управление лицензиями - PTV Vissim``)."""
    for title in titles or ():
        folded = str(title).casefold()
        if any(keyword in folded for keyword in LICENSE_KEYWORDS):
            return True
    return False


def same_create_time(first: float | None, second: float | None) -> bool:
    """Both known and equal within :data:`CREATE_TIME_TOLERANCE_S`."""
    if first is None or second is None:
        return False
    return abs(float(first) - float(second)) <= CREATE_TIME_TOLERANCE_S


def new_processes(
    current: Mapping[int, ProcInfo], before: Mapping[int, ProcInfo] | Collection[int]
) -> dict[int, ProcInfo]:
    """The entries of ``current`` that did not exist in ``before``.

    ``before`` is a PID collection (PLAN 6.4) or, better, the earlier :func:`vissim_processes` result: then a
    PID that was reused by a new process (different create time) also counts as new.
    """
    found: dict[int, ProcInfo] = {}
    for pid, info in current.items():
        if pid not in before:
            found[pid] = info
            continue
        if isinstance(before, Mapping):
            earlier = before[pid]
            earlier_time = getattr(earlier, "create_time", None)
            if (
                earlier_time is not None
                and info.create_time is not None
                and not same_create_time(earlier_time, info.create_time)
            ):
                found[pid] = info
    return found


def ownership_unverified_warning(pid: int) -> str:
    return (
        f"the command line of the new Vissim process (PID {pid}) could not be read, so its ownership could "
        "not be verified; it is treated as the Vissim this server started"
    )


def choose_new_pid(new: Mapping[int, ProcInfo]) -> tuple[int | None, float | None, list[str]]:
    """Which new process is ours (PLAN 6.4, with ``-Embedding`` as the proof of ownership).

    * none -> ``(None, None, [PID_NOT_DETECTED_WARNING])``;
    * one with a readable COM launch command line -> it; one whose command line could not be read (access
      denied) -> it, per PLAN 6.4, with a warning that its ownership is unverified; one whose readable command
      line has no COM switch -> not adopted (someone else started it): ``pid=None`` + warnings;
    * several -> the newest COM-launched one (else the newest unreadable one) with a warning; never one whose
      readable command line has no COM switch.
    """
    if not new:
        return None, None, [PID_NOT_DETECTED_WARNING]
    com = [info for info in new.values() if info.com_launched]
    unreadable = [info for info in new.values() if info.cmdline is None]
    pool = com or unreadable
    pids = ", ".join(str(pid) for pid in sorted(new))
    if not pool:
        if len(new) == 1:
            first = f"{NOT_ADOPTED_WARNING} (PID {pids})"
        else:
            first = (
                f"several new Vissim processes without -Embedding appeared during connect (PIDs {pids}); "
                "none was adopted"
            )
        return None, None, [first, PID_NOT_DETECTED_WARNING]
    chosen = max(
        pool, key=lambda info: (info.create_time if info.create_time is not None else float("-inf"), info.pid)
    )
    warnings: list[str] = []
    if len(new) > 1:
        if len(com) == 1:
            reason = f"using the COM-launched one, PID {chosen.pid}"
        elif com:
            reason = f"assuming the newest one, PID {chosen.pid}"
        else:
            reason = f"assuming the newest one whose command line could not be read, PID {chosen.pid}"
        warnings.append(f"several new Vissim processes appeared (PIDs {pids}); {reason}")
    if not chosen.com_launched:
        warnings.append(ownership_unverified_warning(chosen.pid))
    return chosen.pid, chosen.create_time, warnings


# --- OS seams (monkeypatched by tests)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _process_iter() -> Iterable[Any]:
    """psutil processes with ``info = {"pid", "name"}`` (name-only iteration is fast)."""
    return psutil.process_iter(["pid", "name"])


def _get_process(pid: int) -> Any:
    return psutil.Process(pid)


# --- process listing and detection


def vissim_processes(pattern: re.Pattern[str]) -> dict[int, ProcInfo]:
    """Running processes whose name fully matches ``pattern``, by PID (``{}`` off Windows).

    Create time and command line are read only for matching processes (``None`` when access is denied).
    Errors of the listing itself propagate: a failed "before" snapshot must disable PID detection rather than
    make every running Vissim look new.
    """
    if not _is_windows():
        return {}
    found: dict[int, ProcInfo] = {}
    for proc in _process_iter():
        try:
            info = proc.info
            name = str(info.get("name") or "")
            pid = int(info.get("pid"))
        except Exception:
            continue
        if not name or not pattern.fullmatch(name):
            continue
        found[pid] = ProcInfo(pid, name, _safe_create_time(proc), _safe_cmdline(proc))
    return found


def detect_new_pid(
    before: Mapping[int, ProcInfo] | Collection[int],
    pattern: re.Pattern[str],
    timeout_s: float,
    poll_s: float = 0.25,
) -> tuple[int | None, float | None, list[str]]:
    """Poll for Vissim processes that are not in ``before`` (every ``poll_s``, at most ``timeout_s``).

    Returns ``(pid, create_time, warnings)`` per :func:`choose_new_pid`. Blocking - call it from a thread.
    """
    if not _is_windows():
        return None, None, [PID_NOT_DETECTED_WARNING]
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            new = new_processes(vissim_processes(pattern), before)
        except Exception:
            log.warning("listing processes failed during PID detection", exc_info=True)
            new = {}
        if new:
            return choose_new_pid(new)
        if time.monotonic() >= deadline:
            return None, None, [PID_NOT_DETECTED_WARNING]
        time.sleep(max(0.01, float(poll_s)))


def is_com_launched(pid: int) -> bool:
    """True when the process ``pid`` was started by COM (see :func:`cmdline_is_com_launched`)."""
    if not _is_windows():
        return False
    try:
        return cmdline_is_com_launched(_get_process(pid).cmdline())
    except (psutil.Error, OSError, ValueError):
        return False


def window_titles(pid: int | None) -> list[str]:
    """Titles of the visible top-level windows of ``pid`` (``[]`` off Windows or on any error)."""
    if not _is_windows() or pid is None:
        return []
    try:
        return _window_titles_native(int(pid))
    except Exception:
        log.debug("reading window titles of PID %s failed", pid, exc_info=True)
        return []


# --- identity check, kill, terminate


def pid_matches(pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
    """True when ``pid`` still is the same Vissim process: running, name matches, same create time."""
    return _verified_process(pid, create_time, pattern) is not None


def kill(pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
    """Kill ``pid`` only after :func:`pid_matches`; True when the kill was sent. Never raises."""
    return _end_process(pid, create_time, pattern, "kill")


def terminate(pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> bool:
    """Terminate ``pid`` only after :func:`pid_matches`; True when it was sent. Never raises.

    (On Windows psutil's terminate and kill both call ``TerminateProcess``; PLAN 6.4 still escalates
    terminate -> kill for the case that the first attempt does not end the process.)
    """
    return _end_process(pid, create_time, pattern, "terminate")


def _verified_process(pid: int | None, create_time: float | None, pattern: re.Pattern[str]) -> Any | None:
    if not _is_windows() or pid is None or create_time is None:
        return None
    try:
        proc = _get_process(int(pid))
        name = str(proc.name() or "")
        actual = proc.create_time()
    except (psutil.Error, OSError, ValueError, TypeError):
        return None
    if not name or not pattern.fullmatch(name) or not same_create_time(actual, create_time):
        return None
    return proc


def _end_process(pid: int | None, create_time: float | None, pattern: re.Pattern[str], how: str) -> bool:
    proc = _verified_process(pid, create_time, pattern)
    if proc is None:
        if pid is not None:
            log.info(
                "not sending %s to PID %s: it is not the verified Vissim process (gone or reused)", how, pid
            )
        return False
    try:
        name = str(proc.name() or "")
    except (psutil.Error, OSError):
        return False
    if _forbidden_target(name):
        log.error("refusing to %s %s (PID %s): %s is set (test mode)", how, name, pid, ENV_FORBID_REAL)
        return False
    try:
        getattr(proc, how)()
    except psutil.NoSuchProcess:
        return False
    except (psutil.Error, OSError) as exc:
        log.warning("%s of Vissim PID %s failed: %s", how, pid, exc)
        return False
    log.info("sent %s to Vissim PID %s", how, pid)
    return True


def _forbidden_target(name: str) -> bool:
    return forbid_real_enabled() and bool(REAL_VISSIM_EXE_RE.fullmatch(name))


def _safe_create_time(proc: Any) -> float | None:
    try:
        return float(proc.create_time())
    except (psutil.Error, OSError, ValueError, TypeError):
        return None


def _safe_cmdline(proc: Any) -> tuple[str, ...] | None:
    try:
        cmdline = proc.cmdline()
    except (psutil.Error, OSError, ValueError):
        return None
    return tuple(str(part) for part in cmdline) if cmdline else None


# --- Job Object (kill on close)


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    """winnt.h layout; with natural alignment it is 64 bytes on x64 (48 on x86)."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
        ("PerJobUserTimeLimit", ctypes.c_int64),  # LARGE_INTEGER
        ("LimitFlags", ctypes.c_uint32),  # DWORD
        ("MinimumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("MaximumWorkingSetSize", ctypes.c_size_t),  # SIZE_T
        ("ActiveProcessLimit", ctypes.c_uint32),  # DWORD
        ("Affinity", ctypes.c_size_t),  # ULONG_PTR
        ("PriorityClass", ctypes.c_uint32),  # DWORD
        ("SchedulingClass", ctypes.c_uint32),  # DWORD
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    """winnt.h layout; 144 bytes on x64 (112 on x86)."""

    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class JobObject:
    """A Windows Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (PLAN 6.4).

    The handle is kept open for the life of the server and is never closed manually: however the server
    process ends (normal exit, crash, Claude Code killing the process tree), Windows closes the handle and
    terminates every process in the job. One job is reused for every Vissim the server starts.
    """

    def __init__(self, handle: int) -> None:
        self._handle = int(handle)

    @property
    def handle(self) -> int:
        return self._handle

    @classmethod
    def create(cls) -> JobObject | None:
        """A new kill-on-close job, or None off Windows or when the OS refuses (logged)."""
        if not _is_windows():
            return None
        try:
            return cls(_k32_create_kill_on_close_job())
        except OSError as exc:
            log.warning("creating the kill-on-close job object failed: %s", exc)
            return None

    def assign(self, pid: int, create_time: float | None = None) -> None:
        """Put process ``pid`` into the job; raises ``OSError`` (with ``winerror``) on failure.

        With ``create_time`` the process behind the opened handle is verified first, so a reused PID is never
        assigned. Refuses real Vissim executables while ``VISSIM_MCP_FORBID_REAL`` is set.
        """
        if not _is_windows():
            raise OSError(errno.ENOSYS, "job objects are only available on Windows")
        process = _k32_open_process(
            int(pid), PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
        )
        try:
            name = os.path.basename(_k32_process_image_name(process))
            if _forbidden_target(name):
                raise PermissionError(
                    errno.EPERM,
                    f"refusing to put {name} (PID {pid}) into a kill-on-close job: "
                    f"{ENV_FORBID_REAL} is set (test mode)",
                )
            if create_time is not None and not same_create_time(
                _k32_process_create_time(process), create_time
            ):
                raise ProcessLookupError(
                    errno.ESRCH, f"PID {pid} belongs to another process now (PID reused)"
                )
            _k32_assign_process(self._handle, process)
        finally:
            _k32_close_handle(process)

    def contains(self, pid: int) -> bool:
        """True when process ``pid`` is in this job (False off Windows or on error)."""
        if not _is_windows():
            return False
        try:
            process = _k32_open_process(int(pid), PROCESS_QUERY_LIMITED_INFORMATION)
        except OSError:
            return False
        try:
            return _k32_is_process_in_job(process, self._handle)
        except OSError:
            return False
        finally:
            _k32_close_handle(process)

    def __repr__(self) -> str:
        return f"<JobObject handle={self._handle:#x}>"


# --- ctypes bindings (Windows only, created lazily; a private WinDLL so global prototypes stay untouched)

_dll_lock = threading.Lock()
_dlls: dict[str, Any] = {}


def _kernel32() -> Any:
    with _dll_lock:
        if "kernel32" not in _dlls:
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
            k32.IsProcessInJob.restype = wintypes.BOOL
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetProcessTimes.argtypes = (wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4))
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.QueryFullProcessImageNameW.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            )
            k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            k32.CloseHandle.restype = wintypes.BOOL
            _dlls["kernel32"] = k32
        return _dlls["kernel32"]


def _user32() -> Any:
    with _dll_lock:
        if "user32" not in _dlls:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
            user32.EnumWindows.argtypes = (_WNDENUMPROC, wintypes.LPARAM)
            user32.EnumWindows.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.IsWindowVisible.argtypes = (wintypes.HWND,)
            user32.IsWindowVisible.restype = wintypes.BOOL
            user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
            user32.GetWindowTextW.restype = ctypes.c_int
            _dlls["user32"] = user32
        return _dlls["user32"]


if sys.platform == "win32":
    from ctypes import wintypes as _wintypes

    _WNDENUMPROC: Any = ctypes.WINFUNCTYPE(_wintypes.BOOL, _wintypes.HWND, _wintypes.LPARAM)
else:  # never called off Windows
    _WNDENUMPROC = None


def _win_error(function: str) -> OSError:
    code = ctypes.get_last_error()  # type: ignore[attr-defined]
    error = ctypes.WinError(code)  # type: ignore[attr-defined]
    error.strerror = f"{function} failed: {error.strerror}"
    return error


def _k32_create_kill_on_close_job() -> int:
    k32 = _kernel32()
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        raise _win_error("CreateJobObjectW")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_LIMIT_FLAGS
    if not k32.SetInformationJobObject(
        handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        error = _win_error("SetInformationJobObject")
        k32.CloseHandle(handle)
        raise error
    return int(handle)


def _k32_open_process(pid: int, access: int) -> int:
    handle = _kernel32().OpenProcess(access, False, pid)
    if not handle:
        raise _win_error(f"OpenProcess(PID {pid})")
    return int(handle)


def _k32_process_create_time(process: int) -> float:
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not _kernel32().GetProcessTimes(
        process, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise _win_error("GetProcessTimes")
    ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
    return (ticks - _EPOCH_AS_FILETIME) / 10_000_000


def _k32_process_image_name(process: int) -> str:
    from ctypes import wintypes

    size = wintypes.DWORD(32_768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not _kernel32().QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
        raise _win_error("QueryFullProcessImageNameW")
    return buffer.value


def _k32_assign_process(job: int, process: int) -> None:
    if not _kernel32().AssignProcessToJobObject(job, process):
        raise _win_error("AssignProcessToJobObject")


def _k32_is_process_in_job(process: int, job: int) -> bool:
    from ctypes import wintypes

    result = wintypes.BOOL(False)
    if not _kernel32().IsProcessInJob(process, job, ctypes.byref(result)):
        raise _win_error("IsProcessInJob")
    return bool(result.value)


def _k32_close_handle(handle: int) -> None:
    try:
        _kernel32().CloseHandle(handle)
    except Exception:
        pass


def _window_titles_native(pid: int) -> list[str]:
    """``EnumWindows`` over top-level windows; titles of the visible windows owned by ``pid``.

    ``GetWindowTextW`` on a window of *another* process reads the stored caption without sending a message,
    so a hung Vissim cannot block this call. Windows of this very process are never queried (that would send
    ``WM_GETTEXT`` to our own, possibly busy, threads).
    """
    from ctypes import wintypes

    if pid == os.getpid():
        return []
    user32 = _user32()
    titles: list[str] = []

    def callback(hwnd: Any, _lparam: Any) -> bool:
        try:
            owner = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                buffer = ctypes.create_unicode_buffer(_TITLE_BUFFER_CHARS)
                if user32.GetWindowTextW(hwnd, buffer, _TITLE_BUFFER_CHARS) > 0 and buffer.value:
                    titles.append(buffer.value)
        except Exception:
            pass
        return True

    user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return titles
