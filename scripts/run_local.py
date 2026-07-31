from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from datetime import date, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NODE = Path(r"C:\Program Files\nodejs\node.exe")
NEXT = PROJECT_ROOT / "node_modules" / "next" / "dist" / "bin" / "next"
WEB_URL = "http://127.0.0.1:3000"
HEALTH_URL = "http://127.0.0.1:8000/api/health"
PAPER_DASHBOARD_URL = "http://127.0.0.1:8000/api/paper/dashboard"
PAPER_ADVANCE_URL = "http://127.0.0.1:8000/api/paper/advance"
DAILY_SIMULATION_HOUR = 18


def is_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_daily_paper_if_due(last_attempt: date | None) -> date | None:
    now = datetime.now()
    today = now.date()
    if last_attempt == today or now.hour < DAILY_SIMULATION_HOUR:
        return last_attempt

    print(f"[paper] {now:%Y-%m-%d %H:%M:%S} running the daily simulation.")
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
        print(f"[paper] {result['run']['message']}")
    except Exception:
        print(
            "[paper] Daily simulation failed. Full error follows:",
            file=sys.stderr,
        )
        traceback.print_exc()
    return today


def main() -> int:
    if not NODE.exists():
        print(f"System Node.js was not found: {NODE}", file=sys.stderr)
        return 1
    if not NEXT.exists():
        print("Frontend dependencies are missing. Run scripts/setup.ps1 first.", file=sys.stderr)
        return 1

    commands = [
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        [
            str(NODE),
            str(NEXT),
            "dev",
            "--hostname",
            "127.0.0.1",
            "--port",
            "3000",
        ],
    ]
    processes: list[subprocess.Popen[bytes]] = []

    print("Starting Quant Lab in the foreground.")
    print("API and web logs will remain visible in this window.")
    print("Press Ctrl+C to stop both services.")

    try:
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=PROJECT_ROOT))

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            failed = next((process for process in processes if process.poll() is not None), None)
            if failed is not None:
                print(f"A local service exited with code {failed.returncode}.", file=sys.stderr)
                return failed.returncode or 1
            if is_ready(HEALTH_URL) and is_ready(WEB_URL):
                print(f"Quant Lab is ready: {WEB_URL}")
                webbrowser.open(WEB_URL)
                break
            time.sleep(0.25)
        else:
            print("Startup timed out. Review the visible logs above.", file=sys.stderr)
            return 1

        last_daily_attempt: date | None = None
        while True:
            for process in processes:
                exit_code = process.poll()
                if exit_code is not None:
                    print(f"A local service exited with code {exit_code}.", file=sys.stderr)
                    return exit_code or 1
            last_daily_attempt = run_daily_paper_if_due(last_daily_attempt)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping Quant Lab...")
        return 0
    finally:
        for process in reversed(processes):
            stop_process(process)


if __name__ == "__main__":
    raise SystemExit(main())
