<#
.SYNOPSIS
  vissim-mcp pre-flight checks (PLAN.md, milestone M0).

.DESCRIPTION
  Checks the prerequisites for running vissim-mcp on this computer and writes the results into
  the automatic section of docs\preflight-report.md (between the preflight:auto markers; the rest
  of the report, e.g. answers to Q1-Q7, is preserved).

  Checks: Windows version, Python interpreter (64-bit, >= 3.11), registered Vissim COM ProgIDs,
  COM server executable and type library, running Vissim processes, CodeMeter service, Vissim
  licenses visible to CodeMeter, and (optionally, -ComSmoke) a real COM round trip
  Dispatch -> Net.Links.Count -> Exit via scripts\com_smoke.py.

  The COM smoke test only ever terminates the Vissim process that it started itself.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1
  powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1 -ComSmoke
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$ComSmoke,
    [string]$Report = ""
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Report) { $Report = Join-Path $RepoRoot "docs\preflight-report.md" }
if (-not $Python) {
    $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) { $Python = $venvPy } else { $Python = "py" }
}

$lines = New-Object System.Collections.Generic.List[string]
$problems = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$text) { $script:lines.Add($text) | Out-Null }
function Add-Problem([string]$text) { $script:problems.Add($text) | Out-Null }

Add-Line ("Generated: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz") + " on " + $env:COMPUTERNAME)
Add-Line ""

# --- Windows ---------------------------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem
Add-Line "### Windows"
Add-Line ("- " + $os.Caption + " " + $os.Version + " (" + $os.OSArchitecture + ")")
Add-Line ("- ANSI code page: " + [System.Text.Encoding]::Default.CodePage + " (vissim-mcp forces UTF-8 via PYTHONUTF8=1 / -X utf8)")
Add-Line ""

# --- Python ----------------------------------------------------------------------------
Add-Line "### Python"
$pyArgs = @()
if ($Python -eq "py") { $pyArgs = @("-3.12") }
$pyInfo = & $Python @pyArgs -c "import sys,struct;print(sys.executable);print('%d.%d.%d' % sys.version_info[:3]);print(struct.calcsize('P')*8)" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $pyInfo) {
    Add-Line ("- Python not runnable: " + $Python)
    Add-Problem "Python 3.11+ 64-bit is not available"
} else {
    $pyExe = $pyInfo[0]; $pyVer = $pyInfo[1]; $pyBits = $pyInfo[2]
    Add-Line ("- Interpreter: " + $pyExe)
    Add-Line ("- Version: " + $pyVer + ", " + $pyBits + "-bit")
    $parts = $pyVer.Split(".")
    if ([int]$pyBits -ne 64) { Add-Problem "Python is not 64-bit" }
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 11)) { Add-Problem "Python older than 3.11" }
    $mods = & $Python @pyArgs -c "import importlib.util as u;print(','.join(m+'='+str(bool(u.find_spec(m))) for m in ('pythoncom','win32com','psutil','mcp')))" 2>$null
    Add-Line ("- Modules: " + $mods)
}
Add-Line ""

