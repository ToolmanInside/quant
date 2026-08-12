from __future__ import annotations

"""Quant Lab 本地服务启动器（跨平台：Windows / macOS / Linux）。

启动 FastAPI 后端（uvicorn）与 Next.js 前端（next dev），
并负责每个交易日下午 15:10（北京时间）自动运行模拟盘日终任务。

用法：
    python scripts/run_local.py [--no-browser] [--timeout 60] [--log-file logs/quant-lab.log]

环境变量：
    NODE_BIN  指定 Node.js 可执行文件路径（默认从 PATH 查找）
"""

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
API_PORT = 8000
WEB_PORT = 3000
HEALTH_URL = f"http://{HOST}:{API_PORT}/api/health"
WEB_URL = f"http://{HOST}:{WEB_PORT}"
PAPER_DASHBOARD_URL = f"http://{HOST}:{API_PORT}/api/paper/dashboard"
PAPER_ADVANCE_URL = f"http://{HOST}:{API_PORT}/api/paper/advance"

DEFAULT_LOG_FILE = PROJECT_ROOT / "logs" / "quant-lab.log"
SIMULATION_TIMEZONE = "Asia/Shanghai"
DAILY_SIMULATION_HOUR = 15
DAILY_SIMULATION_MINUTE = 10
STARTUP_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 30
TASK_RETRY_LIMIT = 3
TASK_RETRY_DELAY_SECONDS = 300

NEXT_ENTRY = PROJECT_ROOT / "node_modules" / "next" / "dist" / "bin" / "next"

logger = logging.getLogger("quant-lab")


def _now() -> datetime:
    """统一使用北京时间，避免机器本地时区与注释口径不一致。"""
    return datetime.now(ZoneInfo(SIMULATION_TIMEZONE))


def _find_node() -> Path | None:
    """定位 Node.js：优先 NODE_BIN 环境变量，其次 PATH。"""
    override = os.getenv("NODE_BIN")
    if override:
        candidate = Path(override)
        if candidate.exists():
            return candidate
        logger.warning("NODE_BIN=%s 不存在，回退到 PATH 查找", override)
    found = shutil.which("node")
    return Path(found) if found else None


def _check_runtime() -> tuple[Path | None, str | None]:
    """预检运行环境，返回 (node可执行文件, 错误信息)。"""
    node = _find_node()
    if node is None:
        return None, (
            "未找到 Node.js。请安装 Node.js >= 22（https://nodejs.org），"
            "或通过 NODE_BIN 环境变量指定路径。"
        )
    if not NEXT_ENTRY.exists():
        return None, (
            "前端依赖缺失（node_modules/next 不存在）。"
            "请先运行 scripts/setup.ps1（Windows）或 scripts/setup.sh（macOS/Linux）。"
        )
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return None, (
            "Python 依赖缺失（fastapi/uvicorn）。"
            "请先运行 scripts/setup.ps1（Windows）或 scripts/setup.sh（macOS/Linux）。"
        )
    return node, None


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _check_ports() -> list[str]:
    busy = []
    for name, port in (("API", API_PORT), ("前端", WEB_PORT)):
        if _port_in_use(HOST, port):
            busy.append(f"{name}端口 {port} 已被占用")
    return busy


