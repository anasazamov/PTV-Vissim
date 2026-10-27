r"""Audit log (PLAN 6.8): one JSON line per write operation in ``VISSIM_AUDIT_DIR\audit-YYYY-MM.jsonl``.

Every record starts with ``ts`` (local time with UTC offset and milliseconds) and carries the fields the
caller passes - for the tools (``VissimService.write_audit``) ``tool``, ``generation``, ``pid``, ``args``,
``dry_run``, ``ok``, ``error_code``, ``old`` and ``new``, plus per tool e.g. ``sha256`` and ``backup_path``
(save) or the full ``code`` (exec) - and ``server_pid`` (several server processes may share a monthly file).
Values go through ``output.to_jsonable``, so anything COM returns is written safely.

Attribute values are written only here, never to the server log (PLAN 8.6). A record larger than
:data:`MAX_RECORD_CHARS` keeps its outcome but drops the bulky value fields (``truncated`` says so).

:meth:`AuditLog.write` never raises: when the record cannot be written (disk full, no permission, the
directory is a file, ...) it logs a warning and returns a warning text, which the tool adds to its
``warnings`` - the operation itself is not undone (PLAN 6.8).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .output import dumps, to_jsonable

log = logging.getLogger(__name__)

FILE_PREFIX = "audit-"
FILE_SUFFIX = ".jsonl"
#: Records longer than this lose their value fields (a 5000-item bulk write stays far below it).
MAX_RECORD_CHARS = 2_000_000
#: Value fields that are dropped first from an oversized record.
BULKY_FIELDS = ("old", "new", "changes", "failed", "values", "code", "args")


class AuditLog:
    """Thread-safe appender of JSONL audit records (one file per month, local time)."""

    def __init__(self, audit_dir: str | os.PathLike[str]) -> None:
        self.directory = Path(audit_dir)
        self._lock = threading.Lock()
        self.records_written = 0
        self.failures = 0
        self.last_error: str | None = None

    def path_for(self, when: datetime) -> Path:
        """``<dir>/audit-YYYY-MM.jsonl`` for the local month of ``when``."""
        return self.directory / f"{FILE_PREFIX}{when:%Y-%m}{FILE_SUFFIX}"

    def write(self, record: Mapping[str, Any]) -> str | None:
        """Append ``record`` as one JSON line; None, or a warning text when it could not be written."""
        try:
            now = datetime.now().astimezone()
            entry: dict[str, Any] = {"ts": now.isoformat(timespec="milliseconds")}
            converted = to_jsonable(dict(record))
            entry.update({key: value for key, value in converted.items() if key != "ts"})
            entry.setdefault("server_pid", os.getpid())
            line = _encode(entry)
            path = self.path_for(now)
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                with open(path, "ab") as handle:
                    handle.write(line)
                self.records_written += 1
            return None
        except Exception as exc:
            self.failures += 1
            reason = f"{type(exc).__name__}: {exc}"
            self.last_error = reason
            tool = record.get("tool") if isinstance(record, Mapping) else None
            log.warning(
                "audit record%s could not be written to %s: %s",
                f" of {tool}" if tool else "",
                self.directory,
                reason,
            )
            return f"the audit record of this operation could not be written to {self.directory} ({reason})"


def _encode(entry: dict[str, Any]) -> bytes:
    text = dumps(entry)
    if len(text) > MAX_RECORD_CHARS:
        size = len(text)
        slim = dict(entry)
        for name in BULKY_FIELDS:
            if name in slim:
                slim[name] = f"<omitted: the record had {size} characters>"
                if len(dumps(slim)) <= MAX_RECORD_CHARS:
                    break
        slim["truncated"] = True
        text = dumps(slim)
        if len(text) > MAX_RECORD_CHARS:  # some other field is huge: keep only what identifies the record
            keep = ("ts", "tool", "generation", "pid", "dry_run", "ok", "error_code", "server_pid")
            text = dumps({**{key: slim.get(key) for key in keep}, "truncated": True})
    return (text + "\n").encode("utf-8")
