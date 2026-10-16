"""output.py (PLAN 6.9, 10.1): JSON conversion, pagination and the response size limit."""

from __future__ import annotations

import datetime as dt
import enum
import json
from pathlib import Path

import pytest
import pywintypes  # the fake (tests/fakes)
from mcp.types import CallToolResult, TextContent

from vissim_mcp.errors import ErrorCode, VissimMcpError
from vissim_mcp.output import (
    DEPTH_PLACEHOLDER,
    DEPTH_WARNING,
    MAX_DEPTH,
    NONFINITE_WARNING,
    STRINGS_SHORTENED_WARNING,
    TRUNCATION_HINT,
    dumps,
    error_payload,
    fit_payload,
    ok,
    paginate,
    to_jsonable,
    to_tool_result,
)

# --- to_jsonable


def test_primitives_are_unchanged() -> None:
    for value in (None, True, False, 0, -5, 2**70, "Самарқанд ko'cha", 1.5, ""):
        assert to_jsonable(value) == value
        assert type(to_jsonable(value)) is type(value)


def test_nan_and_inf_become_null_with_a_single_warning() -> None:
    warnings: list[str] = []
    value = [float("nan"), float("inf"), -float("inf"), {"a": float("nan")}, 2.5]
    assert to_jsonable(value, warnings) == [None, None, None, {"a": None}, 2.5]
    assert warnings == [NONFINITE_WARNING]
    assert to_jsonable(float("nan")) is None  # without a warnings list


def test_tuples_and_nested_lists() -> None:
    assert to_jsonable((1, (2, (3, [4, (5,)])))) == [1, [2, [3, [4, [5]]]]]


