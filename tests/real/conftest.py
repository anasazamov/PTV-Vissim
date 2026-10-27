"""Real-Vissim suite (PLAN 10.3, 11 row M6): only with ``-m vissim`` AND ``--vissim-com-license``.

Isolation from the default run:

* every item below tests/real gets the ``vissim`` marker here (backstop for ``pytestmark``), so the default
  ``addopts = -m "not vissim"`` always deselects them;
* without ``--vissim-com-license`` every real test is skipped at collection - no fixture runs, nothing starts;
* real pywin32 is never imported into the pytest process (tests/conftest.py shadows it with tests/fakes):
  the tests drive ``python -X utf8 -m vissim_mcp`` as a SUBPROCESS whose PYTHONPATH has no fakes and whose
  environment has no ``VISSIM_MCP_FORBID_REAL`` - exactly how Claude Code runs the server. The probe test runs
  scripts/probe_com_names.py as a subprocess the same way.

The server only ever kills the Vissim it started itself (``-Embedding`` + PID + create time); an interactive
Vissim is never touched. The session fixture checks at teardown that no Vissim of the test survived.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from real_helpers import REAL_DIR, RealSession, copy_model

_RESULTS: list[dict[str, Any]] = []


def _is_real(item: pytest.Item) -> bool:
    try:
        return Path(str(item.fspath)).resolve().is_relative_to(REAL_DIR)
    except (OSError, ValueError):
        return False


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    licensed = config.getoption("vissim_com_license")
    skip = pytest.mark.skip(
        reason="real Vissim test: needs --vissim-com-license (a license WITH the COM module); "
        "see docs/m6-runbook.md"
    )
    for item in items:
        if not _is_real(item):
            continue
        item.add_marker(pytest.mark.vissim)
        if not licensed:
            item.add_marker(skip)


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    if not _RESULTS:
        return
    terminalreporter.section("M6 observations (copy into docs/test-report-v1.md)")
    for entry in _RESULTS:
        terminalreporter.write_line(json.dumps(entry, ensure_ascii=False, default=str))


@pytest.fixture
def record(request: pytest.FixtureRequest) -> Callable[..., None]:
    """``record(**observations)``: kept for the terminal summary and, with ``--vissim-results``, appended to
    that JSON-lines file."""
    path = request.config.getoption("vissim_results")

    def write(**observations: Any) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "test": request.node.nodeid,
            **observations,
        }
        _RESULTS.append(entry)
        if path:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    return write


@pytest.fixture
def example_model(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path | None]:
    """A copy of the test network (and its .layx) in ``tmp_path/projects/model`` - never the original."""
    source = Path(request.config.getoption("vissim_model"))
    if not source.is_file():
        pytest.fail(f"test network not found: {source} (use --vissim-model)")
    return copy_model(source, tmp_path / "projects" / "model")


@pytest.fixture
def real_session(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Callable[..., RealSession]]:
    """Factory ``real_session(env=None)`` -> a running real server (subprocess, real pywin32).

    Teardown closes Vissim, ends the server and fails the test if a Vissim it started is still running.
    """
    sessions: list[RealSession] = []

    def factory(env: dict[str, str] | None = None) -> RealSession:
        session = RealSession.start(
            tmp_path,
            progid=request.config.getoption("vissim_progid"),
            env=env or {},
        )
        sessions.append(session)
        return session

    yield factory
    problems: list[str] = []
    for session in sessions:
        problems.extend(session.finish())
    if problems:
        pytest.fail("; ".join(problems))
