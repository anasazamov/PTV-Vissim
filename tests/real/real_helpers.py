"""Helpers of the real-Vissim suite: the server as a subprocess with REAL pywin32 (never the fakes).

Nothing here imports pywin32: the pytest process keeps the fakes of tests/conftest.py, the server subprocess
gets ``PYTHONPATH=src`` only and no ``VISSIM_MCP_FORBID_REAL``.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

REAL_DIR = Path(__file__).resolve().parent
TESTS_DIR = REAL_DIR.parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
FAKES_DIR = TESTS_DIR / "fakes"
PROBE_SCRIPT = REPO_ROOT / "scripts" / "probe_com_names.py"

if str(TESTS_DIR) not in sys.path:
    sys.path.append(str(TESTS_DIR))

from stdio_client import StdioServer  # noqa: E402

#: The process-name rule of the server (config.DEFAULT_EXE_PATTERN).
VISSIM_EXE_RE = re.compile(r"^vissim\d*\.exe$", re.IGNORECASE)
#: Claude Code closes stdin and kills the process tree 300 ms later (PLAN 3.3).
EXIT_BUDGET_S = 0.300
CONNECT_TIMEOUT_S = 240.0
LOAD_TIMEOUT_S = 660.0
CALL_TIMEOUT_S = 150.0
VISSIM_GONE_S = 15.0


def real_server_env(workdir: Path, *, progid: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment of a real server: no VISSIM_* from the caller, no fakes, no test-mode switch."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("VISSIM_", "FAKE_VISSIM_")) and name.upper() != "CLAUDE_PROJECT_DIR"
    }
    env["PYTHONPATH"] = str(SRC_DIR)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)
    projects = workdir / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "VISSIM_PROGID": progid,
            "VISSIM_ALLOWED_DIRS": str(projects),
            "VISSIM_LOG_DIR": str(workdir / "logs"),
            "VISSIM_AUDIT_DIR": str(workdir / "audit"),
            "VISSIM_KILL_ON_EXIT": "1",
            "VISSIM_USE_JOB_OBJECT": "1",
            "VISSIM_CONNECT_TIMEOUT_S": "180",
            "VISSIM_LOAD_TIMEOUT_S": "600",
        }
    )
    env.update(extra or {})
    assert "VISSIM_MCP_FORBID_REAL" not in env
    assert str(FAKES_DIR) not in env["PYTHONPATH"]
    return env


def copy_model(source: Path, target_dir: Path, name: str | None = None) -> tuple[Path, Path | None]:
    """Copy ``source`` (.inpx) and the .layx of the same name (if any) into ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem if name else source.stem
    model = target_dir / f"{stem}{source.suffix}"
    shutil.copy2(source, model)
    layout_source = source.with_suffix(".layx")
    layout = None
    if layout_source.is_file():
        layout = target_dir / f"{stem}.layx"
        shutil.copy2(layout_source, layout)
    return model, layout


@dataclass
class OwnVissim:
    pid: int
    create_time: float | None


def process_identity(pid: int) -> tuple[float | None, bool]:
    """``(create_time, com_launched)`` of a Vissim process; raises psutil errors when it is gone."""
    proc = psutil.Process(pid)
    name = proc.name()
    if not VISSIM_EXE_RE.fullmatch(name):
        raise psutil.NoSuchProcess(pid, name, "not a Vissim process")
    try:
        cmdline = " ".join(proc.cmdline()).lower()
    except psutil.Error:
        cmdline = ""
    return proc.create_time(), ("embedding" in cmdline or "automation" in cmdline)


def is_alive(own: OwnVissim) -> bool:
    try:
        create_time, _ = process_identity(own.pid)
    except psutil.Error:
        return False
    return own.create_time is None or abs((create_time or 0) - own.create_time) < 0.05


