r"""policy.py (PLAN 6.7, 10.1 row "policy"): fail-closed path policy, backups and the attribute policy.

Containment is ``Path.is_relative_to`` after resolving the path the way Windows opens it (``D:\A`` vs
``D:\A2``, ``..``, 8.3 short names, junctions and symbolic links), read-only roots, the extension whitelist,
NTFS alternate data streams, relative paths and the non-ASCII warning.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from vissim_mcp.errors import ErrorCode, VissimMcpError
from vissim_mcp.policy import (
    EXPORT_EXT,
    LOAD_LAYOUT_EXT,
    LOAD_NET_EXT,
    NON_ASCII_WARNING,
    SAVE_EXT,
    PathPolicy,
    check_attribute_writable,
    make_backup,
    path_attribute_word,
)

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows path semantics")


def code_of(excinfo: pytest.ExceptionInfo[VissimMcpError]) -> str:
    return excinfo.value.code.value


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    r"""``A`` (allowed, with ``models\original`` read-only), its sibling ``A2`` and an outside folder."""
    root = tmp_path / "A"
    sibling = tmp_path / "A2"
    outside = tmp_path / "outside"
    original = root / "models" / "original"
    for folder in (root, sibling, outside, original, root / "work"):
        folder.mkdir(parents=True, exist_ok=True)
    for file in (
        root / "work" / "net.inpx",
        root / "work" / "net.layx",
        original / "base.inpx",
        sibling / "net.inpx",
        outside / "net.inpx",
    ):
        file.write_text("x", encoding="utf-8")
    return {"root": root, "sibling": sibling, "outside": outside, "original": original}


@pytest.fixture
def policy(tree: dict[str, Path]) -> PathPolicy:
    return PathPolicy([tree["root"]], [tree["original"]])


# --- fail-closed and containment


def test_no_allowed_directories_denies_everything(tree: dict[str, Path]) -> None:
    empty = PathPolicy([], [])
    for check in (empty.check_read, empty.check_write):
        with pytest.raises(VissimMcpError) as excinfo:
            check(tree["root"] / "work" / "net.inpx", LOAD_NET_EXT)
        assert code_of(excinfo) == "PATH_DENIED"
        assert "no allowed directories configured" in excinfo.value.message
    with pytest.raises(VissimMcpError) as excinfo:
        empty.check_read("net.inpx", LOAD_NET_EXT)  # a relative path has no root to resolve against
    assert code_of(excinfo) == "PATH_DENIED"


def test_sibling_with_common_prefix_is_outside(policy: PathPolicy, tree: dict[str, Path]) -> None:
    r"""``...\A2\net.inpx`` starts with the text ``...\A`` but is not inside ``...\A`` (no startswith)."""
    assert str(tree["sibling"]).startswith(str(tree["root"]))
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(tree["sibling"] / "net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(tree["sibling"] / "new.inpx", SAVE_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


def test_inside_paths_are_allowed_and_resolved(policy: PathPolicy, tree: dict[str, Path]) -> None:
    path, warnings = policy.check_read(tree["root"] / "work" / "net.inpx", LOAD_NET_EXT)
    assert path == (tree["root"] / "work" / "net.inpx").resolve()
    assert warnings == []
    dotted = f"{tree['root']}{os.sep}work{os.sep}..{os.sep}work{os.sep}net.inpx"
    assert policy.check_read(dotted, LOAD_NET_EXT)[0] == path
    target, _ = policy.check_write(tree["root"] / "work" / "new.inpx", SAVE_EXT)
    assert target == (tree["root"] / "work" / "new.inpx").resolve()


def test_dot_dot_escape_is_denied(policy: PathPolicy, tree: dict[str, Path]) -> None:
    escape = f"{tree['root']}{os.sep}..{os.sep}outside{os.sep}net.inpx"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(escape, LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read("../outside/net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


def test_relative_path_resolves_against_the_first_root(
    tree: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = PathPolicy([tree["root"], tree["sibling"]], [])
    monkeypatch.chdir(tree["sibling"])  # the working directory must not matter
    path, _ = policy.check_read("work/net.inpx", LOAD_NET_EXT)
    assert path == (tree["root"] / "work" / "net.inpx").resolve()
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read("net.inpx", LOAD_NET_EXT)  # exists in the cwd (A2), not in the first root
    assert code_of(excinfo) == "FILE_NOT_FOUND"
    assert (
        policy.check_read(tree["sibling"] / "net.inpx", LOAD_NET_EXT)[0].parent == tree["sibling"].resolve()
    )


@windows_only
def test_containment_is_case_insensitive_on_windows(tree: dict[str, Path]) -> None:
    policy = PathPolicy([Path(str(tree["root"]).upper())], [])
    path, _ = policy.check_read(str(tree["root"] / "work" / "NET.INPX").lower(), LOAD_NET_EXT)
    assert path.name.lower() == "net.inpx"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(str(tree["sibling"] / "net.inpx").upper(), LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


def test_missing_file_on_read(policy: PathPolicy, tree: dict[str, Path]) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(tree["root"] / "work" / "missing.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "FILE_NOT_FOUND"
    (tree["root"] / "work" / "folder.inpx").mkdir()
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(tree["root"] / "work" / "folder.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "FILE_NOT_FOUND"


# --- read-only roots


def test_readonly_root_blocks_writes_but_not_reads(policy: PathPolicy, tree: dict[str, Path]) -> None:
    base = tree["original"] / "base.inpx"
    assert policy.check_read(base, LOAD_NET_EXT)[0] == base.resolve()
    for target in (base, tree["original"] / "copy.inpx"):
        with pytest.raises(VissimMcpError) as excinfo:
            policy.check_write(target, SAVE_EXT)
        assert code_of(excinfo) == "READ_ONLY_PATH"
    assert policy.is_readonly(base.resolve())
    assert not policy.is_readonly((tree["root"] / "work" / "net.inpx").resolve())
    # a sibling named like the read-only folder is not read-only
    (tree["root"] / "models" / "original2").mkdir()
    policy.check_write(tree["root"] / "models" / "original2" / "x.inpx", SAVE_EXT)


def test_readonly_via_dot_dot_spelling(policy: PathPolicy, tree: dict[str, Path]) -> None:
    sneaky = f"{tree['root']}{os.sep}work{os.sep}..{os.sep}models{os.sep}original{os.sep}base.inpx"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(sneaky, SAVE_EXT)
    assert code_of(excinfo) == "READ_ONLY_PATH"


def test_write_onto_a_directory_is_refused(policy: PathPolicy, tree: dict[str, Path]) -> None:
    (tree["root"] / "work" / "dir.inpx").mkdir()
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(tree["root"] / "work" / "dir.inpx", SAVE_EXT)
    assert code_of(excinfo) == "INVALID_ARGUMENT"


def test_hard_linked_target_is_refused(policy: PathPolicy, tree: dict[str, Path]) -> None:
    link = tree["root"] / "work" / "alias.inpx"
    try:
        os.link(tree["original"] / "base.inpx", link)
    except OSError as exc:
        pytest.skip(f"hard links not supported here: {exc}")
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(link, SAVE_EXT)
    assert code_of(excinfo) == "PATH_DENIED"
    assert excinfo.value.details and excinfo.value.details["hard_links"] == 2


# --- syntax


@pytest.mark.parametrize(
    ("operation", "extensions", "name"),
    [
        ("read", LOAD_NET_EXT, "net.layx"),
        ("read", LOAD_LAYOUT_EXT, "net.inpx"),
        ("write", SAVE_EXT, "new.layx"),
        ("write", SAVE_EXT, "new.csv"),
        ("write", EXPORT_EXT, "new.inpx"),
        ("write", SAVE_EXT, "noext"),
    ],
)
def test_extension_whitelist(
    policy: PathPolicy, tree: dict[str, Path], operation: str, extensions: tuple[str, ...], name: str
) -> None:
    check = policy.check_read if operation == "read" else policy.check_write
    with pytest.raises(VissimMcpError) as excinfo:
        check(tree["root"] / "work" / name, extensions)
    assert code_of(excinfo) == "INVALID_ARGUMENT"


def test_extension_is_case_insensitive(policy: PathPolicy, tree: dict[str, Path]) -> None:
    (tree["root"] / "work" / "UPPER.INPX").write_text("x", encoding="utf-8")
    assert policy.check_read(tree["root"] / "work" / "UPPER.INPX", LOAD_NET_EXT)[0].name == "UPPER.INPX"
    assert policy.check_write(tree["root"] / "work" / "out.CSV", EXPORT_EXT)[0].suffix == ".CSV"


@windows_only
@pytest.mark.parametrize("suffix", [":stream", "::$DATA", ":hidden.inpx", ":x:$DATA"])
def test_ntfs_alternate_data_streams_are_rejected(
    policy: PathPolicy, tree: dict[str, Path], suffix: str
) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(f"{tree['root'] / 'work' / 'net.inpx'}{suffix}", SAVE_EXT)
    assert code_of(excinfo) == "INVALID_ARGUMENT"
    assert "alternate data streams" in excinfo.value.message


@windows_only
@pytest.mark.parametrize(
    "raw",
    [
        "work\\NUL.inpx",
        "work\\con",
        "COM1.inpx",
        "work\\a?.inpx",
        "work\\a*.inpx",
        "work\\a<b.inpx",
        "\\\\?\\C:\\x.inpx",
        "\\\\.\\C:\\x.inpx",
        "C:x.inpx",
        "\\rooted.inpx",
        "work\\tab\there.inpx",
        "",
        "   ",
    ],
)
def test_invalid_windows_paths(policy: PathPolicy, raw: str) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(raw, SAVE_EXT)
    assert code_of(excinfo) == "INVALID_ARGUMENT"


@windows_only
def test_unc_path_is_denied_without_unc_root(policy: PathPolicy) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read("\\\\server\\share\\net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


def test_non_string_path_is_invalid(policy: PathPolicy) -> None:
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(42, LOAD_NET_EXT)
    assert code_of(excinfo) == "INVALID_ARGUMENT"


def test_quoted_path_is_accepted(policy: PathPolicy, tree: dict[str, Path]) -> None:
    quoted = f'"{tree["root"] / "work" / "net.inpx"}"'
    assert policy.check_read(quoted, LOAD_NET_EXT)[0].name == "net.inpx"


# --- warnings


@pytest.mark.parametrize("folder", ["Samarqand_ko'cha", "Самарканд", "Samarqand_ko\u2018cha"])
def test_non_ascii_path_works_with_a_warning(policy: PathPolicy, tree: dict[str, Path], folder: str) -> None:
    directory = tree["root"] / folder
    directory.mkdir()
    net = directory / "tarmoq.inpx"
    net.write_text("x", encoding="utf-8")
    path, warnings = policy.check_read(net, LOAD_NET_EXT)
    if path.as_posix().isascii():
        assert warnings == []
    else:
        assert warnings == [NON_ASCII_WARNING]
    target, write_warnings = policy.check_write(directory / "yangi.inpx", SAVE_EXT)
    assert target.parent == path.parent
    assert write_warnings == warnings


def test_long_path_warning(policy: PathPolicy, tree: dict[str, Path]) -> None:
    long_name = "n" * 200 + ".inpx"
    deep = tree["root"] / ("d" * 60)
    deep.mkdir()
    _target, warnings = policy.check_write(deep / long_name, SAVE_EXT)
    assert any("characters long" in warning for warning in warnings)


# --- Windows resolution: 8.3 short names, junctions, symbolic links


def _short_path(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    kernel32.GetShortPathNameW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    length = kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value if length else str(path)


@windows_only
def test_8dot3_short_names_resolve_before_the_check(tmp_path: Path) -> None:
    root = tmp_path / "AllowedLongDirectoryName"
    original = root / "models" / "original"
    original.mkdir(parents=True)
    (original / "base.inpx").write_text("x", encoding="utf-8")
    outside = tmp_path / "OutsideLongDirectoryName"
    outside.mkdir()
    (outside / "net.inpx").write_text("x", encoding="utf-8")
    short_original = _short_path(original)
    short_outside = _short_path(outside)
    if short_original == str(original) or short_outside == str(outside):
        pytest.skip("8.3 short names are disabled on this volume")
    policy = PathPolicy([root], [original])
    # an 8.3 spelling of the read-only folder is still read-only
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(Path(short_original) / "base.inpx", SAVE_EXT)
    assert code_of(excinfo) == "READ_ONLY_PATH"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(Path(short_original) / "new.inpx", SAVE_EXT)
    assert code_of(excinfo) == "READ_ONLY_PATH"
    # an 8.3 spelling of an outside folder is still outside
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(Path(short_outside) / "net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"
    # an allowed root configured by its 8.3 name still contains the long spelling
    short_root = PathPolicy([_short_path(root)], [])
    assert short_root.check_read(original / "base.inpx", LOAD_NET_EXT)[0].name == "base.inpx"


def _junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


@windows_only
def test_junction_escape_is_denied(tree: dict[str, Path]) -> None:
    policy = PathPolicy([tree["root"]], [tree["original"]])
    escape = tree["root"] / "escape"
    _junction(escape, tree["outside"])
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(escape / "net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(escape / "new.inpx", SAVE_EXT)  # the missing file is under the junction too
    assert code_of(excinfo) == "PATH_DENIED"


@windows_only
def test_junction_into_the_readonly_folder_is_readonly(tree: dict[str, Path]) -> None:
    policy = PathPolicy([tree["root"]], [tree["original"]])
    alias = tree["root"] / "work" / "alias"
    _junction(alias, tree["original"])
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_write(alias / "base.inpx", SAVE_EXT)
    assert code_of(excinfo) == "READ_ONLY_PATH"
    assert (
        policy.check_read(alias / "base.inpx", LOAD_NET_EXT)[0] == (tree["original"] / "base.inpx").resolve()
    )


@windows_only
def test_allowed_root_that_is_a_junction(tree: dict[str, Path], tmp_path: Path) -> None:
    link_root = tmp_path / "linkroot"
    _junction(link_root, tree["root"])
    policy = PathPolicy([link_root], [])
    path, _ = policy.check_read(link_root / "work" / "net.inpx", LOAD_NET_EXT)
    assert path == (tree["root"] / "work" / "net.inpx").resolve()
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(tree["sibling"] / "net.inpx", LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


def test_symlink_escape_is_denied(tree: dict[str, Path]) -> None:
    policy = PathPolicy([tree["root"]], [])
    link = tree["root"] / "work" / "link.inpx"
    try:
        os.symlink(tree["outside"] / "net.inpx", link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links need a privilege here: {exc}")
    with pytest.raises(VissimMcpError) as excinfo:
        policy.check_read(link, LOAD_NET_EXT)
    assert code_of(excinfo) == "PATH_DENIED"


# --- backups


def test_make_backup_copies_with_timestamp(tree: dict[str, Path]) -> None:
    source = tree["root"] / "work" / "net.inpx"
    source.write_text("version 1", encoding="utf-8")
    when = datetime(2026, 10, 27, 9, 5, 7)
    first = make_backup(source, now=when)
    assert first == tree["root"] / "work" / "backups" / "net_20261027_090507.inpx"
    assert first.read_text(encoding="utf-8") == "version 1"
    second = make_backup(source, now=when)  # same second: never overwrite the older backup
    assert second.name == "net_20261027_090507_1.inpx"
    assert source.read_text(encoding="utf-8") == "version 1"


def test_make_backup_with_policy_refuses_a_readonly_backup_folder(tree: dict[str, Path]) -> None:
    policy = PathPolicy([tree["root"]], [tree["original"]])
    with pytest.raises(VissimMcpError) as excinfo:
        make_backup(tree["original"] / "base.inpx", policy=policy)
    assert code_of(excinfo) == "READ_ONLY_PATH"


def test_make_backup_failure_is_internal_and_keeps_the_source(tree: dict[str, Path]) -> None:
    source = tree["root"] / "work" / "net.inpx"
    (tree["root"] / "work" / "backups").write_text("a file blocks the folder", encoding="utf-8")
    with pytest.raises(VissimMcpError) as excinfo:
        make_backup(source)
    assert code_of(excinfo) == "INTERNAL"
    assert source.read_text(encoding="utf-8") == "x"


# --- attribute policy


@pytest.mark.parametrize(
    "name",
    [
        "SupplyFile1",
        "supplyfile2",
        "EvalOutDir",
        "OutputPath",
        "ProjectFolder",
        "Supply_File1",
        "SupplyFile1(1)",
        "\uff33\uff55\uff50\uff50\uff4c\uff59\uff26\uff49\uff4c\uff45",  # full-width "SupplyFile"
        "DIRECTION",  # contains "dir": denied (fail-closed, PLAN 6.7 matches substrings)
    ],
)
def test_path_attributes_are_denied(name: str) -> None:
    assert path_attribute_word(name) is not None
    with pytest.raises(VissimMcpError) as excinfo:
        check_attribute_writable(name, allow_path_attrs=False)
    assert code_of(excinfo) == ErrorCode.ATTRIBUTE_DENIED.value
    check_attribute_writable(name, allow_path_attrs=True)  # VISSIM_ALLOW_PATH_ATTRS=1


@pytest.mark.parametrize("name", ["Name", "NumLanes", "Volume(1)", "SigState", "RelFlow(1)", "SimPeriod"])
def test_ordinary_attributes_are_writable(name: str) -> None:
    assert path_attribute_word(name) is None
    check_attribute_writable(name, allow_path_attrs=False)


def test_from_settings_uses_the_configured_directories(settings: object) -> None:
    policy = PathPolicy.from_settings(settings)
    assert policy.allowed_dirs == tuple(settings.allowed_dirs)  # type: ignore[attr-defined]
    assert policy.readonly_dirs == tuple(settings.readonly_dirs)  # type: ignore[attr-defined]
