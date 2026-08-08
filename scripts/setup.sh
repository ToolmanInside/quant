#!/usr/bin/env bash
# Quant Lab 本地服务安装脚本（macOS / Linux）
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v node >/dev/null 2>&1; then
    echo "错误：未找到 Node.js（>=22）。请先安装：https://nodejs.org" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 python3。请先安装 Python 3.10+。" >&2
    exit 1
fi

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"

echo "==> 安装前端依赖（npm install）…"
npm install --ignore-scripts --no-audit --no-fund

if [ ! -f "$VENV_PYTHON" ]; then
    echo "==> 创建 Python 虚拟环境 .venv …"
    python3 -m venv .venv
fi

echo "==> 安装 Python 依赖…"
"$VENV_PYTHON" -m pip install -r requirements.txt
"$VENV_PYTHON" -m pip install -r requirements-dev.txt

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "==> 已从 .env.example 生成 .env"
    else
        echo "警告：缺少 .env.example，请手动创建 .env 并配置 TUSHARE_TOKEN。" >&2
    fi
fi

echo "Quant Lab 安装完成。"
echo "启动：./start-quant-lab.sh"
echo "如使用真实数据，请把 TUSHARE_TOKEN 填入 .env。"
