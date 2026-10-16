"""Configuration from environment variables (PLAN 5).

Everything is read once at start-up into the frozen :class:`Settings` dataclass. Invalid values raise
:class:`ConfigError` naming the variable; ``python -m vissim_mcp`` prints it to stderr and exits with code 2.

Rules:

* A variable that is missing or blank uses its default - except ``VISSIM_EXE_PATTERN``, where a blank value is
  an error (an empty process-name pattern is dangerous; PLAN 10.1).
* Booleans accept ``1/0/true/false/yes/no/on/off`` (case-insensitive).
* Timeouts are seconds, ``> 0`` and ``<= 86400``; ``VISSIM_WAIT_MAX_S`` must stay below 120 s (Claude Code
  moves tool calls longer than 2 minutes to the background).
* Directory lists are separated by ``;``; blank items and surrounding quotes are dropped; every path is
  ``expanduser().resolve(strict=False)``.
* ``VISSIM_ALLOWED_DIRS`` falls back to ``CLAUDE_PROJECT_DIR``; if both are empty every file operation is
  denied (fail-closed). ``VISSIM_READONLY_DIRS`` defaults to ``<root>\\models\\original`` for every allowed
  root.
* ``VISSIM_EXE_PATTERN`` is compiled with ``re.IGNORECASE`` and is meant to be applied with ``fullmatch`` to
  process names; a pattern that matches core system processes is rejected.
* Non-fatal findings (unknown ``VISSIM_*`` variables, no allowed directories, ...) are collected in
  ``Settings.warnings`` and shown by ``vissim_health``.
"""

from __future__ import annotations

