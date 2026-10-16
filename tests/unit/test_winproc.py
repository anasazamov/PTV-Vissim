"""winproc.py (PLAN 6.4, 10.1 winproc row): PID detection with psutil mocked, identity-checked kill, the
kill-on-close Job Object.

psutil is never used for real here: ``_process_iter`` / ``_get_process`` are replaced by an in-memory table.
The only real OS test (Windows) puts a harmless dummy ``python -c "import time; time.sleep(60)"`` into a job
held by a helper subprocess - never Vissim.
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import psutil
import pytest

from vissim_mcp import winproc
from vissim_mcp.winproc import PID_NOT_DETECTED_WARNING, ProcInfo

PATTERN = re.compile(r"^vissim\d*\.exe$", re.IGNORECASE)
VISSIM_EXE = r"C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe"
REPO = Path(__file__).resolve().parents[2]
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")


class PsProcess:
    """A psutil.Process double: ``info`` for process_iter, name/create_time/cmdline, recorded
    kill/terminate."""

    def __init__(
        self,
        pid: int,
        name: str,
        create_time: float,
        cmdline: Sequence[str] = (),
        *,
        cmdline_error: BaseException | None = None,
        end_error: BaseException | None = None,
    ) -> None:
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self._name = name
        self._create_time = create_time
        self._cmdline = list(cmdline)
        self.cmdline_error = cmdline_error
        self.end_error = end_error
        self.calls: list[str] = []

    def name(self) -> str:
        return self._name

    def create_time(self) -> float:
        return self._create_time

    def cmdline(self) -> list[str]:
        if self.cmdline_error is not None:
            raise self.cmdline_error
        return list(self._cmdline)

    def kill(self) -> None:
        self.calls.append("kill")
        if self.end_error is not None:
            raise self.end_error

    def terminate(self) -> None:
        self.calls.append("terminate")
        if self.end_error is not None:
            raise self.end_error


class PsTable:
    """The mocked process table: ``snapshots`` (consumed one per listing, the last one repeats)."""

    def __init__(self) -> None:
        self.snapshots: list[list[PsProcess]] = [[]]
        self.get_calls: list[int] = []

    def set(self, *processes: PsProcess) -> None:
        self.snapshots = [list(processes)]

    def iterate(self) -> list[PsProcess]:
        current = self.snapshots[0]
        if len(self.snapshots) > 1:
            self.snapshots.pop(0)
        return list(current)

    def get(self, pid: int) -> PsProcess:
        self.get_calls.append(pid)
        for proc in self.snapshots[0]:
            if proc.pid == pid:
                return proc
        raise psutil.NoSuchProcess(pid)


@pytest.fixture
def ps(monkeypatch: pytest.MonkeyPatch) -> PsTable:
    table = PsTable()
    monkeypatch.setattr(winproc, "_is_windows", lambda: True)
    monkeypatch.setattr(winproc, "_process_iter", table.iterate)
    monkeypatch.setattr(winproc, "_get_process", table.get)
    return table


def vissim(pid: int, created: float, *, com: bool = True, name: str = "Vissim260.exe") -> PsProcess:
    return PsProcess(pid, name, created, [VISSIM_EXE, "-Embedding"] if com else [VISSIM_EXE])


# --- pure decision helpers


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        ([VISSIM_EXE, "-Embedding"], True),
        ([VISSIM_EXE, "/Embedding"], True),
        (["vissim.exe", "-automation"], True),
        ([f'"{VISSIM_EXE}" -Embedding'], True),  # one unparsed command line string
        ([r"C:\automation\Vissim.exe"], False),  # a folder name is no switch
        ([VISSIM_EXE, "-EmbeddingX"], False),
        ([VISSIM_EXE, "model.inpx"], False),
        ([], False),
        (None, False),
    ],
)
def test_cmdline_is_com_launched(cmdline: Sequence[str] | None, expected: bool) -> None:
    assert winproc.cmdline_is_com_launched(cmdline) is expected


@pytest.mark.parametrize(
    ("titles", "expected"),
    [
        (["Управление лицензиями - PTV Vissim"], True),
        (["PTV Vissim", "License Management - PTV Vissim"], True),
        (["Lizenzverwaltung"], True),
        (["ライセンス管理"], True),
        (["许可证"], True),
        (["PTV Vissim 2026 - (no network)"], False),
        ([], False),
        (None, False),
    ],
)
def test_looks_like_license_dialog(titles: list[str] | None, expected: bool) -> None:
    assert winproc.looks_like_license_dialog(titles) is expected


def test_same_create_time() -> None:
    assert winproc.same_create_time(100.0, 100.04)
    assert not winproc.same_create_time(100.0, 100.2)
    assert not winproc.same_create_time(None, 100.0) and not winproc.same_create_time(100.0, None)


def test_new_processes_against_a_pid_set_and_a_snapshot() -> None:
    old = ProcInfo(10, "Vissim260.exe", 1.0)
    reused = ProcInfo(10, "Vissim260.exe", 9.0)  # same PID, another process (PID reuse)
    fresh = ProcInfo(11, "Vissim260.exe", 5.0)
    assert winproc.new_processes({10: old, 11: fresh}, {10}) == {11: fresh}
    assert winproc.new_processes({10: old, 11: fresh}, {10: old}) == {11: fresh}
    assert winproc.new_processes({10: reused, 11: fresh}, {10: old}) == {10: reused, 11: fresh}
    unknown_time = ProcInfo(10, "Vissim260.exe", None)
    assert winproc.new_processes({10: unknown_time}, {10: old}) == {}


def test_choose_new_pid_rules() -> None:
    com_old = ProcInfo(20, "Vissim260.exe", 1.0, (VISSIM_EXE, "-Embedding"))
    com_new = ProcInfo(21, "Vissim260.exe", 2.0, (VISSIM_EXE, "-Embedding"))
    user_newest = ProcInfo(22, "Vissim260.exe", 3.0, (VISSIM_EXE,))
    assert winproc.choose_new_pid({}) == (None, None, [PID_NOT_DETECTED_WARNING])
    assert winproc.choose_new_pid({20: com_old}) == (20, 1.0, [])
    pid, created, warnings = winproc.choose_new_pid({20: com_old, 21: com_new})
    assert (pid, created) == (21, 2.0)
    assert warnings == [
        "several new Vissim processes appeared (PIDs 20, 21); assuming the newest one, PID 21"
    ]
    pid, _created, warnings = winproc.choose_new_pid({20: com_old, 22: user_newest})
    assert pid == 20 and warnings[0].endswith("using the COM-launched one, PID 20")


# --- listing and detection (psutil mocked)


def test_vissim_processes_lists_only_matching_names(ps: PsTable) -> None:
    ps.set(
        vissim(100, 1.0),
        PsProcess(101, "VISSIM.EXE", 2.0, ["VISSIM.EXE"], cmdline_error=psutil.AccessDenied(101)),
        PsProcess(102, "VissimViewer.exe", 3.0),
        PsProcess(103, "python.exe", 4.0),
    )
    found = winproc.vissim_processes(PATTERN)
    assert set(found) == {100, 101}
    assert found[100] == ProcInfo(100, "Vissim260.exe", 1.0, (VISSIM_EXE, "-Embedding"))
    assert found[100].com_launched
    assert found[101].cmdline is None and not found[101].com_launched  # access denied: never "COM-launched"


@pytest.mark.parametrize("new_count", [0, 1, 2])
def test_detect_new_pid_with_zero_one_or_two_new_processes(ps: PsTable, new_count: int) -> None:
    user = vissim(200, 1.0, com=False)  # running before Dispatch: never a candidate
    ps.set(user)
    before = winproc.vissim_processes(PATTERN)
    new = [vissim(201 + i, 10.0 + i) for i in range(new_count)]
    ps.set(user, *new)
    pid, created, warnings = winproc.detect_new_pid(before, PATTERN, timeout_s=0.05, poll_s=0.01)
    if new_count == 0:
        assert (pid, created, warnings) == (None, None, [PID_NOT_DETECTED_WARNING])
    elif new_count == 1:
        assert (pid, created, warnings) == (201, 10.0, [])
    else:
        assert (pid, created) == (202, 11.0)  # the newest create time
        assert warnings == [
            "several new Vissim processes appeared (PIDs 201, 202); assuming the newest one, PID 202"
        ]


def test_detect_new_pid_polls_until_the_process_appears(ps: PsTable) -> None:
    ps.snapshots = [[], [], [], [vissim(300, 5.0)]]
    started = time.monotonic()
    assert winproc.detect_new_pid({}, PATTERN, timeout_s=5, poll_s=0.01)[:2] == (300, 5.0)
    assert time.monotonic() - started < 2


def test_detect_new_pid_survives_listing_errors(ps: PsTable, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> list[PsProcess]:
        raise OSError("listing failed")

    monkeypatch.setattr(winproc, "_process_iter", broken)
    assert winproc.detect_new_pid({}, PATTERN, timeout_s=0.05, poll_s=0.01) == (
        None,
        None,
        [PID_NOT_DETECTED_WARNING],
    )


# --- identity check, kill, terminate (psutil mocked)


def test_kill_without_a_known_pid_never_touches_a_process(ps: PsTable) -> None:
    proc = vissim(400, 1.0)
    ps.set(proc)
    assert winproc.kill(None, 1.0, PATTERN) is False
    assert winproc.kill(400, None, PATTERN) is False  # no create time: identity unproven
    assert winproc.terminate(None, None, PATTERN) is False
    assert ps.get_calls == [] and proc.calls == []


def test_kill_refuses_a_reused_pid(ps: PsTable) -> None:
    stranger = vissim(401, 999.0)  # PID 401 now belongs to a process started later
    ps.set(stranger)
    assert winproc.pid_matches(401, 1.0, PATTERN) is False
    assert winproc.kill(401, 1.0, PATTERN) is False
    assert winproc.terminate(401, 1.0, PATTERN) is False
    assert stranger.calls == []


def test_kill_refuses_another_program_with_the_same_pid(ps: PsTable) -> None:
    other = PsProcess(402, "notepad.exe", 1.0)
    ps.set(other)
    assert winproc.kill(402, 1.0, PATTERN) is False and other.calls == []


def test_kill_and_terminate_a_verified_process(ps: PsTable) -> None:
    # A stand-in name: the test-mode guard (VISSIM_MCP_FORBID_REAL, tested below) stays on.
    pattern = re.compile(r"^fake_vissim_standin\d*\.exe$", re.IGNORECASE)
    proc = PsProcess(403, "Fake_Vissim_Standin26.exe", 50.0)
    ps.set(proc)
    assert winproc.pid_matches(403, 50.02, pattern) is True
    assert winproc.terminate(403, 50.0, pattern) is True
    assert winproc.kill(403, 50.0, pattern) is True
    assert proc.calls == ["terminate", "kill"]
    proc.end_error = psutil.NoSuchProcess(403)
    assert winproc.kill(403, 50.0, pattern) is False
    proc.end_error = psutil.AccessDenied(403)
    assert winproc.kill(403, 50.0, pattern) is False
    assert winproc.kill(404, 50.0, pattern) is False  # gone


def test_test_mode_never_kills_a_real_vissim_executable(ps: PsTable) -> None:
    assert os.environ.get("VISSIM_MCP_FORBID_REAL") == "1"
    proc = vissim(405, 7.0)
    ps.set(proc)
    assert winproc.pid_matches(405, 7.0, PATTERN) is True
    assert winproc.kill(405, 7.0, PATTERN) is False
    assert winproc.terminate(405, 7.0, PATTERN) is False
    assert proc.calls == []
    # a stand-in with a non-Vissim name (and a matching custom pattern) is allowed
    standin = PsProcess(406, "fake_vissim_standin.exe", 8.0)
    ps.set(standin)
    assert winproc.kill(406, 8.0, re.compile(r"^fake_vissim_standin\.exe$", re.IGNORECASE)) is True
    assert standin.calls == ["kill"]


def test_is_com_launched_and_window_titles_errors(ps: PsTable, monkeypatch: pytest.MonkeyPatch) -> None:
    ps.set(vissim(500, 1.0), vissim(501, 1.0, com=False))
    assert winproc.is_com_launched(500) is True
    assert winproc.is_com_launched(501) is False
    assert winproc.is_com_launched(502) is False

    def broken(pid: int) -> list[str]:
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(winproc, "_window_titles_native", broken)
    assert winproc.window_titles(500) == []
    assert winproc.window_titles(None) == []


def test_everything_is_a_safe_no_op_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any) -> Any:
        raise AssertionError("no OS access off Windows")

    monkeypatch.setattr(winproc, "_is_windows", lambda: False)
    monkeypatch.setattr(winproc, "_process_iter", forbidden)
    monkeypatch.setattr(winproc, "_get_process", forbidden)
    monkeypatch.setattr(winproc, "_window_titles_native", forbidden)
    assert winproc.vissim_processes(PATTERN) == {}
    assert winproc.detect_new_pid({}, PATTERN, 5.0) == (None, None, [PID_NOT_DETECTED_WARNING])
    assert winproc.kill(1, 1.0, PATTERN) is False and winproc.terminate(1, 1.0, PATTERN) is False
    assert winproc.pid_matches(1, 1.0, PATTERN) is False
    assert winproc.is_com_launched(1) is False and winproc.window_titles(1) == []
    assert winproc.JobObject.create() is None
    with pytest.raises(OSError):
        winproc.JobObject(1).assign(1)
    assert winproc.JobObject(1).contains(1) is False


# --- Job Object


def test_job_limit_structure_layout() -> None:
    pointer_bits = struct.calcsize("P") * 8
    assert ctypes.sizeof(winproc.JOBOBJECT_BASIC_LIMIT_INFORMATION) == (64 if pointer_bits == 64 else 48)
    assert ctypes.sizeof(winproc.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == (144 if pointer_bits == 64 else 112)
    assert winproc.JOB_LIMIT_FLAGS & winproc.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    assert winproc.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS == 9


def test_job_object_assign_checks_identity_and_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    image = {"path": r"C:\Tools\worker.exe"}
    monkeypatch.setattr(winproc, "_is_windows", lambda: True)
    monkeypatch.setattr(
        winproc, "_k32_open_process", lambda pid, access: calls.append(("open", pid, access)) or 777
    )
    monkeypatch.setattr(winproc, "_k32_process_image_name", lambda handle: image["path"])
    monkeypatch.setattr(winproc, "_k32_process_create_time", lambda handle: 1000.0)
    monkeypatch.setattr(
        winproc, "_k32_assign_process", lambda job, handle: calls.append(("assign", job, handle))
    )
    monkeypatch.setattr(winproc, "_k32_close_handle", lambda handle: calls.append(("close", handle)))
    job = winproc.JobObject(55)

    job.assign(4242, 1000.01)
    access = winproc.PROCESS_SET_QUOTA | winproc.PROCESS_TERMINATE | winproc.PROCESS_QUERY_LIMITED_INFORMATION
    assert calls == [("open", 4242, access), ("assign", 55, 777), ("close", 777)]

    calls.clear()
    with pytest.raises(ProcessLookupError):  # the PID belongs to another process now
        job.assign(4242, 2000.0)
    assert calls == [("open", 4242, access), ("close", 777)]

    calls.clear()
    image["path"] = VISSIM_EXE
    with pytest.raises(PermissionError, match="test mode"):
        job.assign(4242, 1000.0)
    assert calls == [("open", 4242, access), ("close", 777)]


def test_job_object_create_reports_failures_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused() -> int:
        raise OSError(5, "CreateJobObjectW failed: Access is denied")

    monkeypatch.setattr(winproc, "_is_windows", lambda: True)
    monkeypatch.setattr(winproc, "_k32_create_kill_on_close_job", refused)
    assert winproc.JobObject.create() is None


@windows_only
def test_window_titles_of_this_process_are_never_queried() -> None:
    assert winproc.window_titles(os.getpid()) == []


# --- the real thing (Windows): kill-on-close with a dummy child, never Vissim

HELPER = r"""
import sys, time
from vissim_mcp import winproc

