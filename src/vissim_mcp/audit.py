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
import sys
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
    """Thread-safe appender of JSONL audit records (one file per month, local time).

    Several server processes may append to the same monthly file: every record is written by one append-only
    write (:func:`_append`), so concurrent records never overwrite each other."""

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
                _append(path, line)
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


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE
    _WriteFile = _kernel32.WriteFile
    _WriteFile.argtypes = (
        wintypes.HANDLE,
        ctypes.c_char_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _WriteFile.restype = wintypes.BOOL
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _FILE_APPEND_DATA = 0x0004
    _SYNCHRONIZE = 0x00100000
    _SHARE_ALL = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    _OPEN_ALWAYS = 4
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE = wintypes.HANDLE(-1).value

    def _append(path: str | os.PathLike[str], data: bytes) -> None:
        """Append ``data`` with one ``WriteFile`` on a handle opened with ``FILE_APPEND_DATA`` only.

        Python's ``open(path, "ab")`` emulates O_APPEND in the C runtime (seek to the end, then write), which
        is not atomic across processes: two servers writing at once can both seek to the same end and one
        record overwrites the other. With append-only access Windows itself places every write at the end.
        """
        handle = _CreateFileW(
            os.fspath(path),
            _FILE_APPEND_DATA | _SYNCHRONIZE,
            _SHARE_ALL,
            None,
            _OPEN_ALWAYS,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle is None or handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            written = wintypes.DWORD(0)
            if not _WriteFile(handle, data, len(data), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if written.value != len(data):
                raise OSError(f"short write to {os.fspath(path)}: {written.value} of {len(data)} bytes")
        finally:
            _CloseHandle(handle)

else:

    def _append(path: str | os.PathLike[str], data: bytes) -> None:
        """Append ``data`` (POSIX ``O_APPEND`` writes are atomic across processes)."""
        with open(path, "ab") as handle:
            handle.write(data)
