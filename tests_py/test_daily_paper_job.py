from datetime import date, datetime
from copy import deepcopy
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
    send_qq_group_messages,
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
                "minimum_invested_ratio": 0.70,
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
    assert "最低仓位 **70%**" in report


def test_midday_report_is_explicitly_read_only_and_not_realtime() -> None:
    dashboard = dashboard_fixture()
    dashboard["latest"]["market_value"] = 300_000
    report = build_position_report(
        dashboard,
        generated_at=datetime(2026, 7, 31, 12, 0),
    )

    assert "午间盘位报告" in report
    assert "总仓位：**54.5%**" in report
    assert "当前收益：**+10.00%**" in report
    assert "历史最高收益：**+15.00%**" in report
    assert "不是盘中实时价格或实时盈亏" in report
    assert "不推进策略、不产生模拟成交" in report
    assert "低于配置下限 70.0%" in report


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


def test_report_never_omits_trading_plans() -> None:
    dashboard = dashboard_fixture()
    dashboard["account"]["pending_plan"] = [
        {
            "symbol": f"00000{index}.SZ",
            "name": f"计划{index}",
            "action": "BUY",
            "target_weight": 0.1,
            "reason": f"完整原因{index}",
        }
        for index in range(1, 8)
    ]

    report = build_markdown_report(
        dashboard,
        generated_at=datetime(2026, 7, 31, 18, 0),
    )

    assert "计划1" in report
    assert "计划7" in report
    assert "另有" not in report


def test_wechat_long_report_is_split_without_omitting_tail() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["markdown"]["content"])
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    content = "\n".join(f"- 完整交易计划 {index}：" + "理由" * 80 for index in range(30))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send_wechat_markdown(
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
            content,
            client=client,
        )

    assert len(captured) > 1
    assert any("完整交易计划 29" in message for message in captured)
    assert all(len(message.encode("utf-8")) < 3900 for message in captured)


def test_qq_markdown_is_downgraded_to_plain_text() -> None:
    markdown = (
        "### 账户概览\n"
        "> 信号日：**2026-08-06**\n"
        "- 当前权益：**¥99,904.96**\n"
        "- 新闻：[中煤能源公告](https://example.com/a)（搜狐证券）\n"
    )
    plain = daily_job._markdown_to_plain_text(markdown)

    assert "账户概览" in plain
    assert "###" not in plain
    assert "**" not in plain
    assert ">" not in plain
    assert "信号日：2026-08-06" in plain
    assert "中煤能源公告 (https://example.com/a)（搜狐证券）" in plain


def test_qq_long_report_is_split_without_omitting_tail() -> None:
    content = "\n\n".join(
        f"#### 段落 {index}\n" + "理由内容" * 200 for index in range(12)
    )
    chunks = daily_job._split_qq_messages(content)

    assert len(chunks) > 1
    assert any("段落 11" in chunk for chunk in chunks)
    assert all(len(chunk) <= daily_job.QQ_TEXT_CHUNK_LIMIT for chunk in chunks)


def test_qq_payload_uses_token_and_checks_success() -> None:
    requests: list[dict] = []
    token_requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requested
        if request.url.path.endswith("/app/getAppAccessToken"):
            token_requested = True
            assert json.loads(request.content) == {
                "appId": "APP_ID",
                "clientSecret": "APP_SECRET",
            }
            return httpx.Response(200, json={"access_token": "TOKEN", "expires_in": 7200})
        assert request.url.path == "/v2/groups/GROUP_OPENID/messages"
        assert request.headers["Authorization"] == "QQBot TOKEN"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"err_code": 0, "message": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        send_qq_group_messages(
            "GROUP_OPENID",
            "APP_ID",
            "APP_SECRET",
            "日报正文",
            client=client,
        )

    assert token_requested
    assert requests == [{"msg_type": 0, "content": "日报正文"}]


