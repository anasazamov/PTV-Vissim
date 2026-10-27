<#
.SYNOPSIS
  Read-only health check of the vissim-mcp installation ("it worked yesterday, not after the reboot").

.DESCRIPTION
  Checks, each with PASS / WARN / FAIL / SKIP and a fix hint for every problem:
    1. runtime python      - the runtime venv's python.exe runs and is 64-bit
    2. installed code      - installed vissim-mcp version and code identical to the repository; dependencies import
    3. claude registration - "claude mcp get vissim": registered, scope, command = runtime python, health status
    4. configuration       - "python -X utf8 -m vissim_mcp --check" with the registered environment
    5. MCP smoke           - initialize -> tools/list (22 tools) -> vissim_health over stdio
    6. COM ProgID          - the ProgID/CLSID is registered and its LocalServer32 executable exists
    7. CodeMeter           - the CodeMeter license service is running
    8. Vissim license      - licenses listed by "cmu --list-network" (Demo/Student licenses have no COM module)
    9. Vissim processes    - leftover COM-launched (-Embedding) Vissim processes that may hold the license seat
   10. log directory       - the server log directory is writable (a probe file is created and deleted)
   11. last log lines      - the end of vissim-mcp.log

  Changes nothing (except the log-directory probe file); never starts or stops Vissim. Exit code 0 = no FAIL,
  1 = at least one FAIL.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
#>
[CmdletBinding()]
param(
    [string]$RuntimeDir = "",
    # Used when the server is not registered: the projects folder / ProgID the configuration check assumes.
    [string]$ProjectsDir = "D:\VissimProjects",
    [string]$ProgId = "",
    [string]$Name = "vissim",
    [string]$Claude = "",
    # Skip the Claude Code registration check (e.g. after install.ps1 -NoRegister).
    [switch]$NoClaude,
    [int]$LogLines = 15
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $RuntimeDir) { $RuntimeDir = Get-DefaultRuntimeDir }
$RuntimeDir = Get-FullPath $RuntimeDir
$rtPy = Join-Path $RuntimeDir "Scripts\python.exe"
$rows = New-Object System.Collections.Generic.List[object]

function Add-Row([string]$Check, [string]$Status, [string]$Detail, [string]$Fix = "") {
    $script:rows.Add([pscustomobject]@{ Check = $Check; Status = $Status; Detail = $Detail; Fix = $Fix }) | Out-Null
    $color = @{ PASS = "Green"; WARN = "Yellow"; FAIL = "Red"; SKIP = "DarkGray"; INFO = "Gray" }[$Status]
    Write-Host ("[" + $Status.PadRight(4) + "] " + $Check + ": " + $Detail) -ForegroundColor $color
    if ($Fix -and $Status -ne "PASS") { Write-Host ("       fix: " + $Fix) -ForegroundColor $color }
}

$oldOutputEncoding = [Console]::OutputEncoding
try { [Console]::OutputEncoding = $script:Utf8NoBom } catch { }
$reinstall = "powershell -ExecutionPolicy Bypass -File " + (Join-Path $PSScriptRoot "install.ps1")
Write-Host ("vissim-mcp doctor - runtime " + $RuntimeDir)
Write-Host ""

