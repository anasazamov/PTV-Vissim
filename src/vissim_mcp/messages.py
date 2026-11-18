"""What Vissim itself reported: its error files and its message log (EXTENSION after M6).

A COM error of Vissim is often only "The network file could not be loaded." or "The simulation could not be
continued."; the reason is in the Messages window, which COM does not expose. Vissim writes the same lines to

* ``<network>_NNN.err`` (or ``<network>.err``) next to the network file, for a run that could not start;
* ``%TEMP%\\VISSIM\\vissim_msgs.txt``, its running message log - e.g. for a network that could not be loaded:
  "the maximum number of signal controllers (20) for this Vissim license is exceeded".

Both readers are best effort: they never raise and return ``[]`` when nothing is found. Level names in the log
are in the language of the Vissim user interface, so levels are not interpreted: the most frequent level of
the log (the "System" chatter) is left out and everything else written since the failing call is returned.
This module never imports ``pythoncom`` / ``win32com`` and makes no COM call.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from collections import Counter
from pathlib import Path

from .config import forbid_real_enabled

log = logging.getLogger(__name__)

MAX_LINES = 20
MAX_CHARS = 2000
MAX_LINE_CHARS = 500
#: Only the end of the message log is read (it grows during a session).
LOG_TAIL_BYTES = 512_000
#: Clock tolerance between the caller's ``time.time()`` and the time stamps Vissim writes (whole seconds).
SLACK_S = 2.0
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def message_log_path() -> Path | None:
    """Vissim's message log of the current user (written by every Vissim instance of this user).

    None in test mode (``VISSIM_MCP_FORBID_REAL``): the test suite must not read what a real Vissim on this
    machine is writing."""
    if forbid_real_enabled():
        return None
    return Path(tempfile.gettempdir()) / "VISSIM" / "vissim_msgs.txt"


def _fields(line: str) -> list[str]:
    return [part.strip() for part in line.split("\t") if part.strip()]


def _bounded(lines: list[str]) -> list[str]:
    kept: list[str] = []
    total = 0
    for line in lines[:MAX_LINES]:
        text = line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 3] + "..."
        total += len(text)
        if total > MAX_CHARS:
            break
        kept.append(text)
    return kept


def error_file_messages(
    folder: str | os.PathLike[str] | None, network_file: str | None, since: float
) -> list[str]:
    """Lines of the newest ``<network stem>*.err`` in ``folder`` written since ``since`` (``time.time()``)."""
    if not folder or not network_file:
        return []
    try:
        stem = os.path.splitext(os.path.basename(network_file))[0].lower()
        newest: tuple[float, str] | None = None
        with os.scandir(folder) as entries:
            for entry in entries:
                lowered = entry.name.lower()
                if not (lowered.startswith(stem) and lowered.endswith(".err") and entry.is_file()):
                    continue
                modified = entry.stat().st_mtime
                if modified >= since - SLACK_S and (newest is None or modified > newest[0]):
                    newest = (modified, entry.path)
        if newest is None:
            return []
        with open(newest[1], encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read(MAX_CHARS * 4)
    except Exception:
        log.debug("reading the Vissim error file failed", exc_info=True)
        return []
    return _bounded([" | ".join(fields) for fields in map(_fields, text.splitlines()) if fields])


def log_messages(since: float, path: str | os.PathLike[str] | None = None) -> list[str]:
    """Non-routine lines Vissim wrote to its message log since ``since`` (``time.time()``), oldest first."""
    try:
        target = Path(path) if path is not None else message_log_path()
        if target is None:
            return []
        with open(target, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8-sig", errors="replace")
    except Exception:
        log.debug("reading the Vissim message log failed", exc_info=True)
        return []
    entries: list[tuple[float, str, str]] = []
    for line in text.splitlines():
        fields = _fields(line)
        if len(fields) < 3 or not _STAMP_RE.fullmatch(fields[0]):
            continue
        try:
            stamp = time.mktime(time.strptime(fields[0], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        entries.append((stamp, fields[1], " | ".join(fields[2:])))
    if not entries:
        return []
    routine = Counter(level for _stamp, level, _message in entries).most_common(1)[0][0]
    recent = [
        f"{level} | {message}"
        for stamp, level, message in entries
        if stamp >= since - SLACK_S and level != routine
    ]
    return _bounded(recent[-MAX_LINES:])
