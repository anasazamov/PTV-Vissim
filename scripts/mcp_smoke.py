"""MCP stdio smoke test of an installed vissim-mcp server (used by install.ps1 and doctor.ps1).

Starts the server exactly as Claude Code would - the ``command``/``args``/``env`` of the server JSON (from
``--server-json`` or ``--python``) on top of the current environment - and checks:

1. ``<command> <args> --check`` exits 0 (the configuration is valid);
2. ``<command> <args> --version`` prints the server version;
3. MCP over stdio: ``initialize`` -> ``notifications/initialized`` -> ``tools/list`` returns exactly the 22
   tools of PLAN 7 -> ``tools/call vissim_health`` returns ``ok: true`` (no COM is touched: start-up and
   ``vissim_health`` never call COM, and ``vissim_connect`` is never called);
4. every stdout line is a JSON-RPC message;
5. after stdin is closed the server exits (Claude Code kills it after 300 ms).

Every server process runs in a working directory that holds decoy ``json.py`` / ``csv.py`` modules (or in
``--cwd``): Claude Code starts the server in the folder it was opened in, and modules there must not shadow
the server's imports (the registered arguments contain ``-P``).

Standard library only. Never starts Vissim. Exit code 0 = all checks passed, 1 = a check failed,
2 = invalid arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
SERVER_ARGS = ["-X", "utf8", "-P", "-m", "vissim_mcp"]
DECOY_MODULES = ("json", "csv")
DECOY_MESSAGE = (
    "vissim-mcp smoke: the module {name} was imported from the working directory {cwd}; "
    "start the server with python -P (register it again with install.ps1)"
)

#: The 22 tools of the PLAN 7 catalogue.
EXPECTED_TOOLS: tuple[str, ...] = (
    "vissim_connect",
    "vissim_health",
    "vissim_info",
    "vissim_restart",
    "vissim_close",
    "vissim_load_network",
    "vissim_save_network",
    "vissim_describe",
    "vissim_list_objects",
    "vissim_get_attributes",
    "vissim_set_attributes",
    "vissim_set_attribute_bulk",
    "vissim_configure_simulation",
    "vissim_start_simulation",
    "vissim_simulation_status",
    "vissim_wait_simulation",
    "vissim_resume_simulation",
    "vissim_stop_simulation",
    "vissim_export_csv",
    "vissim_set_signal_state",
    "vissim_tail_log",
    "vissim_exec_python",
)


class SmokeFailure(Exception):
    pass


def load_server(server_json: str | None, python: str | None, env_pairs: Sequence[str]) -> dict[str, Any]:
    """The server definition: ``{"command", "args", "env"}``."""
    if server_json:
        data = json.loads(Path(server_json).read_text(encoding="utf-8-sig"))
        if "mcpServers" in data:  # a whole .mcp.json
            data = next(iter(data["mcpServers"].values()))
        server = {
            "command": str(data["command"]),
            "args": [str(a) for a in data.get("args", SERVER_ARGS)],
            "env": {str(k): str(v) for k, v in (data.get("env") or {}).items()},
        }
    elif python:
        server = {"command": python, "args": list(SERVER_ARGS), "env": {"PYTHONUTF8": "1"}}
    else:
        raise SmokeFailure("either --server-json or --python is required")
    for pair in env_pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SmokeFailure(f"--env expects KEY=VALUE, got {pair!r}")
        server["env"][key] = value
    return server


def server_environment(extra: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(extra)
    env.setdefault("PYTHONUTF8", "1")
    return env


def kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Kill the server and its children (a venv python.exe is a launcher with a child process)."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def write_decoys(folder: Path) -> None:
    """Modules named like the server's imports; importing one of them stops the server with a message."""
    for name in DECOY_MODULES:
        message = DECOY_MESSAGE.format(name=name, cwd=str(folder))
        (folder / f"{name}.py").write_text(f"raise SystemExit({message!r})\n", encoding="utf-8")


class Session:
    def __init__(self, argv: list[str], env: dict[str, str], cwd: str | None = None) -> None:
        self.process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
        )
        self.lines: list[bytes] = []
        self.stderr_chunks: list[bytes] = []
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw in iter(self.process.stdout.readline, b""):
            self.lines.append(raw)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(message, dict):
                self.messages.put(message)
        self.messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for chunk in iter(lambda: self.process.stderr.read(4096), b""):  # type: ignore[union-attr]
            self.stderr_chunks.append(chunk)

    def stderr_tail(self, lines: int = 15) -> str:
        text = b"".join(self.stderr_chunks).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(
                    f"no answer to {method} within {timeout:g} s; stderr:\n{self.stderr_tail()}"
                )
            try:
                item = self.messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                raise SmokeFailure(f"the server closed stdout during {method}; stderr:\n{self.stderr_tail()}")
            if item.get("id") == request_id:
                if "error" in item:
                    raise SmokeFailure(f"{method} returned an error: {item['error']}")
                return item["result"]

    def check_stdout(self) -> None:
        for number, raw in enumerate(self.lines, start=1):
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise SmokeFailure(f"stdout line {number} is not JSON: {raw[:120]!r}") from None
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise SmokeFailure(f"stdout line {number} is not JSON-RPC: {raw[:120]!r}")


def run_cli(
    server: dict[str, Any], env: dict[str, str], flag: str, timeout: float, cwd: str | None = None
) -> tuple[int, str, str]:
    argv = [server["command"], *server["args"], flag]
    try:
        completed = subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise SmokeFailure(f"cannot start {server['command']!r} (file not found)") from None
    except subprocess.TimeoutExpired:
        raise SmokeFailure(f"'{flag}' did not finish within {timeout:g} s") from None
    out = completed.stdout.decode("utf-8", errors="replace")
    err = completed.stderr.decode("utf-8", errors="replace")
    return completed.returncode, out, err


