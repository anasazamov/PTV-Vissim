# Shared helpers of install.ps1, uninstall.ps1 and doctor.ps1 (dot-sourced). Windows PowerShell 5.1 compatible.
#
# Native programs are started through System.Diagnostics.Process (not the PowerShell call operator): stdout and
# stderr are captured separately and decoded as UTF-8, a non-zero exit code never throws, and the environment
# variables are set for the child process only (nothing global is changed).

Set-StrictMode -Version 2.0

$script:Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function ConvertTo-ArgString {
    # Quote one argument for the Windows command line (CommandLineToArgvW / MSVCRT rules).
    param([AllowEmptyString()][string]$Value)
    if ($Value -eq "") { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $backslashes = 0
    foreach ($ch in $Value.ToCharArray()) {
        if ($ch -eq [char]'\') { $backslashes++; continue }
        if ($ch -eq [char]'"') {
            [void]$sb.Append('\' * ($backslashes * 2 + 1))
            [void]$sb.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) { [void]$sb.Append('\' * $backslashes); $backslashes = 0 }
        [void]$sb.Append($ch)
    }
    [void]$sb.Append('\' * ($backslashes * 2))
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Invoke-Native {
    # Run a program with an argument list; returns @{ExitCode; StdOut; StdErr}. Throws only if it cannot start
    # or exceeds the timeout (then it is killed).
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [hashtable]$Environment = @{},
        [string]$WorkingDirectory = "",
        [int]$TimeoutSec = 1800
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $quoted = @()
    foreach ($a in $Arguments) { $quoted += (ConvertTo-ArgString $a) }
    $psi.Arguments = ($quoted -join " ")
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $script:Utf8NoBom
    $psi.StandardErrorEncoding = $script:Utf8NoBom
    if ($WorkingDirectory) { $psi.WorkingDirectory = $WorkingDirectory }
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
    foreach ($key in $Environment.Keys) { $psi.EnvironmentVariables[[string]$key] = [string]$Environment[$key] }
    $proc = [System.Diagnostics.Process]::Start($psi)
    $proc.StandardInput.Close()
    $outTask = $proc.StandardOutput.ReadToEndAsync()
    $errTask = $proc.StandardError.ReadToEndAsync()
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { & taskkill.exe /T /F /PID $proc.Id | Out-Null } catch { }
        throw ("'" + [System.IO.Path]::GetFileName($FilePath) + "' did not finish within " + $TimeoutSec + " s")
    }
    $proc.WaitForExit()
    return [pscustomobject]@{ ExitCode = $proc.ExitCode; StdOut = $outTask.Result; StdErr = $errTask.Result }
}

function Get-Tail {
    param([string]$Text, [int]$Lines = 20)
    if (-not $Text) { return "" }
    $all = $Text -split "`r?`n" | Where-Object { $_ -ne "" }
    return (($all | Select-Object -Last $Lines) -join [Environment]::NewLine)
}

function Get-FullPath {
    param([string]$Path)
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Test-PathInside {
    # True if $Child equals $Parent or lies below it (case-insensitive, no "D:\A" vs "D:\A2" confusion).
    param([string]$Child, [string]$Parent)
    $c = (Get-FullPath $Child).ToLowerInvariant() + '\'
    $p = (Get-FullPath $Parent).ToLowerInvariant() + '\'
    return $c.StartsWith($p)
}

function Get-RepoVersion {
    param([string]$RepoRoot)
    $init = Join-Path $RepoRoot "src\vissim_mcp\__init__.py"
    $text = [System.IO.File]::ReadAllText($init, $script:Utf8NoBom)
    if ($text -match '(?m)^__version__\s*=\s*["'']([^"'']+)["'']') { return $Matches[1] }
    return $null
}

function Find-Claude {
    # claude.exe on PATH, else %USERPROFILE%\.local\bin\claude.exe; $null if not found.
    param([string]$Explicit = "")
    if ($Explicit) { if (Test-Path -LiteralPath $Explicit -PathType Leaf) { return $Explicit } else { return $null } }
    $cmd = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
    if (Test-Path -LiteralPath $local -PathType Leaf) { return $local }
    return $null
}

function Get-DefaultRuntimeDir {
    return (Join-Path $env:LOCALAPPDATA "vissim-mcp\venv")
}

function Get-DefaultDataDir {
    return (Join-Path $env:LOCALAPPDATA "vissim-mcp")
}

function Get-ProcessUnder {
    # Running processes whose executable lies in $Dir (e.g. a Claude Code session's server python.exe of the
    # runtime venv). Such a folder must not be deleted: Remove-Item would delete what it can and leave it broken.
    param([string]$Dir)
    $prefix = (Get-FullPath $Dir).ToLowerInvariant() + '\'
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($prefix) })
}

function Remove-DirSafely {
    # Delete $Path without ever leaving it half-deleted: first rename it (fails cleanly while a file in it is in
    # use, and nothing is lost), then delete the renamed folder. Throws with a readable message on failure.
    param([string]$Path)
    $users = @(Get-ProcessUnder $Path)
    if ($users.Count -gt 0) {
        throw ("in use by " + (($users | ForEach-Object { $_.Name + " PID " + $_.ProcessId }) -join ", "))
    }
    $trash = $Path + ".deleting-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
    try {
        Rename-Item -LiteralPath $Path -NewName (Split-Path -Leaf $trash) -ErrorAction Stop
    } catch {
        throw ("a file in it is in use (" + $_.Exception.Message + "); nothing was deleted")
    }
    Remove-Item -LiteralPath $trash -Recurse -Force -ErrorAction Stop
}
