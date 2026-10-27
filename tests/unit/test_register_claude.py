"""Tests of scripts/register_claude.py against tests/fake_claude.py - the real claude.exe is never started.

Every call passes ``--claude <fake_claude.py>`` explicitly, and ``find_claude`` tests replace PATH lookup and
the home directory, so nothing here can reach the real Claude Code or its configuration.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "register_claude.py"
FAKE_CLAUDE = REPO_ROOT / "tests" / "fake_claude.py"
SERVER_ARGS = ["-X", "utf8", "-P", "-m", "vissim_mcp"]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("register_claude", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reg = _load()


def fake_claude_key(folder: Path) -> str:
    return os.path.normcase(os.path.normpath(str(folder)))


class FakeClaude:
    def __init__(self, root: Path) -> None:
        self.state_file = root / "claude-state.json"
        self.log_file = root / "claude-log.jsonl"

    def state(self) -> dict[str, dict[str, Any]]:
        if not self.state_file.exists():
            return {}
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def seed(self, scope: str, name: str, config: dict[str, Any], folder: Path | None = None) -> None:
        """user: global; local: entry of ``folder``; project: ``folder``\\.mcp.json."""
        if scope == "project":
            assert folder is not None
            path = folder / ".mcp.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"mcpServers": {}}
            data["mcpServers"][name] = config
            path.write_text(json.dumps(data), encoding="utf-8")
            return
        state = self.state() or {"user": {}, "local": {}}
        if scope == "local":
            assert folder is not None
            state.setdefault("local", {}).setdefault(fake_claude_key(folder), {})[name] = config
        else:
            state.setdefault(scope, {})[name] = config
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def local(self, folder: Path) -> dict[str, Any]:
        return self.state().get("local", {}).get(fake_claude_key(folder), {})

    def calls(self) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def commands(self) -> list[list[str]]:
        return [call["argv"][:2] for call in self.calls()]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    fake = FakeClaude(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(fake.state_file))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(fake.log_file))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FAKE_CLAUDE_STATUS", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_FAIL", raising=False)
    return fake


@pytest.fixture
def runtime_python(tmp_path: Path) -> Path:
    python = tmp_path / "runtime" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    return python


def _register(runtime_python: Path, *extra: str, allowed: str | None = None) -> int:
    allowed_dirs = allowed if allowed is not None else str(runtime_python.parents[2] / "projects")
    return reg.main(
        [
            "--python",
            str(runtime_python),
            "--allowed-dirs",
            allowed_dirs,
            "--claude",
            str(FAKE_CLAUDE),
            *extra,
        ]
    )


def test_server_config_matches_plan_9_1(tmp_path: Path) -> None:
    python = tmp_path / "rt" / "Scripts" / "python.exe"
    config = reg.build_server_config(python, progid="Vissim.Vissim.2026", allowed_dirs=r"D:\VissimProjects")
    assert config == {
        "type": "stdio",
        "command": str(python),
        "args": SERVER_ARGS,
        "env": {
            "PYTHONUTF8": "1",
            "VISSIM_PROGID": "Vissim.Vissim.2026",
            "VISSIM_ALLOWED_DIRS": r"D:\VissimProjects",
            "VISSIM_KILL_ON_EXIT": "1",
        },
        "timeout": 900000,
    }


def test_server_json_is_ascii_and_round_trips() -> None:
    config = reg.build_server_config(
        r"C:\Users\Ali\rt\python.exe", allowed_dirs=[r"D:\Loyiha\Samarqand_ko'cha", r"E:\Йўл"]
    )
    text = reg.server_json(config)
    assert text.isascii()
    assert json.loads(text) == config
    assert config["env"]["VISSIM_ALLOWED_DIRS"] == r"D:\Loyiha\Samarqand_ko'cha;E:\Йўл"


def test_register_fresh_user_scope(fake: FakeClaude, runtime_python: Path, tmp_path: Path) -> None:
    assert _register(runtime_python) == 0
    entry = fake.state()["user"]["vissim"]
    assert entry["command"] == str(runtime_python)
    assert entry["args"] == SERVER_ARGS
    assert entry["env"]["VISSIM_KILL_ON_EXIT"] == "1"
    assert entry["timeout"] == 900000
    # get (home), get (from the allowed projects folder: conflict check), remove, add-json, get (verify)
    assert fake.commands() == [["mcp", "get"]] * 2 + [["mcp", "remove"], ["mcp", "add-json"], ["mcp", "get"]]
    calls = fake.calls()
    add = calls[3]["argv"]
    assert add[:3] == ["mcp", "add-json", "vissim"] and add[-2:] == ["-s", "user"]
    assert json.loads(add[3]) == entry  # the JSON arrived intact as one argument (no shell)
    assert calls[2]["argv"] == ["mcp", "remove", "vissim", "-s", "user"]
    # user scope runs in the home directory, not in the current folder; only the conflict check looks from
    # the projects folder
    assert Path(calls[1]["cwd"]) == tmp_path / "projects"
    assert all(Path(call["cwd"]) == tmp_path / "home" for i, call in enumerate(calls) if i != 1)


def test_register_is_idempotent(fake: FakeClaude, runtime_python: Path) -> None:
    assert _register(runtime_python) == 0
    assert _register(runtime_python, "--progid", "Vissim.Vissim.2026") == 0
    state = fake.state()
    assert list(state["user"]) == ["vissim"]
    assert state["user"]["vissim"]["env"]["VISSIM_PROGID"] == "Vissim.Vissim.2026"
    assert not any(state.get("local", {}).values())


def test_register_replaces_stale_entry_in_same_scope(fake: FakeClaude, runtime_python: Path) -> None:
    fake.seed("user", "vissim", {"type": "stdio", "command": r"C:\old\python.exe", "args": [], "env": {}})
    assert _register(runtime_python) == 0
    assert fake.state()["user"]["vissim"]["command"] == str(runtime_python)


OTHER = {"type": "stdio", "command": r"C:\other\python.exe", "args": [], "env": {}}


def test_conflict_with_other_scope_is_refused(fake: FakeClaude, runtime_python: Path, tmp_path: Path) -> None:
    fake.seed("local", "vissim", OTHER, tmp_path / "home")
    assert _register(runtime_python) == reg.EXIT_CONFLICT
    assert fake.local(tmp_path / "home")["vissim"] == OTHER
    assert "vissim" not in fake.state().get("user", {})
    assert ["mcp", "add-json"] not in fake.commands()


@pytest.mark.parametrize("scope", ["project", "local"])
def test_conflict_seen_only_from_the_projects_folder_is_refused(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path, scope: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A .mcp.json or a local entry of the projects folder is invisible from home but wins there."""
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    fake.seed(scope, "vissim", OTHER, projects)
    assert _register(runtime_python, allowed=str(projects)) == reg.EXIT_CONFLICT
    assert ["mcp", "add-json"] not in fake.commands()
    assert "vissim" not in fake.state().get("user", {})
    err = capsys.readouterr().err
    assert f"-s {scope}" in err and str(projects) in err
    # the projects folder was looked at with claude running in it
    assert any(Path(call["cwd"]) == projects for call in fake.calls())


