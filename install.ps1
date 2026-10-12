# Installs OptiFuzz's dependencies on Windows: a virtual environment with the Python requirements.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

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
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Start OptiFuzz with: .\run.ps1"
