"""Protocol-test launcher: install the stdout guard, make deliberate stdout noise, then run the real server.

Run as ``python -X utf8 tests/protocol/noisy_launcher.py`` with ``PYTHONPATH=tests/fakes``. Importing
``vissim_mcp.__main__`` installs the fd-level guard (its first statement) exactly as ``python -m vissim_mcp``
does; all noise made afterwards must end up on stderr, never on stdout. Production code has no test hooks for
this.

``NOISY_LAUNCHER_FAKE_COM=1`` additionally drives the fake COM layer (``win32com.client.dynamic.Dispatch``) so
that ``FAKE_VISSIM_PRINT_STDOUT=1`` makes the fake Vissim print on every COM call (PLAN 6.1 / 10.2).
"""

import vissim_mcp.__main__ as entry  # installs the stdout guard before anything else

# A statement between the imports keeps import sorters from moving anything above the guard import.
if not entry._stdio_guard.is_installed():
    raise SystemExit("the stdout guard was not installed")

import os
import sys

from noise import make_noise

MARKER = "NOISY-LAUNCHER"

make_noise(MARKER)

if os.environ.get("NOISY_LAUNCHER_FAKE_COM") == "1":
    import pythoncom
    from win32com.client import dynamic

    pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    vissim = dynamic.Dispatch("Vissim.Vissim")
    _ = vissim.Net.Links.Count
    del vissim
    pythoncom.CoUninitialize()

sys.exit(entry.main())
