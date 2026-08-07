#!/usr/bin/env bash
# Quant Lab 本地服务启动器（macOS / Linux）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "错误：Python 环境缺失，请先运行 scripts/setup.sh。" >&2
    exit 1
fi

exec "$VENV_PYTHON" scripts/run_local.py "$@"
