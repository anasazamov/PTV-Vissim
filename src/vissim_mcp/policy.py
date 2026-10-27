r"""File and attribute policy (PLAN 6.7): which files the server may read and write.

:class:`PathPolicy` checks every path a tool receives (``vissim_load_network``, ``vissim_save_network``,
``vissim_export_csv``). It is fail-closed:

* No allowed directory configured (``VISSIM_ALLOWED_DIRS`` and ``CLAUDE_PROJECT_DIR`` both empty) ->
  ``PATH_DENIED: no allowed directories configured`` before the path is even looked at.
* A relative path is relative to the *first* allowed directory - never to the server's working directory.
* The path is resolved the way Windows will open it, *before* the containment check: ``..``, ``/`` and ``\``,
  trailing dots and spaces, 8.3 short names (``ORIGIN~1``), junctions and symbolic links are resolved by the
  file system itself (``realpath(strict=True)`` of the deepest existing ancestor; the missing rest is appended
  with Windows' trailing-dot rule applied). An existing component that cannot be resolved (broken link, no
  access) -> ``PATH_DENIED``.
* Containment uses ``Path.is_relative_to`` - ``D:\A2`` is not inside ``D:\A``, the comparison is
  case-insensitive on Windows - against every root both as configured and as it resolves *now* (a root
  created, or replaced by a junction, after start-up is still compared correctly).
* ``INVALID_ARGUMENT``: NTFS alternate data streams (a ``:`` after the drive), Windows device names (``NUL``,
  ``CON``, ``COM1``, ... in any component, also with an extension), characters Windows forbids (``<>"|?*``,
  control characters), ``\\?\`` / ``\\.\`` / ``\??\`` prefixes, drive-relative (``D:x.inpx``) and
  root-relative (``\x.inpx``) paths, and extensions outside the whitelist of the operation (case-insensitive).
* UNC paths (``\\server\share\...``) are denied without touching the network unless an allowed root is itself
  a UNC path: resolving them can block for a long time and would hand the Windows credentials to that server.
* Reading: the file must exist (``FILE_NOT_FOUND``).
* Writing: never inside a read-only root (``READ_ONLY_PATH`` - checked by path *and* by directory identity, so
  another spelling of the same directory does not help), never onto a directory, never onto a file with more
  than one hard link (another name of that file may be inside a read-only root) -> ``PATH_DENIED``.
* A non-ASCII path works but gets the warning :data:`NON_ASCII_WARNING`; a very long one gets a warning too.

:func:`make_backup` copies a file to ``<dir>\backups\<stem>_YYYYMMDD_HHMMSS<ext>`` before it is overwritten.

Attribute policy: :func:`check_attribute_writable` denies writing attributes whose name contains ``file``,
``path``, ``folder`` or ``dir`` (``SupplyFile1``, ``EvalOutDir``, ...) unless ``VISSIM_ALLOW_PATH_ATTRS=1`` -
such an attribute would let Vissim read or write files outside the file policy.
"""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import ErrorCode, VissimMcpError

LOAD_NET_EXT: tuple[str, ...] = (".inpx",)
LOAD_LAYOUT_EXT: tuple[str, ...] = (".layx",)
SAVE_EXT: tuple[str, ...] = (".inpx",)
EXPORT_EXT: tuple[str, ...] = (".csv",)

BACKUP_DIR_NAME = "backups"
MAX_BACKUP_SUFFIX = 999
NON_ASCII_WARNING = "non-ASCII path; if Vissim fails to read it, move the project to an ASCII path"
#: Windows MAX_PATH: Vissim may not open longer paths.
LONG_PATH_CHARS = 260
MAX_INPUT_CHARS = 4096
#: Words that mark an attribute as a file/path attribute (PLAN 6.7).
PATH_ATTRIBUTE_WORDS: tuple[str, ...] = ("file", "path", "folder", "dir")

