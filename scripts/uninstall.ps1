<#
.SYNOPSIS
  Remove the vissim-mcp registration from Claude Code (and optionally the runtime venv and the data).

.DESCRIPTION
  1. "claude mcp remove vissim -s <Scope>" through scripts\register_claude.py (not registered = success).
  2. -RemoveRuntime: deletes the runtime venv (default %LOCALAPPDATA%\vissim-mcp\venv).
  3. -RemoveData: deletes the server logs and the audit log (%LOCALAPPDATA%\vissim-mcp\logs and \audit).
     Without it they are kept (the audit log is the record of every change made through the server).
  The projects folder (models, results) is never touched. Never starts or stops Vissim.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -RemoveRuntime
  powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [ValidateSet("user", "project", "local")][string]$Scope = "user",
    [string]$RuntimeDir = "",
    [string]$DataDir = "",
    # Working directory of "claude mcp remove" for the project/local scopes.
    [string]$ProjectsDir = "D:\VissimProjects",
    [string]$Name = "vissim",
    [string]$Claude = "",
    [switch]$RemoveRuntime,
    [switch]$RemoveData,
    [switch]$KeepRegistration,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "_common.ps1")

if (-not $RuntimeDir) { $RuntimeDir = Get-DefaultRuntimeDir }
if (-not $DataDir) { $DataDir = Get-DefaultDataDir }
$RuntimeDir = Get-FullPath $RuntimeDir
$DataDir = Get-FullPath $DataDir
$rtPy = Join-Path $RuntimeDir "Scripts\python.exe"
$exitCode = 0

function Remove-Dir([string]$Path, [string]$What) {
    if (-not (Test-Path -LiteralPath $Path)) { Write-Host ("    " + $What + " not present: " + $Path); return }
    if ($DryRun) { Write-Host ("    [dry-run] would delete " + $What + ": " + $Path); return }
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Host ("    deleted " + $What + ": " + $Path)
    } catch {
        Write-Host ("    ERROR: cannot delete " + $Path + ": " + $_.Exception.Message) -ForegroundColor Red
        Write-Host "    Fix: close every Claude Code session that uses the server (its python.exe), then retry." -ForegroundColor Red
        $script:exitCode = 1
    }
}

# --- 1. registration --------------------------------------------------------------------------------------
if ($KeepRegistration) {
    Write-Host "==> Registration kept (-KeepRegistration)"
} else {
    Write-Host ("==> Removing '" + $Name + "' from Claude Code (" + $Scope + " scope)")
    $helper = $null; $pre = @()
    if (Test-Path -LiteralPath $rtPy) {
        try { if ((Invoke-Native -FilePath $rtPy -Arguments @("-c", "pass") -TimeoutSec 60).ExitCode -eq 0) { $helper = $rtPy } } catch { }
    }
    if (-not $helper) {
        $py = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($py) { $helper = $py.Source; $pre = @("-3") }
    }
    $claudeExe = Find-Claude $Claude
    if (-not $helper) {
        Write-Host "    ERROR: no Python found to run register_claude.py; remove manually: claude mcp remove $Name -s $Scope" -ForegroundColor Red
        $exitCode = 1
    } elseif (-not $claudeExe -and -not $DryRun) {
        Write-Host "    ERROR: claude.exe not found; nothing to unregister from (or pass -Claude <path>)" -ForegroundColor Red
        $exitCode = 1
    } else {
        $regArgs = $pre + @("-X", "utf8", (Join-Path $PSScriptRoot "register_claude.py"), "--unregister", "--name", $Name, "--scope", $Scope)
        if ($claudeExe) { $regArgs += @("--claude", $claudeExe) }
        if ($Scope -ne "user") { $regArgs += @("--project-dir", $ProjectsDir) }
        if ($DryRun) { $regArgs += "--dry-run" }
        $r = Invoke-Native -FilePath $helper -Arguments $regArgs -TimeoutSec 300
        foreach ($line in ($r.StdErr -split "`r?`n" | Where-Object { $_ })) { Write-Host ("    " + $line) }
        if ($r.ExitCode -ne 0) { $exitCode = 1 }
    }
}

# --- 2. runtime -----------------------------------------------------------------------------------------------
if ($RemoveRuntime) {
    Write-Host "==> Removing the runtime venv"
    if ((Test-Path -LiteralPath $RuntimeDir) -and -not (Test-Path -LiteralPath (Join-Path $RuntimeDir "pyvenv.cfg"))) {
        Write-Host ("    ERROR: " + $RuntimeDir + " is not a venv (no pyvenv.cfg); not deleted") -ForegroundColor Red
        $exitCode = 1
    } else {
        Remove-Dir $RuntimeDir "runtime venv"
    }
} else {
    Write-Host ("==> Runtime venv kept: " + $RuntimeDir + " (-RemoveRuntime deletes it)")
}

# --- 3. data ----------------------------------------------------------------------------------------------------
if ($RemoveData) {
    Write-Host "==> Removing logs and audit"
    Remove-Dir (Join-Path $DataDir "logs") "server logs"
    Remove-Dir (Join-Path $DataDir "audit") "audit log"
} else {
    Write-Host ("==> Logs and audit kept: " + (Join-Path $DataDir "logs") + ", " + (Join-Path $DataDir "audit") + " (-RemoveData deletes them)")
}
Write-Host "    Projects folder is never touched."

if ($exitCode -eq 0) { Write-Host "Done." -ForegroundColor Green } else { Write-Host "Finished with errors." -ForegroundColor Red }
exit $exitCode
