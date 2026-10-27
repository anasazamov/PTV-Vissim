"""Register the vissim-mcp stdio server with Claude Code (``claude mcp add-json``).

Used by ``scripts/install.ps1`` / ``uninstall.ps1`` / ``doctor.ps1``; standard library only, so any
Python 3.11+ can run it (the installer runs it with the runtime venv's interpreter).

Modes (default: register):

* register - build the server JSON, remove an existing entry of the same name *in the same scope* (a "not
  found" answer is ignored), ``claude mcp add-json <name> <json> -s <scope>``, then verify with
  ``claude mcp get <name>`` that the entry points at the runtime ``python.exe``.
* ``--print-json`` - print the server JSON (exactly what would be registered) and exit; claude is not needed.
* ``--unregister`` - remove the entry from the given scope (not found = success).
* ``--show`` - print what ``claude mcp get <name>`` reports, parsed into JSON (``found``, ``scope``,
  ``command``, ``args``, ``env``, ``status``).
* ``--dry-run`` - print the claude commands instead of running them (no claude process is started).

claude is always started with an argument list (never through a shell). ``user`` scope commands run in the
user's home directory (so that a project-local entry of the current folder does not shadow the check);
``project`` / ``local`` scope commands run in ``--project-dir``.

Exit codes: 0 success, 1 a claude command failed or claude was not found, 2 invalid arguments, 3 the name is
already registered in a different scope (conflict), 4 the verification after registering failed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

DEFAULT_NAME = "vissim"
DEFAULT_SCOPE = "user"
DEFAULT_PROGID = "Vissim.Vissim"
DEFAULT_TIMEOUT_MS = 900_000
SCOPES = ("user", "project", "local")
SERVER_ARGS = ["-X", "utf8", "-m", "vissim_mcp"]
CLAUDE_TIMEOUT_S = 180.0

EXIT_OK = 0
EXIT_CLAUDE_FAILED = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_VERIFY_FAILED = 4


class RegistrationError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_CLAUDE_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ClaudeResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


@dataclass
class RegisteredServer:
    """What ``claude mcp get`` reports about one server."""

    found: bool
    scope: str | None = None
    status: str | None = None
    type: str | None = None
    command: str | None = None
    args: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "found": self.found,
            "scope": self.scope,
            "status": self.status,
            "type": self.type,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "raw": self.raw,
        }


# --- server JSON


def build_server_config(
    python: str | Path,
    *,
    progid: str = DEFAULT_PROGID,
    allowed_dirs: str | Sequence[str | Path] = "",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The ``mcpServers.<name>`` entry (PLAN 9.1) for the runtime interpreter ``python``."""
    if isinstance(allowed_dirs, str):
        dirs = [part.strip() for part in allowed_dirs.split(";") if part.strip()]
    else:
        dirs = [str(part) for part in allowed_dirs]
    env: dict[str, str] = {
        "PYTHONUTF8": "1",
        "VISSIM_PROGID": progid,
        "VISSIM_ALLOWED_DIRS": ";".join(dirs),
        "VISSIM_KILL_ON_EXIT": "1",
    }
    if extra_env:
        env.update(extra_env)
    return {
        "type": "stdio",
        "command": str(python),
        "args": list(SERVER_ARGS),
        "env": env,
        "timeout": int(timeout_ms),
    }


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ENV = frozenset({"PYTHONUTF8", "VISSIM_PROGID", "VISSIM_ALLOWED_DIRS", "VISSIM_KILL_ON_EXIT"})


def parse_env_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """``["KEY=VALUE", ...]`` -> dict. The four base variables have their own options and are refused here."""
    env: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.match(key):
            raise RegistrationError(f"--env expects KEY=VALUE, got {pair!r}", EXIT_USAGE)
        if key.upper() in _RESERVED_ENV and key.upper() != "VISSIM_KILL_ON_EXIT":
            raise RegistrationError(
                f"--env {key}: use --progid / --allowed-dirs instead (PYTHONUTF8 is always 1)", EXIT_USAGE
            )
        env[key] = value
    return env


