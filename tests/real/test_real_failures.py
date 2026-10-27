"""PLAN 10.3 item 5 - failures on a real Vissim.

* Vissim killed (automatically, identity-checked, or by the owner in Task Manager with --vissim-manual) ->
  the next call gives a clear error and vissim_restart recovers;
* a modal "Save changes?" dialog -> TIMEOUT_HUNG, vissim_restart recovers (manual: --vissim-manual -s);
* the server killed like Claude Code kills its process tree -> the Job Object closes Vissim;
* stdin closed -> the server exits within 300 ms and Vissim is closed (VISSIM_KILL_ON_EXIT).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from real_helpers import EXIT_BUDGET_S, RealSession, ask_owner, kill_own_vissim, wait_gone

pytestmark = pytest.mark.vissim


def test_killed_vissim_gives_a_clear_error_and_restart_recovers(
    request: pytest.FixtureRequest,
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    session = real_session()
    session.connect()
    session.load(*example_model)
    own = session.current()
    if request.config.getoption("vissim_manual"):
        ask_owner(
            request.config,
            f"Task Manager'ni oching (Ctrl+Shift+Esc) -> 'Details' -> Vissim260.exe PID {own.pid} -> "
            "'End task'. Boshqa Vissim oynalariga TEGMANG.",
        )
    else:
        kill_own_vissim(own)
    assert wait_gone(own), f"Vissim PID {own.pid} is still running"

    failed = session.tool("vissim_info", timeout=200)
    health = session.ok("vissim_health")
    record(
        after_kill={
            "info": failed,
            "health_connected": health["connected"],
            "vissim_alive": health.get("vissim_alive"),
        }
    )
    assert failed["ok"] is False, failed
    error = failed["error"]
    assert error["code"] in ("COM_ERROR", "NOT_CONNECTED"), error
    assert "vissim_restart" in (error.get("hint") or "") + error["message"], error
    assert health["connected"] is False or health.get("vissim_alive") is False, health

    restarted = session.ok("vissim_restart", timeout=300)
    record(
        restart={
            "pid": restarted["pid"],
            "previous_pid": restarted.get("previous_pid"),
            "killed": restarted.get("killed"),
            "warnings": restarted["warnings"],
        }
    )
    assert restarted["pid"] not in (None, own.pid)
    session.load(*example_model)
    assert session.ok("vissim_info")["counts"]["Links"] > 0


def test_modal_dialog_gives_timeout_hung_and_restart_recovers(
    request: pytest.FixtureRequest,
    real_session: Callable[..., RealSession],
    example_model: tuple[Path, Path | None],
    record: Callable[..., None],
) -> None:
    if not request.config.getoption("vissim_manual"):
        pytest.skip("manual step: run with --vissim-manual -s (see docs/m6-runbook.md)")
    session = real_session({"VISSIM_CALL_TIMEOUT_S": "20"})
    session.connect()
    session.load(*example_model)
    own = session.current()
    link = session.first_key("Net.Links")
    session.ok("vissim_set_attributes", {"path": f"Net.Links[{link}]", "values": {"Name": "M6 dialog test"}})
    ask_owner(
        request.config,
        f"Test ochgan Vissim oynasini toping (PID {own.pid}, sarlavhada 'COM Basic Commands'). Oynaning "
        "yuqori o'ng burchagidagi X tugmasini bosing. 'Save changes?' / 'Сохранить изменения?' dialogi "
        "chiqadi - unga JAVOB BERMANG, ochiq qoldiring.",
    )
    started = time.monotonic()
    blocked = session.tool("vissim_info", timeout=120)
    waited = time.monotonic() - started
    health = session.ok("vissim_health")
    record(
        modal_dialog={
            "info": blocked,
            "waited_s": round(waited, 1),
            "busy_seconds": health["busy_seconds"],
            "current_task": health["current_task"],
        }
    )
    assert blocked["ok"] is False and blocked["error"]["code"] == "TIMEOUT_HUNG", (
        f"the dialog did not block COM: {blocked} - record this in docs/test-report-v1.md"
    )
    assert health["busy_seconds"] > 0

    restarted = session.ok("vissim_restart", timeout=300)
    record(
        restart_after_dialog={
            "pid": restarted["pid"],
            "killed": restarted.get("killed"),
            "warnings": restarted["warnings"],
        }
    )
    assert wait_gone(own), f"the hung Vissim PID {own.pid} survived vissim_restart"
    assert session.ok("vissim_info")["pid"] == restarted["pid"]


def test_killed_server_leaves_no_vissim(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    connected = session.connect()
    own = session.current()
    assert connected["job_object"] is True, connected
    started = time.monotonic()
    session.server.kill_tree()  # TerminateProcess, like Claude Code ending the server's process tree
    gone = wait_gone(own, 10)
    record(server_killed={"vissim_gone": gone, "after_s": round(time.monotonic() - started, 2)})
    assert gone, f"Vissim PID {own.pid} survived the killed server - the Job Object did not close it"


def test_stdin_eof_exits_fast_and_closes_vissim(
    real_session: Callable[..., RealSession], record: Callable[..., None]
) -> None:
    session = real_session()
    session.connect()
    own = session.current()
    exit_s = session.server.close_stdin_and_time_exit()
    gone = wait_gone(own, 10)
    record(stdin_eof={"exit_s": round(exit_s, 3), "vissim_gone": gone})
    assert exit_s < EXIT_BUDGET_S, f"server needed {exit_s * 1000:.0f} ms after stdin closed"
    assert gone, f"Vissim PID {own.pid} survived the server exit"
