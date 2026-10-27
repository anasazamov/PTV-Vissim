# vissim-mcp implementation contract (supplements PLAN.md)

PLAN.md (Uzbek) is the source of truth for WHAT to build. This contract fixes HOW the pieces fit so that several
engineers working one after another produce one coherent code base. If PLAN.md and this contract disagree, follow
PLAN.md and report the conflict. Items marked EXTENSION are additive features in line with the owner's requirement
("the server must work reliably at any time, also after a reboot"); they never change PLAN.md behaviour.

## 0. Hard rules for every engineer/agent

- Repo root is `D:\PTV-Vissim` (git). PLAN.md's `vissim-mcp/` tree maps 1:1 onto this root.
- Dev interpreter: `D:\PTV-Vissim\.venv\Scripts\python.exe` (Python 3.12.10, 64-bit; mcp 1.30.0, pywin32 312,
  psutil, pytest, pytest-asyncio, ruff). Never upgrade mcp to 2.x (PLAN pins `mcp>=1.10,<2`).
- The console code page is cp1251: ALWAYS run Python with `-X utf8` and set `PYTHONUTF8=1` for subprocesses. Use explicit
  `encoding="utf-8"` for every file opened in code.
- **Never start real Vissim, never call real COM Dispatch, never kill or touch any Vissim*.exe process.** The owner keeps
  an interactive `Vissim260.exe` open. The only license on this PC is a Demo license without the COM module (see
  docs/preflight-report.md). All tests use fakes; `tests/conftest.py` shadows pywin32 with `tests/fakes` and sets
  `VISSIM_MCP_FORBID_REAL=1`. Protocol tests set `VISSIM_EXE_PATTERN` to a name no real program has.
- Do not modify global config (git config, `~/.claude.json`, `~/.claude/settings.json`), do not run
  `claude mcp add/remove`, do not push, do not tag. Do not commit the untracked `.claude/` directory.
- Code, identifiers, docstrings, log messages, commit messages: English. README/troubleshooting/report docs: Uzbek (Latin).
- Keep `ruff check src tests scripts` clean and all tests green before committing. Commit messages `M<n>: <summary>` with
  the trailer `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never rewrite earlier commits.

## 1. Facts established in M0

- Vissim 2026 (2026.0.10.0), `C:\Program Files\PTV Vision\PTV Vissim 2026\Exe\Vissim260.exe`; COM-launched instances
  have the command line `"...\Vissim260.exe" -Embedding`. ProgIDs `Vissim.Vissim`, `Vissim.Vissim.2026`, ... (one CLSID).
- With the Demo license a COM-launched Vissim shows the window `Управление лицензиями - PTV Vissim` ("License not found");
  `Dispatch` fails after 30-120 s with `com_error(-2146959355 /*CO_E_SERVER_EXEC_FAILURE*/, <Russian text>, None, None)`.
  COM error texts are localized — classify by HRESULT only.
- pywin32 `win32com.client.dynamic.Dispatch` exposes (from the type library) callable `AttValue(name)`,
  `SetAttValue(name, value)`, `ItemByKey(key)`, `ItemKeyExists(key)`, `GetMultipleAttributes(tuple)`,
  `SetMultipleAttributes`, `RunContinuous()`, `RunSingleStep()`, `Stop()`, `LoadNet(path, additive)`, `LoadLayout(path)`,
  `SaveNetAs(path)`, `Exit()`, `SuspendUpdateGUI()`, `ResumeUpdateGUI(bool)` and property getters `Count`, `Net`,
  `Simulation`, `Evaluation`, `Graphics`, `CurrentNetworkWindow`, `Attributes`. `getattr` of a method name returns a bound
  method (`types.MethodType`). Never use gencache.
- Documented Vissim 2026 attributes (docs/com-names.md): Simulation `SimPeriod`, `SimRes`, `SimSec` (read-only),
  `SimBreakAt` (0 = no break per official example), `RandSeed` (min 1), `UseMaxSimSpeed`, `NumCores`, `IsRunning`,
  `IsSimBreakAtActive` (semantics unknown — do not use); `Graphics.CurrentNetworkWindow.QuickMode`; Evaluation
  `KeepPrevResults` (1 KeepNone, 2 KeepCurrent, 3 KeepAll); SignalGroup `SigState` ("GREEN","RED","AMBER","REDAMBER" or
  ints 1..11) and `ContrByCOM`. Configurable: `VISSIM_SEED_ATTR` (default `RandSeed`), `VISSIM_BREAK_DISABLE_VALUE` (0).
- mcp 1.30.0: `stdio_server(stdin=, stdout=)` accepts custom streams; a tool returning `CallToolResult` with
  `structured_output=False` is passed through unchanged.

## 2. Response format (all 22 tools)

Every tool is registered through `tools/_common.register_tool` and returns `CallToolResult` with one text block of compact
JSON: `{"ok": true, ...}` or `{"ok": false, "error": {"code", "message", "hint"?, "details"?}}`, `isError = not ok`,
optional `"warnings"`. Tool bodies return a dict of fields or raise `VissimMcpError`; they never raise anything else to
FastMCP (the wrapper maps it to INTERNAL). Results are fitted to `VISSIM_MAX_RESULT_CHARS`.

## 3. Module contracts (implemented M1-M2, still to build M3-M7)

- Existing (M1-M2): `stdio_guard`, `config` (Settings, all PLAN 5 variables), `errors` (16 codes, HRESULT mapping),
  `output` (to_jsonable, paginate, fit_payload), `logging_setup`, `server` (stdin EOF → exit ≤ 300 ms), `backend/`,
  `worker` (ComWorker, WorkerContext.vissim), `service` (VissimService.call(fn, timeout_s=, task_name=), connect,
  restart, close, health, shutdown_fast, `jobs` registry hook and background step), `winproc` (identity-checked kill,
  JobObject 0x2000, `-Embedding` ownership rule), `tools/lifecycle` (5 tools).
- `policy.py` (PLAN 6.7): `PathPolicy(allowed_dirs, readonly_dirs)` with `check_read(path, extensions)` and
  `check_write(path, extensions)` → `(Path, warnings)`; relative paths resolve against the first allowed root; resolve
  before containment (`Path.is_relative_to`, case-insensitive on Windows; handles `D:\A` vs `D:\A2`, 8.3 short names,
  junctions/symlinks); no roots → PATH_DENIED; NTFS ADS → INVALID_ARGUMENT; wrong extension → INVALID_ARGUMENT; missing
  file on read → FILE_NOT_FOUND; readonly → READ_ONLY_PATH; non-ASCII → warning `"non-ASCII path; if Vissim fails to read
  it, move the project to an ASCII path"`; `make_backup(path)` → `<dir>\backups\<stem>_YYYYMMDD_HHMMSS<ext>`;
  `check_attribute_writable(name, allow_path_attrs)` → ATTRIBUTE_DENIED for names containing file/path/folder/dir.