# --- COM registration ------------------------------------------------------------------
Add-Line "### Vissim COM registration"
$progIds = Get-ChildItem Registry::HKEY_CLASSES_ROOT -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -like "Vissim.Vissim*" }
if (-not $progIds) {
    Add-Line "- No Vissim.Vissim* ProgID found"
    Add-Problem "Vissim COM server is not registered (run Vissim as administrator -> Help -> Register COM Server)"
} else {
    $clsids = @{}
    foreach ($p in $progIds) {
        $clsid = (Get-ItemProperty -Path ("Registry::HKEY_CLASSES_ROOT\" + $p.PSChildName + "\CLSID") -ErrorAction SilentlyContinue).'(default)'
        Add-Line ("- ``" + $p.PSChildName + "`` -> " + $clsid)
        if ($clsid) { $clsids[$clsid] = $true }
    }
    foreach ($c in $clsids.Keys) {
        $base = "Registry::HKEY_CLASSES_ROOT\CLSID\" + $c
        $server = (Get-ItemProperty -Path ($base + "\LocalServer32") -ErrorAction SilentlyContinue).'(default)'
        $typelib = (Get-ItemProperty -Path ($base + "\TypeLib") -ErrorAction SilentlyContinue).'(default)'
        Add-Line ("- CLSID " + $c + ": LocalServer32 = ``" + $server + "``, TypeLib = " + $typelib)
        if ($server) {
            $exe = $server.Trim('"').Split('"')[0]
            if (-not (Test-Path $exe)) { Add-Problem ("COM server executable missing: " + $exe) }
            else { Add-Line ("- Executable version: " + (Get-Item $exe).VersionInfo.FileVersion) }
        }
    }
}
Add-Line ""

# --- Processes ---------------------------------------------------------------------------
Add-Line "### Running Vissim processes"
$procs = Get-CimInstance Win32_Process -Filter "Name LIKE 'Vissim%'" -ErrorAction SilentlyContinue
if (-not $procs) { Add-Line "- none" }
foreach ($pr in $procs) {
    $kind = "interactive"
    if ($pr.CommandLine -match "(?i)embedding|automation") { $kind = "COM-launched" }
    Add-Line ("- PID " + $pr.ProcessId + " " + $pr.Name + " (" + $kind + ")")
}
Add-Line ""

# --- CodeMeter / licenses ----------------------------------------------------------------
Add-Line "### CodeMeter and licenses"
$svc = Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match "CodeMeter" }
if (-not $svc) { Add-Line "- CodeMeter service not found"; Add-Problem "CodeMeter runtime is not installed" }
foreach ($s in $svc) { Add-Line ("- Service " + $s.Name + ": " + $s.Status + " (" + $s.StartType + ")") }
$cmu = $null
foreach ($cand in @("C:\Program Files\CodeMeter\Runtime\bin\cmu.exe", "C:\Program Files (x86)\CodeMeter\Runtime\bin\cmu32.exe")) {
    if (Test-Path $cand) { $cmu = $cand; break }
}
if ($cmu) {
    $net = & $cmu --list-network 2>$null
    $products = @()
    $current = $null
    foreach ($l in $net) {
        if ($l -match "^\s*Text:\s*(.+)$") { $current = $Matches[1].Trim() }
        elseif ($l -match "Used=\s*(\d+)\s+Free=\s*(\d+)\s+Total=\s*(\d+)" -and $current) {
            $products += ("" + $current + " (used " + $Matches[1] + ", free " + $Matches[2] + ", total " + $Matches[3] + ")")
            $current = $null
        }
    }
    if ($products.Count -eq 0) { Add-Line "- No licenses listed by CodeMeter" }
    foreach ($pd in $products) { Add-Line ("- License: " + $pd) }
    $vissimProducts = @($products | Where-Object { $_ -match "Vissim" })
    $onlyDemo = ($vissimProducts.Count -gt 0) -and (@($vissimProducts | Where-Object { $_ -notmatch "(?i)demo|student" }).Count -eq 0)
    if ($onlyDemo) { Add-Problem "Only Demo/Student Vissim licenses found - these do not include the COM module" }
    if ($vissimProducts.Count -eq 0) { Add-Line "- (no local Vissim license; a network or PTV Hub subscription license may still be used)" }
} else {
    Add-Line "- cmu.exe not found; licenses not listed"
}
Add-Line ""

# --- COM smoke test ------------------------------------------------------------------------
if ($ComSmoke) {
    Add-Line "### COM smoke test (Dispatch -> Net.Links.Count -> Exit)"
    $smokeOut = Join-Path $env:TEMP ("vissim-com-smoke-" + [guid]::NewGuid().ToString() + ".json")
    & $Python @pyArgs -X utf8 (Join-Path $PSScriptRoot "com_smoke.py") --out $smokeOut 2>&1 | ForEach-Object { Write-Host $_ }
    $rc = $LASTEXITCODE
    if (Test-Path $smokeOut) {
        $res = Get-Content $smokeOut -Raw -Encoding UTF8 | ConvertFrom-Json
        Remove-Item $smokeOut -ErrorAction SilentlyContinue
        if ($rc -eq 0) { Add-Line ("- OK: Dispatch " + $res.dispatch_s + " s, Net.Links.Count = " + $res.links_count) }
        elseif ($rc -eq 2) {
            Add-Line ("- FAILED: license dialog shown by the COM-launched Vissim: " + (($res.license_dialog) -join " | "))
            Add-Problem "No Vissim license with the COM module is available to COM-launched instances"
        } else {
            Add-Line ("- FAILED (exit " + $rc + "): " + ($res.com_error | ConvertTo-Json -Compress))
            Add-Problem "COM smoke test failed"
        }
    } else {
        Add-Line ("- FAILED (exit " + $rc + "): no result file")
        Add-Problem "COM smoke test failed"
    }
    Add-Line ""
}

# --- Summary -------------------------------------------------------------------------------
Add-Line "### Summary"
if ($problems.Count -eq 0) { Add-Line "- All checks passed" }
foreach ($pb in $problems) { Add-Line ("- PROBLEM: " + $pb) }

# --- Write the automatic section of the report ------------------------------------------------
$start = "<!-- preflight:auto:start -->"
$end = "<!-- preflight:auto:end -->"
$section = $start + "`n" + ($lines -join "`n") + "`n" + $end
$utf8 = New-Object System.Text.UTF8Encoding($false)
if (Test-Path $Report) {
    $content = [System.IO.File]::ReadAllText($Report, $utf8)
    $i = $content.IndexOf($start); $j = $content.IndexOf($end)
    if ($i -ge 0 -and $j -gt $i) {
        $content = $content.Substring(0, $i) + $section + $content.Substring($j + $end.Length)
    } else {
        $content = $content.TrimEnd() + "`n`n## Automatic checks`n`n" + $section + "`n"
    }
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Report) | Out-Null
    $content = "# Pre-flight report`n`n## Automatic checks`n`n" + $section + "`n"
}
[System.IO.File]::WriteAllText($Report, $content, $utf8)

$lines | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host ("Report updated: " + $Report)
if ($problems.Count -gt 0) { exit 1 } else { exit 0 }
