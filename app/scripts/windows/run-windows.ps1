# Starts the OptiFuzz desktop window (Windows). pythonw.exe: no console window at all.
$root = Split-Path (Split-Path (Split-Path $PSScriptRoot))  # repository root (this script lives in app\scripts\windows)
Set-Location $root
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "run_desktop.py" -WorkingDirectory $root
