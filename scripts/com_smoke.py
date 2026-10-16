"""Minimal COM smoke test for PTV Vissim (PLAN.md M0): Dispatch -> Net.Links.Count -> Exit.

All output goes to stderr. The script only ever terminates a Vissim process that it caused
to start itself (new PID, COM-launched with ``-Embedding``); an interactively opened Vissim
is never touched.

Exit codes: 0 = OK, 2 = license dialog shown (no license with the COM module),
3 = other failure, 4 = pywin32 not available.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time

try:
    import psutil
except ImportError:  # pragma: no cover - environment problem
    print("psutil is required: pip install psutil", file=sys.stderr)
    sys.exit(4)

EXE_PATTERN = re.compile(r"^vissim\d*\.exe$", re.IGNORECASE)
LICENSE_KEYWORDS = ("licen", "lizenz", "лиценз", "ライセンス", "许可", "許可")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def vissim_pids() -> set[int]:
    pids = set()
    for proc in psutil.process_iter(["pid", "name"]):
        name = proc.info.get("name") or ""
        if EXE_PATTERN.match(name):
            pids.add(proc.info["pid"])
    return pids


def is_com_launched(pid: int) -> bool:
    try:
        cmdline = " ".join(psutil.Process(pid).cmdline()).lower()
    except (psutil.Error, OSError):
        return False
    return "embedding" in cmdline or "automation" in cmdline


def window_titles(pid: int) -> list[str]:
    try:
        import win32gui
        import win32process
    except ImportError:
        return []
    titles: list[str] = []

    def callback(hwnd, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid and win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd)
                if text:
                    titles.append(text)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        pass
    return titles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--progid", default="Vissim.Vissim")
    parser.add_argument("--timeout", type=float, default=150.0, help="overall deadline in seconds")
    parser.add_argument("--out", help="optional path for a JSON result file")
    args = parser.parse_args()

    try:
        import pythoncom
        import pywintypes
        from win32com.client import dynamic
    except ImportError:
        log("pywin32 is not installed in this interpreter")
        return 4

    before = vissim_pids()
    result: dict = {"progid": args.progid, "before_pids": sorted(before), "windows": []}
    stop = threading.Event()
    own: dict = {"pid": None}

    def observer() -> None:
        started = time.monotonic()
        while not stop.is_set():
            new = [p for p in vissim_pids() - before if is_com_launched(p)]
            if len(new) == 1:
                own["pid"] = new[0]
                titles = window_titles(new[0])
                if titles and titles not in result["windows"]:
                    result["windows"].append(titles)
                    log(f"Vissim window(s): {titles}")
                if any(k in t.lower() for t in titles for k in LICENSE_KEYWORDS):
                    result["license_dialog"] = titles
                    log("License dialog detected -> terminating the COM-launched Vissim")
                    _kill(new[0], result)
                    return
            if time.monotonic() - started > args.timeout:
                result["deadline_hit"] = True
                if own["pid"]:
                    _kill(own["pid"], result)
                return
            time.sleep(0.25)

    watcher = threading.Thread(target=observer, daemon=True)
    watcher.start()
    pythoncom.CoInitialize()
    t0 = time.monotonic()
    code = 3
    try:
        vissim = dynamic.Dispatch(args.progid)
        result["dispatch_s"] = round(time.monotonic() - t0, 1)
        log(f"Dispatch OK in {result['dispatch_s']} s")
        result["links_count"] = vissim.Net.Links.Count
        log(f"Net.Links.Count = {result['links_count']}")
        vissim.Exit()
        vissim = None
        result["exit_called"] = True
        code = 0
    except pywintypes.com_error as exc:
        result["dispatch_s"] = round(time.monotonic() - t0, 1)
        result["com_error"] = {"hresult": exc.args[0] if exc.args else None, "message": str(exc.args[1:2])}
        log(f"COM error after {result['dispatch_s']} s: {exc.args!r}")
        code = 2 if result.get("license_dialog") else 3
    finally:
        stop.set()
        watcher.join(2)
        pid = own["pid"]
        if pid and psutil.pid_exists(pid):
            for _ in range(40):
                if not psutil.pid_exists(pid):
                    break
                time.sleep(0.25)
            if psutil.pid_exists(pid):
                _kill(pid, result)
        result["after_pids"] = sorted(vissim_pids())
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass

    if result.get("license_dialog"):
        code = 2
    log(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=1, default=str)
    return code


def _kill(pid: int, result: dict) -> None:
    if not is_com_launched(pid):
        result.setdefault("not_killed", []).append(pid)
        return
    try:
        psutil.Process(pid).kill()
        result.setdefault("killed", []).append(pid)
    except psutil.Error as exc:
        result.setdefault("kill_errors", []).append(repr(exc))


if __name__ == "__main__":
    sys.exit(main())
