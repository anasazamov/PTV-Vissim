"""Entry point: ``python -X utf8 -m vissim_mcp`` (or the ``vissim-mcp`` console script).

The stdout guard (PLAN 6.1) is the very first statement, before anything else is imported - so importing this
module redirects fd 1 of the importing process. Tests therefore run it only in subprocesses.

Exit codes: 0 normal end (stdin closed), 1 unexpected error, 2 invalid configuration or command line,
130 interrupted (Ctrl+C).
"""

from vissim_mcp import stdio_guard as _stdio_guard

_stdio_guard.install()

import argparse
import json
import logging
import os
import platform
import sys
from collections.abc import Sequence

from vissim_mcp import __version__
from vissim_mcp.config import ConfigError, load_settings

EXIT_CONFIG_ERROR = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Run the stdio MCP server (never returns in server mode), or handle ``--version`` / ``--check``."""
    proto_out = _stdio_guard.install()  # idempotent: the protected original stdout
    args = _parser().parse_args(argv)

    if args.version:
        proto_out.write(f"vissim-mcp {__version__}\n".encode())
        return 0

    try:
        settings = load_settings()
    except ConfigError as exc:
        sys.stderr.write(f"vissim-mcp: configuration error: {exc}\n")
        sys.stderr.flush()
        return EXIT_CONFIG_ERROR

    if args.check:
        sys.stderr.write(json.dumps(settings.public_dict(), ensure_ascii=False, indent=2) + "\n")
        for warning in settings.warnings:
            sys.stderr.write(f"vissim-mcp: warning: {warning}\n")
        sys.stderr.flush()
        return 0

    # Imported only in server mode: --version and --check must work even if the MCP SDK is broken.
    from vissim_mcp.audit import AuditLog
    from vissim_mcp.logging_setup import setup_logging
    from vissim_mcp.server import run
    from vissim_mcp.service import VissimService

    log_path = setup_logging(settings)
    log = logging.getLogger("vissim_mcp")
    log.info(
        "vissim-mcp %s starting: pid %d, Python %s, log file %s, audit directory %s",
        __version__,
        os.getpid(),
        platform.python_version(),
        log_path or "<stderr only>",
        settings.audit_dir,
    )
    for warning in settings.warnings:
        log.warning("configuration: %s", warning)
    run(proto_out, VissimService(settings, audit=AuditLog(settings.audit_dir)), settings)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vissim-mcp",
        description="Stdio MCP server for PTV Vissim (COM). Configured with VISSIM_* environment variables.",
    )
    parser.add_argument("--version", action="store_true", help="print the version to stdout and exit")
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the configuration, print it as JSON to stderr and exit (0 = valid, 2 = invalid)",
    )
    return parser


if __name__ == "__main__":
    sys.exit(main())
