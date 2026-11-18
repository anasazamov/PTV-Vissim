# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`vissim-mcp`: a stdio MCP server (Python, `src/vissim_mcp`) that lets Claude Code drive PTV Vissim through COM on
Windows. 24 tools named `vissim_*` (the 22 of PLAN 7 plus `vissim_call_method` and
`vissim_set_signal_program`). Version lives in `src/vissim_mcp/__init__.py`.

Sources of truth, in order: `PLAN.md` (Uzbek; WHAT to build, cited in code as "PLAN 6.2" etc.), then
`docs/implementation-contract.md` (English; HOW the modules fit, hard rules). If they disagree, follow PLAN.md and
report the conflict. `docs/com-names.md` lists the Vissim COM names and which of them are still unverified.

## Commands

The console code page is cp1251, so Python is always run with `-X utf8` (and `PYTHONUTF8=1` for subprocesses).

```powershell
# dev venv (editable install); already present as .venv
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"

.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider          # all tests (unit + protocol)
.venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_jobs.py      # one file
.venv\Scripts\python.exe -X utf8 -m pytest tests\unit\test_jobs.py -k "early_pause"   # one test
.venv\Scripts\python.exe -X utf8 -m ruff check src tests scripts        # lint (CI gate, line length 110)

.venv\Scripts\python.exe -X utf8 -m vissim_mcp --check                  # validate VISSIM_* config, print it to stderr
```

CI (`.github/workflows/ci.yml`) runs ruff and pytest on Windows and Linux with Python 3.12, so everything outside
`backend/real.py` and `winproc.py` must import and test without pywin32.

`pyproject.toml` sets `addopts = -m "not vissim"`: `tests/real` (marker `vissim`) is always deselected. It starts
real Vissim instances (about 9 minutes), so run it only when the user asks for a real-Vissim check:

```powershell
.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider tests\real -m vissim --vissim-com-license -rA
.venv\Scripts\python.exe -X utf8 scripts\probe_com_names.py --i-have-a-com-license   # COM names only
```

Details and the manual steps are in `docs/m6-runbook.md`.

Install / diagnose / remove the registered server (Windows PowerShell 5.1 scripts):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1     # idempotent; also the "update" command
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1      # read-only checks
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

## Two environments — edits do not reach the running server

- `.venv` (repo, editable) is for development and tests only.
- Claude Code launches the server from a separate **non-editable** runtime venv at
  `%LOCALAPPDATA%\vissim-mcp\venv`, registered in user scope with an absolute `python.exe` path and
  `-X utf8 -P -m vissim_mcp`.

So the `mcp__vissim__*` tools in a session run the last *installed* copy. Source changes take effect only after
`scripts\install.ps1` and then `/mcp` → Reconnect (stdio servers are not restarted automatically). `install.ps1`
rewrites the registration in `~/.claude.json`; do not run it, `uninstall.ps1` or `claude mcp add/remove` unless the
user asks.

## Hard rules (from the implementation contract)

- Do not start real Vissim, call real COM `Dispatch`, or kill/touch any `Vissim*.exe` unless the user explicitly
  asks for a real-Vissim run — and then only through `tests/real`, `scripts/probe_com_names.py` or the `vissim_*`
  tools, which touch only instances they started themselves. The owner usually has interactive Vissim windows open.
- This PC has an Academic license that works through COM (CodeMeter lists it as "PTV Base License" next to
  "PTV Vissim (Demo)"). The automatic part of M6 (real-Vissim verification) was run on 2026-11-18
  (`docs/test-report-v1.md`); the manual steps C1–C3 of `docs/m6-runbook.md` are still open, so `v1.0.0` is untagged.
- A COM-launched Vissim now and then shows the license dialog even with the valid license; `connect`/`restart`
  retry (`VissimService.start_retries`). A real test that fails with "license dialog" is this flake, not a bug.
- Vissim facts that shaped the code (all in `docs/com-names.md`): a network without a file makes Vissim open a
  'Save as' dialog when a run starts (so `start_simulation` refuses it); after a run failed to start, later
  runs fail until the network is reloaded; using a COM reference obtained before `Vissim.New()` crashes
  Vissim (always resolve from the root, as `resolver.py` does); the Academic license refuses networks over
  10 000 m or with more than 20 signal controllers.
- Real Vissim returns booleans as 1/0 and enums as upper-case strings ("KEEPALL"); keep `tests/fake_vissim.py`
  in line with what `docs/com-names.md` and the test report record about the real behaviour.
- COM error texts are localized (Russian on this PC): classify by HRESULT only, never by message text.
- Never use `win32com` gencache; dispatch is late-bound (`win32com.client.dynamic`).
- `mcp` stays `<2`. Runtime dependency versions are pinned in `requirements-runtime.txt` (passed to pip with `-c`).
- Open every file with explicit `encoding="utf-8"`.
- Language: code, identifiers, docstrings, log and commit messages in English; `README.md`,
  `docs/troubleshooting.md` and report docs in Uzbek (Latin script).
- Commits are `M<n>: <summary>`; never rewrite earlier commits; do not commit `.claude/`.

## Architecture

