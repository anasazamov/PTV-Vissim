<#
.SYNOPSIS
  Install (or refresh) the vissim-mcp server for Claude Code so that it works at any time, also after a reboot.

.DESCRIPTION
  Idempotent; run it again after every "git pull" or when scripts\doctor.ps1 reports a problem.

   1. Picks a 64-bit Python 3.12 (then 3.13, 3.11) through the py launcher (or uses -Python).
      Unless -NoRegister or -Defaults: reads the current registration ("claude mcp get vissim" in -Scope) and
      keeps its runtime folder, VISSIM_ALLOWED_DIRS, VISSIM_PROGID and extra variables for every parameter that
      is not given explicitly, so a plain rerun never resets them. -ExtraEnv is merged over the kept variables
      ('KEY=' removes one).
   2. Creates or refreshes a dedicated runtime venv OUTSIDE the repository (default
      %LOCALAPPDATA%\vissim-mcp\venv). A broken venv (e.g. its base Python was removed or updated to another
      minor version, or pip is missing) is recreated. Recreating never deletes the old venv first: it stops if a
      process (a Claude Code session's server) runs from it, moves it aside to <venv>.previous, and puts it back
      if any later step fails; it is deleted only after the new venv passed the smoke test.
   3. Installs the package NON-editable from a temporary copy of the repository (the repository stays clean)
      and force-reinstalls the package itself, so the current code is installed even with the same version.
      Dependencies are installed with the tested versions of requirements-runtime.txt (pip -c).
   4. Checks the runtime: 64-bit, dependencies importable, installed code identical to the repository.
   5. Creates <ProjectsDir>\models\original (the read-only folder for original models).
   6. Validates the configuration with "python -X utf8 -P -m vissim_mcp --check" using exactly the environment
      that will be registered.
   7. MCP stdio smoke test (scripts\mcp_smoke.py): initialize -> tools/list (22 tools) -> vissim_health.
   8. Unless -NoRegister: registers the server with Claude Code (scripts\register_claude.py ->
      "claude mcp add-json vissim <json> -s <Scope>"). User scope = available in every folder, survives reboots,
      no project approval prompt.

  Never starts Vissim. Exit code 0 = success, 1 = a step failed (the message says which and how to fix it).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ProjectsDir E:\Vissim -ProgId Vissim.Vissim.2026
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -NoRegister -RuntimeDir C:\Temp\rt -ProjectsDir C:\Temp\proj
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Defaults    # ignore the current registration's settings
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$RuntimeDir = "",
    [string]$ProjectsDir = "D:\VissimProjects",
    [string]$ProgId = "Vissim.Vissim",
    [ValidateSet("user", "project", "local")][string]$Scope = "user",
    [switch]$NoRegister,
    [switch]$Force,
    # Additional server variables, e.g. -ExtraEnv 'VISSIM_CALL_TIMEOUT_S=180','VISSIM_MAX_ROWS=1000'.
    [string[]]$ExtraEnv = @(),
    # Path of claude.exe (default: PATH, then %USERPROFILE%\.local\bin\claude.exe).
    [string]$Claude = "",
    # Do not reuse the settings of the current registration; use the given parameters and the defaults.
    [switch]$Defaults
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

$RepoRoot = Split-Path -Parent $PSScriptRoot
$results = New-Object System.Collections.Generic.List[object]
$started = Get-Date
$explicit = @{}
foreach ($k in $PSBoundParameters.Keys) { $explicit[$k] = $true }
$AllowedDirs = ""
$rtPy = ""
$script:venvBackup = $null

function Write-Step([string]$Text) { Write-Host ""; Write-Host ("==> " + $Text) -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host ("    " + $Text) }
function Write-Warn([string]$Text) { Write-Host ("    WARNING: " + $Text) -ForegroundColor Yellow }
function Add-Result([string]$Step, [string]$Status, [string]$Detail) {
    $script:results.Add([pscustomobject]@{ Step = $Step; Status = $Status; Detail = $Detail }) | Out-Null
}
function Fail([string]$Step, [string]$Message, [string]$Hint = "") {
    Add-Result $Step "FAIL" $Message
    $text = $Message
    if ($Hint) { $text = $text + [Environment]::NewLine + "Fix: " + $Hint }
    throw (New-Object System.Exception $text)
}

function Get-PythonProbe([string]$Exe, [string[]]$Prefix = @()) {
    $code = "import sys,struct,json;print(json.dumps({'exe':sys.executable,'ver':'%d.%d.%d'%sys.version_info[:3],'bits':struct.calcsize('P')*8,'venv':sys.prefix!=sys.base_prefix}))"
    try {
        $r = Invoke-Native -FilePath $Exe -Arguments ($Prefix + @("-c", $code)) -TimeoutSec 60
    } catch {
        return $null
    }
    if ($r.ExitCode -ne 0) { return $null }
    try { return ($r.StdOut.Trim() | ConvertFrom-Json) } catch { return $null }
}

function Select-BasePython {
    if ($Python) {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            Fail "python" ("-Python does not exist: " + $Python) "pass the full path of a 64-bit python.exe 3.11+"
        }
        $probe = Get-PythonProbe $Python
        if (-not $probe) { Fail "python" ("cannot run " + $Python) "check the Python installation" }
        return $probe
    }
    $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        foreach ($v in @("3.12", "3.13", "3.11")) {
            foreach ($tag in @("-$v-64", "-$v")) {
                $probe = Get-PythonProbe $py.Source @($tag)
                if ($probe -and [int]$probe.bits -eq 64 -and -not $probe.venv) { return $probe }
            }
        }
    }
    $cand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
    if ($cand) {
        $probe = Get-PythonProbe $cand.Source
        if ($probe -and [int]$probe.bits -eq 64 -and $probe.ver -match '^3\.(11|12|13)\.') { return $probe }
    }
    Fail "python" "no 64-bit Python 3.12 / 3.13 / 3.11 found (py launcher or PATH)" `
        "install Python 3.12 (64-bit) from python.org with the 'py launcher' option, or pass -Python <path>"
}

function Backup-Venv([string]$Dir) {
    # Move the old venv aside instead of deleting it: nothing is lost if it is in use, and it is put back if a
    # later step fails (Restore-Venv). Deleted only after the new venv passed the smoke test.
    if (-not (Test-Path -LiteralPath (Join-Path $Dir "pyvenv.cfg"))) {
        Fail "runtime venv" ("refusing to replace " + $Dir + ": it is not a Python venv (no pyvenv.cfg)") `
            "choose an empty or venv folder with -RuntimeDir"
    }
    $users = @(Get-ProcessUnder $Dir)
    if ($users.Count -gt 0) {
        $list = ($users | ForEach-Object { $_.Name + " PID " + $_.ProcessId }) -join ", "
        Fail "runtime venv" ("the runtime venv is in use (" + $list + "); it was not changed") `
            "close every Claude Code session that uses the server (or /mcp -> vissim -> Disable), then rerun"
    }
    $backup = $Dir + ".previous"
    if (Test-Path -LiteralPath $backup) {
        try { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction Stop }
        catch { $backup = $Dir + ".previous-" + [guid]::NewGuid().ToString("N").Substring(0, 8) }
    }
    try {
        Rename-Item -LiteralPath $Dir -NewName (Split-Path -Leaf $backup) -ErrorAction Stop
    } catch {
        Fail "runtime venv" ("cannot move the old venv aside (a file in it is in use): " + $_.Exception.Message + "; nothing was deleted") `
            "close every Claude Code session that uses the server (or /mcp -> vissim -> Disable), then rerun"
    }
    $script:venvBackup = $backup
    Write-Info ("old venv moved aside: " + $backup + " (restored if the installation fails)")
}