def is_ready(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def _start_process(command: list[str]) -> subprocess.Popen[bytes]:
    kwargs: dict = {"cwd": PROJECT_ROOT}
    if os.name != "nt":
        # POSIX：独立进程组，便于退出时整组终止（杀净子进程）
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """按平台终止整个进程树，避免 uvicorn/next 的子进程残留。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_daily_paper_if_due(
    last_attempt: date | None,
    failures: int,
    next_attempt_at: datetime,
) -> tuple[date | None, int, datetime]:
    """到 15:10（北京时间）且当天未运行时执行日终任务。

    失败后延迟 TASK_RETRY_DELAY_SECONDS 重试，最多 TASK_RETRY_LIMIT 次；
    重试耗尽后当天不再尝试（避免无限重试）。
    """
    now = _now()
    today = now.date()
    due = (now.hour, now.minute) >= (DAILY_SIMULATION_HOUR, DAILY_SIMULATION_MINUTE)
    if last_attempt == today or not due:
        return last_attempt, failures, next_attempt_at
    if failures >= TASK_RETRY_LIMIT:
        # 今日重试次数耗尽，标记已尝试，次日再试
        return today, failures, next_attempt_at
    if now < next_attempt_at:
        return last_attempt, failures, next_attempt_at

    logger.info("开始运行每日模拟盘任务（北京时间 %s）", now.strftime("%H:%M:%S"))
    try:
        with urllib.request.urlopen(PAPER_DASHBOARD_URL, timeout=5) as response:
            dashboard = json.loads(response.read().decode("utf-8"))
        payload = json.dumps(
            {
                "account_id": dashboard["account"]["account_id"],
                "symbols": dashboard["account"]["universe"],
                "as_of_date": today.isoformat(),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            PAPER_ADVANCE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
        logger.info("每日模拟盘任务完成：%s", result["run"]["message"])
        return today, 0, next_attempt_at
    except Exception:
        failures += 1
        logger.exception(
            "每日模拟盘任务第 %s/%s 次失败，%s 秒后重试",
            failures,
            TASK_RETRY_LIMIT,
            TASK_RETRY_DELAY_SECONDS,
        )
        if failures >= TASK_RETRY_LIMIT:
            logger.error("当日重试次数耗尽，任务将在次日自动补跑")
            return today, failures, next_attempt_at
        return last_attempt, failures, now + timedelta(seconds=TASK_RETRY_DELAY_SECONDS)


def _setup_logging(log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        handlers=handlers,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="启动 Quant Lab 本地服务（后端 + 前端 + 每日模拟任务）"
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开浏览器",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=STARTUP_TIMEOUT_SECONDS,
        help=f"启动等待超时秒数（默认 {STARTUP_TIMEOUT_SECONDS}）",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="日志文件路径（默认 logs/quant-lab.log）",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="不写日志文件，仅输出到控制台",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_file = (
        None
        if args.no_log_file
        else (args.log_file or DEFAULT_LOG_FILE)
    )
    _setup_logging(log_file)
    logger.info("Quant Lab 本地服务启动器（Python %s，%s）", sys.version.split()[0], os.name)

    node, error = _check_runtime()
    if error:
        logger.error(error)
        return 1

    busy = _check_ports()
    if busy:
        for message in busy:
            logger.error(message)
        logger.error(
            "请先关闭占用端口的进程，或确认没有重复启动；"
            "如需更换端口请修改 scripts/run_local.py 顶部常量。"
        )
        return 1

    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            HOST,
            "--port",
            str(API_PORT),
        ],
        [
            str(node),
            str(NEXT_ENTRY),
            "dev",
            "--hostname",
            HOST,
            "--port",
            str(WEB_PORT),
        ],
    ]
    processes: list[subprocess.Popen[bytes]] = []

    logger.info("启动后端 API（端口 %s）与前端（端口 %s）…", API_PORT, WEB_PORT)
    try:
        for command in commands:
            processes.append(_start_process(command))

        deadline = time.monotonic() + max(args.timeout, 10)
        while time.monotonic() < deadline:
            failed = next(
                (process for process in processes if process.poll() is not None),
                None,
            )
            if failed is not None:
                logger.error("有服务提前退出（退出码 %s），请查看上方日志。", failed.returncode)
                return failed.returncode or 1
            if is_ready(HEALTH_URL) and is_ready(WEB_URL):
                logger.info("Quant Lab 已就绪：%s", WEB_URL)
                if not args.no_browser:
                    try:
                        webbrowser.open(WEB_URL)
                    except Exception:
                        logger.warning("自动打开浏览器失败，请手动访问 %s", WEB_URL)
                break
            time.sleep(0.25)
        else:
            logger.error(
                "启动超时（%s 秒）。请确认依赖安装完整："
                "scripts/setup.ps1（Windows）或 scripts/setup.sh（macOS/Linux）。",
                args.timeout,
            )
            return 1

        last_attempt: date | None = None
        failures = 0
        next_attempt_at = _now() - timedelta(seconds=1)
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    logger.error("有服务退出（退出码 %s），正在停止全部服务。", exit_code)
                    return exit_code or 1
            last_attempt, failures, next_attempt_at = run_daily_paper_if_due(
                last_attempt, failures, next_attempt_at
            )
            time.sleep(POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，正在停止 Quant Lab…")
        return 0
    finally:
        for process in reversed(processes):
            _terminate_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