def wait_gone(own: OwnVissim, timeout_s: float = VISSIM_GONE_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while is_alive(own):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def kill_own_vissim(own: OwnVissim) -> None:
    """Kill a Vissim of this test - only after checking name, create time and the ``-Embedding`` switch."""
    create_time, com_launched = process_identity(own.pid)
    assert com_launched, f"PID {own.pid} was not started by COM; refusing to kill it"
    assert own.create_time is None or abs((create_time or 0) - own.create_time) < 0.05, "PID reused"
    psutil.Process(own.pid).kill()


def ask_owner(config: pytest.Config, message: str) -> None:
    """Manual step: print instructions and wait for Enter (needs ``--vissim-manual`` and ``-s``)."""
    if not config.getoption("vissim_manual"):
        pytest.skip("manual step: run with --vissim-manual -s (see docs/m6-runbook.md)")
    if config.getoption("capture") != "no":
        pytest.skip("manual step: needs -s so that the instructions are visible and Enter can be read")
    print("\n" + "=" * 78 + "\nQO'LDA QADAM / MANUAL STEP\n" + message.strip() + "\n" + "=" * 78, flush=True)
    input("Tayyor bo'lgach Enter bosing / press Enter when done: ")


@dataclass
class RealSession:
    """A real server subprocess plus every Vissim it reported (for the teardown check)."""

    server: StdioServer
    workdir: Path
    own: list[OwnVissim] = field(default_factory=list)

    @classmethod
    def start(cls, workdir: Path, *, progid: str, env: dict[str, str]) -> RealSession:
        server = StdioServer.start(
            ["-m", "vissim_mcp"], env=real_server_env(workdir, progid=progid, extra=env), cwd=REPO_ROOT
        )
        session = cls(server, workdir)
        init = server.initialize()
        assert "result" in init, init
        return session

    @property
    def projects(self) -> Path:
        return self.workdir / "projects"

    # --- tools

    def tool(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT_S
    ) -> dict:
        payload = self.server.call_tool(name, arguments or {}, timeout=timeout)["payload"]
        if name in ("vissim_connect", "vissim_restart") and payload.get("ok") and payload.get("pid"):
            self._track(int(payload["pid"]))
        return payload

    def ok(self, name: str, arguments: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT_S) -> dict:
        payload = self.tool(name, arguments, timeout)
        assert payload.get("ok") is True, (
            f"{name} failed: {payload}\n--- server stderr (tail) ---\n" + (self.server.stderr_text()[-3000:])
        )
        return payload

    def connect(self) -> dict:
        payload = self.ok("vissim_connect", timeout=CONNECT_TIMEOUT_S)
        assert payload["pid"] is not None, f"PID of the started Vissim not detected: {payload}"
        return payload

    def load(self, model: Path, layout: Path | None = None) -> dict:
        args: dict[str, Any] = {"inpx_path": str(model)}
        if layout is not None:
            args["layx_path"] = str(layout)
        return self.ok("vissim_load_network", args, timeout=LOAD_TIMEOUT_S)

    def wait_job(self, job_id: str, timeout_s: float, *, until: tuple[str, ...] = ()) -> dict:
        """Poll vissim_wait_simulation until the job left 'running' (or reached a state of ``until``)."""
        deadline = time.monotonic() + timeout_s
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.ok("vissim_wait_simulation", {"job_id": job_id, "max_wait_s": 30}, timeout=60)
            if status["state"] != "running" or status["state"] in until:
                return status
        raise AssertionError(f"job {job_id} still running after {timeout_s} s: {status}")

    def first_key(self, collection: str) -> Any:
        rows = self.ok("vissim_list_objects", {"collection": collection, "attributes": ["No"], "limit": 1})
        assert rows["rows"], f"{collection} is empty"
        return rows["rows"][0][0]

    # --- processes

    def _track(self, pid: int) -> None:
        try:
            create_time, _ = process_identity(pid)
        except psutil.Error:
            create_time = None
        if all(o.pid != pid for o in self.own):
            self.own.append(OwnVissim(pid, create_time))

    def current(self) -> OwnVissim:
        assert self.own, "no Vissim was started"
        return self.own[-1]

    def finish(self) -> list[str]:
        """Close Vissim, end the server, verify that no Vissim of this session survived (kill + report)."""
        problems: list[str] = []
        if self.server.process.poll() is None:
            try:
                health = self.server.call_tool("vissim_health", timeout=10)["payload"]
                if health.get("connected"):
                    self.server.call_tool("vissim_close", timeout=60)
            except Exception:  # the test may have broken the session on purpose; the PID check below decides
                pass
            try:
                self.server.close_stdin()
                self.server.wait(timeout=10)
            except Exception:
                pass
        self.server.kill_tree()
        for own in self.own:
            if not wait_gone(own):
                problems.append(f"Vissim PID {own.pid} of this test was still running after the server ended")
                try:
                    kill_own_vissim(own)
                except Exception as exc:
                    problems.append(f"could not kill PID {own.pid}: {exc}")
        return problems
