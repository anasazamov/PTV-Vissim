"""A stand-in for ``claude.exe`` (``claude mcp get/add-json/remove``) used by the registration tests.

State lives in the JSON file named by ``FAKE_CLAUDE_STATE`` (``{"user": {...}, "project": {...}, "local":
{...}}``); every invocation (argv and cwd) is appended to the JSON-lines file ``FAKE_CLAUDE_LOG``.
``FAKE_CLAUDE_STATUS`` sets the health status shown by ``get`` (default ``✓ Connected``);
``FAKE_CLAUDE_FAIL`` = ``add-json`` / ``remove`` / ``get`` makes that sub-command fail.
The output imitates Claude Code 2.1: ``get`` prints ``Scope:``, ``Status:``, ``Type:``, ``Command:``,
``Args:`` and an ``Environment:`` block; unknown names print ``No MCP server named "<name>"`` (exit 1).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCOPES_BY_PRECEDENCE = ("local", "project", "user")
SCOPE_TEXT = {
    "user": "User config (available in all your projects)",
    "project": "Project config (shared via .mcp.json)",
    "local": "Local config (private to you in this project)",
}


def _state_path() -> Path:
    return Path(os.environ["FAKE_CLAUDE_STATE"])


def _load() -> dict[str, dict[str, dict]]:
    path = _state_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {scope: {} for scope in SCOPES_BY_PRECEDENCE}


def _save(state: dict[str, dict[str, dict]]) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _scope_arg(args: list[str], default: str) -> str:
    for flag in ("-s", "--scope"):
        if flag in args:
            return args[args.index(flag) + 1]
    return default


def main(argv: list[str]) -> int:
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[:1] == ["--version"]:
        print("2.1.311 (Claude Code)")
        return 0
    if len(argv) < 3 or argv[0] != "mcp":
        print(f"fake claude: unsupported arguments {argv}", file=sys.stderr)
        return 2
    command, name, rest = argv[1], argv[2], argv[3:]
    if os.environ.get("FAKE_CLAUDE_FAIL") == command:
        print(f"fake claude: {command} failed on purpose", file=sys.stderr)
        return 1
    state = _load()

    if command == "get":
        for scope in SCOPES_BY_PRECEDENCE:
            entry = state.get(scope, {}).get(name)
            if entry is None:
                continue
            status = os.environ.get("FAKE_CLAUDE_STATUS", "✓ Connected")
            lines = [
                f"{name}:",
                f"  Scope: {SCOPE_TEXT[scope]}",
                f"  Status: {status}",
                f"  Type: {entry.get('type', 'stdio')}",
                f"  Command: {entry['command']}",
                f"  Args: {' '.join(entry.get('args', []))}",
                "  Environment:",
                *[f"    {key}={value}" for key, value in entry.get("env", {}).items()],
                "",
                f'To remove this server, run: claude mcp remove "{name}" -s {scope}',
            ]
            sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
            return 0
        print(f'No MCP server named "{name}". Configured servers: none')
        return 1

    if command == "remove":
        scope = _scope_arg(rest, "local")
        if name not in state.get(scope, {}):
            print(f"No {scope}-scoped MCP server found with name: {name}", file=sys.stderr)
            return 1
        del state[scope][name]
        _save(state)
        print(f"Removed MCP server {name} from {scope} config")
        return 0

    if command == "add-json":
        payload, scope = rest[0], _scope_arg(rest[1:], "local")
        config = json.loads(payload)
        if name in state.setdefault(scope, {}):
            print(f"MCP server {name} already exists in {scope} config", file=sys.stderr)
            return 1
        state[scope][name] = config
        _save(state)
        print(f"Added stdio MCP server {name} to {scope} config")
        return 0

    print(f"fake claude: unsupported command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
