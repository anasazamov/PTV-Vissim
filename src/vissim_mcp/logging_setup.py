"""Logging: rotating file log (INFO+) and stderr (WARNING+). Never writes to stdout, never breaks start-up.

Handlers are attached to the root logger, so warnings of third-party libraries (mcp, anyio, ...) end up in the
same file; the root level is WARNING and the ``vissim_mcp`` logger is INFO. Attaching a handler to the root
logger also stops FastMCP's ``logging.basicConfig`` from installing its own INFO-level stderr handler.

Several server processes (one per Claude Code session) may share the log file. On Windows a file that another
process holds open cannot be renamed, so a failed rollover is swallowed and retried a minute later instead of
printing a traceback for every record.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

LOGGER_NAME = "vissim_mcp"
LOG_FILE_NAME = "vissim-mcp.log"
LOG_FORMAT = "%(asctime)s %(process)d %(threadName)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 5_000_000
BACKUP_COUNT = 5
_MARKER = "_vissim_mcp_handler"

_state_lock = threading.Lock()
_saved_root_level: int | None = None


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """``RotatingFileHandler`` that survives rollover failures and never prints tracebacks."""

    rollover_retry_s = 60.0

    def __init__(
        self, filename: str | Path, max_bytes: int = MAX_BYTES, backup_count: int = BACKUP_COUNT
    ) -> None:
        super().__init__(
            filename, mode="a", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8", delay=True
        )
        self._rollover_blocked_until = 0.0
        self.error_count = 0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._rollover_blocked_until:
            return False
        try:
            return bool(super().shouldRollover(record))
        except Exception:
            return False

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError:
            # Typically PermissionError: another vissim-mcp process has the file open. The base class has
            # already closed our stream; emit() reopens it lazily (delay=True) and we keep appending for a
            # while.
            self._rollover_blocked_until = time.monotonic() + self.rollover_retry_s

    def handleError(self, record: logging.LogRecord) -> None:
        self.error_count += 1


class SafeStreamHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler whose failures (e.g. a closed stderr pipe) are silent."""

    def handleError(self, record: logging.LogRecord) -> None:
        pass


def log_file_path(settings: Settings) -> Path:
    return settings.log_dir / LOG_FILE_NAME


def setup_logging(settings: Settings) -> Path | None:
    """Configure logging; return the log file path, or None when only stderr logging is possible.

    Idempotent: handlers installed by an earlier call are replaced.
    """
    global _saved_root_level
    problem: str | None = None
    log_path: Path | None = None
    with _state_lock:
        root = logging.getLogger()
        _remove_our_handlers(root)
        if _saved_root_level is None:
            _saved_root_level = root.level
        formatter = logging.Formatter(LOG_FORMAT)

        candidate = log_file_path(settings)
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            with open(candidate, "a", encoding="utf-8"):
                pass
            file_handler = SafeRotatingFileHandler(candidate)
            _install(root, file_handler, logging.INFO, formatter)
            log_path = candidate
        except OSError as exc:
            problem = f"cannot write the log file {candidate}: {exc}; logging to stderr only"

        if sys.stderr is not None:
            _install(root, SafeStreamHandler(sys.stderr), logging.WARNING, formatter)

        root.setLevel(logging.WARNING)
        logging.getLogger(LOGGER_NAME).setLevel(logging.INFO)
        logging.captureWarnings(True)

    if problem:
        logging.getLogger(LOGGER_NAME).warning(problem)
    return log_path


def reset_logging() -> None:
    """Remove the handlers installed by :func:`setup_logging` and restore the root level (used by tests)."""
    global _saved_root_level
    with _state_lock:
        root = logging.getLogger()
        _remove_our_handlers(root)
        if _saved_root_level is not None:
            root.setLevel(_saved_root_level)
            _saved_root_level = None
        logging.getLogger(LOGGER_NAME).setLevel(logging.NOTSET)
        logging.captureWarnings(False)


def flush_logging(timeout_s: float = 0.05) -> None:
    """Flush all root handlers, waiting at most ``timeout_s`` in total for handler locks.

    Used on the exit path: a handler blocked on a full stderr pipe must not delay process exit.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    for handler in list(logging.getLogger().handlers):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        lock: Any = getattr(handler, "lock", None)
        if lock is None:
            _flush_quietly(handler)
            continue
        if not lock.acquire(timeout=remaining):
            continue
        try:
            _flush_quietly(handler)
        finally:
            lock.release()


def _install(
    root: logging.Logger, handler: logging.Handler, level: int, formatter: logging.Formatter
) -> None:
    handler.setLevel(level)
    handler.setFormatter(formatter)
    setattr(handler, _MARKER, True)
    root.addHandler(handler)


def _remove_our_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _flush_quietly(handler: logging.Handler) -> None:
    try:
        handler.flush()
    except Exception:
        pass
