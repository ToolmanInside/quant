$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "System Node.js was not found on PATH. Install Node.js >= 22 first."
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "System npm was not found on PATH. Install Node.js >= 22 first."
}

$systemPython = Get-Command python -All -ErrorAction SilentlyContinue |
    Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
    Select-Object -First 1

if (-not $systemPython) {
    throw "A usable system Python installation was not found on PATH."
}

$env:npm_config_cache = Join-Path $projectRoot ".npm-cache"
$env:PIP_CACHE_DIR = Join-Path $projectRoot ".pip-cache"

npm install --ignore-scripts --no-audit --no-fund

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $systemPython.Source -m venv (Join-Path $projectRoot ".venv")
}

& $venvPython -m pip install -r (Join-Path $projectRoot "requirements.txt")
& $venvPython -m pip install -r (Join-Path $projectRoot "requirements-dev.txt")

$envFile = Join-Path $projectRoot ".env"
$exampleEnvFile = Join-Path $projectRoot ".env.example"
if (-not (Test-Path -LiteralPath $envFile)) {
    if (Test-Path -LiteralPath $exampleEnvFile) {
        Copy-Item -LiteralPath $exampleEnvFile -Destination $envFile
        Write-Host "Created .env from .env.example."
    } else {
        Write-Warning ".env.example not found; create .env manually and add TUSHARE_TOKEN."
    }
}

Write-Host "Quant Lab setup completed."
Write-Host "Add TUSHARE_TOKEN to .env when you are ready to use real data."
