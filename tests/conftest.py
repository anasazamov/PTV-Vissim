"""Shared test setup.

* ``tests/fakes`` is put first on ``sys.path`` *before anything imports pywin32*: the fake ``pythoncom``,
  ``pywintypes`` and ``win32com`` shadow the real ones in this process (the MCP SDK imports ``pywintypes`` on
  Windows). Real COM is therefore unreachable from in-process tests; ``VISSIM_MCP_FORBID_REAL=1`` additionally
  makes the real backend refuse to dispatch (contract section 0).
* Every test runs with a clean environment: all ``VISSIM_*`` / ``FAKE_VISSIM_*`` variables and
  ``CLAUDE_PROJECT_DIR`` are removed, and log, audit and allowed directories point into ``tmp_path``.
* In-process services use ``FakeProcs`` (``tests/fake_procs.py``) instead of ``vissim_mcp.winproc``: unit
  tests never list, kill or put into a job object any real process.
* ``server_env`` / ``start_server`` run ``python -X utf8 -m vissim_mcp`` as a subprocess against the fakes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

TESTS_DIR = Path(__file__).resolve().parent
FAKES_DIR = TESTS_DIR / "fakes"
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"

for _path in (str(TESTS_DIR), str(FAKES_DIR)):  # FAKES_DIR ends up first
    if _path in sys.path:
        sys.path.remove(_path)
    sys.path.insert(0, _path)

os.environ["VISSIM_MCP_FORBID_REAL"] = "1"

import pythoncom  # noqa: E402 - must resolve to tests/fakes
import pywintypes  # noqa: E402

if not getattr(pythoncom, "__fake__", False) or not getattr(pywintypes, "__fake__", False):
    raise RuntimeError(
        "tests/fakes must shadow pywin32 (pythoncom/pywintypes were imported before tests/conftest.py ran)"
    )

from fake_backend import BackendFactory, FakeBackend  # noqa: E402
from fake_procs import FakeProcs  # noqa: E402
from fake_vissim import FakeVissim  # noqa: E402
from service_harness import Harness, make_harness  # noqa: E402
from stdio_client import StdioServer  # noqa: E402
from vissim_mcp.config import Settings, load_settings  # noqa: E402
from vissim_mcp.logging_setup import reset_logging  # noqa: E402
from vissim_mcp.service import VissimService  # noqa: E402

_CLEARED_PREFIXES = ("VISSIM_", "FAKE_VISSIM_")
_CLEARED_NAMES = ("CLAUDE_PROJECT_DIR",)


def pytest_addoption(parser: pytest.Parser) -> None:
    """Options of the real-Vissim suite (tests/real, PLAN 10.3). They live here because pytest reads options
    only from the conftest files it loads at start-up; tests/real/conftest.py uses them."""
    group = parser.getgroup("vissim", "real PTV Vissim tests (tests/real, run with -m vissim)")
    group.addoption(
        "--vissim-com-license",
        action="store_true",
        default=False,
        help="confirm that a Vissim license WITH the COM module is active; without it every real test is "
        "skipped before anything starts",
    )
    group.addoption(
        "--vissim-manual",
        action="store_true",
        default=False,
        help="also run the manual steps (modal dialog, Task Manager kill); needs -s",
    )
    group.addoption("--vissim-progid", default="Vissim.Vissim", help="COM ProgID for the real tests")
    group.addoption(
        "--vissim-model",
        default=(
            r"C:\Users\Public\Documents\PTV Vision\PTV Vissim 2026\Examples Training\COM\Basic Commands"
            r"\COM Basic Commands.inpx"
        ),
        help="network that the real tests copy and load (default: the COM Basic Commands example)",
    )
    group.addoption(
        "--vissim-results",
        default=None,
        help="append the observations of the real tests to this JSON-lines file (for docs/test-report-v1.md)",
    )
    group.addoption(
        "--vissim-stress-timeout",
        type=float,
        default=3600.0,
        help="wall-clock limit in seconds for the 5 x 3600 s stress run (default 3600)",
    )


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Clean VISSIM_* environment with log/audit/allowed directories inside ``tmp_path``."""
    for name in list(os.environ):
        if name.upper().startswith(_CLEARED_PREFIXES) or name.upper() in _CLEARED_NAMES:
            monkeypatch.delenv(name, raising=False)
    dirs = {
        "log": tmp_path / "logs",
        "audit": tmp_path / "audit",
        "projects": tmp_path / "projects",
    }
    dirs["projects"].mkdir()
    monkeypatch.setenv("VISSIM_MCP_FORBID_REAL", "1")
    monkeypatch.setenv("VISSIM_LOG_DIR", str(dirs["log"]))
    monkeypatch.setenv("VISSIM_AUDIT_DIR", str(dirs["audit"]))
    monkeypatch.setenv("VISSIM_ALLOWED_DIRS", str(dirs["projects"]))
    return dirs


@pytest.fixture
def settings(isolated_env: dict[str, Path]) -> Settings:
    return load_settings()


@pytest.fixture
def fake_vissim() -> FakeVissim:
    return FakeVissim()


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def procs() -> FakeProcs:
    """In-memory process table standing in for ``vissim_mcp.winproc`` (no real process is ever touched)."""
    return FakeProcs()


@pytest.fixture
def backends(procs: FakeProcs) -> Iterator[BackendFactory]:
    """``backend_factory`` whose every Dispatch "starts" a COM-launched fake Vissim process in ``procs``."""
    factory = BackendFactory(procs)
    yield factory
    factory.release_all()


@pytest.fixture
def service(settings: Settings, backends: BackendFactory, procs: FakeProcs) -> Iterator[VissimService]:
    """VissimService wired to FakeBackend and FakeProcs (never the real winproc)."""
    svc = VissimService(settings, backend_factory=backends, procs=procs)
    yield svc
    svc.shutdown_fast()
    backends.release_all()


@pytest.fixture
def harness(isolated_env: dict[str, Path]) -> Iterator[Callable[..., Harness]]:
    """Factory ``harness(env=None, **fake_options)`` -> ``Harness`` (service + FakeProcs + fake backends with
    short timings); every harness is shut down and its fakes released at teardown."""
    made: list[Harness] = []

    def factory(*args: Any, **kwargs: Any) -> Harness:
        created = make_harness(*args, **kwargs)
        made.append(created)
        return created

    yield factory
    for created in made:
        created.close()


@pytest.fixture
def clean_logging() -> Iterator[None]:
    """Undo ``setup_logging`` after the test (it changes the root logger of this process)."""
    yield
    reset_logging()


@pytest.fixture
def server_env(isolated_env: dict[str, Path]) -> dict[str, str]:
    """Environment for a server subprocess: fakes first on PYTHONPATH, UTF-8, isolated directories."""
    env = dict(os.environ)
    python_path = [str(FAKES_DIR), str(SRC_DIR)]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)
    return env


@pytest.fixture
def start_server(server_env: dict[str, str]) -> Iterator[Callable[..., StdioServer]]:
    """Factory ``start_server(args=None, env=None)`` -> running ``StdioServer`` (killed at teardown)."""
    started: list[StdioServer] = []

    def factory(args: list[str] | None = None, env: dict[str, str] | None = None) -> StdioServer:
        merged = dict(server_env)
        merged.update(env or {})
        server = StdioServer.start(args or ["-m", "vissim_mcp"], env=merged, cwd=REPO_ROOT)
        started.append(server)
        return server

    yield factory
    for server in started:
        server.kill_tree()