import difflib
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Invalid configuration value. The message always starts with the variable name."""


ENV_PROGID = "VISSIM_PROGID"
ENV_ALLOWED_DIRS = "VISSIM_ALLOWED_DIRS"
ENV_READONLY_DIRS = "VISSIM_READONLY_DIRS"
ENV_CALL_TIMEOUT_S = "VISSIM_CALL_TIMEOUT_S"
ENV_CONNECT_TIMEOUT_S = "VISSIM_CONNECT_TIMEOUT_S"
ENV_LOAD_TIMEOUT_S = "VISSIM_LOAD_TIMEOUT_S"
ENV_CHUNK_SIM_S = "VISSIM_CHUNK_SIM_S"
ENV_WAIT_MAX_S = "VISSIM_WAIT_MAX_S"
ENV_EXE_PATTERN = "VISSIM_EXE_PATTERN"
ENV_PID_DETECT_S = "VISSIM_PID_DETECT_S"
ENV_KILL_ON_EXIT = "VISSIM_KILL_ON_EXIT"
ENV_USE_JOB_OBJECT = "VISSIM_USE_JOB_OBJECT"
ENV_MAX_ROWS = "VISSIM_MAX_ROWS"
ENV_MAX_RESULT_CHARS = "VISSIM_MAX_RESULT_CHARS"
ENV_ALLOW_PATH_ATTRS = "VISSIM_ALLOW_PATH_ATTRS"
ENV_ALLOW_EXEC = "VISSIM_MCP_ALLOW_EXEC"
ENV_LOG_DIR = "VISSIM_LOG_DIR"
ENV_AUDIT_DIR = "VISSIM_AUDIT_DIR"
ENV_SEED_ATTR = "VISSIM_SEED_ATTR"
ENV_BREAK_DISABLE_VALUE = "VISSIM_BREAK_DISABLE_VALUE"
ENV_PROJECT_DIR = "CLAUDE_PROJECT_DIR"
#: Test-only safety switch (see tests/conftest.py and the real COM backend); not a user setting.
ENV_FORBID_REAL = "VISSIM_MCP_FORBID_REAL"

#: The PLAN 5 variables, in table order.
PLAN_ENV_VARS: tuple[str, ...] = (
    ENV_PROGID,
    ENV_ALLOWED_DIRS,
    ENV_READONLY_DIRS,
    ENV_CALL_TIMEOUT_S,
    ENV_CONNECT_TIMEOUT_S,
    ENV_LOAD_TIMEOUT_S,
    ENV_CHUNK_SIM_S,
    ENV_WAIT_MAX_S,
    ENV_EXE_PATTERN,
    ENV_PID_DETECT_S,
    ENV_KILL_ON_EXIT,
    ENV_USE_JOB_OBJECT,
    ENV_MAX_ROWS,
    ENV_MAX_RESULT_CHARS,
    ENV_ALLOW_PATH_ATTRS,
    ENV_ALLOW_EXEC,
    ENV_LOG_DIR,
    ENV_AUDIT_DIR,
    ENV_SEED_ATTR,
    ENV_BREAK_DISABLE_VALUE,
)
_KNOWN_VISSIM_VARS = frozenset((*PLAN_ENV_VARS, ENV_FORBID_REAL))

DEFAULT_PROGID = "Vissim.Vissim"
DEFAULT_EXE_PATTERN = r"^vissim\d*\.exe$"
DEFAULT_SEED_ATTR = "RandSeed"
DEFAULT_BREAK_DISABLE_VALUE = 0
DEFAULT_CALL_TIMEOUT_S = 120.0
DEFAULT_CONNECT_TIMEOUT_S = 180.0
DEFAULT_LOAD_TIMEOUT_S = 600.0
DEFAULT_CHUNK_SIM_S = 60
DEFAULT_WAIT_MAX_S = 90.0
DEFAULT_PID_DETECT_S = 20.0
DEFAULT_MAX_ROWS = 500
DEFAULT_MAX_RESULT_CHARS = 40_000

MAX_TIMEOUT_S = 86_400.0
WAIT_MAX_LIMIT_S = 120.0
MAX_SIM_PERIOD_S = 2_678_400
MIN_RESULT_CHARS = 1_000
CLAUDE_CODE_FILE_THRESHOLD_CHARS = 50_000

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})

_PROGID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]+)*$")
_CLSID_RE = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")
_PROGID_MAX_LEN = 39  # Windows limit for ProgIDs
_ATTR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

#: Process names that a valid VISSIM_EXE_PATTERN must never match (fail-safe for the kill logic).
_FORBIDDEN_EXE_NAMES = (
    "explorer.exe",
    "python.exe",
    "pythonw.exe",
    "py.exe",
    "svchost.exe",
    "csrss.exe",
    "winlogon.exe",
    "lsass.exe",
    "services.exe",
    "dllhost.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "node.exe",
    "claude.exe",
    "codemeter.exe",
)
#: Typical Vissim executable names; a pattern matching none of them only produces a warning.
_TYPICAL_VISSIM_EXE_NAMES = ("Vissim.exe", "Vissim260.exe", "VISSIM250.exe", "vissim2025.exe")


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Validated server configuration (one field per PLAN 5 variable, plus derived values)."""

    progid: str
    allowed_dirs: tuple[Path, ...]
    readonly_dirs: tuple[Path, ...]
    call_timeout_s: float
    connect_timeout_s: float
    load_timeout_s: float
    chunk_sim_s: int
    wait_max_s: float
    exe_pattern: re.Pattern[str]
    pid_detect_s: float
    kill_on_exit: bool
    use_job_object: bool
    max_rows: int
    max_result_chars: int
    allow_path_attrs: bool
    allow_exec: bool
    log_dir: Path
    audit_dir: Path
    seed_attr: str
    break_disable_value: int | float
    project_dir: Path | None
    allowed_dirs_source: str
    warnings: tuple[str, ...] = ()

    @property
    def job_object_enabled(self) -> bool:
        """PLAN 6.4: the Job Object is only used together with VISSIM_KILL_ON_EXIT=1."""
        return self.use_job_object and self.kill_on_exit

    @property
    def file_operations_enabled(self) -> bool:
        """False when no allowed directory is configured (fail-closed)."""
        return bool(self.allowed_dirs)

    def public_dict(self) -> dict[str, Any]:
        """JSON-able view for ``vissim_health`` and ``--check`` (nothing here is secret)."""
        return {
            ENV_PROGID: self.progid,
            ENV_ALLOWED_DIRS: [str(p) for p in self.allowed_dirs],
            "allowed_dirs_source": self.allowed_dirs_source,
            ENV_READONLY_DIRS: [str(p) for p in self.readonly_dirs],
            ENV_CALL_TIMEOUT_S: self.call_timeout_s,
            ENV_CONNECT_TIMEOUT_S: self.connect_timeout_s,
            ENV_LOAD_TIMEOUT_S: self.load_timeout_s,
            ENV_CHUNK_SIM_S: self.chunk_sim_s,
            ENV_WAIT_MAX_S: self.wait_max_s,
            ENV_EXE_PATTERN: self.exe_pattern.pattern,
            ENV_PID_DETECT_S: self.pid_detect_s,
            ENV_KILL_ON_EXIT: self.kill_on_exit,
            ENV_USE_JOB_OBJECT: self.use_job_object,
            "job_object_enabled": self.job_object_enabled,
            ENV_MAX_ROWS: self.max_rows,
            ENV_MAX_RESULT_CHARS: self.max_result_chars,
            ENV_ALLOW_PATH_ATTRS: self.allow_path_attrs,
            ENV_ALLOW_EXEC: self.allow_exec,
            ENV_LOG_DIR: str(self.log_dir),
            ENV_AUDIT_DIR: str(self.audit_dir),
            ENV_SEED_ATTR: self.seed_attr,
            ENV_BREAK_DISABLE_VALUE: self.break_disable_value,
            ENV_PROJECT_DIR: str(self.project_dir) if self.project_dir is not None else None,
        }


