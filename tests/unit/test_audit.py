"""audit.py (PLAN 6.8, 10.1 row "audit"): JSONL record format, one file per month, never raises."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from vissim_mcp import audit as audit_module
from vissim_mcp.audit import AuditLog
from vissim_mcp.service import AUDIT_CORE_FIELDS, VissimService

ISO_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}$")


def read_records(directory: Path) -> list[dict[str, object]]:
    files = sorted(directory.glob("audit-*.jsonl"))
    records: list[dict[str, object]] = []
    for file in files:
        for line in file.read_text(encoding="utf-8").splitlines():
            records.append(json.loads(line))
    return records


def test_record_format_and_monthly_file(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit")
    assert log.write({"tool": "vissim_set_attributes", "args": {"path": "Net.Links[1]"}, "ok": True}) is None
    assert log.write({"tool": "vissim_save_network", "sha256": "ab" * 32, "ok": True}) is None
    expected = tmp_path / "audit" / f"audit-{datetime.now():%Y-%m}.jsonl"
    assert expected.is_file()
    lines = expected.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert list(first)[0] == "ts"
    assert ISO_LOCAL.match(str(first["ts"])), first["ts"]
    assert first["tool"] == "vissim_set_attributes" and first["args"] == {"path": "Net.Links[1]"}
    assert isinstance(first["server_pid"], int)
    assert log.records_written == 2 and log.failures == 0


def test_path_for_uses_the_month() -> None:
    log = AuditLog("X")
    assert log.path_for(datetime(2026, 1, 31)).name == "audit-2026-01.jsonl"
    assert log.path_for(datetime(2026, 12, 1)).name == "audit-2026-12.jsonl"


def test_values_are_made_json_safe_and_utf8(tmp_path: Path) -> None:
    log = AuditLog(tmp_path)
    assert (
        log.write(
            {
                "tool": "t",
                "new": {"Name": "Samarqand ko'chasi — Самарканд", "x": math.nan, "t": (1, 2)},
                "ts": "ignored",
            }
        )
        is None
    )
    raw = next(tmp_path.glob("audit-*.jsonl")).read_bytes()
    assert "Самарканд".encode() in raw  # UTF-8, not \u escapes
    record = read_records(tmp_path)[0]
    assert record["new"] == {"Name": "Samarqand ko'chasi — Самарканд", "x": None, "t": [1, 2]}
    assert record["ts"] != "ignored"


def test_disk_error_returns_a_warning_and_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "audit"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    log = AuditLog(blocker)
    warning = log.write({"tool": "vissim_save_network", "ok": True})
    assert warning is not None and "could not be written" in warning
    assert log.failures == 1 and log.last_error


def test_unserialisable_record_never_raises(tmp_path: Path) -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("no text")

    log = AuditLog(tmp_path)
    assert log.write({"tool": "t", "value": Broken()}) is None  # to_jsonable falls back to a placeholder
    assert log.write(None) is not None  # type: ignore[arg-type]


def test_oversized_record_drops_value_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit_module, "MAX_RECORD_CHARS", 500)
    log = AuditLog(tmp_path)
    assert log.write({"tool": "bulk", "ok": True, "old": {"k": "v" * 2000}, "new": {"k": "w" * 10}}) is None
    record = read_records(tmp_path)[0]
    assert record["truncated"] is True and record["ok"] is True and record["tool"] == "bulk"
    assert str(record["old"]).startswith("<omitted")


async def test_service_write_audit_fills_core_fields(
    service: VissimService, isolated_env: dict[str, Path]
) -> None:
    assert service.write_audit("vissim_x", {"args": {"a": 1}, "extra": 5}) is None
    record = read_records(isolated_env["audit"])[-1]
    for name in ("tool", "generation", "pid", *AUDIT_CORE_FIELDS):
        assert name in record, name
    assert record["tool"] == "vissim_x" and record["extra"] == 5 and record["old"] is None


async def test_service_write_audit_survives_a_raising_log(service: VissimService) -> None:
    class Exploding:
        def write(self, record: object) -> None:
            raise OSError("disk on fire")

    service.audit = Exploding()
    warning = service.write_audit("vissim_x", {})
    assert warning is not None and "disk on fire" in warning
    service.audit = None
    assert "disabled" in str(service.write_audit("vissim_x", {}))


_WRITER = """
import sys
from vissim_mcp.audit import AuditLog
log = AuditLog(sys.argv[1])
for index in range(int(sys.argv[3])):
    assert log.write({"tool": "t", "writer": sys.argv[2], "index": index, "pad": "x" * 300}) is None
"""


def test_several_processes_share_the_monthly_file_without_losing_records(tmp_path: Path) -> None:
    """Python's "ab" mode is seek-then-write on Windows; concurrent servers lost records with it."""
    directory = tmp_path / "audit"
    src = Path(__file__).resolve().parents[2] / "src"
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONUTF8": "1"}
    writers, per_writer = 4, 250
    procs = [
        subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", _WRITER, str(directory), str(n), str(per_writer)], env=env
        )
        for n in range(writers)
    ]
    assert [proc.wait(timeout=60) for proc in procs] == [0] * writers
    records = read_records(directory)
    assert len(records) == writers * per_writer
    assert {(r["writer"], r["index"]) for r in records} == {
        (str(n), i) for n in range(writers) for i in range(per_writer)
    }
