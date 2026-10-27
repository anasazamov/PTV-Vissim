"""stdio_guard.py (PLAN 6.1). Installing the guard redirects fd 1 of the whole process, so every test runs a
small script in a subprocess and inspects its real stdout and stderr."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from noise import NOISE_LABELS

PROTOCOL_DIR = Path(__file__).resolve().parent


def run_script(code: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    script = textwrap.dedent(code)
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, str(PROTOCOL_DIR)],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_guard_sends_all_noise_to_stderr_and_keeps_the_protocol_stream(server_env: dict[str, str]) -> None:
    result = run_script(
        """
        import sys
        from vissim_mcp import stdio_guard
        proto = stdio_guard.install()
        assert stdio_guard.install() is proto and stdio_guard.is_installed()
        sys.path.insert(0, sys.argv[1])
        from noise import make_noise
        make_noise("GUARDED")
        proto.write('{"jsonrpc":"2.0","method":"Самарқанд"}\\n'.encode("utf-8"))
        """,
        server_env,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout == '{"jsonrpc":"2.0","method":"Самарқанд"}\n'.encode()
    stderr = result.stderr.decode("utf-8", "replace")
    for label in NOISE_LABELS:
        assert f"GUARDED {label}" in stderr, label


def test_without_the_guard_the_same_noise_reaches_stdout(server_env: dict[str, str]) -> None:
    """Positive control: proves that the noise generators really target stdout at every level."""
    result = run_script(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from noise import make_noise
        make_noise("UNGUARDED")
        """,
        server_env,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    stdout = result.stdout.decode("utf-8", "replace")
    for label in NOISE_LABELS:
        assert f"UNGUARDED {label}" in stdout, label


@pytest.mark.parametrize("closed_fds", ["2", "0,2"])
def test_guard_survives_missing_standard_handles(server_env: dict[str, str], closed_fds: str) -> None:
    """Without stderr (and stdin) the free numbers 0/2 must not capture the protocol duplicate."""
    result = run_script(
        f"""
        import os, sys
        for fd in ({closed_fds},):
            os.close(fd)
        from vissim_mcp import stdio_guard
        proto = stdio_guard.install()
        assert proto.fileno() > 2, proto.fileno()
        print("lost noise")
        sys.stderr.write("lost stderr noise\\n")
        os.write(1, b"lost fd noise\\n")
        proto.write(b"PROTOCOL\\n")
        """,
        server_env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"PROTOCOL\n"


def test_guard_moves_stdin_off_the_inheritable_standard_handle(server_env: dict[str, str]) -> None:
    """sys.stdin still reads the client's data; fd 0 (and STD_INPUT_HANDLE) is NUL, so a child process started
    without stdin=DEVNULL neither blocks on nor reads from the protocol pipe."""
    script = textwrap.dedent(
        """
        import os, subprocess, sys
        from vissim_mcp import stdio_guard
        proto = stdio_guard.install()
        assert sys.stdin.fileno() > 2, sys.stdin.fileno()
        assert not os.get_inheritable(sys.stdin.fileno())
        assert os.read(0, 100) == b""  # fd 0 is NUL now
        child = subprocess.run(
            [sys.executable, "-c", "import sys; print('CHILD', len(sys.stdin.read()))"],
            timeout=30, capture_output=True,
        )
        assert child.stdout.strip() == b"CHILD 0", child
        child = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('CHILD2 %d' % len(sys.stdin.read()))"],
            timeout=30,
        )
        line = sys.stdin.buffer.readline()
        proto.write(b"GOT " + line)
        """
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script],
        input=b'{"jsonrpc":"2.0"}\nsecond line\n',
        capture_output=True,
        env=server_env,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout == b'GOT {"jsonrpc":"2.0"}\n'
    assert "CHILD2 0" in result.stderr.decode("utf-8", "replace")
