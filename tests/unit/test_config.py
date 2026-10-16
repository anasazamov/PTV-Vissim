"""config.py (PLAN 5, 10.1): defaults, parsing and validation of every variable."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from vissim_mcp import config
from vissim_mcp.config import PLAN_ENV_VARS, ConfigError, is_valid_progid, load_settings


def env_for(tmp_path: Path, **values: str) -> dict[str, str]:
    """A minimal explicit environment (log/audit dirs inside tmp_path) plus ``values``."""
    env = {"VISSIM_LOG_DIR": str(tmp_path / "logs"), "VISSIM_AUDIT_DIR": str(tmp_path / "audit")}
    env.update(values)
    return env


def config_error(env: dict[str, str]) -> str:
    with pytest.raises(ConfigError) as info:
        load_settings(env)
    return str(info.value)


# --- defaults


def test_defaults_match_plan_section_5(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS=str(root)))

    assert settings.progid == "Vissim.Vissim"
    assert settings.allowed_dirs == (root.resolve(),)
    assert settings.allowed_dirs_source == "VISSIM_ALLOWED_DIRS"
    assert settings.readonly_dirs == ((root / "models" / "original").resolve(),)
    assert settings.call_timeout_s == 120.0
    assert settings.connect_timeout_s == 180.0
    assert settings.load_timeout_s == 600.0
    assert settings.chunk_sim_s == 60
    assert settings.wait_max_s == 90.0
    assert settings.exe_pattern.pattern == r"^vissim\d*\.exe$"
    assert settings.exe_pattern.flags & re.IGNORECASE
    assert settings.pid_detect_s == 20.0
    assert settings.kill_on_exit is True
    assert settings.use_job_object is True
    assert settings.job_object_enabled is True
    assert settings.max_rows == 500
    assert settings.max_result_chars == 40_000
    assert settings.allow_path_attrs is False
    assert settings.allow_exec is False
    assert settings.seed_attr == "RandSeed"
    assert settings.break_disable_value == 0 and isinstance(settings.break_disable_value, int)
    assert settings.project_dir is None
    assert settings.warnings == ()


def test_default_exe_pattern_matches_vissim_names_only(tmp_path: Path) -> None:
    pattern = load_settings(env_for(tmp_path)).exe_pattern
    for name in ("Vissim260.exe", "VISSIM260.EXE", "vissim.exe", "Vissim2025.exe"):
        assert pattern.fullmatch(name), name
    for name in ("Vissim260.exe.bak", "VissimViewer.exe", "notvissim.exe", "python.exe"):
        assert not pattern.fullmatch(name), name


def test_default_state_dirs_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: True)
    settings = load_settings({"LOCALAPPDATA": str(tmp_path / "Local")})
    assert settings.log_dir == (tmp_path / "Local" / "vissim-mcp" / "logs").resolve()
    assert settings.audit_dir == (tmp_path / "Local" / "vissim-mcp" / "audit").resolve()


def test_default_state_dirs_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    settings = load_settings({"XDG_STATE_HOME": str(tmp_path / "state")})
    assert settings.log_dir == (tmp_path / "state" / "vissim-mcp" / "logs").resolve()
    assert settings.audit_dir == (tmp_path / "state" / "vissim-mcp" / "audit").resolve()


def test_load_settings_reads_os_environ_by_default(isolated_env: dict[str, Path]) -> None:
    settings = load_settings()
    assert settings.log_dir == isolated_env["log"].resolve()
    assert settings.allowed_dirs == (isolated_env["projects"].resolve(),)


# --- allowed / readonly


def test_no_allowed_dirs_is_fail_closed(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path))
    assert settings.allowed_dirs == ()
    assert settings.readonly_dirs == ()
    assert settings.allowed_dirs_source == "none"
    assert settings.file_operations_enabled is False
    assert any("every file operation will be denied" in w for w in settings.warnings)


def test_claude_project_dir_is_the_fallback(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    settings = load_settings(env_for(tmp_path, CLAUDE_PROJECT_DIR=str(project)))
    assert settings.allowed_dirs == (project.resolve(),)
    assert settings.allowed_dirs_source == "CLAUDE_PROJECT_DIR"
    assert settings.project_dir == project.resolve()
    assert settings.readonly_dirs == ((project / "models" / "original").resolve(),)


def test_allowed_dirs_list_parsing(tmp_path: Path) -> None:
    first = tmp_path / "A"
    second = tmp_path / "B with space"
    raw = f' "{first}" ;; ; {second} ;{first}; '
    settings = load_settings(
        env_for(tmp_path, VISSIM_ALLOWED_DIRS=raw, CLAUDE_PROJECT_DIR=str(tmp_path / "x"))
    )
    assert settings.allowed_dirs == (
        first.resolve(),
        second.resolve(),
    )  # quotes/blanks dropped, de-duplicated
    assert settings.allowed_dirs_source == "VISSIM_ALLOWED_DIRS"
    assert settings.readonly_dirs == (
        (first / "models" / "original").resolve(),
        (second / "models" / "original").resolve(),
    )


def test_allowed_dirs_with_only_separators_falls_back(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS=" ; ;", CLAUDE_PROJECT_DIR=str(project)))
    assert settings.allowed_dirs == (project.resolve(),)
    assert settings.allowed_dirs_source == "CLAUDE_PROJECT_DIR"


def test_explicit_readonly_dirs_replace_the_default(tmp_path: Path) -> None:
    settings = load_settings(
        env_for(
            tmp_path,
            VISSIM_ALLOWED_DIRS=str(tmp_path / "root"),
            VISSIM_READONLY_DIRS=f"{tmp_path / 'ro1'};{tmp_path / 'ro2'}",
        )
    )
    assert settings.readonly_dirs == ((tmp_path / "ro1").resolve(), (tmp_path / "ro2").resolve())


def test_relative_dirs_are_resolved_with_a_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS="relative"))
    assert settings.allowed_dirs == ((tmp_path / "relative").resolve(),)
    assert any("relative path" in w and "VISSIM_ALLOWED_DIRS" in w for w in settings.warnings)


def test_user_home_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS="~/models"))
    assert settings.allowed_dirs == ((tmp_path / "models").resolve(),)


def test_non_ascii_directories_are_kept(tmp_path: Path) -> None:
    special = tmp_path / "Samarqand_ko'cha_Самарканд"
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS=str(special)))
    assert settings.allowed_dirs == (special.resolve(),)
    assert str(special.name) in json.dumps(settings.public_dict(), ensure_ascii=False)


def test_nul_in_path_is_rejected(tmp_path: Path) -> None:
    message = config_error(env_for(tmp_path, VISSIM_ALLOWED_DIRS="D:\\bad\x00path"))
    assert message.startswith("VISSIM_ALLOWED_DIRS")


# --- numbers


TIMEOUT_VARS = (
    "VISSIM_CALL_TIMEOUT_S",
    "VISSIM_CONNECT_TIMEOUT_S",
    "VISSIM_LOAD_TIMEOUT_S",
    "VISSIM_PID_DETECT_S",
    "VISSIM_WAIT_MAX_S",
)


@pytest.mark.parametrize("name", TIMEOUT_VARS)
@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "abc", "nan", "inf", "1e9", "12 s"])
def test_invalid_seconds_are_rejected_with_the_variable_name(tmp_path: Path, name: str, value: str) -> None:
    assert config_error(env_for(tmp_path, **{name: value})).startswith(f"{name}:")


@pytest.mark.parametrize("name", TIMEOUT_VARS)
def test_seconds_accept_decimals(tmp_path: Path, name: str) -> None:
    settings = load_settings(env_for(tmp_path, **{name: " 2.5 "}))
    field = {
        "VISSIM_CALL_TIMEOUT_S": "call_timeout_s",
        "VISSIM_CONNECT_TIMEOUT_S": "connect_timeout_s",
        "VISSIM_LOAD_TIMEOUT_S": "load_timeout_s",
        "VISSIM_PID_DETECT_S": "pid_detect_s",
        "VISSIM_WAIT_MAX_S": "wait_max_s",
    }[name]
    assert getattr(settings, field) == 2.5


def test_wait_max_must_stay_below_two_minutes(tmp_path: Path) -> None:
    assert "less than 120" in config_error(env_for(tmp_path, VISSIM_WAIT_MAX_S="120"))
    assert load_settings(env_for(tmp_path, VISSIM_WAIT_MAX_S="119.5")).wait_max_s == 119.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VISSIM_CHUNK_SIM_S", "0"),
        ("VISSIM_CHUNK_SIM_S", "-60"),
        ("VISSIM_CHUNK_SIM_S", "1.5"),
        ("VISSIM_CHUNK_SIM_S", "2678401"),
        ("VISSIM_MAX_ROWS", "0"),
        ("VISSIM_MAX_ROWS", "many"),
        ("VISSIM_MAX_RESULT_CHARS", "999"),
        ("VISSIM_MAX_RESULT_CHARS", "-1"),
    ],
)
def test_invalid_integers_are_rejected(tmp_path: Path, name: str, value: str) -> None:
    assert config_error(env_for(tmp_path, **{name: value})).startswith(f"{name}:")


def test_valid_integers(tmp_path: Path) -> None:
    settings = load_settings(
        env_for(tmp_path, VISSIM_CHUNK_SIM_S="30", VISSIM_MAX_ROWS="5", VISSIM_MAX_RESULT_CHARS="1000")
    )
    assert (settings.chunk_sim_s, settings.max_rows, settings.max_result_chars) == (30, 5, 1000)


def test_result_chars_above_claude_code_limit_warns(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path, VISSIM_MAX_RESULT_CHARS="60000"))
    assert settings.max_result_chars == 60000
    assert any("50000" in w for w in settings.warnings)


# --- booleans


BOOL_VARS = {
    "VISSIM_KILL_ON_EXIT": "kill_on_exit",
    "VISSIM_USE_JOB_OBJECT": "use_job_object",
    "VISSIM_ALLOW_PATH_ATTRS": "allow_path_attrs",
    "VISSIM_MCP_ALLOW_EXEC": "allow_exec",
}


@pytest.mark.parametrize("name", list(BOOL_VARS))
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("On", True),
        ("0", False),
        ("false", False),
        ("no", False),
        (" off ", False),
    ],
)
def test_boolean_words(tmp_path: Path, name: str, raw: str, expected: bool) -> None:
    assert getattr(load_settings(env_for(tmp_path, **{name: raw})), BOOL_VARS[name]) is expected


@pytest.mark.parametrize("name", list(BOOL_VARS))
@pytest.mark.parametrize("raw", ["2", "maybe", "y", "enabled"])
def test_invalid_booleans(tmp_path: Path, name: str, raw: str) -> None:
    assert config_error(env_for(tmp_path, **{name: raw})).startswith(f"{name}:")


def test_job_object_requires_kill_on_exit(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path, VISSIM_KILL_ON_EXIT="0", VISSIM_USE_JOB_OBJECT="1"))
    assert settings.use_job_object is True
    assert settings.job_object_enabled is False


def test_blank_values_mean_default(tmp_path: Path) -> None:
    settings = load_settings(
        env_for(tmp_path, VISSIM_CALL_TIMEOUT_S="   ", VISSIM_KILL_ON_EXIT="", VISSIM_PROGID="")
    )
    assert settings.call_timeout_s == 120.0
    assert settings.kill_on_exit is True
    assert settings.progid == "Vissim.Vissim"


# --- exe pattern


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_exe_pattern_is_an_error(tmp_path: Path, value: str) -> None:
    assert "must not be empty" in config_error(env_for(tmp_path, VISSIM_EXE_PATTERN=value))


def test_invalid_exe_regex(tmp_path: Path) -> None:
    assert "invalid regular expression" in config_error(env_for(tmp_path, VISSIM_EXE_PATTERN="(vissim"))


@pytest.mark.parametrize("value", [".*", r".*\.exe", "python.exe", "(vissim|explorer)\\.exe"])
def test_exe_pattern_matching_system_processes_is_rejected(tmp_path: Path, value: str) -> None:
    assert "must match only Vissim executables" in config_error(env_for(tmp_path, VISSIM_EXE_PATTERN=value))


def test_custom_exe_pattern_warns_when_it_matches_no_vissim_name(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path, VISSIM_EXE_PATTERN=r"^fakevissim\d*\.exe$"))
    assert settings.exe_pattern.fullmatch("FakeVissim7.exe")
    assert any("VISSIM_EXE_PATTERN" in w for w in settings.warnings)


# --- names and values


@pytest.mark.parametrize(
    "value", ["Vissim.Vissim.2026", "Vissim.Vissim-64.26", "{A93F962D-8DE3-48FC-9F71-B12A0F730FA3}"]
)
def test_valid_progids(tmp_path: Path, value: str) -> None:
    assert load_settings(env_for(tmp_path, VISSIM_PROGID=value)).progid == value
    assert is_valid_progid(value)


@pytest.mark.parametrize(
    "value", ["Vissim Vissim", "1Vissim", "Vissim..Vissim", "V" * 40, "{not-a-guid}", "Vis$im"]
)
def test_invalid_progids(tmp_path: Path, value: str) -> None:
    assert config_error(env_for(tmp_path, VISSIM_PROGID=value)).startswith("VISSIM_PROGID:")
    assert not is_valid_progid(value)


@pytest.mark.parametrize("value", ["Rand Seed", "_RandSeed", "1Seed", "Seed(1)", "x" * 65])
def test_invalid_seed_attribute(tmp_path: Path, value: str) -> None:
    assert config_error(env_for(tmp_path, VISSIM_SEED_ATTR=value)).startswith("VISSIM_SEED_ATTR:")


def test_custom_seed_attribute(tmp_path: Path) -> None:
    assert load_settings(env_for(tmp_path, VISSIM_SEED_ATTR="RandomSeed_2")).seed_attr == "RandomSeed_2"


@pytest.mark.parametrize(
    ("raw", "expected"), [("0", 0), ("3600", 3600), ("3600.0", 3600), ("3600.5", 3600.5)]
)
def test_break_disable_value(tmp_path: Path, raw: str, expected: float) -> None:
    value = load_settings(env_for(tmp_path, VISSIM_BREAK_DISABLE_VALUE=raw)).break_disable_value
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize("raw", ["-1", "abc", "nan", "inf"])
def test_invalid_break_disable_value(tmp_path: Path, raw: str) -> None:
    assert config_error(env_for(tmp_path, VISSIM_BREAK_DISABLE_VALUE=raw)).startswith(
        "VISSIM_BREAK_DISABLE_VALUE:"
    )


def test_unknown_vissim_variables_produce_warnings(tmp_path: Path) -> None:
    settings = load_settings(
        env_for(tmp_path, VISSIM_ALLOWED_DIR="D:\\x", VISSIM_FOO="1", VISSIM_MCP_FORBID_REAL="1", OTHER="x")
    )
    joined = "\n".join(settings.warnings)
    assert "VISSIM_ALLOWED_DIR is ignored (did you mean VISSIM_ALLOWED_DIRS?)" in joined
    assert "VISSIM_FOO is ignored" in joined
    assert "VISSIM_MCP_FORBID_REAL" not in joined
    assert "OTHER" not in joined


# --- public view


def test_public_dict_is_json_and_lists_every_plan_variable(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path, VISSIM_ALLOWED_DIRS=str(tmp_path / "p")))
    public = settings.public_dict()
    json.dumps(public, allow_nan=False)
    for name in PLAN_ENV_VARS:
        assert name in public, name
    assert public["VISSIM_EXE_PATTERN"] == r"^vissim\d*\.exe$"
    assert public["VISSIM_ALLOWED_DIRS"] == [str((tmp_path / "p").resolve())]
    assert public["CLAUDE_PROJECT_DIR"] is None
    assert len(PLAN_ENV_VARS) == 20


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_settings(env_for(tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.max_rows = 1  # type: ignore[misc]
    assert dataclasses.replace(settings, max_rows=7).max_rows == 7