# --- 1. runtime python --------------------------------------------------------------------------------------
$rtOk = $false
if (-not (Test-Path -LiteralPath $rtPy)) {
    Add-Row "runtime python" "FAIL" ("missing: " + $rtPy) $reinstall
} else {
    try {
        $r = Invoke-Native -FilePath $rtPy -Arguments @("-c", "import struct,platform;print(platform.python_version(), struct.calcsize('P')*8)") -TimeoutSec 60
        if ($r.ExitCode -eq 0) {
            $parts = $r.StdOut.Trim() -split " "
            if ($parts[1] -eq "64") { Add-Row "runtime python" "PASS" ("Python " + $parts[0] + " 64-bit: " + $rtPy); $rtOk = $true }
            else { Add-Row "runtime python" "FAIL" ("Python " + $parts[0] + " is " + $parts[1] + "-bit") ($reinstall + " -Force") }
        } else {
            Add-Row "runtime python" "FAIL" ("does not run (exit " + $r.ExitCode + "): " + (Get-Tail $r.StdErr 2)) `
                ("the base Python was probably removed or updated; " + $reinstall + " -Force")
        }
    } catch {
        Add-Row "runtime python" "FAIL" ("cannot start: " + $_.Exception.Message) ($reinstall + " -Force")
    }
}

# --- 2. installed code -------------------------------------------------------------------------------------
if ($rtOk) {
    $r = Invoke-Native -FilePath $rtPy -Arguments @("-X", "utf8", (Join-Path $PSScriptRoot "runtime_info.py"), "--repo-src", (Join-Path $RepoRoot "src")) -TimeoutSec 120
    $info = $null
    try { $info = $r.StdOut.Trim() | ConvertFrom-Json } catch { }
    if (-not $info) {
        Add-Row "installed code" "FAIL" ("runtime_info.py failed: " + (Get-Tail $r.StdErr 3)) ($reinstall + " -Force")
    } elseif (-not $info.installed) {
        Add-Row "installed code" "FAIL" ("vissim_mcp does not import: " + $info.install_error) $reinstall
    } elseif (-not $info.modules_ok) {
        $errs = ($info.module_errors.PSObject.Properties | ForEach-Object { $_.Name + ": " + $_.Value }) -join "; "
        Add-Row "installed code" "FAIL" ("dependencies do not import: " + $errs) ($reinstall + " -Force")
    } elseif (-not $info.matches_repo) {
        Add-Row "installed code" "WARN" ("installed " + $info.version + ", repository " + $info.repo_version + " - the installed code differs from the repository") `
            ("after 'git pull' or local changes: " + $reinstall)
    } else {
        Add-Row "installed code" "PASS" ("vissim-mcp " + $info.version + " (identical to the repository), editable=" + $info.editable)
    }
} else {
    Add-Row "installed code" "SKIP" "runtime python does not run"
}

# --- 3. claude registration --------------------------------------------------------------------------------
$registered = $null
if ($NoClaude) {
    Add-Row "claude registration" "SKIP" "-NoClaude"
} else {
    $claudeExe = Find-Claude $Claude
    if (-not $claudeExe) {
        Add-Row "claude registration" "FAIL" "claude.exe not found (PATH or %USERPROFILE%\.local\bin\claude.exe)" "install Claude Code (native installer)"
    } else {
        $helperPy = $rtPy
        if (-not $rtOk) {
            $helperPy = $null
            $pyCmd = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($pyCmd) { $helperPy = $pyCmd.Source }
        }
        if (-not $helperPy) {
            Add-Row "claude registration" "SKIP" "no Python available to run register_claude.py"
        } else {
            $pre = @()
            if ($helperPy -like "*py.exe") { $pre = @("-3") }
            $r = Invoke-Native -FilePath $helperPy -Arguments ($pre + @("-X", "utf8", (Join-Path $PSScriptRoot "register_claude.py"), "--show", "--name", $Name, "--claude", $claudeExe)) -TimeoutSec 300
            try { $registered = $r.StdOut.Trim() | ConvertFrom-Json } catch { $registered = $null }
            if (-not $registered) {
                Add-Row "claude registration" "FAIL" ("'claude mcp get " + $Name + "' could not be read: " + (Get-Tail $r.StdErr 3)) "run: claude mcp list"
            } elseif (-not $registered.found) {
                Add-Row "claude registration" "FAIL" ("'" + $Name + "' is not registered in Claude Code") $reinstall
                $registered = $null
            } else {
                $problems = @()
                $cmdPath = ""
                if ($registered.command) { $cmdPath = $registered.command.Trim('"') }
                if (-not $cmdPath -or ((Get-FullPath $cmdPath) -ne (Get-FullPath $rtPy))) {
                    $problems += ("command is '" + $registered.command + "', not the runtime python")
                }
                if ($registered.scope -and $registered.scope -ne "user") {
                    $problems += ("scope is '" + $registered.scope + "' (user scope is recommended)")
                }
                $status = "" + $registered.status
                if ($status -match "(?i)fail|error") { $problems += ("health status: " + $status) }
                if ($problems.Count -eq 0) {
                    Add-Row "claude registration" "PASS" ("scope " + $registered.scope + ", status " + $status + ", " + $registered.command)
                } else {
                    Add-Row "claude registration" "FAIL" ($problems -join "; ") ($reinstall + "  (then /mcp -> " + $Name + " -> Reconnect)")
                }
            }
        }
    }
}

# --- 4./5. configuration and MCP smoke with the registered (or default) environment ----------------------------
$smoke = $null
if ($rtOk) {
    $serverEnv = @{}
    $source = "registered"
    if ($registered -and $registered.env) {
        foreach ($p in $registered.env.PSObject.Properties) { $serverEnv[$p.Name] = [string]$p.Value }
    }
    if ($serverEnv.Count -eq 0) {
        $source = "default (not registered)"
        $pid0 = $ProgId
        if (-not $pid0) { $pid0 = "Vissim.Vissim" }
        $serverEnv = @{ PYTHONUTF8 = "1"; VISSIM_PROGID = $pid0; VISSIM_ALLOWED_DIRS = $ProjectsDir; VISSIM_KILL_ON_EXIT = "1" }
    }
    $r = Invoke-Native -FilePath $rtPy -Arguments @("-X", "utf8", "-m", "vissim_mcp", "--check") -Environment $serverEnv -TimeoutSec 120
    if ($r.ExitCode -eq 0) {
        $warn = @(($r.StdErr -split "`r?`n") | Where-Object { $_ -like "vissim-mcp: warning:*" })
        if ($warn.Count -gt 0) { Add-Row "configuration" "WARN" ($source + " env: " + ($warn -join " | ")) "correct the variable named in the warning" }
        else { Add-Row "configuration" "PASS" ($source + " env is valid (VISSIM_ALLOWED_DIRS=" + $serverEnv["VISSIM_ALLOWED_DIRS"] + ")") }
    } else {
        Add-Row "configuration" "FAIL" ($source + " env: " + (Get-Tail $r.StdErr 3)) "fix the variable named in the message (re-register with install.ps1)"
    }

    $server = @{ type = "stdio"; command = $rtPy; args = @("-X", "utf8", "-m", "vissim_mcp"); env = $serverEnv }
    $jsonFile = Join-Path ([System.IO.Path]::GetTempPath()) ("vissim-mcp-doctor-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        [System.IO.File]::WriteAllText($jsonFile, ($server | ConvertTo-Json -Depth 4), $script:Utf8NoBom)
        $r = Invoke-Native -FilePath $rtPy -Arguments @("-X", "utf8", (Join-Path $PSScriptRoot "mcp_smoke.py"), "--server-json", $jsonFile, "--json") -TimeoutSec 300
    } finally {
        Remove-Item -LiteralPath $jsonFile -Force -ErrorAction SilentlyContinue
    }
    try { $smoke = ($r.StdOut.Trim() -split "`r?`n" | Select-Object -Last 1) | ConvertFrom-Json } catch { $smoke = $null }
    if ($r.ExitCode -eq 0 -and $smoke -and $smoke.ok) {
        Add-Row "MCP smoke" "PASS" ("" + $smoke.tools + " tools, vissim_health ok, start-up " + $smoke.startup_s + " s, exit " + $smoke.exit_after_stdin_close_ms + " ms after stdin close")
    } else {
        Add-Row "MCP smoke" "FAIL" (Get-Tail $r.StdErr 4) ("see docs\troubleshooting.md; " + $reinstall + " -Force")
    }
} else {
    Add-Row "configuration" "SKIP" "runtime python does not run"
    Add-Row "MCP smoke" "SKIP" "runtime python does not run"
}

# --- 6. COM ProgID -----------------------------------------------------------------------------------------
$progIdToCheck = $ProgId
if (-not $progIdToCheck -and $registered -and $registered.env -and ($registered.env.PSObject.Properties.Name -contains "VISSIM_PROGID")) {
    $progIdToCheck = $registered.env.VISSIM_PROGID
}
if (-not $progIdToCheck) { $progIdToCheck = "Vissim.Vissim" }
try {
    $clsid = $null
    if ($progIdToCheck -match '^\{') { $clsid = $progIdToCheck }
    else { $clsid = (Get-ItemProperty -LiteralPath ("Registry::HKEY_CLASSES_ROOT\" + $progIdToCheck + "\CLSID") -ErrorAction Stop).'(default)' }
    $server32 = (Get-ItemProperty -LiteralPath ("Registry::HKEY_CLASSES_ROOT\CLSID\" + $clsid + "\LocalServer32") -ErrorAction Stop).'(default)'
    $exe = $server32.Trim()
    if ($exe.StartsWith('"')) { $exe = $exe.Substring(1).Split('"')[0] } else { $exe = ($exe -split '\s+-')[0].Trim() }
    if (Test-Path -LiteralPath $exe) {
        Add-Row "COM ProgID" "PASS" ($progIdToCheck + " -> " + $clsid + " -> " + $exe)
    } else {
        Add-Row "COM ProgID" "FAIL" ("LocalServer32 executable missing: " + $exe) "start Vissim as administrator -> Help -> Register COM Server"
    }
} catch {
    Add-Row "COM ProgID" "FAIL" ($progIdToCheck + " is not registered") "start Vissim as administrator -> Help -> Register COM Server (or set -ProgId / VISSIM_PROGID)"
}

# --- 7. CodeMeter ------------------------------------------------------------------------------------------
$svc = @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "CodeMeter" })
if ($svc.Count -eq 0) {
    Add-Row "CodeMeter" "FAIL" "CodeMeter service not found" "install the CodeMeter runtime (comes with Vissim)"
} else {
    $running = @($svc | Where-Object { $_.Status -eq "Running" })
    $desc = ($svc | ForEach-Object { $_.Name + "=" + $_.Status + "/" + $_.StartType }) -join ", "
    if ($running.Count -gt 0) { Add-Row "CodeMeter" "PASS" $desc }
    else { Add-Row "CodeMeter" "FAIL" $desc "as administrator: Start-Service CodeMeter; set its start type to Automatic (services.msc)" }
}

# --- 8. Vissim license ---------------------------------------------------------------------------------------
$cmu = $null
foreach ($cand in @("C:\Program Files\CodeMeter\Runtime\bin\cmu.exe", "C:\Program Files (x86)\CodeMeter\Runtime\bin\cmu32.exe")) {
    if (Test-Path -LiteralPath $cand) { $cmu = $cand; break }
}
if (-not $cmu) {
    Add-Row "Vissim license" "WARN" "cmu.exe not found; licenses not listed" "check Vissim -> Help -> License"
} else {
    $r = Invoke-Native -FilePath $cmu -Arguments @("--list-network") -TimeoutSec 60
    $products = @(); $current = $null
    foreach ($l in ($r.StdOut -split "`r?`n")) {
        if ($l -match "^\s*Text:\s*(.+)$") { $current = $Matches[1].Trim() }
        elseif ($l -match "Used=\s*(\d+)\s+Free=\s*(\d+)\s+Total=\s*(\d+)" -and $current) {
            $products += ($current + " (used " + $Matches[1] + ", free " + $Matches[2] + ")"); $current = $null
        }
    }
    $vissim = @($products | Where-Object { $_ -match "Vissim" })
    $full = @($vissim | Where-Object { $_ -notmatch "(?i)demo|student" })
    if ($full.Count -gt 0) { Add-Row "Vissim license" "PASS" ($full -join "; ") }
    elseif ($vissim.Count -gt 0) {
        Add-Row "Vissim license" "FAIL" ("only " + ($vissim -join "; ") + " - no COM module") "activate a full Vissim license with the COM module (or PTV Hub subscription) and save it as default"
    } else {
        Add-Row "Vissim license" "WARN" "no local Vissim license listed (a network / PTV Hub license may still work)" "check Vissim -> Help -> License"
    }
}

# --- 9. Vissim processes (read-only) ---------------------------------------------------------------------------
$procs = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'Vissim%'" -ErrorAction SilentlyContinue)
$embedded = @($procs | Where-Object { $_.CommandLine -match "(?i)-Embedding" })
if ($embedded.Count -gt 0) {
    $ids = ($embedded | ForEach-Object { "PID " + $_.ProcessId }) -join ", "
    Add-Row "Vissim processes" "WARN" ("COM-launched Vissim running: " + $ids + " (owned by a running or crashed MCP server)") `
        "if no Claude Code session uses it: vissim_close / vissim_restart, or end exactly that PID in Task Manager"
} else {
    Add-Row "Vissim processes" "PASS" ("" + $procs.Count + " Vissim process(es), none COM-launched")
}

# --- 10. log directory ------------------------------------------------------------------------------------------
$logDir = $null
if ($smoke -and $smoke.ok -and $smoke.log_dir) { $logDir = $smoke.log_dir } else { $logDir = Join-Path (Get-DefaultDataDir) "logs" }
try {
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $probe = Join-Path $logDir (".doctor-" + [guid]::NewGuid().ToString("N") + ".tmp")
    [System.IO.File]::WriteAllText($probe, "ok", $script:Utf8NoBom)
    Remove-Item -LiteralPath $probe -Force
    Add-Row "log directory" "PASS" ("writable: " + $logDir)
} catch {
    Add-Row "log directory" "FAIL" ("not writable: " + $logDir + " (" + $_.Exception.Message + ")") "check permissions / antivirus, or set VISSIM_LOG_DIR"
}

# --- 11. last log lines -------------------------------------------------------------------------------------------
$logFile = Join-Path $logDir "vissim-mcp.log"
if (Test-Path -LiteralPath $logFile) {
    $tail = @(Get-Content -LiteralPath $logFile -Tail $LogLines -Encoding UTF8)
    $errors = @($tail | Where-Object { $_ -match " (ERROR|CRITICAL) " })
    if ($errors.Count -gt 0) { Add-Row "last log lines" "WARN" ("" + $errors.Count + " error line(s) in the last " + $LogLines + " lines of " + $logFile) "read them below / vissim_tail_log" }
    else { Add-Row "last log lines" "INFO" ($logFile) }
    Write-Host ""
    Write-Host ("--- last " + $tail.Count + " lines of " + $logFile) -ForegroundColor DarkGray
    foreach ($line in $tail) { Write-Host ("    " + $line) -ForegroundColor DarkGray }
} else {
    Add-Row "last log lines" "INFO" ("no log file yet: " + $logFile)
}

try { [Console]::OutputEncoding = $oldOutputEncoding } catch { }

Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
$rows | Format-Table Check, Status, Detail -AutoSize -Wrap | Out-String -Width 160 | Write-Host
$failed = @($rows | Where-Object { $_.Status -eq "FAIL" })
if ($failed.Count -gt 0) {
    Write-Host ("" + $failed.Count + " check(s) FAILED:") -ForegroundColor Red
    foreach ($f in $failed) { Write-Host ("  - " + $f.Check + ": " + $f.Fix) -ForegroundColor Red }
    Write-Host "Details: docs\troubleshooting.md"
    exit 1
}
Write-Host "No failed checks." -ForegroundColor Green
exit 0