def test_mcp_json_in_projects_folder_is_a_conflict_even_if_claude_does_not_report_it(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    (projects / ".mcp.json").write_text(json.dumps({"mcpServers": {"vissim": OTHER}}), encoding="utf-8")
    assert reg.mcp_json_has(projects, "vissim")
    assert not reg.mcp_json_has(projects, "other")
    assert not reg.mcp_json_has(tmp_path, "vissim")
    claude = reg.Claude(FAKE_CLAUDE, cwd=tmp_path / "home")
    conflict = reg.find_conflict(claude, "vissim", "user", [projects])
    assert conflict is not None and conflict[0].scope == "project" and conflict[1] == projects


def test_user_entry_seen_from_projects_folder_is_not_a_conflict(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    assert _register(runtime_python, allowed=str(projects)) == 0
    assert _register(runtime_python, "--progid", "Vissim.Vissim.2026", allowed=str(projects)) == 0
    assert fake.state()["user"]["vissim"]["env"]["VISSIM_PROGID"] == "Vissim.Vissim.2026"


def test_failed_add_json_restores_the_previous_entry(
    fake: FakeClaude,
    runtime_python: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _register(runtime_python, "--env", "VISSIM_LOAD_TIMEOUT_S=1200") == 0
    before = fake.state()["user"]["vissim"]
    monkeypatch.setenv("FAKE_CLAUDE_FAIL_ONCE", "add-json")
    assert _register(runtime_python, "--progid", "Vissim.Vissim.2026") == reg.EXIT_CLAUDE_FAILED
    after = fake.state()["user"]["vissim"]
    assert after["command"] == before["command"]
    assert after["args"] == before["args"]
    assert after["env"] == before["env"]
    assert "was restored" in capsys.readouterr().err


def test_remove_failure_other_than_not_found_fails(
    fake: FakeClaude, runtime_python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "remove")
    assert _register(runtime_python) == reg.EXIT_CLAUDE_FAILED
    assert ["mcp", "add-json"] not in fake.commands()


def test_add_json_failure(fake: FakeClaude, runtime_python: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "add-json")
    assert _register(runtime_python) == reg.EXIT_CLAUDE_FAILED


def test_verification_fails_on_failed_health_check(
    fake: FakeClaude, runtime_python: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_STATUS", "✗ Failed to connect")
    assert _register(runtime_python) == reg.EXIT_VERIFY_FAILED


def test_dry_run_starts_nothing(
    fake: FakeClaude, runtime_python: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register(runtime_python, "--dry-run") == 0
    assert fake.calls() == []
    assert fake.state() == {}
    err = capsys.readouterr().err
    assert "[dry-run]" in err and "add-json" in err and "-s user" in err


def test_print_json_needs_no_claude(runtime_python: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = reg.main(
        ["--print-json", "--python", str(runtime_python), "--allowed-dirs", str(runtime_python.parent)]
    )
    assert code == 0
    config = json.loads(capsys.readouterr().out)
    assert config["command"] == str(runtime_python)
    assert config["env"]["VISSIM_ALLOWED_DIRS"] == str(runtime_python.parent)


def test_unregister_and_not_found(fake: FakeClaude, runtime_python: Path) -> None:
    assert _register(runtime_python) == 0
    args = ["--unregister", "--claude", str(FAKE_CLAUDE)]
    assert reg.main(args) == 0
    assert "vissim" not in fake.state()["user"]
    assert reg.main(args) == 0  # "not found" is not an error


def test_show_parses_get_output(
    fake: FakeClaude, runtime_python: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _register(runtime_python) == 0
    capsys.readouterr()
    assert reg.main(["--show", "--claude", str(FAKE_CLAUDE)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["found"] is True
    assert shown["scope"] == "user"
    assert shown["status"] == "✓ Connected"
    assert reg.same_path(shown["command"], runtime_python)
    assert shown["args"] == "-X utf8 -P -m vissim_mcp"
    assert shown["env"]["VISSIM_KILL_ON_EXIT"] == "1"
    assert shown["env"]["PYTHONUTF8"] == "1"


def test_show_when_missing(fake: FakeClaude, capsys: pytest.CaptureFixture[str]) -> None:
    assert reg.main(["--show", "--claude", str(FAKE_CLAUDE)]) == 0
    assert json.loads(capsys.readouterr().out)["found"] is False


def test_project_scope_runs_in_project_dir(fake: FakeClaude, runtime_python: Path, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert _register(runtime_python, "--scope", "project", "--project-dir", str(project)) == 0
    assert "vissim" in json.loads((project / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert all(Path(call["cwd"]) == project for call in fake.calls())
    # a rerun replaces its own project entry; a user registration is then refused (two scopes)
    assert _register(runtime_python, "--scope", "project", "--project-dir", str(project)) == 0
    assert _register(runtime_python, allowed=str(project)) == reg.EXIT_CONFLICT


def test_project_scope_refused_while_a_user_entry_exists(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert _register(runtime_python) == 0
    assert _register(runtime_python, "--scope", "project", "--project-dir", str(project)) == reg.EXIT_CONFLICT
    assert not (project / ".mcp.json").exists()


def test_show_with_project_dir_reports_the_entry_used_there(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    fake.seed("local", "vissim", OTHER, projects)
    assert reg.main(["--show", "--claude", str(FAKE_CLAUDE)]) == 0
    assert json.loads(capsys.readouterr().out)["found"] is False
    assert reg.main(["--show", "--claude", str(FAKE_CLAUDE), "--project-dir", str(projects)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["found"] is True and shown["scope"] == "local"


def test_current_settings_round_trip(
    fake: FakeClaude, runtime_python: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    allowed = f"{tmp_path / 'a'};{tmp_path / 'b'}"
    args = ["--progid", "Vissim.Vissim.2026", "--env", "VISSIM_LOAD_TIMEOUT_S=1200"]
    args += ["--env", "VISSIM_KILL_ON_EXIT=0"]
    assert _register(runtime_python, *args, allowed=allowed) == 0
    capsys.readouterr()
    assert reg.main(["--current-settings", "--claude", str(FAKE_CLAUDE)]) == 0
    settings = json.loads(capsys.readouterr().out)
    assert settings == {
        "found": True,
        "scope": "user",
        "command": str(runtime_python),
        "runtime_dir": str(runtime_python.parents[1]),
        "allowed_dirs": allowed,
        "progid": "Vissim.Vissim.2026",
        "extra_env": ["VISSIM_KILL_ON_EXIT=0", "VISSIM_LOAD_TIMEOUT_S=1200"],
    }
    # another scope: nothing to reuse
    project = tmp_path / "proj"
    project.mkdir()
    code = reg.main(
        ["--current-settings", "--scope", "project", "--project-dir", str(project)]
        + ["--claude", str(FAKE_CLAUDE)]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"found": False, "scope": "user"}


def test_current_settings_when_not_registered(fake: FakeClaude, capsys: pytest.CaptureFixture[str]) -> None:
    assert reg.main(["--current-settings", "--claude", str(FAKE_CLAUDE)]) == 0
    assert json.loads(capsys.readouterr().out) == {"found": False, "scope": None}


def test_argument_validation(runtime_python: Path, tmp_path: Path) -> None:
    assert reg.main(["--claude", str(FAKE_CLAUDE)]) == reg.EXIT_USAGE  # --python missing
    assert reg.main(["--python", "python.exe", "--claude", str(FAKE_CLAUDE)]) == reg.EXIT_USAGE
    missing = tmp_path / "nope" / "python.exe"
    assert reg.main(["--python", str(missing), "--claude", str(FAKE_CLAUDE)]) == reg.EXIT_USAGE
    assert _register(runtime_python, allowed="relative\\dir") == reg.EXIT_USAGE
    assert _register(runtime_python, "--timeout-ms", "500") == reg.EXIT_USAGE


def test_claude_not_found(runtime_python: Path, tmp_path: Path) -> None:
    code = reg.main(["--python", str(runtime_python), "--claude", str(tmp_path / "missing" / "claude.exe")])
    assert code == reg.EXIT_CLAUDE_FAILED


def test_find_claude_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg.shutil, "which", lambda name: None)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert reg.find_claude() is None
    exe = tmp_path / ".local" / "bin" / "claude.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert reg.find_claude() == exe
    assert reg.find_claude(str(tmp_path / "other.exe")) is None


def test_batch_shim_is_refused(tmp_path: Path) -> None:
    with pytest.raises(reg.RegistrationError):
        reg.claude_prefix(tmp_path / "claude.cmd")
    assert reg.claude_prefix(Path(r"C:\x\claude.exe")) == [r"C:\x\claude.exe"]


def test_parse_get_output_tolerates_unknown_format() -> None:
    entry = reg.parse_get_output(reg.ClaudeResult(0, "vissim:\n  something new: 1\n", ""))
    assert entry.found and entry.scope is None and entry.command is None
    missing = reg.parse_get_output(reg.ClaudeResult(1, 'No MCP server named "vissim".', ""))
    assert not missing.found


def test_cli_end_to_end(fake: FakeClaude, runtime_python: Path) -> None:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(SCRIPT),
            "--python",
            str(runtime_python),
            "--allowed-dirs",
            str(runtime_python.parent),
            "--claude",
            str(FAKE_CLAUDE),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "verified: scope user" in completed.stderr
    assert fake.state()["user"]["vissim"]["command"] == str(runtime_python)


EXAMPLES = REPO_ROOT / "examples"
READ_ONLY_TOOLS = {
    "vissim_health",
    "vissim_info",
    "vissim_describe",
    "vissim_list_objects",
    "vissim_get_attributes",
    "vissim_simulation_status",
    "vissim_wait_simulation",
    "vissim_tail_log",
}


def test_mcp_json_example_matches_registered_config() -> None:
    example = json.loads((EXAMPLES / ".mcp.json.example").read_text(encoding="utf-8"))
    entry = example["mcpServers"]["vissim"]
    expected = reg.build_server_config(
        entry["command"], progid="Vissim.Vissim", allowed_dirs=r"D:\VissimProjects"
    )
    assert entry == expected


def test_settings_example_allows_only_read_tools_and_denies_exec() -> None:
    settings = json.loads((EXAMPLES / "settings.json.example").read_text(encoding="utf-8"))
    allow = settings["permissions"]["allow"]
    assert all(rule.startswith("mcp__vissim__") for rule in allow)
    assert {rule.removeprefix("mcp__vissim__") for rule in allow} == READ_ONLY_TOOLS
    assert settings["permissions"]["deny"] == ["mcp__vissim__vissim_exec_python"]


def test_claude_md_example_names_existing_tools() -> None:
    import re

    text = (EXAMPLES / "CLAUDE.md.example").read_text(encoding="utf-8")
    smoke = importlib.util.spec_from_file_location(
        "mcp_smoke_for_examples", REPO_ROOT / "scripts" / "mcp_smoke.py"
    )
    assert smoke is not None and smoke.loader is not None
    module = importlib.util.module_from_spec(smoke)
    smoke.loader.exec_module(module)
    named = set(re.findall(r"\bvissim_[a-z_]+\b", text))
    assert named and named <= set(module.EXPECTED_TOOLS)


def test_extra_env(fake: FakeClaude, runtime_python: Path) -> None:
    assert (
        _register(runtime_python, "--env", "VISSIM_CALL_TIMEOUT_S=180", "--env", "VISSIM_KILL_ON_EXIT=0") == 0
    )
    env = fake.state()["user"]["vissim"]["env"]
    assert env["VISSIM_CALL_TIMEOUT_S"] == "180"
    assert env["VISSIM_KILL_ON_EXIT"] == "0"
    assert _register(runtime_python, "--env", r"VISSIM_ALLOWED_DIRS=C:\x") == reg.EXIT_USAGE
    assert _register(runtime_python, "--env", "no-equals-sign") == reg.EXIT_USAGE


def test_server_args_are_the_same_everywhere() -> None:
    """register_claude, mcp_smoke, doctor.ps1 and the example start the server identically (with -P)."""
    smoke = importlib.util.spec_from_file_location("smoke_args", REPO_ROOT / "scripts" / "mcp_smoke.py")
    assert smoke is not None and smoke.loader is not None
    module = importlib.util.module_from_spec(smoke)
    smoke.loader.exec_module(module)
    assert reg.SERVER_ARGS == module.SERVER_ARGS == SERVER_ARGS
    doctor = (REPO_ROOT / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    line = '$serverArgs = @("-X", "utf8", "-P", "-m", "vissim_mcp")'
    assert line in doctor
    assert '"-m", "vissim_mcp")' not in doctor.replace(line, "")


def test_runtime_constraints_pin_the_dependencies_within_pyproject() -> None:
    import tomllib

    from packaging.requirements import Requirement

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for line in (REPO_ROOT / "requirements-runtime.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        assert sep and version, f"constraint {line!r} is not pinned with =="
        pins[name.strip().lower().replace("_", "-")] = version.strip()
    for dependency in pyproject["project"]["dependencies"]:
        req = Requirement(dependency)
        key = req.name.lower().replace("_", "-")
        assert key in pins, f"{req.name} is not pinned in requirements-runtime.txt"
        assert req.specifier.contains(pins[key], prereleases=True), (req.name, pins[key], str(req.specifier))
    assert pins["mcp"].startswith("1.")
    install = (REPO_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "requirements-runtime.txt" in install and '@("-c", $constraintsFile)' in install