def server_json(config: dict[str, Any]) -> str:
    """Compact JSON for the ``add-json`` argument (ASCII-escaped: safe on any console code page)."""
    return json.dumps(config, ensure_ascii=True, separators=(",", ":"))


# --- claude executable


def find_claude(explicit: str | None = None) -> Path | None:
    """``explicit`` (if given), else ``claude`` on PATH, else ``%USERPROFILE%\\.local\\bin\\claude.exe``."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    found = shutil.which("claude")
    if found:
        return Path(found)
    home = os.environ.get("USERPROFILE") or str(Path.home())
    for name in ("claude.exe", "claude"):
        candidate = Path(home) / ".local" / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def claude_prefix(claude: Path) -> list[str]:
    """The argv prefix that starts ``claude``. A ``.py`` file (a test double) runs with this interpreter."""
    suffix = claude.suffix.lower()
    if suffix == ".py":
        return [sys.executable, "-X", "utf8", str(claude)]
    if suffix in (".cmd", ".bat"):
        raise RegistrationError(
            f"{claude} is a batch shim; JSON arguments cannot be passed through cmd.exe safely. "
            "Install the native Claude Code (claude.exe) or pass --claude <path to claude.exe>.",
            EXIT_USAGE,
        )
    return [str(claude)]


class Claude:
    """Runs ``claude mcp ...`` with an argument list (no shell)."""

    def __init__(self, claude: Path, *, cwd: Path, dry_run: bool = False, verbose: bool = True) -> None:
        self.prefix = claude_prefix(claude)
        self.cwd = cwd
        self.dry_run = dry_run
        self.verbose = verbose

    def run(self, *args: str) -> ClaudeResult:
        argv = [*self.prefix, *args]
        if self.dry_run:
            _say(f"[dry-run] (cwd {self.cwd}) {subprocess.list2cmdline(argv)}")
            return ClaudeResult(0, "", "")
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLAUDE_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RegistrationError(f"cannot start claude ({argv[0]}): {exc}") from None
        except subprocess.TimeoutExpired:
            raise RegistrationError(
                f"'claude {' '.join(args[:2])}' did not finish within {CLAUDE_TIMEOUT_S:g} s"
            ) from None
        return ClaudeResult(completed.returncode, completed.stdout or "", completed.stderr or "")

    def get(self, name: str) -> RegisteredServer:
        result = self.run("mcp", "get", name)
        if self.dry_run:
            return RegisteredServer(found=False)
        return parse_get_output(result)


# --- parsing "claude mcp get"


def parse_get_output(result: ClaudeResult) -> RegisteredServer:
    """Parse the human-readable ``claude mcp get <name>`` output (tolerant: unknown lines are ignored)."""
    text = result.text
    if result.returncode != 0 or "no mcp server named" in text.lower():
        return RegisteredServer(found=False, raw=text)
    server = RegisteredServer(found=True, raw=text)
    in_env = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_env = False
            continue
        key, sep, value = stripped.partition(":")
        key_l = key.strip().lower()
        if sep and key_l in ("scope", "status", "type", "command", "args", "environment"):
            in_env = key_l == "environment"
            value = value.strip()
            if key_l == "scope":
                server.scope = _scope_from_text(value)
            elif key_l == "status":
                server.status = value
            elif key_l == "type":
                server.type = value
            elif key_l == "command":
                server.command = value
            elif key_l == "args":
                server.args = value
            continue
        if in_env and "=" in stripped:
            env_key, _, env_value = stripped.partition("=")
            server.env[env_key.strip()] = env_value.strip()
        elif in_env:
            in_env = False
    return server


_NOT_FOUND_RE = re.compile(
    r"\bno\b[^\n]*\bmcp server\b[^\n]*\b(found|named)\b|\bnot found\b|\bdoes not exist\b", re.IGNORECASE
)


def _is_not_found(text: str) -> bool:
    """True for claude's "no such server" answers (wording differs between versions)."""
    return bool(_NOT_FOUND_RE.search(text))