def is_valid_progid(value: str) -> bool:
    """True for a syntactically valid ProgID (``Vissim.Vissim.2026``) or CLSID string (``{...}``)."""
    if _CLSID_RE.match(value):
        return True
    return len(value) <= _PROGID_MAX_LEN and bool(_PROGID_RE.match(value))


def forbid_real_enabled(env: Mapping[str, str] | None = None) -> bool:
    """True while the test-only safety switch ``VISSIM_MCP_FORBID_REAL`` is set (any value but
    0/false/no/off).

    Read at call time (not part of :class:`Settings`): the real COM backend then refuses to dispatch unless
    the pywin32 modules are the test fakes, and ``winproc`` never kills or jobs a process named like real
    Vissim.
    """
    value = (os.environ if env is None else env).get(ENV_FORBID_REAL, "")
    return value.strip().lower() not in ("", *_FALSE_WORDS)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read and validate the configuration from ``env`` (default: ``os.environ``)."""
    if env is None:
        env = os.environ
    warnings: list[str] = []

    progid = _get(env, ENV_PROGID) or DEFAULT_PROGID
    if not is_valid_progid(progid):
        raise ConfigError(
            f"{ENV_PROGID}: invalid ProgID {progid!r} (expected e.g. 'Vissim.Vissim' or "
            f"'Vissim.Vissim.2026', at most {_PROGID_MAX_LEN} characters, or a CLSID in braces)"
        )

    project_dir_raw = _get(env, ENV_PROJECT_DIR)
    project_dir = _parse_path(ENV_PROJECT_DIR, project_dir_raw, warnings) if project_dir_raw else None

    allowed_raw = _get(env, ENV_ALLOWED_DIRS)
    allowed_dirs: tuple[Path, ...] = ()
    source = "none"
    if allowed_raw is not None:
        allowed_dirs = _parse_path_list(ENV_ALLOWED_DIRS, allowed_raw, warnings)
        source = ENV_ALLOWED_DIRS
    if not allowed_dirs and project_dir is not None:
        allowed_dirs = (project_dir,)
        source = ENV_PROJECT_DIR
    if not allowed_dirs:
        source = "none"
        warnings.append(
            f"no allowed directories ({ENV_ALLOWED_DIRS} and {ENV_PROJECT_DIR} are empty): "
            "every file operation will be denied"
        )

    readonly_raw = _get(env, ENV_READONLY_DIRS)
    if readonly_raw is not None:
        readonly_dirs = _parse_path_list(ENV_READONLY_DIRS, readonly_raw, warnings)
    else:
        readonly_dirs = tuple(
            _resolve(ENV_READONLY_DIRS, root / "models" / "original") for root in allowed_dirs
        )

    call_timeout_s = _parse_seconds(env, ENV_CALL_TIMEOUT_S, DEFAULT_CALL_TIMEOUT_S)
    connect_timeout_s = _parse_seconds(env, ENV_CONNECT_TIMEOUT_S, DEFAULT_CONNECT_TIMEOUT_S)
    load_timeout_s = _parse_seconds(env, ENV_LOAD_TIMEOUT_S, DEFAULT_LOAD_TIMEOUT_S)
    pid_detect_s = _parse_seconds(env, ENV_PID_DETECT_S, DEFAULT_PID_DETECT_S)
    wait_max_s = _parse_seconds(env, ENV_WAIT_MAX_S, DEFAULT_WAIT_MAX_S, below=WAIT_MAX_LIMIT_S)
    chunk_sim_s = _parse_int(env, ENV_CHUNK_SIM_S, DEFAULT_CHUNK_SIM_S, minimum=1, maximum=MAX_SIM_PERIOD_S)
    max_rows = _parse_int(env, ENV_MAX_ROWS, DEFAULT_MAX_ROWS, minimum=1)
    max_result_chars = _parse_int(
        env, ENV_MAX_RESULT_CHARS, DEFAULT_MAX_RESULT_CHARS, minimum=MIN_RESULT_CHARS
    )
    if max_result_chars > CLAUDE_CODE_FILE_THRESHOLD_CHARS:
        warnings.append(
            f"{ENV_MAX_RESULT_CHARS}={max_result_chars} is above {CLAUDE_CODE_FILE_THRESHOLD_CHARS}: "
            "Claude Code saves such long tool results to a file instead of showing them to the model"
        )

    exe_pattern = _parse_exe_pattern(env, warnings)

    kill_on_exit = _parse_bool(env, ENV_KILL_ON_EXIT, True)
    use_job_object = _parse_bool(env, ENV_USE_JOB_OBJECT, True)
    allow_path_attrs = _parse_bool(env, ENV_ALLOW_PATH_ATTRS, False)
    allow_exec = _parse_bool(env, ENV_ALLOW_EXEC, False)

    state_dir = _default_state_dir(env)
    log_dir_raw = _get(env, ENV_LOG_DIR)
    log_dir = (
        _parse_path(ENV_LOG_DIR, log_dir_raw, warnings)
        if log_dir_raw
        else _resolve(ENV_LOG_DIR, state_dir / "logs")
    )
    audit_dir_raw = _get(env, ENV_AUDIT_DIR)
    audit_dir = (
        _parse_path(ENV_AUDIT_DIR, audit_dir_raw, warnings)
        if audit_dir_raw
        else _resolve(ENV_AUDIT_DIR, state_dir / "audit")
    )

    seed_attr = _get(env, ENV_SEED_ATTR) or DEFAULT_SEED_ATTR
    if not _ATTR_NAME_RE.match(seed_attr):
        raise ConfigError(
            f"{ENV_SEED_ATTR}: invalid attribute name {seed_attr!r} "
            "(letters, digits and '_', starting with a letter)"
        )

    break_disable_value = _parse_break_disable_value(env)

    warnings.extend(_unknown_variable_warnings(env))

    return Settings(
        progid=progid,
        allowed_dirs=allowed_dirs,
        readonly_dirs=readonly_dirs,
        call_timeout_s=call_timeout_s,
        connect_timeout_s=connect_timeout_s,
        load_timeout_s=load_timeout_s,
        chunk_sim_s=chunk_sim_s,
        wait_max_s=wait_max_s,
        exe_pattern=exe_pattern,
        pid_detect_s=pid_detect_s,
        kill_on_exit=kill_on_exit,
        use_job_object=use_job_object,
        max_rows=max_rows,
        max_result_chars=max_result_chars,
        allow_path_attrs=allow_path_attrs,
        allow_exec=allow_exec,
        log_dir=log_dir,
        audit_dir=audit_dir,
        seed_attr=seed_attr,
        break_disable_value=break_disable_value,
        project_dir=project_dir,
        allowed_dirs_source=source,
        warnings=tuple(warnings),
    )


# --- parsing helpers


def _get(env: Mapping[str, str], name: str) -> str | None:
    """The stripped value of ``name``, or None when it is missing or blank."""
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    word = raw.lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(f"{name}: expected a boolean (1/0, true/false, yes/no, on/off), got {raw!r}")


def _parse_seconds(env: Mapping[str, str], name: str, default: float, *, below: float | None = None) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}: expected a number of seconds, got {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name}: must be a finite number of seconds, got {raw!r}")
    if value <= 0:
        raise ConfigError(f"{name}: must be greater than 0, got {raw!r}")
    if below is not None and value >= below:
        raise ConfigError(f"{name}: must be less than {below:g} seconds, got {raw!r}")
    if value > MAX_TIMEOUT_S:
        raise ConfigError(f"{name}: must be at most {MAX_TIMEOUT_S:g} seconds (24 h), got {raw!r}")
    return value


def _parse_int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None = None
) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{name}: expected an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name}: must be at least {minimum}, got {raw!r}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name}: must be at most {maximum}, got {raw!r}")
    return value


def _parse_exe_pattern(env: Mapping[str, str], warnings: list[str]) -> re.Pattern[str]:
    raw = env.get(ENV_EXE_PATTERN)
    if raw is None:
        text = DEFAULT_EXE_PATTERN
    else:
        text = raw.strip()
        if not text:
            raise ConfigError(
                f"{ENV_EXE_PATTERN}: must not be empty (it selects the Vissim processes to manage)"
            )
    try:
        pattern = re.compile(text, re.IGNORECASE)
    except re.error as exc:
        raise ConfigError(f"{ENV_EXE_PATTERN}: invalid regular expression {text!r}: {exc}") from None
    for name in _FORBIDDEN_EXE_NAMES:
        if pattern.fullmatch(name):
            raise ConfigError(
                f"{ENV_EXE_PATTERN}: pattern {text!r} matches {name!r}; it must match only Vissim executables"
            )
    if not any(pattern.fullmatch(name) for name in _TYPICAL_VISSIM_EXE_NAMES):
        warnings.append(
            f"{ENV_EXE_PATTERN}={text!r} matches none of {', '.join(_TYPICAL_VISSIM_EXE_NAMES)}; "
            "Vissim processes may not be detected"
        )
    return pattern


def _parse_break_disable_value(env: Mapping[str, str]) -> int | float:
    raw = _get(env, ENV_BREAK_DISABLE_VALUE)
    if raw is None:
        return DEFAULT_BREAK_DISABLE_VALUE
    try:
        number = float(raw)
    except ValueError:
        raise ConfigError(f"{ENV_BREAK_DISABLE_VALUE}: expected a number, got {raw!r}") from None
    if not math.isfinite(number):
        raise ConfigError(f"{ENV_BREAK_DISABLE_VALUE}: must be a finite number, got {raw!r}")
    if number < 0:
        raise ConfigError(f"{ENV_BREAK_DISABLE_VALUE}: must be >= 0 (SimBreakAt minimum), got {raw!r}")
    return int(number) if number.is_integer() else number


def _parse_path_list(name: str, raw: str, warnings: list[str]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for item in raw.split(";"):
        if not _strip_quotes(item):
            continue
        path = _parse_path(name, item, warnings)
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _parse_path(name: str, raw: str, warnings: list[str]) -> Path:
    text = _strip_quotes(raw)
    if not text:
        raise ConfigError(f"{name}: empty path")
    if "\x00" in text:
        raise ConfigError(f"{name}: path contains a NUL character")
    try:
        path = Path(text).expanduser()
    except RuntimeError as exc:
        raise ConfigError(f"{name}: cannot expand {text!r}: {exc}") from None
    if not path.is_absolute():
        warnings.append(f"{name}: relative path {text!r} was resolved against the current directory")
    return _resolve(name, path)


def _resolve(name: str, path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"{name}: cannot resolve path {str(path)!r}: {exc}") from None


def _strip_quotes(text: str) -> str:
    return text.strip().strip('"').strip("'").strip()


def _is_windows() -> bool:
    return sys.platform == "win32"


def _default_state_dir(env: Mapping[str, str]) -> Path:
    """``%LOCALAPPDATA%\\vissim-mcp`` on Windows, else ``$XDG_STATE_HOME/vissim-mcp`` (``~/.local/state``)."""
    if _is_windows():
        base = _get(env, "LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    else:
        base = _get(env, "XDG_STATE_HOME")
        root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "vissim-mcp"


def _unknown_variable_warnings(env: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    for key in sorted(env):
        upper = key.upper()
        if not upper.startswith("VISSIM_") or upper in _KNOWN_VISSIM_VARS:
            continue
        guess = difflib.get_close_matches(upper, PLAN_ENV_VARS, n=1, cutoff=0.75)
        hint = f" (did you mean {guess[0]}?)" if guess else ""
        found.append(f"unknown variable {key} is ignored{hint}")
    return found
