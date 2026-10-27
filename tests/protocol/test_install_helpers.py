"""scripts/mcp_smoke.py and scripts/runtime_info.py (the installer's and doctor's checks) against the server
running on the fake COM layer. Nothing here starts Vissim or the real claude.exe."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "scripts" / "mcp_smoke.py"
RUNTIME_INFO = REPO_ROOT / "scripts" / "runtime_info.py"


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


smoke_mod = _load(SMOKE)
info_mod = _load(RUNTIME_INFO)


def _run(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


def test_expected_tools_are_the_22_of_plan_7() -> None:
    assert len(smoke_mod.EXPECTED_TOOLS) == 22
    assert len(set(smoke_mod.EXPECTED_TOOLS)) == 22


def test_smoke_passes_against_server(server_env: dict[str, str]) -> None:
    completed = _run(SMOKE, ["--python", sys.executable, "--json"], server_env)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    assert summary["ok"] is True
    assert summary["tools"] == 22
    assert summary["health"]["ok"] is True and summary["health"]["connected"] is False
    assert summary["check"] == "ok"
    assert summary["exit_code"] == 0


def test_smoke_with_server_json_and_invalid_config_fails(server_env: dict[str, str], tmp_path: Path) -> None:
    config = {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-X", "utf8", "-P", "-m", "vissim_mcp"],
        "env": {"PYTHONUTF8": "1", "VISSIM_CALL_TIMEOUT_S": "-5"},
        "timeout": 900000,
    }
    path = tmp_path / "server.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    completed = _run(SMOKE, ["--server-json", str(path), "--json"], server_env)
    assert completed.returncode == 1
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["ok"] is False and "VISSIM_CALL_TIMEOUT_S" in result["error"]


def test_smoke_runs_in_a_folder_with_decoy_modules(server_env: dict[str, str], tmp_path: Path) -> None:
    """Claude Code starts the server in the opened folder: a csv.py / json.py there must not shadow it."""
    good = {"command": sys.executable, "args": list(smoke_mod.SERVER_ARGS), "env": {"PYTHONUTF8": "1"}}
    assert "-P" in good["args"]
    path = tmp_path / "server.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    completed = _run(SMOKE, ["--server-json", str(path), "--json"], server_env)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    assert summary["ok"] is True and summary["cwd"] and summary["args"] == good["args"]

    # the same server started without -P imports the decoy from the working directory and fails
    old = dict(good, args=["-X", "utf8", "-m", "vissim_mcp"])
    path.write_text(json.dumps(old), encoding="utf-8")
    completed = _run(SMOKE, ["--server-json", str(path), "--json"], server_env)
    assert completed.returncode == 1
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["ok"] is False and "imported from the working directory" in result["error"]


def test_smoke_reads_a_whole_mcp_json(tmp_path: Path) -> None:
    path = tmp_path / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"vissim": {"command": "C:\\rt\\python.exe", "env": {"A": "1"}}}}),
        encoding="utf-8",
    )
    server = smoke_mod.load_server(str(path), None, ["B=2"])
    assert server == {
        "command": "C:\\rt\\python.exe",
        "args": ["-X", "utf8", "-P", "-m", "vissim_mcp"],
        "env": {"A": "1", "B": "2"},
    }


def test_smoke_detects_a_wrong_tool_list(server_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in server_env.items():
        monkeypatch.setenv(key, value)
    server = smoke_mod.load_server(None, sys.executable, [])
    with pytest.raises(smoke_mod.SmokeFailure, match="expected 23"):
        smoke_mod.smoke(server, timeout=60, expected=(*smoke_mod.EXPECTED_TOOLS, "vissim_extra"))


def test_smoke_missing_command_is_reported(tmp_path: Path) -> None:
    server = {"command": str(tmp_path / "missing.exe"), "args": [], "env": {}}
    with pytest.raises(smoke_mod.SmokeFailure, match="cannot start"):
        smoke_mod.smoke(server, timeout=10, expected=smoke_mod.EXPECTED_TOOLS)


def test_runtime_info_compares_with_repo(server_env: dict[str, str]) -> None:
    completed = _run(RUNTIME_INFO, ["--repo-src", str(REPO_ROOT / "src")], server_env)
    assert completed.returncode == 0, completed.stderr
    info = json.loads(completed.stdout)
    assert info["installed"] is True
    assert info["bits"] in (32, 64)
    assert info["repo_version"] == info["version"]
    # the test environment imports the package from the repository's src (PYTHONPATH / editable install)
    assert info["code_hash"] == info["repo_code_hash"]
    assert info["matches_repo"] is True


def test_tree_hash_ignores_line_endings_and_sees_changes(tmp_path: Path) -> None:
    a = tmp_path / "a" / "pkg"
    b = tmp_path / "b" / "pkg"
    for root, newline in ((a, b"\n"), (b, b"\r\n")):
        (root / "sub").mkdir(parents=True)
        (root / "__init__.py").write_bytes(b"x = 1" + newline)
        (root / "sub" / "m.py").write_bytes(b"y = 2" + newline)
        (root / "sub" / "data.txt").write_bytes(b"ignored" + newline * 3)
    assert info_mod.tree_hash(a) == info_mod.tree_hash(b)
    (b / "sub" / "m.py").write_bytes(b"y = 3\n")
    assert info_mod.tree_hash(a) != info_mod.tree_hash(b)
    assert info_mod.repo_version(tmp_path / "missing") is None