def nested(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def test_nesting_up_to_the_depth_limit_is_kept() -> None:
    warnings: list[str] = []
    assert to_jsonable(nested(MAX_DEPTH), warnings) == nested(MAX_DEPTH)
    assert warnings == []


def test_nesting_beyond_the_depth_limit_is_replaced() -> None:
    warnings: list[str] = []
    result = to_jsonable(nested(MAX_DEPTH + 5), warnings)
    for _ in range(MAX_DEPTH):
        assert isinstance(result, list)
        result = result[0]
    assert result == DEPTH_PLACEHOLDER
    assert warnings == [DEPTH_WARNING]


def test_self_referencing_structures_are_bounded() -> None:
    loop: list[object] = []
    loop.append(loop)
    json.dumps(to_jsonable(loop), allow_nan=False)


def test_dict_keys_become_strings() -> None:
    value = {1: "a", None: "b", 2.5: "d", (1, 2): "e", "x": {"y": 1}}
    assert to_jsonable(value) == {"1": "a", "null": "b", "2.5": "d", "(1, 2)": "e", "x": {"y": 1}}
    assert to_jsonable({True: "c"}) == {"true": "c"}  # (True and 1 are the same dict key in Python)


def test_dates_and_times_become_iso_strings() -> None:
    assert to_jsonable(dt.datetime(2026, 10, 16, 12, 0, 5)) == "2026-10-16T12:00:05"
    assert to_jsonable(dt.date(2026, 10, 16)) == "2026-10-16"
    assert to_jsonable(dt.time(7, 30)) == "07:30:00"
    aware = dt.datetime(2026, 10, 16, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    assert to_jsonable(aware) == "2026-10-16T12:00:00+05:00"


def test_pywintypes_time_and_time_like_objects() -> None:
    assert to_jsonable(pywintypes.Time(dt.datetime(2026, 1, 2, 3, 4, 5))) == "2026-01-02T03:04:05"

    class OldStyleTime:
        year = 2026

        def Format(self, fmt: str) -> str:
            return "2026-10-16T00:00:00" if fmt == "%Y-%m-%dT%H:%M:%S" else "?"

        def __str__(self) -> str:
            return "not used"

    assert to_jsonable(OldStyleTime()) == "2026-10-16T00:00:00"


def test_bytes_become_hex() -> None:
    assert to_jsonable(b"\x00\xff") == "00ff"
    assert to_jsonable(bytearray(b"\x01")) == "01"
    assert to_jsonable(memoryview(b"\x02")) == "02"


def test_paths_are_never_truncated() -> None:
    path = Path("D:/") / ("x" * 300) / "model.inpx"
    assert to_jsonable(path) == str(path)


def test_unknown_objects_use_str_cut_to_200_characters() -> None:
    class Long:
        def __str__(self) -> str:
            return "z" * 500

    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("no")

    text = to_jsonable(Long())
    assert len(text) == 200 and text.endswith("...")
    assert to_jsonable(Broken()) == "<Broken>"
    assert to_jsonable(3 + 4j) == "(3+4j)"


def test_enums_use_their_value() -> None:
    class Color(enum.IntEnum):
        RED = 1

    assert to_jsonable(ErrorCode.COM_ERROR) == "COM_ERROR"
    assert to_jsonable(Color.RED) == 1 and type(to_jsonable(Color.RED)) is int


def test_sets_become_lists() -> None:
    assert to_jsonable({3, 1, 2}) == [1, 2, 3]
    assert sorted(to_jsonable(frozenset({1, "a"})), key=str) == [1, "a"]


def test_result_is_always_json_safe() -> None:
    messy = {"a": [float("nan"), (1, 2)], "b": {3: dt.date(2026, 1, 1)}, "c": object(), "d": b"\x01"}
    json.dumps(to_jsonable(messy), allow_nan=False)


# --- dumps etc.


def test_dumps_is_compact_and_keeps_unicode() -> None:
    assert dumps({"a": "Самарқанд ko'cha", "b": [1, 2]}) == '{"a":"Самарқанд ko\'cha","b":[1,2]}'


def test_dumps_rejects_nan() -> None:
    with pytest.raises(ValueError):
        dumps({"x": float("nan")})


def test_dumps_replaces_lone_surrogates() -> None:
    text = dumps({"s": "bad \udc80 value"})
    assert "\ufffd" in text
    text.encode("utf-8")
    # ... so that the MCP SDK (pydantic) can serialize the tool result:
    CallToolResult(content=[TextContent(type="text", text=text)]).model_dump_json()


def test_ok_and_error_payloads() -> None:
    assert ok(a=1) == {"ok": True, "a": 1}
    assert ok(a=1, warnings=[]) == {"ok": True, "a": 1}
    assert ok(warnings=["w"]) == {"ok": True, "warnings": ["w"]}
    err = VissimMcpError(ErrorCode.PATH_DENIED, "outside", hint="h")
    assert error_payload(err) == {
        "ok": False,
        "error": {"code": "PATH_DENIED", "message": "outside", "hint": "h"},
    }
    assert error_payload(err, ["w"])["warnings"] == ["w"]
    assert "warnings" not in error_payload(err, [])


def test_to_tool_result_mirrors_ok() -> None:
    success = to_tool_result({"ok": True, "x": "ü"})
    failure = to_tool_result(error_payload(VissimMcpError(ErrorCode.INTERNAL, "x")))
    assert success.isError is False and failure.isError is True
    assert success.content[0].type == "text"
    assert success.content[0].text == '{"ok":true,"x":"ü"}'


# --- paginate


def test_paginate_meta() -> None:
    rows = list(range(10))
    assert paginate(rows, 0, 3, 500) == (
        [0, 1, 2],
        {"total": 10, "offset": 0, "returned": 3, "truncated": True},
    )
    assert paginate(rows, 8, 5, 500) == (
        [8, 9],
        {"total": 10, "offset": 8, "returned": 2, "truncated": False},
    )
    assert paginate(rows, 20, 5, 500) == ([], {"total": 10, "offset": 20, "returned": 0, "truncated": False})
    assert paginate([], 0, 5, 500) == ([], {"total": 0, "offset": 0, "returned": 0, "truncated": False})


def test_paginate_clamps_limit_to_max_rows() -> None:
    page, meta = paginate(list(range(10)), 0, 100, 4)
    assert page == [0, 1, 2, 3]
    assert meta["truncated"] is True


@pytest.mark.parametrize(("offset", "limit"), [(-1, 5), (0, 0), (0, -3), (True, 5), (0, 1.5), ("0", 5)])
def test_paginate_rejects_bad_arguments(offset: object, limit: object) -> None:
    with pytest.raises(VissimMcpError) as info:
        paginate([1, 2, 3], offset, limit, 500)  # type: ignore[arg-type]
    assert info.value.code is ErrorCode.INVALID_ARGUMENT


# --- fit_payload


def size(payload: object) -> int:
    return len(dumps(payload))


def test_small_payload_is_returned_unchanged() -> None:
    payload = ok(rows=[[1]], total=1)
    assert fit_payload(payload, 1000) is payload


def test_rows_are_dropped_from_the_end_until_it_fits() -> None:
    rows = [[i, "x" * 50] for i in range(200)]
    payload = ok(columns=["No", "Name"], rows=rows, total=200, offset=0, returned=200, truncated=False)
    fitted = fit_payload(payload, 2000)
    assert size(fitted) <= 2000
    assert fitted["truncated"] is True
    assert fitted["hint"] == TRUNCATION_HINT
    assert fitted["returned"] == len(fitted["rows"]) > 0
    assert fitted["rows"] == rows[: fitted["returned"]]
    assert fitted["total"] == 200
    one_more = dict(fitted, rows=rows[: fitted["returned"] + 1], returned=fitted["returned"] + 1)
    assert size(one_more) > 2000  # as many rows as possible were kept
    assert len(payload["rows"]) == 200  # input untouched


def test_long_strings_are_shortened_when_there_are_no_rows() -> None:
    payload = ok(text="x" * 10_000, other="short")
    fitted = fit_payload(payload, 2000)
    assert size(fitted) <= 2000
    assert fitted["other"] == "short"
    assert "chars truncated]" in fitted["text"]
    assert STRINGS_SHORTENED_WARNING in fitted["warnings"]
    assert len(payload["text"]) == 10_000


def test_rows_and_strings_together() -> None:
    payload = ok(rows=[[i] for i in range(100)], note="n" * 5000)
    fitted = fit_payload(payload, 1500)
    assert size(fitted) <= 1500
    assert fitted["truncated"] is True


def test_impossible_fit_returns_an_internal_error() -> None:
    payload = ok(items=[{"k": "v" * 150} for _ in range(1000)])
    fitted = fit_payload(payload, 1000)
    assert fitted["ok"] is False
    assert fitted["error"]["code"] == "INTERNAL"
    assert "too large" in fitted["error"]["message"]
    assert size(fitted) < 1000