def _scope_from_text(value: str) -> str | None:
    first = value.strip().split(" ", 1)[0].lower() if value.strip() else ""
    return first if first in SCOPES else (value.strip() or None)


def same_path(a: str | Path | None, b: str | Path | None) -> bool:
    if not a or not b:
        return False
    norm_a = os.path.normcase(os.path.normpath(str(a).strip().strip('"')))
    norm_b = os.path.normcase(os.path.normpath(str(b).strip().strip('"')))
    return norm_a == norm_b


# --- operations


def remove(claude: Claude, name: str, scope: str) -> bool:
    """Remove ``name`` from ``scope``; True if something was removed, False if it was not there."""
    result = claude.run("mcp", "remove", name, "-s", scope)
    if result.returncode == 0:
        return not claude.dry_run
    if _is_not_found(result.text):
        return False
    raise RegistrationError(
        f"'claude mcp remove {name} -s {scope}' failed: {result.text or result.returncode}"
    )


def register(
    claude: Claude, name: str, scope: str, config: dict[str, Any], *, allow_other_scope: bool = False
) -> RegisteredServer | None:
    """Remove + add-json + verify. Returns the verified entry (None in a dry run)."""
    if not claude.dry_run:
        existing = claude.get(name)
        if existing.found and existing.scope in SCOPES and existing.scope != scope and not allow_other_scope:
            raise RegistrationError(
                f"'{name}' is already registered in the {existing.scope} scope "
                f"(command: {existing.command}). "
                f"Claude Code would use that entry instead of the new {scope}-scope one (precedence local > "
                f"project > user; never register one name in two scopes). Remove it first: "
                f"claude mcp remove {name} -s {existing.scope}",
                EXIT_CONFLICT,
            )
    if remove(claude, name, scope):
        _say(f"removed the previous '{name}' entry ({scope} scope)")
    result = claude.run("mcp", "add-json", name, server_json(config), "-s", scope)
    if result.returncode != 0:
        raise RegistrationError(
            f"'claude mcp add-json {name} ... -s {scope}' failed (exit {result.returncode}): {result.text}"
        )
    if claude.dry_run:
        claude.run("mcp", "get", name)
        return None
    return verify(claude, name, scope, config)


def verify(claude: Claude, name: str, scope: str, config: dict[str, Any]) -> RegisteredServer:
    entry = claude.get(name)
    if not entry.found:
        raise RegistrationError(
            f"'claude mcp get {name}' does not find the server after adding it: {entry.raw}",
            EXIT_VERIFY_FAILED,
        )
    problems: list[str] = []
    if entry.scope is not None and entry.scope != scope:
        problems.append(f"scope is {entry.scope!r}, expected {scope!r} (another scope takes precedence)")
    if entry.command is not None and not same_path(entry.command, config["command"]):
        problems.append(f"command is {entry.command!r}, expected {config['command']!r}")
    if entry.command is None and config["command"].lower() not in entry.raw.lower():
        problems.append(f"the runtime python {config['command']!r} does not appear in 'claude mcp get'")
    status = (entry.status or "").lower()
    if "fail" in status or "error" in status:
        problems.append(f"health check status: {entry.status} (run scripts\\doctor.ps1)")
    if problems:
        raise RegistrationError("verification failed: " + "; ".join(problems), EXIT_VERIFY_FAILED)
    return entry


# --- command line


