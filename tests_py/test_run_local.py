from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import scripts.run_local as runner


def _fixed_now(hour: int = 19, minute: int = 0) -> datetime:
    return datetime(
        2026, 8, 7, hour, minute, tzinfo=ZoneInfo(runner.SIMULATION_TIMEZONE)
    )


def test_daily_task_runs_once_per_day_at_or_after_1510(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_now", lambda: _fixed_now())
    today = date(2026, 8, 7)
    last_attempt = None
    failures = 0
    # 初始重试时间为当天零点之前，确保第一个到点场景必然执行
    next_attempt_at = _fixed_now(0, 0) - timedelta(seconds=1)

    # 未到 15:10 不执行
    monkeypatch.setattr(runner, "_now", lambda: _fixed_now(15, 9))
    last_attempt, failures, _ = runner.run_daily_paper_if_due(
        last_attempt, failures, next_attempt_at
    )
    assert last_attempt is None

    # 已到 15:10，执行成功
    monkeypatch.setattr(runner, "_now", lambda: _fixed_now(15, 10))

    calls = {"n": 0}

    def _response(body: bytes):
        return type(
            "FakeResponse",
            (),
            {
                "read": lambda self: body,
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
            },
        )()

    def fake_success(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次：dashboard
            return _response(
                b'{"account": {"account_id": "default", "universe": []}}'
            )
        return _response(b'{"run": {"message": "ok"}}')

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_success)
    last_attempt, failures, _ = runner.run_daily_paper_if_due(
        last_attempt, failures, next_attempt_at
    )
    assert last_attempt == today
    assert failures == 0

    # 同一天不重复执行
    last_attempt, failures, _ = runner.run_daily_paper_if_due(
        last_attempt, failures, next_attempt_at
    )
    assert last_attempt == today


def test_daily_task_retries_then_gives_up_for_today(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_now", lambda: _fixed_now(15, 12))
    today = date(2026, 8, 7)

    def fake_failure(*args, **kwargs):
        raise ConnectionError("boom")

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_failure)

    last_attempt = None
    failures = 0
    next_attempt_at = runner._now() - timedelta(seconds=1)

    # 第一次失败：计数+1，安排重试
    last_attempt, failures, next_attempt_at = runner.run_daily_paper_if_due(
        last_attempt, failures, next_attempt_at
    )
    assert last_attempt is None
    assert failures == 1
    assert next_attempt_at > runner._now()

    # 重试时间未到：不执行
    last_attempt, failures, next_attempt_at = runner.run_daily_paper_if_due(
        last_attempt, failures, next_attempt_at
    )
    assert failures == 1

    # 依次推进时间，让 3 次重试全部发生
    for index in range(3):
        monkeypatch.setattr(
            runner,
            "_now",
            lambda index=index: _fixed_now(15, 18 + index * 6),
        )
        last_attempt, failures, next_attempt_at = runner.run_daily_paper_if_due(
            last_attempt, failures, next_attempt_at
        )
    assert failures == runner.TASK_RETRY_LIMIT
    assert last_attempt == today  # 当日放弃，次日不再纠缠


def test_find_node_uses_override_then_path(monkeypatch) -> None:
    monkeypatch.delenv("NODE_BIN", raising=False)
    fake = Path("/usr/bin/node")
    monkeypatch.setattr(runner.shutil, "which", lambda name: str(fake))
    assert runner._find_node() == fake

    # NODE_BIN 存在时优先
    monkeypatch.setenv("NODE_BIN", str(fake))
    assert runner._find_node() == fake

    # NODE_BIN 无效时回退 PATH
    monkeypatch.setenv("NODE_BIN", "/nonexistent/node")
    assert runner._find_node() == fake


def test_port_in_use_detects_busy_port() -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert runner._port_in_use("127.0.0.1", port) is True
    assert runner._port_in_use("127.0.0.1", 1) is False


def test_runtime_check_reports_missing_node(monkeypatch) -> None:
    monkeypatch.setattr(runner, "_find_node", lambda: None)
    node, error = runner._check_runtime()
    assert node is None
    assert "Node.js" in error
