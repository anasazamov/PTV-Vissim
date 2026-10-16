"""A minimal raw MCP stdio client for protocol tests.

Unlike the SDK client it keeps every raw stdout line, so tests can assert that *each* line is a JSON-RPC
message (the SDK client would silently drop or convert non-JSON lines). stderr is collected separately.
"""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

PROTOCOL_VERSION = "2025-06-18"


class ProtocolError(AssertionError):
    pass


class StdioServer:
    """A server subprocess spoken to over stdin/stdout with newline-delimited JSON-RPC."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.stdout_lines: list[bytes] = []
        self._stderr_chunks: list[bytes] = []
        self._messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._ids = itertools.count(1)
        self.notifications: list[dict[str, Any]] = []
        self._stdout_thread = threading.Thread(target=self._read_stdout, name="test-stdout", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="test-stderr", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def start(cls, args: list[str], *, env: dict[str, str], cwd: Path) -> StdioServer:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd),
        )
        return cls(process)

    # --- readers

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for raw in iter(self.process.stdout.readline, b""):
            self.stdout_lines.append(raw)
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue  # recorded in stdout_lines; assert_stdout_is_jsonrpc() reports it
            if isinstance(message, dict):
                self._messages.put(message)
        self._messages.put(None)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for chunk in iter(lambda: self.process.stderr.read1(65536), b""):  # type: ignore[union-attr]
            self._stderr_chunks.append(chunk)

    # --- protocol

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def send_raw(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> dict[str, Any]:
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    f"no response to {method} within {timeout} s; stderr:\n{self.stderr_text()}"
                )
            try:
                item = self._messages.get(timeout=remaining)
            except queue.Empty:
                continue
            if item is None:
                raise ProtocolError(
                    f"server closed stdout while waiting for {method}; stderr:\n{self.stderr_text()}"
                )
            if item.get("id") == request_id and ("result" in item or "error" in item):
                return item
            self.notifications.append(item)

    def initialize(self) -> dict[str, Any]:
        response = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "vissim-mcp-tests", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 15.0
    ) -> dict[str, Any]:
        """``tools/call`` -> ``{"result": CallToolResult, "payload": JSON of the first text block}``."""
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        if "error" in response:
            raise ProtocolError(f"tools/call {name} failed: {response['error']}")
        result = response["result"]
        payload = json.loads(result["content"][0]["text"])
        return {"result": result, "payload": payload}

    # --- checks

    def assert_stdout_is_jsonrpc(self) -> None:
        """Every stdout line must be UTF-8 JSON with ``"jsonrpc": "2.0"`` (no stray output at all)."""
        for number, raw in enumerate(self.stdout_lines, start=1):
            try:
                text = raw.decode("utf-8")
                message = json.loads(text)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProtocolError(f"stdout line {number} is not JSON: {raw[:200]!r} ({exc})") from None
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ProtocolError(f"stdout line {number} is not a JSON-RPC 2.0 message: {raw[:200]!r}")
            if not text.endswith("\n") or text.endswith("\r\n"):
                raise ProtocolError(f"stdout line {number} is not terminated by a bare LF: {raw[-20:]!r}")

    # --- lifecycle

    def close_stdin(self) -> None:
        assert self.process.stdin is not None
        self.process.stdin.close()

    def close_stdin_and_time_exit(self, timeout: float = 10.0) -> float:
        """Close stdin and return the seconds until the process has exited."""
        assert self.process.stdin is not None
        started = time.perf_counter()
        self.process.stdin.close()
        self.process.wait(timeout=timeout)
        return time.perf_counter() - started

    def wait(self, timeout: float = 10.0) -> int:
        return self.process.wait(timeout=timeout)

    def finish_reading(self, timeout: float = 5.0) -> None:
        """Wait until both pipes reached EOF (after the process exited)."""
        self._stdout_thread.join(timeout)
        self._stderr_thread.join(timeout)

    def stderr_text(self) -> str:
        return b"".join(self._stderr_chunks).decode("utf-8", errors="replace")

    def kill_tree(self) -> None:
        """Kill the process and its children (the venv ``python.exe`` is a launcher with a child)."""
        try:
            parent = psutil.Process(self.process.pid)
            children = parent.children(recursive=True)
        except psutil.Error:
            children = []
        for proc in children:
            try:
                proc.kill()
            except psutil.Error:
                pass
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            if self.process.stdin is not None and not self.process.stdin.closed:
                self.process.stdin.close()
        except OSError:
            pass
        # The reader threads finish at EOF once the process tree is gone; close the pipes only after that
        # (closing a pipe while a reader holds its buffer lock would block).
        self.finish_reading(timeout=5)
        for stream, reader in (
            (self.process.stdout, self._stdout_thread),
            (self.process.stderr, self._stderr_thread),
        ):
            if stream is None or reader.is_alive():
                continue
            try:
                stream.close()
            except OSError:
                pass
