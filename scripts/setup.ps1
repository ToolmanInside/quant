$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$nodeDirectory = "C:\Program Files\nodejs"
$npmPath = Join-Path $nodeDirectory "npm.cmd"
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $npmPath)) {
    throw "System Node.js was not found at: $npmPath"
}

$systemPython = Get-Command python -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
    Select-Object -First 1

if (-not $systemPython) {
    throw "A usable system Python installation was not found on PATH."
}

$env:Path = "$nodeDirectory;$env:Path"
$env:npm_config_cache = Join-Path $projectRoot ".npm-cache"
$env:PIP_CACHE_DIR = Join-Path $projectRoot ".pip-cache"

& $npmPath install --ignore-scripts --no-audit --no-fund

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $systemPython.Source -m venv (Join-Path $projectRoot ".venv")
}

& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")

$envFile = Join-Path $projectRoot ".env"
$exampleEnvFile = Join-Path $projectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath $exampleEnvFile -Destination $envFile
}

Write-Host "Quant Lab setup completed."
Write-Host "Add TUSHARE_TOKEN to .env when you are ready to use real data."