function Restore-Venv {
    if (-not $script:venvBackup -or -not (Test-Path -LiteralPath $script:venvBackup)) { return }
    try {
        if (Test-Path -LiteralPath $RuntimeDir) { Remove-Item -LiteralPath $RuntimeDir -Recurse -Force -ErrorAction Stop }
        Rename-Item -LiteralPath $script:venvBackup -NewName (Split-Path -Leaf $RuntimeDir) -ErrorAction Stop
        Write-Host ("The previous runtime venv was restored: " + $RuntimeDir + " (the server keeps working as before).") -ForegroundColor Yellow
    } catch {
        Write-Host ("Could not restore the previous runtime venv: " + $_.Exception.Message) -ForegroundColor Red
        Write-Host ("  It is kept at " + $script:venvBackup + ". Fix the problem above and rerun the installer.") -ForegroundColor Red
    }
    $script:venvBackup = $null
}

function Remove-VenvBackup {
    if (-not $script:venvBackup) { return }
    try {
        Remove-Item -LiteralPath $script:venvBackup -Recurse -Force -ErrorAction Stop
        Write-Info ("deleted the old venv " + $script:venvBackup)
    } catch {
        Write-Warn ("could not delete the old venv " + $script:venvBackup + " (" + $_.Exception.Message + "); delete it by hand")
    }
    $script:venvBackup = $null
}

