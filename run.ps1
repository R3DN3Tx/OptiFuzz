# Starts the OptiFuzz desktop window (Windows). pythonw.exe: no console window at all.
Set-Location $PSScriptRoot
Start-Process -FilePath ".\.venv\Scripts\pythonw.exe" -ArgumentList "run_desktop.py" -WorkingDirectory $PSScriptRoot
