# Starts the OptiFuzz desktop window (Windows). pythonw.exe: no console window at all.
Set-Location (Split-Path (Split-Path $PSScriptRoot))
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "run_desktop.py" -WorkingDirectory (Split-Path (Split-Path $PSScriptRoot))
