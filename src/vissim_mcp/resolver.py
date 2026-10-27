r"""Object paths (PLAN 6.5): ``"Net.SignalControllers[1].SGs[2]"`` -> the COM object.

Grammar (``parse_path``, COM-free)::

    path    = [ "Vissim" ( "." | end ) ] segment ( "." segment )*      # empty or "Vissim" = the root
    segment = NAME [ "[" key "]" ]
    NAME    = [A-Za-z][A-Za-z0-9_]*
    key     = integer | '"' chars '"' | "'" chars "'"                   # backslash escapes the next char

``Net.Links[5]`` is ``vissim.Net.Links.ItemByKey(5)``; ``Net.Nodes["A1"]`` uses the string key ``"A1"``. At
most :data:`MAX_SEGMENTS` segments and :data:`MAX_PATH_CHARS` characters. Names starting with ``_``
(``_oleobj_``, ``__class__``) cannot be written at all.

``resolve`` walks a path with ``getattr`` (a COM property get) and ``ItemByKey``. A segment that yields a
method (``Net.Links.ItemByKey``, ``Exit``) is refused - so a path can never *call* anything but property gets
and ``ItemByKey``. Failures are ``RESOLVE_FAILED`` naming the segment and where it stopped
(``Segment 'SGs' on 'Net.SignalControllers[1]': ...``); a COM error keeps its HRESULT in ``details``. A lost
connection (``RPC_E_DISCONNECTED``, ...) is re-raised unchanged so that the service notices it.
"""

from __future__ import annotations

import re
import types
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .errors import (
    ErrorCode,
    VissimMcpError,
    describe_exception,
    from_com_error,
    is_com_error,
    is_connection_error,
)

MAX_SEGMENTS = 16
MAX_PATH_CHARS = 512
#: Integer keys are limited to the 64-bit range (what a COM VARIANT can carry).
MAX_KEY = 2**63 - 1
ROOT_NAME = "Vissim"

_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_INT_RE = re.compile(r"[+-]?\d+")
_ROOT_RE = re.compile(r"vissim(?=\.|$)", re.IGNORECASE)
_PLAIN_TYPES = (str, bytes, bytearray, int, float, bool)
_RESOLVE_HINT = "Check the path segment by segment with vissim_describe (e.g. vissim_describe(path='Net'))."

Key = int | str


@dataclass(frozen=True)
class Segment:
    """One path segment: a property ``name`` and optionally an ``ItemByKey`` ``key``."""

    name: str
    key: Key | None = None

    def __str__(self) -> str:
        return self.name if self.key is None else f"{self.name}[{_format_key(self.key)}]"


def parse_path(path: str) -> list[Segment]:
    """Parse an object path (see the module docstring); ``INVALID_ARGUMENT`` on any syntax error."""
    if not isinstance(path, str):
        raise _syntax_error(path, f"an object path must be a string, got {type(path).__name__}")
    text = path.strip()
    if len(text) > MAX_PATH_CHARS:
        raise _syntax_error(path, f"longer than {MAX_PATH_CHARS} characters")
    if not text:
        return []
    pos = 0
    root = _ROOT_RE.match(text)
    if root:
        pos = root.end()
        if pos == len(text):
            return []
        pos += 1  # the "." after "Vissim"
    segments: list[Segment] = []
    while True:
        name_match = _NAME_RE.match(text, pos)
        if name_match is None:
            raise _syntax_error(path, _expected_name(text, pos))
        name = name_match.group()
        pos = name_match.end()
        key: Key | None = None
        if pos < len(text) and text[pos] == "[":
            key, pos = _parse_key(path, text, pos + 1)
        segments.append(Segment(name, key))
        if len(segments) > MAX_SEGMENTS:
            raise _syntax_error(path, f"more than {MAX_SEGMENTS} segments")
        if pos == len(text):
            return segments
        if text[pos] != ".":
            raise _syntax_error(path, f"unexpected {text[pos]!r} at position {pos} (expected '.' or '[')")
        pos += 1
        if pos == len(text):
            raise _syntax_error(path, "the path ends with '.'")


def format_path(segments: Sequence[Segment]) -> str:
    """The canonical text of ``segments`` (``"Vissim"`` for the root); ``parse_path`` reads it back."""
    return ".".join(str(segment) for segment in segments) or ROOT_NAME


def is_com_object(value: Any) -> bool:
    """True for a COM object (pywin32 ``CDispatch`` or a test fake): it has ``_oleobj_``. Never calls COM."""
    if value is None or isinstance(value, _PLAIN_TYPES):
        return False
    try:
        return hasattr(value, "_oleobj_")
    except Exception:
        return False