_WINDOWS = sys.platform == "win32"
#: Windows device names (also reserved with an extension: ``NUL.inpx``); compared upper-case.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    | {f"COM{c}" for c in "0123456789\u00b9\u00b2\u00b3"}
    | {f"LPT{c}" for c in "0123456789\u00b9\u00b2\u00b3"}
)
_FORBIDDEN_CHARS = frozenset('<>"|?*')
_SEPARATORS_RE = re.compile(r"[\\/]+")
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "\\??\\")
_TRAILING_ARGS_RE = re.compile(r"^(?P<base>[^()]*?)\s*\([^()]*\)\s*$")
_NON_ALNUM_RE = re.compile(r"[\W_]+")

_NO_ROOTS_HINT = (
    "Set VISSIM_ALLOWED_DIRS (';'-separated directories) in the MCP server configuration; when the server is "
    "started by Claude Code, CLAUDE_PROJECT_DIR is used if VISSIM_ALLOWED_DIRS is empty."
)
_RESOLVE_HINT = (
    "A component of the path exists but cannot be resolved (broken link, no access); use another path."
)


def _invalid(raw: str, reason: str) -> VissimMcpError:
    return VissimMcpError(ErrorCode.INVALID_ARGUMENT, f"invalid path {raw!r}: {reason}", hint="")