def smoke(
    server: dict[str, Any], *, timeout: float, expected: Sequence[str], cwd: str | None = None
) -> dict[str, Any]:
    """Run all checks; returns a summary dict, raises SmokeFailure on the first failed check.

    Without ``cwd`` the server runs in a temporary folder with decoy modules (see the module docstring).
    """
    if cwd is None:
        with tempfile.TemporaryDirectory(prefix="vissim-mcp-smoke-", ignore_cleanup_errors=True) as folder:
            write_decoys(Path(folder))
            return smoke(server, timeout=timeout, expected=expected, cwd=folder)
    env = server_environment(server["env"])
    summary: dict[str, Any] = {"command": server["command"], "args": server["args"], "cwd": cwd}

    code, _out, err = run_cli(server, env, "--check", timeout, cwd)
    if code != 0:
        raise SmokeFailure(f"--check failed (exit {code}): {err.strip()[-800:]}")
    try:
        config, _end = json.JSONDecoder().raw_decode(err[err.index("{") :])
    except ValueError:
        config = {}
    summary["check"] = "ok"
    summary["allowed_dirs"] = config.get("VISSIM_ALLOWED_DIRS")
    summary["readonly_dirs"] = config.get("VISSIM_READONLY_DIRS")
    summary["log_dir"] = config.get("VISSIM_LOG_DIR")
    summary["audit_dir"] = config.get("VISSIM_AUDIT_DIR")
    summary["config_warnings"] = [
        line.split("warning:", 1)[1].strip() for line in err.splitlines() if "vissim-mcp: warning:" in line
    ]

    code, out, err = run_cli(server, env, "--version", timeout, cwd)
    if code != 0 or not out.startswith("vissim-mcp "):
        raise SmokeFailure(f"--version failed (exit {code}): {out.strip()} {err.strip()[-400:]}")
    version = out.split()[1]
    summary["version"] = version

    session = Session([server["command"], *server["args"]], env, cwd)
    try:
        started = time.monotonic()
        init = session.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "vissim-mcp-smoke", "version": "1"},
            },
            timeout,
        )
        summary["startup_s"] = round(time.monotonic() - started, 2)
        server_info = init.get("serverInfo", {})
        if server_info.get("version") != version:
            raise SmokeFailure(f"serverInfo.version {server_info.get('version')!r} != --version {version!r}")
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = [tool["name"] for tool in session.request("tools/list", {}, timeout).get("tools", [])]
        summary["tools"] = len(tools)
        missing = sorted(set(expected) - set(tools))
        extra = sorted(set(tools) - set(expected))
        if missing or extra or len(tools) != len(expected):
            raise SmokeFailure(
                f"tools/list returned {len(tools)} tools, expected {len(expected)}; "
                f"missing {missing}, unexpected {extra}"
            )

        result = session.request("tools/call", {"name": "vissim_health", "arguments": {}}, timeout)
        payload = json.loads(result["content"][0]["text"])
        if result.get("isError") or payload.get("ok") is not True:
            raise SmokeFailure(f"vissim_health failed: {payload}")
        summary["health"] = {
            "ok": True,
            "connected": payload.get("connected"),
            "version": payload.get("version"),
            "python": payload.get("python"),
            "warnings": payload.get("warnings", []),
        }

        assert session.process.stdin is not None
        closed_at = time.perf_counter()
        session.process.stdin.close()
        try:
            exit_code = session.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise SmokeFailure("the server did not exit within 5 s after stdin was closed") from None
        summary["exit_after_stdin_close_ms"] = round((time.perf_counter() - closed_at) * 1000)
        summary["exit_code"] = exit_code
        time.sleep(0.1)  # let the reader collect the last lines
        session.check_stdout()
        if exit_code != 0:
            raise SmokeFailure(f"the server exited with code {exit_code}; stderr:\n{session.stderr_tail()}")
    finally:
        kill_tree(session.process)
    return summary


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp_smoke.py", description=__doc__.splitlines()[0])
    parser.add_argument("--server-json", help="server JSON (as registered) or a .mcp.json file")
    parser.add_argument("--python", help="runtime python.exe (used when --server-json is not given)")
    parser.add_argument("--env", action="append", default=[], help="extra KEY=VALUE for the server (repeat)")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds per step (default %(default)s)")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON to stdout")
    parser.add_argument(
        "--cwd", help="working directory of the server (default: a temporary folder with decoy modules)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        server = load_server(args.server_json, args.python, args.env)
    except (SmokeFailure, OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"mcp_smoke: ERROR: {exc}\n")
        return 2
    try:
        summary = smoke(server, timeout=args.timeout, expected=EXPECTED_TOOLS, cwd=args.cwd)
    except SmokeFailure as exc:
        if args.json:
            sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) + "\n")
        sys.stderr.write(f"mcp_smoke: FAILED: {exc}\n")
        return 1
    if args.json:
        sys.stdout.write(json.dumps({"ok": True, **summary}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(
            f"mcp_smoke: OK - vissim-mcp {summary['version']}, {summary['tools']} tools, vissim_health ok, "
            f"start-up {summary['startup_s']} s, exit {summary['exit_after_stdin_close_ms']} ms after stdin "
            f"close\n"
        )
        for warning in summary["config_warnings"]:
            sys.stdout.write(f"mcp_smoke: configuration warning: {warning}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
