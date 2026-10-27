"""A stand-in for ``claude.exe`` (``claude mcp get/add-json/remove``) used by the registration tests.

Scopes behave like Claude Code's and depend on the working directory:

* ``user`` entries live in the JSON file named by ``FAKE_CLAUDE_STATE`` (``{"user": {...}, "local": {...}}``)
  and are visible from every folder;
* ``local`` entries are kept per folder in the same file (``"local": {"<normalised cwd>": {...}}``) and are
  visible only from that folder;
* ``project`` entries are the ``.mcp.json`` file of the working directory.

``get`` reports the entry with the highest precedence (local > project > user) seen from the working
directory. Every invocation (argv and cwd) is appended to the JSON-lines file ``FAKE_CLAUDE_LOG``.
``FAKE_CLAUDE_STATUS`` sets the health status shown by ``get`` (default ``✓ Connected``);
``FAKE_CLAUDE_FAIL`` = ``add-json`` / ``remove`` / ``get`` makes that sub-command fail every time,
``FAKE_CLAUDE_FAIL_ONCE`` only the first time. The output imitates Claude Code 2.1: ``get`` prints ``Scope:``,
``Status:``, ``Type:``, ``Command:``, ``Args:`` and an ``Environment:`` block; unknown names print
``No MCP server named "<name>"`` (exit 1).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCOPES_BY_PRECEDENCE = ("local", "project", "user")
SCOPE_TEXT = {
    "user": "User config (available in all your projects)",
    "project": "Project config (shared via .mcp.json)",
    "local": "Local config (private to you in this project)",
}


def cwd_key(path: str | Path | None = None) -> str:
    return os.path.normcase(os.path.normpath(str(path or os.getcwd())))


def _state_path() -> Path:
    return Path(os.environ["FAKE_CLAUDE_STATE"])


def _load() -> dict[str, Any]:
    path = _state_path()
    state: dict[str, Any] = {}
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    state.setdefault("user", {})
    state.setdefault("local", {})
    return state


def _save(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


def _mcp_json() -> Path:
    return Path(os.getcwd()) / ".mcp.json"


def _project_servers() -> dict[str, Any]:
    path = _mcp_json()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig")).get("mcpServers", {})


def _save_project(servers: dict[str, Any]) -> None:
    _mcp_json().write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


def _servers(state: dict[str, Any], scope: str) -> dict[str, Any]:
    if scope == "user":
        return state["user"]
    if scope == "local":
        return state["local"].setdefault(cwd_key(), {})
    return _project_servers()


def _scope_arg(args: list[str], default: str) -> str:
    for flag in ("-s", "--scope"):
        if flag in args:
            return args[args.index(flag) + 1]
    return default


def _should_fail(command: str) -> bool:
    if os.environ.get("FAKE_CLAUDE_FAIL") == command:
        return True
    if os.environ.get("FAKE_CLAUDE_FAIL_ONCE") == command:
        marker = _state_path().with_name(_state_path().name + f".failed-{command}")
        if not marker.exists():
            marker.write_text("1", encoding="utf-8")
            return True
    return False


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
    if _should_fail(command):
        print(f"fake claude: {command} failed on purpose", file=sys.stderr)
        return 1
    state = _load()

    if command == "get":
        for scope in SCOPES_BY_PRECEDENCE:
            entry = _servers(state, scope).get(name)
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
        servers = _servers(state, scope)
        if name not in servers:
            print(f"No {scope}-scoped MCP server found with name: {name}", file=sys.stderr)
            return 1
        del servers[name]
        if scope == "project":
            _save_project(servers)
        else:
            _save(state)
        print(f"Removed MCP server {name} from {scope} config")
        return 0

    if command == "add-json":
        payload, scope = rest[0], _scope_arg(rest[1:], "local")
        config = json.loads(payload)
        servers = _servers(state, scope)
        if name in servers:
            print(f"MCP server {name} already exists in {scope} config", file=sys.stderr)
            return 1
        servers[name] = config
        if scope == "project":
            _save_project(servers)
        else:
            _save(state)
        print(f"Added stdio MCP server {name} to {scope} config")
        return 0

    print(f"fake claude: unsupported command {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
