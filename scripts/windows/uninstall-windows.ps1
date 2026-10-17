# Removes what install-windows.ps1 created: the Start Menu shortcut and the virtual environment.
# Your data (projects, results, settings) is kept unless you confirm or pass -RemoveData.
# The program folder itself is not deleted; remove it by hand if you want it gone.
param([switch]$RemoveData)
$ErrorActionPreference = "Stop"
$root = Split-Path (Split-Path $PSScriptRoot)  # repository root (this script lives in scripts\windows)
$venv = Join-Path $root ".venv"

# A running OptiFuzz holds files inside .venv open and would make the removal fail halfway.
$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Write-Error "OptiFuzz is running. Close it and run this again."
}

$shortcutPath = Join-Path ([Environment]::GetFolderPath("Programs")) "OptiFuzz.lnk"
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "Removed the Start menu shortcut."
}
if (Test-Path $venv) {
    Remove-Item $venv -Recurse -Force
    Write-Host "Removed the virtual environment (.venv)."
}

$dataDir = Join-Path $env:APPDATA "OptiFuzz"
if (Test-Path $dataDir) {
    if (-not $RemoveData) {
        $answer = Read-Host "Also delete your OptiFuzz data (projects, results, settings) in $dataDir ? [y/N]"
        $RemoveData = $answer -match '^(y|yes|s|si|sí)$'
    }
    if ($RemoveData) {
        Remove-Item $dataDir -Recurse -Force
        Write-Host "Removed your data ($dataDir)."
    } else {
        Write-Host "Kept your data in $dataDir."
    }
}

Write-Host ""
Write-Host "Done. Python and Git were not touched. To finish, delete the program folder: $root"