job = winproc.JobObject.create()
if job is None:
    print("NO-JOB", flush=True)
    sys.exit(3)
states = []
for spec in sys.argv[2:]:
    pid, created = spec.split(":")
    if sys.argv[1] == "assign":
        job.assign(int(pid), float(created))
    states.append(str(job.contains(int(pid))))
print("ASSIGNED " + " ".join(states), flush=True)
time.sleep(120)  # holds the job handle until it is killed
"""


def _spawn_quiet(args: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **kwargs,
    )


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
        family = [*parent.children(recursive=True), parent]
    except psutil.Error:
        return
    for proc in family:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(family, timeout=10)


def _read_line(stream: Any, timeout: float) -> str:
    box: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: box.put(stream.readline()), daemon=True).start()
    try:
        return box.get(timeout=timeout).decode("utf-8", "replace").strip()
    except queue.Empty:
        return "<no output>"


@pytest.fixture
def dummy_child() -> Iterator[psutil.Process]:
    """A harmless sleeper started from the base interpreter (a single process, not the venv launcher)."""
    executable = getattr(sys, "_base_executable", None) or sys.executable
    popen = _spawn_quiet([executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
    proc = psutil.Process(popen.pid)
    yield proc
    _kill_tree(popen.pid)
    popen.wait(10)


@windows_only
@pytest.mark.parametrize("assign", [True, False], ids=["in-job", "control-not-in-job"])
def test_kill_on_close_job_ends_the_child_when_its_holder_dies(
    dummy_child: psutil.Process, assign: bool
) -> None:
    """PLAN 6.4: however the server dies, Windows closes the job handle and ends the processes in the job.
    The control case (same helper, nothing assigned) proves that it is the job that ends the child."""
    assert dummy_child.name().lower() == "python.exe"  # never a Vissim executable
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYTHONUTF8"] = "1"
    mode = "assign" if assign else "control"
    spec = f"{dummy_child.pid}:{dummy_child.create_time()!r}"
    helper = _spawn_quiet(
        [sys.executable, "-X", "utf8", "-c", HELPER, mode, spec], stdout=subprocess.PIPE, env=env
    )
    try:
        assert helper.stdout is not None
        assert _read_line(helper.stdout, timeout=30) == f"ASSIGNED {assign}"
        time.sleep(0.2)
        assert dummy_child.is_running()  # alive while the job handle is open

        _kill_tree(helper.pid)  # the helper dies abruptly: Windows closes its job handle
        gone, _alive = psutil.wait_procs([dummy_child], timeout=10 if assign else 1.0)
        if assign:
            assert gone, "the dummy child survived the end of its kill-on-close job"
        else:
            assert not gone and dummy_child.is_running()
    finally:
        _kill_tree(helper.pid)
        helper.wait(10)
