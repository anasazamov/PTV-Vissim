"""vissim_mcp.messages: what Vissim wrote to its error files and its message log."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult

from fake_vissim import DISP_E_EXCEPTION
from service_harness import Harness
from vissim_mcp import messages
from vissim_mcp.server import build_server

TAB = chr(9)


def _stamp(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def _line(when: float, level: str, *fields: str) -> str:
    return TAB + TAB.join([_stamp(when), level.rjust(26), *fields])


def test_log_messages_keep_what_is_not_routine(tmp_path: Path) -> None:
    now = time.time()
    log = tmp_path / "vissim_msgs.txt"
    lines = [_line(now - 600, "Система", "Command line: Vissim260.exe -Embedding")]
    lines += [_line(now - 600, "Ошибка", "an old error of an earlier call")]
    lines += [_line(now, "Система", f"Einlesen von Modell {index}.u3dm...") for index in range(30)]
    lines += [
        _line(
            now, "Ошибка", "Максимальное количество светосигнальных устройств (20) превышено.", "Ключи: 50"
        ),
        _line(now, "Ошибка", "Не удалось прочесть файл model.inpx."),
        _line(now, "Указание", "Процесс вызвал ошибку."),
        "a line without a time stamp",
    ]
    log.write_text(chr(0xFEFF) + "\n".join(lines) + "\n", encoding="utf-8")
    assert messages.log_messages(now, log) == [
        "Ошибка | Максимальное количество светосигнальных устройств (20) превышено. | Ключи: 50",
        "Ошибка | Не удалось прочесть файл model.inpx.",
        "Указание | Процесс вызвал ошибку.",
    ]
    assert messages.log_messages(now + 3600, log) == []
    assert messages.log_messages(now, tmp_path / "missing.txt") == []


def test_log_messages_are_bounded(tmp_path: Path) -> None:
    now = time.time()
    log = tmp_path / "vissim_msgs.txt"
    lines = [_line(now, "System", f"routine {index}") for index in range(100)]
    lines += [_line(now, "Error", "x" * 2000 + f" {index}") for index in range(50)]
    log.write_text("\n".join(lines), encoding="utf-8")
    found = messages.log_messages(now, log)
    assert 0 < len(found) <= messages.MAX_LINES
    assert sum(len(line) for line in found) <= messages.MAX_CHARS
    assert all(len(line) <= messages.MAX_LINE_CHARS for line in found)


def test_error_file_messages(tmp_path: Path) -> None:
    now = time.time()
    (tmp_path / "net_001.err").write_text(
        chr(0xFEFF) + "  Error" + TAB + "first" + TAB + "obj 1\n", encoding="utf-8"
    )
    old = tmp_path / "net_000.err"
    old.write_text("old", encoding="utf-8")
    os.utime(old, (now - 3600, now - 3600))
    (tmp_path / "other.err").write_text("not ours", encoding="utf-8")
    assert messages.error_file_messages(tmp_path, "Net.inpx", now) == ["Error | first | obj 1"]
    assert messages.error_file_messages(tmp_path, "Net.inpx", now + 3600) == []
    assert messages.error_file_messages(tmp_path / "missing", "net.inpx", now) == []
    assert messages.error_file_messages(None, "net.inpx", now) == []
    assert messages.error_file_messages(tmp_path, "", now) == []


def test_the_real_message_log_is_never_read_in_test_mode() -> None:
    assert messages.message_log_path() is None
    assert messages.log_messages(0.0) == []


async def test_a_failed_load_reports_what_vissim_logged(
    harness: Callable[..., Harness],
    isolated_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Vissim (M6): "The network file could not be loaded." - the reason, here the license limit of 20
    signal controllers, is only in the message log."""
    log = tmp_path / "vissim_msgs.txt"
    monkeypatch.setattr(messages, "message_log_path", lambda: log)
    model = isolated_env["projects"] / "big.inpx"
    model.write_text("<network/>", encoding="utf-8")
    h = harness()
    mcp = build_server(h.service, h.settings)

    async def call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await mcp.call_tool(name, arguments or {})
        assert isinstance(result, CallToolResult)
        payload: dict[str, Any] = json.loads(result.content[0].text)
        return payload

    assert (await call("vissim_connect"))["ok"] is True
    now = time.time()
    log.write_text(
        "\n".join(
            [_line(now, "Система", f"routine {index}") for index in range(5)]
            + [_line(now, "Ошибка", "Максимальное количество светосигнальных устройств (20) превышено.")]
        ),
        encoding="utf-8",
    )
    h.vissim.raise_com_error_next(DISP_E_EXCEPTION, "The network file could not be loaded.")
    failed = await call("vissim_load_network", {"inpx_path": "big.inpx"})
    assert failed["error"]["code"] == "COM_ERROR", failed
    reported = failed["error"]["details"]["vissim_messages"]
    assert reported == ["Ошибка | Максимальное количество светосигнальных устройств (20) превышено."]
    assert failed["error"]["message"].endswith("Vissim reported: " + reported[0])
    assert "vissim_messages" in failed["error"]["hint"]