class PathPolicy:
    """Fail-closed path checks for reading and writing files (see the module docstring)."""

    def __init__(
        self,
        allowed_dirs: Iterable[str | os.PathLike[str]],
        readonly_dirs: Iterable[str | os.PathLike[str]] = (),
    ) -> None:
        self.allowed_dirs: tuple[Path, ...] = tuple(Path(p) for p in allowed_dirs)
        self.readonly_dirs: tuple[Path, ...] = tuple(Path(p) for p in readonly_dirs)

    @classmethod
    def from_settings(cls, settings: Any) -> PathPolicy:
        return cls(settings.allowed_dirs, settings.readonly_dirs)

    # --- public checks

    def check_read(self, path: Any, extensions: Sequence[str] | str | None) -> tuple[Path, list[str]]:
        """A file the server may read: inside an allowed root, whitelisted extension, existing.

        ``extensions`` is the whitelist (e.g. :data:`LOAD_NET_EXT`; ``None`` only for internal checks of a
        location). Returns ``(resolved path, warnings)``; raises ``VissimMcpError`` (``PATH_DENIED``,
        ``INVALID_ARGUMENT``, ``FILE_NOT_FOUND``).
        """
        resolved, warnings = self._check(path, extensions)
        if not resolved.exists():
            raise VissimMcpError(
                ErrorCode.FILE_NOT_FOUND, f"file not found: {resolved}", details={"path": str(resolved)}
            )
        if not resolved.is_file():
            raise VissimMcpError(
                ErrorCode.FILE_NOT_FOUND, f"not a file: {resolved}", hint="", details={"path": str(resolved)}
            )
        return resolved, warnings

    def check_write(self, path: Any, extensions: Sequence[str] | str | None) -> tuple[Path, list[str]]:
        """A file the server may create or overwrite: like :meth:`check_read` (the file need not exist), plus
        not inside a read-only root (``READ_ONLY_PATH``), not a directory, not a file with several hard links.
        """
        resolved, warnings = self._check(path, extensions)
        if self.is_readonly(resolved):
            raise VissimMcpError(
                ErrorCode.READ_ONLY_PATH,
                f"'{resolved}' is inside a read-only directory ({_join(self.readonly_dirs)})",
                details={"path": str(resolved), "readonly_dirs": [str(p) for p in self.readonly_dirs]},
            )
        if resolved.is_dir():
            raise VissimMcpError(
                ErrorCode.INVALID_ARGUMENT,
                f"'{resolved}' is a directory, not a file",
                hint="",
                details={"path": str(resolved)},
            )
        links = _link_count(resolved)
        if links > 1:
            raise VissimMcpError(
                ErrorCode.PATH_DENIED,
                f"'{resolved}' has {links} hard links; overwriting it would also change the file under its "
                "other names (one of them may be inside a read-only directory)",
                hint="Save under a new file name.",
                details={"path": str(resolved), "hard_links": links},
            )
        return resolved, warnings

    def is_readonly(self, path: Path) -> bool:
        """True when ``path`` (already resolved) lies inside a read-only root - by path, or because one of its
        existing parent directories *is* a read-only root (same volume and file id), whatever its spelling."""
        roots = self._roots(self.readonly_dirs)
        if _inside(path, roots):
            return True
        protected = _identities(roots)
        if not protected:
            return False
        return any(identity in protected for identity in _ancestor_identities(path))

    def resolve(self, path: Any) -> Path:
        """Resolve ``path`` like Windows will open it (see the module docstring); no containment check."""
        raw = _clean(path)
        text = os.path.expanduser(raw)
        drive = _check_syntax(text, raw)
        if _WINDOWS:
            if drive.startswith(("\\\\", "//")) and not any(_is_unc(root) for root in self.allowed_dirs):
                raise VissimMcpError(
                    ErrorCode.PATH_DENIED,
                    f"network (UNC) path {raw!r} is not allowed: no allowed directory is a UNC path",
                    hint="Use a local path inside VISSIM_ALLOWED_DIRS (vissim_health shows them).",
                )
        if not os.path.isabs(text):
            if not self.allowed_dirs:
                raise VissimMcpError(
                    ErrorCode.PATH_DENIED, "no allowed directories configured", hint=_NO_ROOTS_HINT
                )
            text = os.path.join(os.fspath(self.allowed_dirs[0]), text)
        try:
            return Path(_resolve_existing(os.path.abspath(text)))
        except (OSError, ValueError, RuntimeError) as exc:
            raise VissimMcpError(
                ErrorCode.PATH_DENIED, f"cannot resolve path {raw!r}: {_os_text(exc)}", hint=_RESOLVE_HINT
            ) from None

    # --- internals

    def _check(self, path: Any, extensions: Sequence[str] | str | None) -> tuple[Path, list[str]]:
        if not self.allowed_dirs:
            raise VissimMcpError(
                ErrorCode.PATH_DENIED, "no allowed directories configured", hint=_NO_ROOTS_HINT
            )
        resolved = self.resolve(path)
        if not _inside(resolved, self._roots(self.allowed_dirs)):
            raise VissimMcpError(
                ErrorCode.PATH_DENIED,
                f"'{resolved}' is outside the allowed directories ({_join(self.allowed_dirs)})",
                details={"path": str(resolved), "allowed_dirs": [str(p) for p in self.allowed_dirs]},
            )
        if extensions is not None:
            wanted = _extensions(extensions)
            if resolved.suffix.lower() not in wanted:
                raise VissimMcpError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"file type '{resolved.suffix or '(none)'}' is not allowed here; "
                    f"expected {', '.join(wanted)}",
                    hint="",
                    details={"path": str(resolved)},
                )
        warnings: list[str] = []
        text = str(resolved)
        if not text.isascii():
            warnings.append(NON_ASCII_WARNING)
        if len(text) >= LONG_PATH_CHARS:
            warnings.append(
                f"the path is {len(text)} characters long; Vissim may not open paths longer than "
                f"{LONG_PATH_CHARS - 1} characters"
            )
        return resolved, warnings

    @staticmethod
    def _roots(roots: Sequence[Path]) -> list[Path]:
        """Every root as configured and as it resolves now (a junction or 8.3 name may differ)."""
        out: list[Path] = []
        for root in roots:
            out.append(root)
            try:
                current = Path(_resolve_existing(os.path.abspath(os.fspath(root))))
            except (OSError, ValueError, RuntimeError):
                continue
            if current != root:
                out.append(current)
        return out


# --- backups