function Read-CurrentSettings([string]$HelperPython) {
    # The parameters of the existing registration in -Scope; $null if there is none (or -NoRegister/-Defaults).
    if ($NoRegister -or $Defaults) { return $null }
    $claudeExe = Find-Claude $Claude
    if (-not $claudeExe) { return $null }
    $a = @("-X", "utf8", (Join-Path $PSScriptRoot "register_claude.py"), "--current-settings", "--scope", $Scope, "--claude", $claudeExe)
    if ($Scope -ne "user") {
        if (-not (Test-Path -LiteralPath $ProjectsDir -PathType Container)) { return $null }
        $a += @("--project-dir", $ProjectsDir)
    }
    $r = Invoke-Native -FilePath $HelperPython -Arguments $a -TimeoutSec 600
    $cur = $null
    try { $cur = $r.StdOut.Trim() | ConvertFrom-Json } catch { }
    if ($r.ExitCode -ne 0 -or -not $cur) {
        Fail "current settings" ("cannot read the current registration: " + (Get-Tail $r.StdErr 5)) `
            "check 'claude mcp get vissim'; or rerun with -Defaults (then give -ProjectsDir/-ProgId/-ExtraEnv again)"
    }
    if (-not $cur.found) { return $null }
    return $cur
}

$oldOutputEncoding = [Console]::OutputEncoding
$exitCode = 0
try {
    try { [Console]::OutputEncoding = $script:Utf8NoBom } catch { }

    Write-Host ("vissim-mcp installer - repository " + $RepoRoot)

    # --- 0. Arguments -----------------------------------------------------------------------------------
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "pyproject.toml")) -or
        -not (Test-Path -LiteralPath (Join-Path $RepoRoot "src\vissim_mcp\__init__.py"))) {
        Fail "arguments" ("not a vissim-mcp repository: " + $RepoRoot) "run the script from the repository's scripts folder"
    }

    # --- 1. Python ----------------------------------------------------------------------------------------
    Write-Step "Selecting a 64-bit Python (3.12, then 3.13, 3.11)"
    $base = Select-BasePython
    Write-Info ("Python " + $base.ver + " (" + $base.bits + "-bit): " + $base.exe)
    if ($base.ver -notmatch '^3\.(11|12|13)\.') { Write-Warn ("Python " + $base.ver + " is not tested; 3.12 is recommended") }
    if ([int]$base.bits -ne 64) { Fail "python" "Python is not 64-bit" "install 64-bit Python 3.12" }
    $baseMinor = ($base.ver -split '\.')[0..1] -join '.'
    Add-Result "python" "OK" ("Python " + $base.ver + " 64-bit")

    # --- 1b. Settings of the current registration (a plain rerun keeps them) --------------------------------
    $keptEnv = @()
    if (-not $NoRegister -and -not $Defaults) {
        Write-Step ("Reading the current registration (" + $Scope + " scope)")
        $cur = Read-CurrentSettings $base.exe
        if (-not $cur) {
            Write-Info "no current registration in this scope; using the given parameters and the defaults"
            Add-Result "current settings" "OK" "none registered"
        } else {
            $kept = @()
            if (-not $explicit.ContainsKey("RuntimeDir") -and $cur.runtime_dir) {
                if ((Test-PathInside $cur.runtime_dir $RepoRoot) -or (Test-PathInside $RepoRoot $cur.runtime_dir)) {
                    Write-Warn ("the registered runtime " + $cur.runtime_dir + " is inside the repository; using the default runtime folder instead")
                } else {
                    $RuntimeDir = $cur.runtime_dir; $kept += ("RuntimeDir=" + $RuntimeDir)
                }
            }
            if (-not $explicit.ContainsKey("ProjectsDir") -and $cur.allowed_dirs) {
                $AllowedDirs = [string]$cur.allowed_dirs
                $ProjectsDir = (($AllowedDirs -split ';') | Where-Object { $_.Trim() } | Select-Object -First 1).Trim()
                $kept += ("ProjectsDir=" + $AllowedDirs)
            }
            if (-not $explicit.ContainsKey("ProgId") -and $cur.progid) { $ProgId = [string]$cur.progid; $kept += ("ProgId=" + $ProgId) }
            $keptEnv = @($cur.extra_env | Where-Object { $_ })
            if ($keptEnv.Count -gt 0) { $kept += ("ExtraEnv=" + ($keptEnv -join ",")) }
            $detail = "nothing to keep"
            if ($kept.Count -gt 0) { $detail = "kept " + ($kept -join "; ") }
            Write-Info ($detail + "  (give a parameter explicitly to change it; -Defaults ignores the registration)")
            Add-Result "current settings" "OK" $detail
        }
    }

    Write-Step "Checking arguments"
    if (-not $RuntimeDir) { $RuntimeDir = Get-DefaultRuntimeDir }
    if (-not [System.IO.Path]::IsPathRooted($RuntimeDir)) { Fail "arguments" ("-RuntimeDir must be absolute: " + $RuntimeDir) }
    if (-not [System.IO.Path]::IsPathRooted($ProjectsDir)) { Fail "arguments" ("-ProjectsDir must be absolute: " + $ProjectsDir) }
    $RuntimeDir = Get-FullPath $RuntimeDir
    $rtPy = Join-Path $RuntimeDir "Scripts\python.exe"
    $ProjectsDir = Get-FullPath $ProjectsDir
    if (-not $AllowedDirs) { $AllowedDirs = $ProjectsDir }
    if ((Test-PathInside $RuntimeDir $RepoRoot) -or (Test-PathInside $RepoRoot $RuntimeDir)) {
        Fail "arguments" ("-RuntimeDir must be outside the repository: " + $RuntimeDir) `
            "use the default %LOCALAPPDATA%\vissim-mcp\venv (a git checkout or clean must never break the server)"
    }
    if ($ProgId -notmatch '^([A-Za-z][A-Za-z0-9_\-]*(\.[A-Za-z0-9_\-]+)*|\{[0-9A-Fa-f\-]{36}\})$' -or
        ($ProgId -notmatch '^\{' -and $ProgId.Length -gt 39)) {
        Fail "arguments" ("invalid -ProgId: " + $ProgId) "e.g. Vissim.Vissim or Vissim.Vissim.2026"
    }
    # "powershell -File" passes 'A=1','B=2' as one string "A=1,B=2": split it before the next KEY=.
    # The kept variables come first; -ExtraEnv overrides them by name, 'KEY=' (empty value = default) removes one.
    $envMap = [ordered]@{}
    foreach ($item in (@($keptEnv) + @($ExtraEnv))) {
        foreach ($pair in ($item -split ',(?=\s*[A-Za-z_][A-Za-z0-9_]*=)')) {
            $pair = $pair.Trim()
            if (-not $pair) { continue }
            if ($pair -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { Fail "arguments" ("-ExtraEnv expects KEY=VALUE, got '" + $pair + "'") }
            $key = $Matches[1]; $value = $Matches[2]
            if ($envMap.Contains($key)) { $envMap.Remove($key) }
            if ($value -ne "") { $envMap[$key] = $value }
        }
    }
    $envArgs = @()
    foreach ($key in $envMap.Keys) { $envArgs += @("--env", ($key + "=" + $envMap[$key])) }
    $repoVersion = Get-RepoVersion $RepoRoot
    Write-Info ("version " + $repoVersion + "; runtime " + $RuntimeDir + "; projects " + $AllowedDirs + "; ProgID " + $ProgId)
    if ($envMap.Count -gt 0) { Write-Info ("extra variables: " + (($envMap.Keys | ForEach-Object { $_ + "=" + $envMap[$_] }) -join "; ")) }
    Add-Result "arguments" "OK" ("vissim-mcp " + $repoVersion)

    # --- 2. Runtime venv ----------------------------------------------------------------------------------
    Write-Step "Preparing the runtime venv"
    $venvAction = "reused"
    if (Test-Path -LiteralPath $RuntimeDir) {
        if ($Force) {
            Write-Info "-Force: recreating the venv"
            Backup-Venv $RuntimeDir
            $venvAction = "recreated (-Force)"
        } elseif (-not (Test-Path -LiteralPath (Join-Path $RuntimeDir "pyvenv.cfg"))) {
            if (@(Get-ChildItem -LiteralPath $RuntimeDir -Force).Count -gt 0) {
                Fail "runtime venv" ($RuntimeDir + " exists, is not empty and is not a venv") "choose another -RuntimeDir"
            }
            $venvAction = "created"
        } else {
            $rtProbe = Get-PythonProbe $rtPy
            $rtMinor = $null
            if ($rtProbe) { $rtMinor = ($rtProbe.ver -split '\.')[0..1] -join '.' }
            if (-not $rtProbe) {
                Write-Warn "the existing venv does not run (its base Python was probably removed or moved); recreating"
                Backup-Venv $RuntimeDir
                $venvAction = "recreated (broken)"
            } elseif ($rtMinor -ne $baseMinor -or [int]$rtProbe.bits -ne 64) {
                Write-Info ("the existing venv uses Python " + $rtProbe.ver + "; recreating with " + $base.ver)
                Backup-Venv $RuntimeDir
                $venvAction = "recreated (Python " + $rtProbe.ver + " -> " + $base.ver + ")"
            } else {
                $pipOk = $false
                try { $pipOk = ((Invoke-Native -FilePath $rtPy -Arguments @("-m", "pip", "--version") -TimeoutSec 120).ExitCode -eq 0) } catch { }
                if (-not $pipOk) {
                    Write-Warn "pip does not run in the existing venv (a half-deleted venv?); recreating"
                    Backup-Venv $RuntimeDir
                    $venvAction = "recreated (pip missing)"
                }
            }
        }
    } else {
        $venvAction = "created"
    }
    if (-not (Test-Path -LiteralPath $rtPy)) {
        $parent = Split-Path -Parent $RuntimeDir
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        $r = Invoke-Native -FilePath $base.exe -Arguments @("-m", "venv", $RuntimeDir) -TimeoutSec 600
        if ($r.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $rtPy)) {
            Fail "runtime venv" ("python -m venv failed: " + (Get-Tail ($r.StdErr + $r.StdOut) 10)) ("check disk space and permissions of " + $RuntimeDir)
        }
    }
    Write-Info ("venv " + $venvAction + ": " + $rtPy)
    Add-Result "runtime venv" "OK" ($venvAction + ": " + $RuntimeDir)

    # --- 3. Install the package (non-editable) ------------------------------------------------------------
    Write-Step "Installing vissim-mcp into the runtime venv (non-editable; the first run downloads dependencies)"
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("vissim-mcp-build-" + [guid]::NewGuid().ToString("N"))
    try {
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $RepoRoot "pyproject.toml") -Destination $stage
        Copy-Item -LiteralPath (Join-Path $RepoRoot "README.md") -Destination $stage
        Copy-Item -LiteralPath (Join-Path $RepoRoot "src") -Destination $stage -Recurse
        Get-ChildItem -LiteralPath (Join-Path $stage "src") -Recurse -Force -Directory |
            Where-Object { $_.Name -eq "__pycache__" -or $_.Name -like "*.egg-info" } |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        $pipBase = @("-m", "pip", "install", "--disable-pip-version-check", "--no-input")
        # The tested dependency versions: a recreated venv gets the same set, not whatever is newest that day.
        $constraints = @()
        $constraintsFile = Join-Path $RepoRoot "requirements-runtime.txt"
        if (Test-Path -LiteralPath $constraintsFile) { $constraints = @("-c", $constraintsFile) }
        else { Write-Warn "requirements-runtime.txt not found; dependencies are not pinned" }
        $steps = @(
            @{ Name = "build tools"; Args = ($pipBase + @("setuptools>=68", "wheel")) },
            @{ Name = "dependencies"; Args = ($pipBase + $constraints + @("--no-build-isolation", $stage)) },
            @{ Name = "package (force-reinstall)"; Args = ($pipBase + @("--no-build-isolation", "--no-deps", "--force-reinstall", $stage)) }
        )
        foreach ($s in $steps) {
            Write-Info ("pip: " + $s.Name)
            $r = Invoke-Native -FilePath $rtPy -Arguments $s.Args -TimeoutSec 1800
            if ($r.ExitCode -ne 0) {
                Write-Host (Get-Tail ($r.StdOut + [Environment]::NewLine + $r.StdErr) 25)
                Fail "install" ("pip install (" + $s.Name + ") failed with exit code " + $r.ExitCode) `
                    "check the internet connection / proxy (a new venv downloads mcp, pywin32, psutil; pip asks the package index even when wheels are cached), then rerun"
            }
        }
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    }
    Add-Result "install" "OK" "pip install (non-editable, package force-reinstalled)"

    # --- 4. Verify the runtime ----------------------------------------------------------------------------
    Write-Step "Verifying the runtime"
    $r = Invoke-Native -FilePath $rtPy -Arguments @("-X", "utf8", (Join-Path $PSScriptRoot "runtime_info.py"), "--repo-src", (Join-Path $RepoRoot "src")) -TimeoutSec 120
    if ($r.ExitCode -ne 0) { Fail "runtime check" ("runtime_info.py failed: " + (Get-Tail $r.StdErr 10)) "rerun with -Force" }
    $info = $r.StdOut.Trim() | ConvertFrom-Json
    if (-not $info.modules_ok) {
        $errs = ($info.module_errors.PSObject.Properties | ForEach-Object { $_.Name + ": " + $_.Value }) -join "; "
        Fail "runtime check" ("dependencies do not import: " + $errs) "rerun with -Force (recreates the venv)"
    }
    if (-not $info.installed) { Fail "runtime check" ("vissim_mcp does not import: " + $info.install_error) "rerun with -Force" }
    if ($info.editable) { Fail "runtime check" "the package is installed in editable mode" "rerun with -Force" }
    if (-not (Test-PathInside $info.package_dir $RuntimeDir)) {
        Fail "runtime check" ("vissim_mcp is imported from " + $info.package_dir + ", not from the runtime venv") `
            "remove PYTHONPATH / PYTHONHOME from the environment, then rerun"
    }
    if (-not $info.matches_repo) {
        Fail "runtime check" ("installed code (" + $info.version + ") differs from the repository (" + $info.repo_version + ")") "rerun with -Force"
    }
    Write-Info ("vissim-mcp " + $info.version + " in " + $info.package_dir + " (code identical to the repository)")
    Add-Result "runtime check" "OK" ("vissim-mcp " + $info.version + ", Python " + $info.python_version + " " + $info.bits + "-bit")

    # --- 5. Projects folder --------------------------------------------------------------------------------
    Write-Step "Preparing the projects folder"
    $original = Join-Path $ProjectsDir "models\original"
    if (-not (Test-Path -LiteralPath $original)) {
        New-Item -ItemType Directory -Path $original -Force | Out-Null
        Write-Info ("created " + $original)
    } else {
        Write-Info ("exists: " + $original)
    }
    if ($ProjectsDir -match '[^\x20-\x7E]') { Write-Warn "the projects path is not ASCII; Vissim may fail to read files there" }
    Add-Result "projects folder" "OK" ($AllowedDirs + " (read-only: models\original)")

    # --- 6. Configuration check with the exact registered environment ----------------------------------------
    Write-Step "Validating the configuration (python -X utf8 -P -m vissim_mcp --check)"
    $register = Join-Path $PSScriptRoot "register_claude.py"
    $r = Invoke-Native -FilePath $rtPy -Arguments (@("-X", "utf8", $register, "--print-json", "--python", $rtPy, "--progid", $ProgId, "--allowed-dirs", $AllowedDirs) + $envArgs) -TimeoutSec 60
    if ($r.ExitCode -ne 0) { Fail "configuration" ("register_claude.py --print-json failed: " + (Get-Tail $r.StdErr 10)) }
    $serverJson = $r.StdOut
    $server = $serverJson | ConvertFrom-Json
    $serverEnv = @{}
    foreach ($p in $server.env.PSObject.Properties) { $serverEnv[$p.Name] = [string]$p.Value }
    $r = Invoke-Native -FilePath $server.command -Arguments (@($server.args) + @("--check")) -Environment $serverEnv -TimeoutSec 120
    if ($r.ExitCode -ne 0) {
        Fail "configuration" ("--check failed (exit " + $r.ExitCode + "): " + (Get-Tail $r.StdErr 5)) "fix the value named in the message"
    }
    $configWarnings = @(($r.StdErr -split "`r?`n") | Where-Object { $_ -like "vissim-mcp: warning:*" })
    foreach ($w in $configWarnings) { Write-Warn $w }
    $detail = "valid"
    if ($configWarnings.Count -gt 0) { $detail = "valid, " + $configWarnings.Count + " warning(s)" }
    Write-Info ("env: " + (($serverEnv.Keys | Sort-Object | ForEach-Object { $_ + "=" + $serverEnv[$_] }) -join "; "))
    Add-Result "configuration" "OK" $detail

    # --- 7. MCP stdio smoke --------------------------------------------------------------------------------
    Write-Step "MCP stdio smoke test (initialize -> tools/list -> vissim_health)"
    $jsonFile = Join-Path ([System.IO.Path]::GetTempPath()) ("vissim-mcp-server-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        [System.IO.File]::WriteAllText($jsonFile, $serverJson, $script:Utf8NoBom)
        $r = Invoke-Native -FilePath $rtPy -Arguments @("-X", "utf8", (Join-Path $PSScriptRoot "mcp_smoke.py"), "--server-json", $jsonFile, "--json") -TimeoutSec 300
    } finally {
        Remove-Item -LiteralPath $jsonFile -Force -ErrorAction SilentlyContinue
    }
    $smoke = $null
    try { $smoke = ($r.StdOut.Trim() -split "`r?`n" | Select-Object -Last 1) | ConvertFrom-Json } catch { }
    if ($r.ExitCode -ne 0 -or -not $smoke -or -not $smoke.ok) {
        $why = Get-Tail $r.StdErr 15
        Fail "MCP smoke" ("the server failed the stdio smoke test: " + $why) "see docs\troubleshooting.md; rerun with -Force"
    }
    Write-Info ("" + $smoke.tools + " tools, vissim_health ok, start-up " + $smoke.startup_s + " s, exit " + $smoke.exit_after_stdin_close_ms + " ms after stdin close")
    Add-Result "MCP smoke" "OK" ("" + $smoke.tools + " tools, vissim_health ok")
    # The new venv works: the old one (moved aside) is no longer needed.
    Remove-VenvBackup

    # --- 8. Registration --------------------------------------------------------------------------------------
    if ($NoRegister) {
        Write-Step "Registration skipped (-NoRegister)"
        Add-Result "registration" "SKIPPED" "-NoRegister"
    } else {
        Write-Step ("Registering with Claude Code (" + $Scope + " scope)")
        $claudeExe = Find-Claude $Claude
        if (-not $claudeExe) {
            Fail "registration" "claude.exe not found (PATH or %USERPROFILE%\.local\bin\claude.exe)" `
                "install Claude Code (native), or pass -Claude <path>; or use -NoRegister and register later"
        }
        $regArgs = @("-X", "utf8", $register, "--python", $rtPy, "--progid", $ProgId, "--allowed-dirs", $AllowedDirs, "--scope", $Scope, "--claude", $claudeExe) + $envArgs
        if ($Scope -ne "user") { $regArgs += @("--project-dir", $ProjectsDir) }
        $r = Invoke-Native -FilePath $rtPy -Arguments $regArgs -TimeoutSec 600
        foreach ($line in ($r.StdErr -split "`r?`n" | Where-Object { $_ })) { Write-Info $line }
        if ($r.ExitCode -eq 3) {
            Fail "registration" "the name 'vissim' is already registered in another scope" "see the message above (claude mcp remove vissim -s <scope>), then rerun"
        }
        if ($r.ExitCode -ne 0) { Fail "registration" ("register_claude.py failed with exit code " + $r.ExitCode) "see the message above; scripts\doctor.ps1" }
        Add-Result "registration" "OK" ("'vissim' in the " + $Scope + " scope -> " + $rtPy)
    }
} catch {
    $exitCode = 1
    Write-Host ""
    Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    Restore-Venv
} finally {
    try { [Console]::OutputEncoding = $oldOutputEncoding } catch { }
}

Write-Host ""
Write-Host "Summary" -ForegroundColor Cyan
$results | Format-Table -AutoSize -Wrap | Out-String -Width 160 | Write-Host
$elapsed = [int]((Get-Date) - $started).TotalSeconds
if ($exitCode -eq 0) {
    Write-Host ("vissim-mcp is installed (" + $elapsed + " s).") -ForegroundColor Green
    Write-Host ("  Runtime python : " + $rtPy)
    Write-Host ("  Projects       : " + $AllowedDirs)
    if (-not $NoRegister) {
        Write-Host "  Next: open a NEW Claude Code session (or /mcp -> vissim -> Reconnect) and run /mcp: 'vissim' must be connected."
    } else {
        Write-Host "  Not registered (-NoRegister). Register later: rerun without -NoRegister."
    }
    Write-Host "  Check at any time: powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1"
} else {
    Write-Host ("Installation FAILED (" + $elapsed + " s). Fix the problem above and run the installer again.") -ForegroundColor Red
}
exit $exitCode
