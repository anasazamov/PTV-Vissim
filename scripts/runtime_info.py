"""Describe a vissim-mcp runtime interpreter as JSON (used by install.ps1 and doctor.ps1).

Run it with the runtime ``python.exe``. Prints to stdout one JSON object: interpreter version, bitness and
paths; whether the runtime dependencies import (``mcp``, ``psutil``, ``pywintypes``/``pythoncom``/
``win32com.client.dynamic`` on Windows - importing them never starts COM or Vissim); the installed
``vissim_mcp`` version and package directory; and a hash of the installed package's ``*.py`` files. With
``--repo-src <repo>\\src`` the same hash is computed for the repository source, so a stale installation
(older code with the same version number) is detected.

Standard library only (plus the imports it checks). Exit code 0 always when it could run.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import platform
import re
import struct
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PACKAGE = "vissim_mcp"
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def tree_hash(package_dir: Path) -> str:
    """sha256 over the sorted relative paths and contents of every ``*.py`` file below ``package_dir``."""
    digest = hashlib.sha256()
    for path in sorted(package_dir.rglob("*.py"), key=lambda p: p.relative_to(package_dir).as_posix()):
        rel = path.relative_to(package_dir).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()


def repo_version(src_dir: Path) -> str | None:
    init = src_dir / PACKAGE / "__init__.py"
    try:
        match = _VERSION_RE.search(init.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _try_import(name: str) -> str | None:
    try:
        importlib.import_module(name)
    except Exception as exc:  # any import failure is a finding, not a crash
        return f"{type(exc).__name__}: {exc}"
    return None


def collect(repo_src: Path | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "executable": sys.executable,
        "base_prefix": sys.base_prefix,
        "prefix": sys.prefix,
        "in_venv": sys.prefix != sys.base_prefix,
        "python_version": platform.python_version(),
        "bits": struct.calcsize("P") * 8,
    }
    modules = ["mcp", "psutil"]
    if sys.platform == "win32":
        modules += ["pywintypes", "pythoncom", "win32com.client.dynamic"]
    errors = {name: err for name in modules if (err := _try_import(name)) is not None}
    info["modules_ok"] = not errors
    info["module_errors"] = errors

    try:
        package = importlib.import_module(PACKAGE)
    except Exception as exc:
        info["installed"] = False
        info["install_error"] = f"{type(exc).__name__}: {exc}"
    else:
        package_dir = Path(package.__file__ or "").resolve().parent
        info["installed"] = True
        info["version"] = getattr(package, "__version__", None)
        info["package_dir"] = str(package_dir)
        info["code_hash"] = tree_hash(package_dir)
        try:
            from importlib.metadata import distribution

            dist = distribution("vissim-mcp")
            direct_url = dist.read_text("direct_url.json")
            info["editable"] = bool(direct_url and '"editable": true' in direct_url.replace("'", '"'))
        except Exception:
            info["editable"] = None
    if repo_src is not None:
        repo_package = repo_src / PACKAGE
        info["repo_version"] = repo_version(repo_src)
        info["repo_code_hash"] = tree_hash(repo_package) if repo_package.is_dir() else None
        info["matches_repo"] = bool(
            info.get("installed")
            and info.get("code_hash") == info["repo_code_hash"]
            and info.get("version") == info["repo_version"]
        )
    return info


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="runtime_info.py", description=__doc__.splitlines()[0])
    parser.add_argument("--repo-src", help="the repository's src directory, to compare the installed code")
    args = parser.parse_args(argv)
    info = collect(Path(args.repo_src) if args.repo_src else None)
    sys.stdout.write(json.dumps(info, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