def make_backup(
    path: str | os.PathLike[str], *, policy: PathPolicy | None = None, now: datetime | None = None
) -> Path:
    """Copy ``path`` to ``<dir>\\backups\\<stem>_YYYYMMDD_HHMMSS<ext>`` (``_1``, ``_2``, ... on collision).

    The copy keeps the file times. With ``policy`` the backup location must pass ``policy.check_write`` (the
    ``backups`` folder could be a junction into a read-only or foreign directory). Never overwrites an
    existing backup. Raises ``VissimMcpError`` - ``INTERNAL`` when the copy itself fails (the source is left
    untouched).
    """
    source = Path(path)
    backup_dir = source.parent / BACKUP_DIR_NAME
    stem = f"{source.stem}_{(now or datetime.now()):%Y%m%d_%H%M%S}"
    suffix = source.suffix
    if policy is not None:
        backup_dir = policy.check_write(backup_dir / f"{stem}{suffix}", None)[0].parent
    try:
        backup_dir.mkdir(exist_ok=True)
        for index in range(MAX_BACKUP_SUFFIX + 1):
            candidate = backup_dir / (f"{stem}_{index}{suffix}" if index else f"{stem}{suffix}")
            try:
                with open(candidate, "xb") as handle, open(source, "rb") as original:
                    shutil.copyfileobj(original, handle, 1024 * 1024)
            except FileExistsError:
                continue  # an older backup with this name: never overwrite it
            except BaseException:
                _remove_quietly(candidate)  # a partial copy is useless
                raise
            try:
                shutil.copystat(source, candidate)
            except OSError:
                pass  # the content is safe; only the file times could not be copied
            return candidate
    except OSError as exc:
        raise VissimMcpError(
            ErrorCode.INTERNAL,
            f"could not back up '{source}' to '{backup_dir}': {_os_text(exc)}",
            hint=(
                "Free disk space or check the permissions of the backups folder; "
                "the existing file was not changed."
            ),
            details={"path": str(source)},
        ) from None
    raise VissimMcpError(
        ErrorCode.INTERNAL,
        f"could not back up '{source}': more than {MAX_BACKUP_SUFFIX} backups with the same time stamp",
        hint="Wait a second and retry.",
    )


# --- attribute policy


def path_attribute_word(name: str) -> str | None:
    """The word (``file``, ``path``, ``folder``, ``dir``) that makes ``name`` a path attribute, or None.

    A trailing sub-attribute part ``(...)`` is ignored (``SupplyFile1(1)`` -> ``SupplyFile1``); the rest is
    compared after Unicode compatibility normalisation and case folding with every non-alphanumeric character
    removed, so ``Supply_File1``, ``ＳｕｐｐｌｙＦｉｌｅ1`` or ``Link\\SupplyFile1`` match too (fail-closed).
    """
    text = str(name).strip()
    match = _TRAILING_ARGS_RE.match(text)
    base = match.group("base") if match else text
    folded = _NON_ALNUM_RE.sub("", unicodedata.normalize("NFKC", base).casefold())
    for word in PATH_ATTRIBUTE_WORDS:
        if word in folded:
            return word
    return None


def check_attribute_writable(name: str, allow_path_attrs: bool) -> None:
    """``ATTRIBUTE_DENIED`` for a file/path attribute unless ``VISSIM_ALLOW_PATH_ATTRS=1`` (PLAN 6.7)."""
    if allow_path_attrs:
        return
    word = path_attribute_word(name)
    if word is not None:
        raise VissimMcpError(
            ErrorCode.ATTRIBUTE_DENIED,
            f"writing attribute {name!r} is not allowed: its name contains '{word}' (file and path "
            "attributes could bypass the file policy)",
            details={"attribute": name, "matched": word},
        )


# --- helpers