Startup (`__main__.py` → `server.py`): `stdio_guard.install()` is the first statement, before any other import. It
redirects fd 1 to stderr and hands back the protected original stdout, which is the only thing the MCP transport
writes to — stdout must carry nothing but JSON-RPC, even if a library prints. Importing `vissim_mcp.__main__`
therefore redirects the importing process's stdout; tests only run it as a subprocess. Startup never touches COM.

Exit: Claude Code closes stdin and kills the process tree 300 ms later. The stdin reader thread itself runs the exit
sequence on EOF (`service.shutdown_fast()` → bounded log flush → `os._exit`), with a 250 ms watchdog timer; nothing
waits for the SDK task group or a hung COM call.

Threading model — the central design constraint:

- `worker.py` `ComWorker`: one daemon thread `vissim-com` (STA) runs **every** COM call. Tools submit a function that
  receives a `WorkerContext` (`ctx.vissim` is the COM root) and must return plain data; COM objects never leave the
  thread (a leaked one becomes `INTERNAL`). The loop drains queued calls, pumps messages, then runs one background
  step.
- `service.py` `VissimService`: owns the worker of the current *generation*, the PID/Job Object bookkeeping, path
  policy, audit log and job registry; it never touches COM itself. `service.call(fn, timeout_s=, task_name=)` is the
  way to run COM work: it adds the watchdog (`TIMEOUT_HUNG`), HRESULT→error mapping and disconnect detection.
  `vissim_restart` abandons the worker and bumps the generation; late results from an old generation are discarded.
- `jobs.py`: a simulation is a background job, not a blocking call. The worker's background step calls
  `SimulationJob.tick` — one chunk (`SimBreakAt` = next target, `RunContinuous()`, read `SimSec`) — only when the
  queue is empty, so tool calls are served between chunks. The module docstring documents the state machine and its
  stall/early-pause rules.
- `vissim_health`, `vissim_simulation_status` and `vissim_wait_simulation` read snapshots and never call COM, so they
  work while Vissim is hung.

COM isolation: `worker.py`, `jobs.py` and the tools never import `pythoncom`/`win32com`; COM is reached only through
the `ComBackend` protocol (`backend/base.py`), implemented by `backend/real.py` (pywin32) and by the test fake.

Process safety (`winproc.py`): the server connects only to a Vissim it launched itself (COM-launched, `-Embedding`
command line) and kills a process only when PID, name and create time still match. A kill-on-close Job Object is the
backstop if the server dies.

Tools (`tools/*.py`, one module per group, registered in `server.TOOL_MODULES` order):

- Every tool goes through `tools/_common.register_tool`. The body is an `async` function returning a dict of fields
  or raising `VissimMcpError`; the wrapper produces one text block of compact JSON
  (`{"ok": true, ...}` / `{"ok": false, "error": {"code", "message", "hint"}}`), maps any other exception to
  `INTERNAL`, and fits the result to `VISSIM_MAX_RESULT_CHARS`. The docstring is the tool description (required).
- Write tools wrap their work in `async with AuditRecord(...)`: exactly one JSONL audit record per call, for success,
  policy refusal and failure alike. For that reason their arguments use the lenient types `StringArg` / `FlagArg`
  and are validated inside the body — a FastMCP schema rejection would bypass both the audit record and the error
  format.
- `tools/network.py` (`vissim_call_method`) is the only way to call COM methods: an allow-list of
  network-editing prefixes (`Add`, `Remove`, `Duplicate`, ...), `New` on the root, object arguments as
  `{"ref": path}`. Never widen it to file, import/export or simulation methods - those have their own tools
  and policies.
- `messages.py` reads what Vissim itself reported (`<network>_NNN.err`, `%TEMP%\VISSIM\vissim_msgs.txt`) and
  attaches it to failed loads and failed runs as `details.vissim_messages`; in test mode it never reads the
  real log.
- Object paths like `Net.Links[1]` are parsed and resolved by `resolver.py`; file paths go through `policy.py`
  (fail-closed allowed roots, read-only `models\original`, extension allow-list, path-like attribute names denied).
- Logs never contain argument or attribute values; those go only to the audit log.

The tool count (24) is asserted in several places (`scripts/mcp_smoke.py`, `doctor.ps1`, tests, README), and
`examples/*` are checked against the server configuration by unit tests — adding or renaming a tool or a `VISSIM_*`
setting (`config.py`) means updating those too.

## Tests

- `tests/conftest.py` puts `tests/fakes` (fake `pythoncom`, `pywintypes`, `win32com`) first on `sys.path` before
  anything imports pywin32 and sets `VISSIM_MCP_FORBID_REAL=1`; an autouse fixture clears all `VISSIM_*` variables
  and points log/audit/allowed dirs into `tmp_path`.
- `tests/unit`: in-process, using `FakeVissim` (`tests/fake_vissim.py`), `FakeBackend`/`BackendFactory` and
  `FakeProcs` (stand-in for `winproc`). The `harness` fixture (`tests/service_harness.py`) builds a service with
  shortened timings.
- `tests/protocol`: the server as a real subprocess over stdio (`start_server` fixture, `tests/stdio_client.py`),
  including stdout-noise and exit-within-300 ms checks, plus the install helper scripts (`register_claude.py` is
  tested against `tests/fake_claude.py`, never the real `claude.exe`).
- When Vissim behaviour needs modelling, extend `FakeVissim` rather than mocking individual COM calls.