def test_qq_err_code_marks_message_as_failed_and_raises() -> None:
    calls = {"message": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app/getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "TOKEN"})
        calls["message"] += 1
        return httpx.Response(
            200,
            json={"err_code": 40034005, "message": "回复消息msg_id已过期"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            send_qq_group_messages(
                "GROUP_OPENID",
                "APP_ID",
                "APP_SECRET",
                "日报正文",
                client=client,
                retry_count=1,
            )
            raise AssertionError("应当抛出推送失败异常")
        except RuntimeError as exc:
            assert "QQ群第 1/1 段推送失败" in str(exc)

    # 初始1次 + 重试1次
    assert calls["message"] == 2


def test_unified_config_resolves_secrets_and_all_runtime_settings(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("BOCHA_API_KEY", "test-bocha-token")
    monkeypatch.setenv(
        "WECHAT_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
    )
    monkeypatch.setenv("QQ_APP_ID", "test-qq-app-id")
    monkeypatch.setenv("QQ_APP_SECRET", "test-qq-app-secret")
    monkeypatch.setenv("QQ_GROUP_OPENID", "test-qq-group-openid")

    config = load_unified_config(Path("config/quant-config.json"))

    assert config.tushare_token == "test-token"
    assert config.bocha_api_key == "test-bocha-token"
    assert config.market_universe.mode == "full_market"
    assert config.news_research.enabled
    assert config.wechat_webhook_url.endswith("key=test")
    assert config.notification_provider == "wechat"
    assert config.qq_app_id == "test-qq-app-id"
    assert config.qq_group_openid == "test-qq-group-openid"
    assert config.paper_account.frequency == "1d"
    assert config.paper_account.strategy_id == "moving_average"
    assert config.paper_account.risk_profile == "aggressive"
    assert config.paper_account.minimum_invested_ratio == 0.70
    assert config.position_report_at == "12:00"
    assert config.daily_close_at == "18:00"
    assert config.state_directory.as_posix() == ".quant-state/accounts"
    assert config.reinitialize_on_config_change


def test_unified_config_rejects_string_boolean(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    payload = json.loads(Path("config/quant-config.json").read_text(encoding="utf-8"))
    payload["notification"]["wechat_enabled"] = "false"
    path = tmp_path / "quant-config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    try:
        load_unified_config(path)
    except ValueError as exc:
        assert "JSON 布尔值" in str(exc)
    else:
        raise AssertionError("string boolean must be rejected")


def test_unified_config_normalizes_symbols(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("BOCHA_API_KEY", "test-bocha-token")
    monkeypatch.setenv(
        "WECHAT_WEBHOOK_URL",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
    )
    monkeypatch.setenv("QQ_APP_ID", "test-qq-app-id")
    monkeypatch.setenv("QQ_APP_SECRET", "test-qq-app-secret")
    monkeypatch.setenv("QQ_GROUP_OPENID", "test-qq-group-openid")
    payload = json.loads(Path("config/quant-config.json").read_text(encoding="utf-8"))
    payload = deepcopy(payload)
    payload["paper_account"]["symbols"] = [
        "159611",
        "002317.sz",
        "600183",
        "603738",
        "600367",
    ]
    path = tmp_path / "quant-config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    config = load_unified_config(path)

    assert config.paper_account.symbols == [
        "159611.SZ",
        "002317.SZ",
        "600183.SH",
        "603738.SH",
        "600367.SH",
    ]


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


def test_full_market_catch_up_rebuilds_each_date_in_order(
    tmp_path,
    monkeypatch,
) -> None:
    state_path = tmp_path / "account.txt"
    config = JobConfig(
        account_id="github-actions",
        strategy_id="moving_average",
        frequency="1d",
        universe_mode="full_market",
        symbols=["000001.SZ", "000002.SZ", "000003.SZ", "600001.SH", "600002.SH"],
        backtest_start_date=date(2024, 1, 1),
        backtest_end_date=date(2025, 12, 31),
        simulation_start_date=date(2026, 1, 1),
        initial_cash=100_000,
    )
    store = PaperStore(state_path)
    store.reset_account(
        config.account_id,
        config.initial_cash,
        config.symbols,
        "v1.0-balanced",
        {
            "strategy_id": config.strategy_id,
            "frequency": config.frequency,
            "universe_mode": config.universe_mode,
            "backtest_start_date": config.backtest_start_date.isoformat(),
            "backtest_end_date": config.backtest_end_date.isoformat(),
            "simulation_start_date": config.simulation_start_date.isoformat(),
        },
    )
    account = store.account(config.account_id)
    assert account is not None
    account["last_date"] = "2026-07-28"
    store.save_account(account)
    store.close()

    class FakeMarketProvider:
        def fetch_open_dates(self, start_date, end_date):
            return [date(2026, 7, day) for day in (29, 30, 31)]

    monkeypatch.setattr(
        daily_job,
        "TushareDataProvider",
        lambda token: FakeMarketProvider(),
    )
    research_calls: list[tuple[date, bool]] = []

    def fake_active(
        provider,
        active_store,
        active_config,
        as_of_date,
        universe_config,
        news_config,
        bocha_api_key,
        include_news=True,
    ):
        research_calls.append((as_of_date, include_news))
        return active_config.symbols, {
            "mode": "full_market",
            "trade_date": as_of_date.isoformat(),
            "candidates": [],
            "warnings": [],
        }

    advance_calls: list[date] = []

    def fake_advance(request, provider, active_store):
        advance_calls.append(request.as_of_date)
        active_account = active_store.account(request.account_id)
        assert active_account is not None
        active_account["last_date"] = request.as_of_date.isoformat()
        active_store.save_account(active_account)
        return {"run": {"processed_days": 1, "data_errors": []}}

    monkeypatch.setattr(daily_job, "_active_market_universe", fake_active)
    monkeypatch.setattr(daily_job, "advance_paper_simulation", fake_advance)

    result = run_daily_job(
        config,
        state_path=state_path,
        as_of_date=date(2026, 7, 31),
        tushare_token="test-token",
        market_universe=daily_job.MarketUniverseConfig(mode="full_market"),
    )

    assert advance_calls == [date(2026, 7, day) for day in (29, 30, 31)]
    assert research_calls == [
        (date(2026, 7, 29), False),
        (date(2026, 7, 30), False),
        (date(2026, 7, 31), True),
    ]
    assert result["run"]["processed_days"] == 3
    assert result["run"]["point_in_time_research_days"] == 3