def _clean(path: Any) -> str:
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str):
        raise VissimMcpError(
            ErrorCode.INVALID_ARGUMENT, f"a path must be a string, got {type(path).__name__}", hint=""
        )
    text = path.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()  # a quoted path ("D:\My models\a.inpx")
    if not text:
        raise VissimMcpError(ErrorCode.INVALID_ARGUMENT, "the path is empty", hint="")
    if len(text) > MAX_INPUT_CHARS:
        raise _invalid(text[:60] + "...", f"longer than {MAX_INPUT_CHARS} characters")
    if any(ord(char) < 32 or char == "\x7f" for char in text):
        raise _invalid(text, "control characters are not allowed")
    if text[:4].replace("/", "\\") in _DEVICE_PREFIXES:
        raise _invalid(
            text,
            "device and extended-length prefixes (\\\\?\\, \\\\.\\, \\??\\) are not supported; "
            "use a normal path",
        )
    return text


def _check_syntax(text: str, raw: str) -> str:
    """Windows path rules (applied on every platform where they make sense); returns the drive part."""
    drive, rest = ntpath.splitdrive(text) if _WINDOWS else ("", text)
    if ":" in rest:
        raise _invalid(raw, "NTFS alternate data streams (a ':' after the drive) are not allowed")
    bad = sorted({char for char in rest if char in _FORBIDDEN_CHARS})
    if bad:
        raise _invalid(raw, f"the characters {''.join(bad)!r} are not allowed in Windows file names")
    if _WINDOWS:
        if drive and not drive.startswith(("\\\\", "//")) and not rest.startswith(("\\", "/")):
            raise _invalid(
                raw,
                f"'{drive}' without a backslash is relative to the current directory of that drive; "
                f"write {drive}\\...",
            )
        if not drive and rest.startswith(("\\", "/")):
            raise _invalid(
                raw,
                "a path starting with '\\' is relative to the current drive; "
                "write the drive letter (D:\\...)",
            )
    for component in _SEPARATORS_RE.split(rest):
        if _is_reserved(component):
            raise _invalid(raw, f"'{component}' is a Windows device name")
    return drive


def _is_reserved(component: str) -> bool:
    stem = component.rstrip(". ").partition(".")[0].rstrip(" ")
    return stem.upper() in _RESERVED_NAMES


def _resolve_existing(path: str) -> str:
    """Resolve the deepest existing ancestor of the absolute ``path`` strictly (links, junctions, 8.3 names)
    and append the missing rest. Windows ignores trailing dots of path components (and spaces of the last
    one), so they are dropped from the missing rest as well; an empty component is invalid."""
    head = path
    tail: list[str] = []
    while not os.path.lexists(head):
        parent, name = os.path.split(head)
        if not name or parent == head:
            return path  # nothing exists (e.g. an unknown drive): there is nothing to resolve
        tail.append(name)
        head = parent
    resolved = os.path.realpath(head, strict=True)
    for name in reversed(tail):
        if _WINDOWS:
            name = name.rstrip(". ")
            if not name:
                raise ValueError(f"empty path component in {path!r}")
        resolved = os.path.join(resolved, name)
    return resolved


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _identities(roots: Iterable[Path]) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for root in roots:
        try:
            stat = os.stat(root)
        except (OSError, ValueError):
            continue
        if stat.st_ino:
            found.add((stat.st_dev, stat.st_ino))
    return found


def _ancestor_identities(path: Path) -> Iterator[tuple[int, int]]:
    for parent in path.parents:
        try:
            stat = os.stat(parent)
        except (OSError, ValueError):
            continue
        if stat.st_ino:
            yield (stat.st_dev, stat.st_ino)


def _link_count(path: Path) -> int:
    try:
        return int(os.stat(path).st_nlink) if path.is_file() else 0
    except (OSError, ValueError):
        return 0


def _is_unc(path: Path) -> bool:
    return ntpath.splitdrive(os.fspath(path))[0].startswith(("\\\\", "//"))


def _extensions(extensions: Sequence[str] | str) -> tuple[str, ...]:
    items = (extensions,) if isinstance(extensions, str) else tuple(extensions)
    return tuple(item.lower() if item.startswith(".") else f".{item.lower()}" for item in items)


def _join(paths: Sequence[Path]) -> str:
    return "; ".join(str(p) for p in paths) or "none"


def _os_text(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
