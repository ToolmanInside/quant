$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runner = Join-Path $projectRoot "scripts\run_local.py"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python environment is missing. Run scripts\setup.ps1 first."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "System Node.js was not found on PATH. Install Node.js >= 22 or set NODE_BIN."
}

Set-Location -LiteralPath $projectRoot
& $venvPython $runner
