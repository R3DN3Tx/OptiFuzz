# Installs OptiFuzz's dependencies on Windows: a virtual environment with the Python requirements,
# and a Start Menu shortcut. Pass -NoShortcut to skip the shortcut and leave nothing outside this folder.
param([switch]$NoShortcut)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path (Split-Path (Split-Path $PSScriptRoot)))  # repository root (this script lives in app\scripts\windows)

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python 3.11 or newer is required (python.org). Make sure 'python' is on PATH."
}
& python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Python 3.11 or newer is required (dirsearch needs it)."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "Git is required (dirsearch is installed from its GitHub repository)."
}

& python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r app\requirements.txt

if ($NoShortcut) {
    Write-Host ""
    Write-Host "Done (no shortcut created). Start OptiFuzz with: powershell -ExecutionPolicy Bypass -File .\app\scripts\windows\run-windows.ps1"
    return
}

# Start Menu shortcut, so OptiFuzz is found and launched like any installed app (no .ps1 needed).
$root = (Get-Location).Path
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Programs")) "OptiFuzz.lnk"
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $root ".venv\Scripts\pythonw.exe"
$shortcut.Arguments = "app\run_desktop.py"
$shortcut.WorkingDirectory = $root
# The app list of the Start menu shows this icon as it is: dark ink when Windows is in light mode (the same rule
# the app uses for its taskbar icon), light ink otherwise. The app keeps it in step when the theme changes.
$lightMode = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" -ErrorAction SilentlyContinue).SystemUsesLightTheme -eq 1
$iconFile = if ($lightMode) { "icon-light.ico" } else { "icon-dark.ico" }
$shortcut.IconLocation = Join-Path $root "app\frontend\$iconFile"
$shortcut.Description = "OptiFuzz"
$shortcut.Save()

Write-Host ""
Write-Host "Done. Search for 'OptiFuzz' in the Start menu to launch it (shortcut: $shortcutPath)."
