from datetime import date, datetime
import json
from pathlib import Path

import httpx

from backend.paper_store import PaperStore
import scripts.daily_paper_job as daily_job
from scripts.daily_paper_job import (
    JobConfig,
    build_markdown_report,
    build_position_report,
    load_unified_config,
    run_daily_job,
    send_wechat_markdown,
)


def dashboard_fixture() -> dict:
    return {
        "account": {
            "initial_cash": 500_000,
            "cash": 120_000,
            "peak_equity": 575_000,
            "configuration": {
                "strategy_id": "moving_average",
                "strategy_name": "双均线趋势",
                "frequency": "1d",
                "backtest_start_date": "2024-01-01",
                "backtest_end_date": "2025-12-31",
                "simulation_start_date": "2026-01-01",
            },
            "pending_plan": [
                {
                    "symbol": "002317.SZ",
                    "name": "测试股票",
                    "action": "BUY",
                    "target_weight": 0.2,
                    "reason": "趋势信号保持向上",
                }
            ],
        },
        "latest": {
            "trade_date": "2026-07-31",
            "equity": 550_000,
            "market_value": 430_000,
            "daily_return": 0.012,
            "drawdown": -0.0435,
            "breadth": 0.615,
            "data_quality": 1.0,
            "market_regime": "进攻",
            "top_sectors": [{"name": "医药生物"}],
        },
        "positions": [
            {
                "symbol": "600183.SH",
                "name": "生益科技",
                "shares": 1_000,
                "avg_price": 31.25,
                "entry_date": "2026-07-20",
            }
        ],
        "daily_journals": [
            {"reflection": {"conclusion": "行情与预期一致，继续检查成交偏差。"}}
        ],
        "run": {"processed_days": 1},
    }


def test_report_contains_account_holdings_returns_and_plan() -> None:
    report = build_markdown_report(
        dashboard_fixture(),
        generated_at=datetime(2026, 7, 31, 18, 0),
    )

    assert "当前持仓（1 个）" in report
    assert "生益科技" in report
    assert "当前收益：**+10.00%**" in report
    assert "历史最高收益：**+15.00%**" in report
    assert "买入 测试股票" in report
    assert "下一交易日计划" in report
    assert "配置初始资金：**¥500,000.00**" in report
    assert "2024-01-01" in report
    assert "2025-12-31" in report
    assert "模拟盘起点：**2026-01-01**" in report


def test_midday_report_is_explicitly_read_only_and_not_realtime() -> None:
    report = build_position_report(
        dashboard_fixture(),
        generated_at=datetime(2026, 7, 31, 12, 0),
    )

    assert "午间盘位报告" in report
    assert "总仓位：**78.2%**" in report
    assert "当前收益：**+10.00%**" in report
    assert "历史最高收益：**+15.00%**" in report
    assert "不是盘中实时价格或实时盈亏" in report
    assert "不推进策略、不产生模拟成交" in report


def test_wechat_payload_uses_markdown_and_checks_success() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send_wechat_markdown(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            "日报内容",
            client=client,
        )

    assert captured == {
        "msgtype": "markdown",
        "markdown": {"content": "日报内容"},
    }


def test_unified_config_resolves_secrets_and_all_runtime_settings(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv(
        "WECHAT_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
    )

    config = load_unified_config(Path("config/quant-config.json"))

    assert config.tushare_token == "test-token"
    assert config.wechat_webhook_url.endswith("key=test")
    assert config.paper_account.frequency == "1d"
    assert config.paper_account.strategy_id == "moving_average"
    assert config.position_report_at == "12:00"
    assert config.daily_close_at == "18:00"
    assert config.state_directory.as_posix() == ".quant-state/accounts"
    assert config.reinitialize_on_config_change


def test_changed_account_config_is_reinitialized_automatically(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "account.txt"
    store = PaperStore(state_path)
    store.reset_account(
        "github-actions",
        500_000,
        ["000001.SZ", "000002.SZ", "000003.SZ", "600001.SH", "600002.SH"],
        "v1.0-balanced",
        {
            "strategy_id": "moving_average",
            "frequency": "1d",
            "backtest_start_date": "2024-01-01",
            "backtest_end_date": "2025-12-31",
            "simulation_start_date": "2026-01-01",
        },
    )
    store.close()

    captured = {}
    monkeypatch.setattr(daily_job, "TushareDataProvider", lambda token: object())

    def fake_replay(request, provider, replay_store):
        captured["request"] = request
        return {"reinitialized": True}

    monkeypatch.setattr(daily_job, "replay_paper_simulation", fake_replay)
    result = run_daily_job(
        JobConfig(
            account_id="github-actions",
            strategy_id="moving_average",
            frequency="1d",
            symbols=[
                "000001.SZ",
                "000002.SZ",
                "000003.SZ",
                "600001.SH",
                "600002.SH",
            ],
            backtest_start_date=date(2023, 1, 1),
            backtest_end_date=date(2024, 12, 31),
            simulation_start_date=date(2025, 1, 1),
            initial_cash=100_000,
        ),
        state_path=state_path,
        as_of_date=date(2026, 7, 31),
        tushare_token="test-token",
    )

    assert result == {"reinitialized": True}
    assert captured["request"].initial_cash == 100_000
    assert captured["request"].backtest_start_date == date(2023, 1, 1)