def _say(message: str) -> None:
    sys.stderr.write(f"register_claude: {message}\n")
    sys.stderr.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="register_claude.py",
        description="Register the vissim-mcp stdio server with Claude Code (claude mcp add-json).",
    )
    parser.add_argument("--python", help="absolute path of the runtime python.exe (the server command)")
    parser.add_argument("--progid", default=DEFAULT_PROGID, help="VISSIM_PROGID (default %(default)s)")
    parser.add_argument("--allowed-dirs", default="", help="VISSIM_ALLOWED_DIRS (';'-separated)")
    parser.add_argument("--scope", choices=SCOPES, default=DEFAULT_SCOPE, help="default %(default)s")
    parser.add_argument("--name", default=DEFAULT_NAME, help="server name (default %(default)s)")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="default %(default)s")
    parser.add_argument(
        "--claude", help="path of claude.exe (default: PATH, then ~\\.local\\bin\\claude.exe)"
    )
    parser.add_argument("--project-dir", help="working directory for project/local scope (default: cwd)")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="additional server environment variable, e.g. VISSIM_CALL_TIMEOUT_S=180 (repeatable)",
    )
    parser.add_argument(
        "--allow-other-scope",
        action="store_true",
        help="register even if the name already exists in another scope (not recommended)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--print-json", action="store_true", help="print the server JSON and exit")
    mode.add_argument("--unregister", action="store_true", help="remove the entry from --scope")
    mode.add_argument("--show", action="store_true", help="print 'claude mcp get' parsed as JSON")
    parser.add_argument("--dry-run", action="store_true", help="print the claude commands, run nothing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _main(args)
    except RegistrationError as exc:
        _say(f"ERROR: {exc}")
        return exc.exit_code


def _main(args: argparse.Namespace) -> int:
    if args.timeout_ms < 1000:
        raise RegistrationError("--timeout-ms must be at least 1000 (Claude Code ignores smaller values)", 2)
    needs_python = not (args.unregister or args.show)
    config: dict[str, Any] | None = None
    if needs_python:
        if not args.python:
            raise RegistrationError("--python is required", EXIT_USAGE)
        python = Path(args.python)
        if not python.is_absolute():
            raise RegistrationError(f"--python must be an absolute path, got {args.python!r}", EXIT_USAGE)
        if not args.print_json and not python.is_file():
            raise RegistrationError(f"--python does not exist: {python}", EXIT_USAGE)
        for part in [p.strip() for p in args.allowed_dirs.split(";") if p.strip()]:
            if not (Path(part).is_absolute() or PureWindowsPath(part).is_absolute()):
                raise RegistrationError(f"--allowed-dirs entries must be absolute, got {part!r}", EXIT_USAGE)
        config = build_server_config(
            python,
            progid=args.progid,
            allowed_dirs=args.allowed_dirs,
            timeout_ms=args.timeout_ms,
            extra_env=parse_env_pairs(args.env),
        )
    if args.print_json:
        assert config is not None
        sys.stdout.write(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return EXIT_OK

    claude_path = find_claude(args.claude)
    if claude_path is None:
        if args.dry_run:
            claude_path = Path(args.claude or "claude.exe")
        else:
            where = args.claude or "PATH or %USERPROFILE%\\.local\\bin\\claude.exe"
            raise RegistrationError(f"claude was not found ({where}); install Claude Code first")
    if args.scope == "user":
        cwd = Path.home()
    else:
        cwd = Path(args.project_dir) if args.project_dir else Path.cwd()
        if not cwd.is_dir():
            raise RegistrationError(f"--project-dir does not exist: {cwd}", EXIT_USAGE)
    claude = Claude(claude_path, cwd=cwd, dry_run=args.dry_run)

    if args.show:
        entry = claude.get(args.name)
        sys.stdout.write(json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n")
        return EXIT_OK
    if args.unregister:
        removed = remove(claude, args.name, args.scope)
        if claude.dry_run:
            _say("dry run: nothing was changed")
        else:
            outcome = "removed from" if removed else "was not registered in"
            _say(f"'{args.name}' {outcome} the {args.scope} scope")
        return EXIT_OK

    assert config is not None
    _say(f"claude: {claude_path}")
    _say(f"registering '{args.name}' ({args.scope} scope): {config['command']} {' '.join(config['args'])}")
    entry = register(claude, args.name, args.scope, config, allow_other_scope=args.allow_other_scope)
    if entry is not None:
        _say(f"verified: scope {entry.scope}, status {entry.status}, command {entry.command}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