- `audit.py` (PLAN 6.8): `AuditLog(dir).write(record) -> warning | None`, JSONL `audit-YYYY-MM.jsonl`, never raises.
- `resolver.py` (PLAN 6.5): `parse_path` (COM-free; `["Vissim."] seg ("." seg)*`, `seg = NAME ["[" key "]"]`, int or
  quoted string keys, `_` names rejected), `resolve(root, path)` (getattr + ItemByKey; bound methods rejected;
  RESOLVE_FAILED with segment context).
- `jobs.py` (PLAN 6.6) incl. robustness rules: after a chunk `RunContinuous()` read `SimSec`: not advanced → ERROR;
  beyond target+1 → ERROR "did not pause at SimBreakAt"; below target-1 → early pause (stale break) accepted with a warning;
  final branch uses `VISSIM_BREAK_DISABLE_VALUE`, tolerates up to 3 early pauses, never relies on SimSec after a run.
  `JobRegistry` (current + last 20); status/health never call COM.
- Tools (PLAN 7): files (load_network, save_network), objects (describe [+ EXTENSION include_attributes],
  list_objects, get_attributes, set_attributes, set_attribute_bulk ≤ 5000), simulation (configure, start, status,
  wait ≤ VISSIM_WAIT_MAX_S, resume, stop), results (export_csv `;` utf-8-sig), signals (set_signal_state, JOB_RUNNING while
  running), diagnostics (tail_log ≤ 500 lines of the server log only, exec_python disabled unless
  VISSIM_MCP_ALLOW_EXEC=1). Timeouts: connect → connect_timeout_s; load/save → load_timeout_s; else call_timeout_s.
  All writes audited.

## 4. Reliability requirements

1. The server process never dies because of a tool error. 2. Start-up does not touch COM. 3. Stdout carries only
JSON-RPC. 4. Stdin EOF → exit ≤ 300 ms; Vissim killed (KILL_ON_EXIT) and Job Object as backstop. 5. Never kill a process
not proven ours. 6. health/status never block on COM. 7. Bounded resources. 8. UTF-8 everywhere. 9. A hung or crashed
Vissim is recoverable via vissim_restart without restarting the MCP server.