def resolve(root: Any, path: str | Sequence[Segment]) -> Any:
    """Walk ``path`` from the COM ``root``: ``getattr`` per segment, then ``ItemByKey(key)`` for a key."""
    segments = parse_path(path) if isinstance(path, str) else list(path)
    current = root
    for index, segment in enumerate(segments):
        parent = format_path(segments[:index])
        if not is_com_object(current):
            raise _resolve_error(
                segment, parent, f"'{parent}' is a value ({type(current).__name__}), not an object"
            )
        try:
            value = getattr(current, segment.name)
        except AttributeError:
            raise _resolve_error(segment, parent, f"'{parent}' has no property '{segment.name}'") from None
        except Exception as exc:
            raise _resolve_failure(segment, parent, exc) from None
        if isinstance(value, types.MethodType) or (callable(value) and not is_com_object(value)):
            raise _resolve_error(
                segment,
                parent,
                f"'{segment.name}' is a method, not a property; object paths may only contain properties "
                "and keys",
            )
        if segment.key is not None:
            if not is_com_object(value):
                raise _resolve_error(segment, parent, f"'{segment.name}' is a value, not a collection")
            try:
                item_by_key = value.ItemByKey
            except AttributeError:
                raise _resolve_error(
                    segment, parent, f"'{segment.name}' is not a collection (it has no ItemByKey)"
                ) from None
            except Exception as exc:
                raise _resolve_failure(segment, parent, exc) from None
            try:
                value = item_by_key(segment.key)
            except Exception as exc:
                raise _resolve_failure(
                    segment, parent, exc, prefix=f"ItemByKey({_format_key(segment.key)}) failed"
                ) from None
        current = value
    return current


# --- helpers


def _parse_key(path: str, text: str, pos: int) -> tuple[Key, int]:
    while pos < len(text) and text[pos] == " ":
        pos += 1
    if pos >= len(text):
        raise _syntax_error(path, "unterminated '[' (expected a key and ']')")
    key: Key
    quote = text[pos]
    if quote in "\"'":
        pos += 1
        chars: list[str] = []
        while True:
            if pos >= len(text):
                raise _syntax_error(path, "unterminated string key")
            char = text[pos]
            if char == "\\":
                if pos + 1 >= len(text):
                    raise _syntax_error(path, "unterminated string key")
                chars.append(text[pos + 1])
                pos += 2
                continue
            if char == quote:
                pos += 1
                break
            chars.append(char)
            pos += 1
        key = "".join(chars)
    else:
        match = _INT_RE.match(text, pos)
        if match is None:
            raise _syntax_error(
                path, f'keys must be integers or quoted strings (e.g. Net.Nodes["A1"]), at position {pos}'
            )
        number = int(match.group())
        if abs(number) > MAX_KEY:
            raise _syntax_error(path, "integer key out of range")
        key = number
        pos = match.end()
    while pos < len(text) and text[pos] == " ":
        pos += 1
    if pos >= len(text) or text[pos] != "]":
        raise _syntax_error(path, f"expected ']' at position {pos}")
    return key, pos + 1


def _expected_name(text: str, pos: int) -> str:
    if pos < len(text) and text[pos] == "_":
        return f"names starting with '_' are not allowed (position {pos})"
    if pos < len(text) and text[pos] == ".":
        return f"empty segment at position {pos}"
    if pos < len(text):
        return (
            f"expected a name (a letter, then letters, digits or '_') at position {pos}, found {text[pos]!r}"
        )
    return "expected a name"


def _format_key(key: Key) -> str:
    if isinstance(key, int):
        return str(key)
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _syntax_error(path: Any, reason: str) -> VissimMcpError:
    shown = path if isinstance(path, str) and len(path) <= 120 else (str(path)[:117] + "...")
    return VissimMcpError(
        ErrorCode.INVALID_ARGUMENT,
        f"invalid object path {shown!r}: {reason}",
        hint='Paths look like Net.Links[5], Net.SignalControllers[1].SGs[2], Net.Nodes["A1"] or Simulation.',
    )


def _resolve_error(
    segment: Segment, parent: str, reason: str, details: dict[str, Any] | None = None
) -> VissimMcpError:
    info = {"segment": str(segment), "on": parent, **(details or {})}
    return VissimMcpError(
        ErrorCode.RESOLVE_FAILED,
        f"Segment '{segment}' on '{parent}': {reason}",
        hint=_RESOLVE_HINT,
        details=info,
    )


def _resolve_failure(
    segment: Segment, parent: str, exc: Exception, prefix: str | None = None
) -> BaseException:
    """``RESOLVE_FAILED`` for ``exc`` - except a lost connection, which is returned unchanged (re-raised)."""
    if is_connection_error(exc):
        return exc
    if isinstance(exc, VissimMcpError):
        return exc
    reason = describe_exception(exc, limit=300)
    if prefix:
        reason = f"{prefix}: {reason}"
    details = from_com_error(exc).details if is_com_error(exc) else None
    return _resolve_error(segment, parent, reason, details)
