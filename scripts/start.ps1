$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runner = Join-Path $projectRoot "scripts\run_local.py"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Python environment is missing. Run scripts\setup.ps1 first."
}

if (-not (Test-Path -LiteralPath "C:\Program Files\nodejs\node.exe")) {
    throw "System Node.js was not found at C:\Program Files\nodejs."
}

Set-Location -LiteralPath $projectRoot
& $venvPython $runner
