"""logging_setup.py: file + stderr logging that never breaks start-up and never blocks the exit path."""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path

import pytest

from vissim_mcp.config import Settings
from vissim_mcp.logging_setup import (
    LOG_FILE_NAME,
    SafeRotatingFileHandler,
    flush_logging,
    log_file_path,
    setup_logging,
)


def our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_vissim_mcp_handler", False)]


def test_setup_creates_the_log_file_and_writes_info(settings: Settings, clean_logging: None) -> None:
    path = setup_logging(settings)
    assert path == settings.log_dir / LOG_FILE_NAME == log_file_path(settings)
    logging.getLogger("vissim_mcp.test").info("hello-info Самарқанд")
    flush_logging(1.0)
    text = path.read_text(encoding="utf-8")
    assert "INFO vissim_mcp.test: hello-info Самарқанд" in text


def test_stderr_receives_warnings_only(
    settings: Settings, clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    setup_logging(settings)
    log = logging.getLogger("vissim_mcp.test")
    log.info("quiet-info")
    log.warning("loud-warning")
    captured = capsys.readouterr()
    assert "loud-warning" in captured.err
    assert "quiet-info" not in captured.err
    assert captured.out == ""


def test_setup_is_idempotent(settings: Settings, clean_logging: None) -> None:
    setup_logging(settings)
    setup_logging(settings)
    assert len(our_handlers()) == 2  # one file handler + one stderr handler


def test_third_party_info_is_suppressed(settings: Settings, clean_logging: None) -> None:
    path = setup_logging(settings)
    assert path is not None
    logging.getLogger("mcp.server.lowlevel.server").info("sdk-chatter")
    logging.getLogger("mcp.server.lowlevel.server").warning("sdk-warning")
    flush_logging(1.0)
    text = path.read_text(encoding="utf-8")
    assert "sdk-chatter" not in text
    assert "sdk-warning" in text


def test_unwritable_log_dir_falls_back_to_stderr(
    settings: Settings, tmp_path: Path, clean_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    broken = dataclasses.replace(settings, log_dir=blocker / "logs")
    assert setup_logging(broken) is None
    assert "logging to stderr only" in capsys.readouterr().err
    logging.getLogger("vissim_mcp.test").warning("still-logged")
    assert "still-logged" in capsys.readouterr().err


def test_failed_rollover_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler = SafeRotatingFileHandler(tmp_path / "shared.log", max_bytes=300, backup_count=2)
    log = logging.getLogger("vissim_mcp.rollover_test")
    log.propagate = False
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    def locked(source: str, dest: str) -> None:
        raise PermissionError(
            32, "The process cannot access the file because it is being used by another process"
        )

    monkeypatch.setattr(handler, "rotate", locked)
    try:
        for number in range(40):
            log.info("line %02d %s", number, "x" * 20)
        handler.flush()
        text = (tmp_path / "shared.log").read_text(encoding="utf-8")
        assert all(f"line {number:02d}" in text for number in range(40))  # nothing lost
        assert handler.error_count == 0
        assert handler._rollover_blocked_until > time.monotonic()  # retried later, not on every record
        assert capsys.readouterr().err == ""  # no tracebacks
    finally:
        log.removeHandler(handler)
        handler.close()
        log.propagate = True


def test_rollover_works_when_the_file_is_free(tmp_path: Path) -> None:
    handler = SafeRotatingFileHandler(tmp_path / "own.log", max_bytes=300, backup_count=2)
    log = logging.getLogger("vissim_mcp.rollover_ok")
    log.propagate = False
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        for number in range(40):
            log.info("line %02d %s", number, "y" * 20)
    finally:
        log.removeHandler(handler)
        handler.close()
        log.propagate = True
    assert (tmp_path / "own.log.1").exists()
    assert not (tmp_path / "own.log.3").exists()  # backupCount respected


def test_flush_logging_never_waits_long_for_a_busy_handler(settings: Settings, clean_logging: None) -> None:
    setup_logging(settings)
    handler = our_handlers()[0]
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with handler.lock:  # type: ignore[union-attr]
            acquired.set()
            release.wait(5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert acquired.wait(5)
    try:
        started = time.perf_counter()
        flush_logging(0.05)
        assert time.perf_counter() - started < 0.5
    finally:
        release.set()
        holder.join(5)
