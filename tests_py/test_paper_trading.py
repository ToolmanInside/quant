from datetime import date

import numpy as np
import pandas as pd

from backend.data.providers import MarketData
from backend.models import PaperAdvanceRequest, PaperSimulationRequest
from backend.paper_store import PaperStore
from backend.paper_trading import (
    PaperCosts,
    RISK_PROFILE_INITIAL_VERSION,
    VERSION_LIBRARY,
    _allocate_capped_weights,
    _analyze,
    _apply_corporate_actions,
    _execute_pending,
    _evaluate_version,
    _feature_row,
    advance_paper_simulation,
    replay_paper_simulation,
)


SYMBOLS = [
    "000001.SZ",
    "000002.SZ",
    "000003.SZ",
    "600001.SH",
    "600002.SH",
    "600003.SH",
]


def market_frame(symbol_index: int) -> pd.DataFrame:
    dates = pd.bdate_range("2023-01-01", "2025-12-31")
    index = np.arange(len(dates), dtype=float)
    trend = 0.008 + symbol_index * 0.001
    close = 10 + index * trend + np.sin(index / (12 + symbol_index)) * 0.35
    open_price = np.roll(close, 1)
    open_price[0] = close[0]
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": open_price,
            "high": np.maximum(open_price, close) * 1.01,
            "low": np.minimum(open_price, close) * 0.99,
            "close": close,
            "adj_open": open_price,
            "adj_high": np.maximum(open_price, close) * 1.01,
            "adj_low": np.minimum(open_price, close) * 0.99,
            "adj_close": close,
            "volume": 8_000_000 + symbol_index * 1_000_000,
            "amount": close * (8_000_000 + symbol_index * 1_000_000),
        }
    )


def ma_cross_frame(
    *,
    cross_today: bool,
    cross_day_volume_ratio: float = 1.0,
) -> pd.DataFrame:
    """构造"震荡后启动趋势"的日线：100天震荡 + 70天单边趋势。

    cross_today=True 时最后一根 K 线恰为 5/20 金叉日（可控制当日量比）；
    cross_today=False 时最后一根为金叉后 15 天（持续多头、非金叉日）。
    """
    osc_days, trend_days = 100, 70
    n = osc_days + trend_days
    index = np.arange(n, dtype=float)
    osc = np.sin(index / 4.0) * 1.0 + np.sin(index / 11.0) * 0.5
    trend = (index - osc_days) * 0.20 + np.sin(index / 20.0) * 0.10
    close = np.where(index < osc_days, 10 + osc, 10 + trend)
    dates = pd.bdate_range("2025-01-01", periods=n)
    fast = pd.Series(close).rolling(5).mean()
    slow = pd.Series(close).rolling(20).mean()
    cross_up = np.where(
        (fast > slow)
        & (fast.shift(1) <= slow.shift(1))
        & (index > osc_days + 2)
    )[0]
    assert len(cross_up) > 0, "构造数据应产生趋势段金叉"
    cross_idx = int(cross_up[0])
    end = cross_idx + 1 if cross_today else min(cross_idx + 15, n)

    frame = pd.DataFrame(
        {
            "trade_date": dates[:end],
            "open": close[:end],
            "high": close[:end] * 1.01 + 0.05,
            "low": close[:end] * 0.99 - 0.05,
            "close": close[:end],
            "adj_open": close[:end],
            "adj_high": close[:end] * 1.01 + 0.05,
            "adj_low": close[:end] * 0.99 - 0.05,
            "adj_close": close[:end],
        }
    )
    base_volume = 8_000_000
    frame["volume"] = base_volume
    if cross_today:
        frame.loc[frame.index[-1], "volume"] = int(
            base_volume * cross_day_volume_ratio
        )
    frame["amount"] = frame["close"] * frame["volume"]
    return frame


class FakeProvider:
    def fetch_daily(self, symbol: str, start_date: date, end_date: date) -> MarketData:
        frame = market_frame(SYMBOLS.index(symbol))
        mask = (frame["trade_date"].dt.date >= start_date) & (
            frame["trade_date"].dt.date <= end_date
        )
        return MarketData(frame.loc[mask].reset_index(drop=True), "fixture")

    def fetch_industries(self, symbols: list[str]) -> dict[str, dict[str, str]]:
        return {
            symbol: {
                "sector_code": f"S{index % 3}",
                "sector_name": f"行业{index % 3}",
                "name": f"测试股票{index}",
            }
            for index, symbol in enumerate(symbols)
        }


class FullMarketProvider(FakeProvider):
    """FakeProvider + minimal full-market snapshot APIs.

    快照只推荐 SYMBOLS[5]（不在请求池里），用来验证全市场模式确实按截面选股，
    而不是局限于 request.symbols。
    """

    def __init__(self) -> None:
        self.snapshot_calls: list[date] = []

    def fetch_open_dates(self, start_date: date, end_date: date) -> list[date]:
        frame = market_frame(0)
        days = [
            d
            for d in frame["trade_date"].dt.date.tolist()
            if start_date <= d <= end_date
        ]
        return sorted(days)

    def fetch_market_snapshot(
        self,
        as_of_date: date,
    ) -> tuple[date, pd.DataFrame, list[str]]:
        self.snapshot_calls.append(as_of_date)
        rows = []
        for index, symbol in enumerate(SYMBOLS):
            rows.append(
                {
                    "ts_code": symbol,
                    "name": f"测试股票{index}",
                    "trade_date": pd.Timestamp(as_of_date),
                    "close": 10.0 + index,
                    "amount": 5_000_000.0,  # 单位千元 → 50 亿元，过成交额门槛
                    "list_date": "20100101",
                    "pct_chg": 5.0 - index,  # SYMBOLS[0] 涨幅最高
                    "industry": f"行业{index % 3}",
                }
            )
        return as_of_date, pd.DataFrame(rows), []

    def fetch_market_technical_breadth(
        self,
        trade_date: date,
    ) -> dict[str, float | int | str]:
        return {
            "trade_date": trade_date.isoformat(),
            "above_ma20": 0.6,
            "above_ma60": 0.5,
            "composite": 0.55,
            "coverage": 0.9,
            "sample_size": len(SYMBOLS),
        }

    def fetch_market_technical_frame(self, trade_date: date) -> pd.DataFrame:
        rows = []
        for index, symbol in enumerate(SYMBOLS):
            close = 10.0 + index
            # 让最后一只明显站上双均线，验证趋势袖套能从全市场捞人
            ma20 = close * (0.90 if index == len(SYMBOLS) - 1 else 1.05)
            ma60 = close * (0.85 if index == len(SYMBOLS) - 1 else 1.10)
            rows.append(
                {
                    "ts_code": symbol,
                    "trade_date": trade_date.strftime("%Y%m%d"),
                    "close_qfq": close,
                    "ma_qfq_20": ma20,
                    "ma_qfq_60": ma60,
                }
            )
        return pd.DataFrame(rows)


def test_full_market_advance_scans_beyond_requested_pool(tmp_path) -> None:
    """全市场模式 advance 应调用截面快照，而非局限固定池。"""
    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account(
        "default",
        100_000,
        SYMBOLS[:5],
        "v1.0-balanced",
        {
            "strategy_id": "moving_average",
            "universe_mode": "full_market",
            "backtest_start_date": "2024-01-01",
        },
    )
    account = store.account("default")
    assert account is not None
    account["last_date"] = "2025-12-26"
    store.save_account(account)

    provider = FullMarketProvider()
    result = advance_paper_simulation(
        PaperAdvanceRequest(
            account_id="default",
            symbols=SYMBOLS[:5],
            as_of_date=date(2025, 12, 31),
        ),
        provider,
        store,
    )

    # 每个新交易日都做了一次全市场截面扫描
    assert provider.snapshot_calls, "全市场模式必须调用 fetch_market_snapshot"
    assert result["run"]["mode"] == "advance"
    assert result["run"]["processed_days"] >= 1
    store.close()


def test_replay_creates_auditable_paper_account(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.txt")
    request = PaperSimulationRequest(
        strategy_id="momentum",
        symbols=SYMBOLS,
        backtest_start_date=date(2023, 1, 1),
        backtest_end_date=date(2024, 12, 31),
        simulation_start_date=date(2025, 1, 1),
        simulation_end_date=date(2025, 12, 31),
        initial_cash=500_000,
    )

    dashboard = replay_paper_simulation(request, FakeProvider(), store)

    assert dashboard["run"]["backtest_days"] > 400
    assert dashboard["run"]["processed_days"] > 200
    assert dashboard["journal_count"] == dashboard["run"]["processed_days"]
    assert dashboard["daily_journals"][0]["decision"]["summary"]
    assert dashboard["daily_journals"][0]["reflection"]["next_focus"]
    assert dashboard["equity_curve"][0]["trade_date"] >= "2025-01-01"
    assert dashboard["account"]["configuration"]["backtest_end_date"] == "2024-12-31"
    assert dashboard["account"]["configuration"]["simulation_start_date"] == "2025-01-01"
    assert dashboard["account"]["configuration"]["strategy_id"] == "momentum"
    assert dashboard["latest"]["equity"] > 0
    assert dashboard["account"]["last_date"] == "2025-12-31"
    assert dashboard["versions"]
    assert dashboard["upgrade_events"]
    assert dashboard["holding_summary"]["invested_days"] > 0
    assert dashboard["holding_summary"]["last_holding_positions"]
    assert all(
        item["action"] in {"BUY", "SELL", "CLOSE"}
        for item in dashboard["executions"]
    )
    assert dashboard["account"]["pending_plan"] is not None
    store.close()


def test_all_daily_strategy_styles_produce_valid_analysis() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    industries = FakeProvider().fetch_industries(SYMBOLS)
    for strategy_id in ("moving_average", "momentum", "breakout"):
        result = _analyze(
            pd.Timestamp("2025-12-31"),
            frames,
            industries,
            VERSION_LIBRARY["v1.0-balanced"],
            {},
            500_000,
            strategy_id,
        )
        assert result["ready"]
        assert all(
            item["action"] in {"BUY", "SELL", "CLOSE"}
            for item in result["plan"]
        )


def test_defensive_regime_allows_small_breakout_probe() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    for symbol in SYMBOLS[1:]:
        frame = frames[symbol]
        falling = np.linspace(18.0, 8.0, 100)
        for column in ("close", "adj_close"):
            frame.loc[frame.index[-100:], column] = falling
        frame.loc[frame.index[-100:], "high"] = falling * 1.01
        frame.loc[frame.index[-100:], "adj_high"] = falling * 1.01
        frame.loc[frame.index[-100:], "low"] = falling * 0.99
        frame.loc[frame.index[-100:], "adj_low"] = falling * 0.99

    leader = frames[SYMBOLS[0]]
    breakout_price = float(leader["adj_high"].iloc[-21:-1].max()) * 1.05
    for column in ("close", "adj_close"):
        leader.loc[leader.index[-1], column] = breakout_price
    for column in ("high", "adj_high"):
        leader.loc[leader.index[-1], column] = breakout_price * 1.01

    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        VERSION_LIBRARY["v1.0-balanced"],
        {},
        500_000,
        "breakout",
    )

    assert result["market_regime"] == "防守"
    assert result["target_weights"][SYMBOLS[0]] > 0
    assert any(item["action"] == "BUY" for item in result["plan"])


def test_daily_advance_is_idempotent_without_new_data(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.txt")
    replay_paper_simulation(
        PaperSimulationRequest(
            symbols=SYMBOLS,
            backtest_start_date=date(2024, 1, 1),
            backtest_end_date=date(2025, 3, 31),
            simulation_start_date=date(2025, 4, 1),
            simulation_end_date=date(2025, 12, 31),
        ),
        FakeProvider(),
        store,
    )
    result = advance_paper_simulation(
        PaperAdvanceRequest(
            symbols=SYMBOLS,
            as_of_date=date(2025, 12, 31),
        ),
        FakeProvider(),
        store,
    )
    assert result["run"]["processed_days"] == 0
    assert "未重复执行" in result["run"]["message"]
    assert result["journal_count"] > 0
    store.close()


def test_corporate_action_preserves_economic_value_and_cost_basis(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, [SYMBOLS[0]], "v1.0-balanced")
    store.add_execution(
        "default",
        {
            "trade_date": "2025-06-10",
            "symbol": SYMBOLS[0],
            "name": "测试股票",
            "sector": "测试行业",
            "action": "BUY",
            "quantity": 1_000,
            "price": 10.0,
        },
    )
    account = store.account("default")
    assert account is not None
    positions = {
        SYMBOLS[0]: {
            "name": "测试股票",
            "sector": "测试行业",
            "shares": 1_000,
            "avg_price": 10.0,
            "cost_basis_total": 10_000.0,
            "entry_date": "2025-06-10",
        }
    }

    applied = _apply_corporate_actions(
        account,
        positions,
        pd.Timestamp("2025-06-20"),
        [
            {
                "symbol": SYMBOLS[0],
                "end_date": "20241231",
                "stock_dividend_per_share": 1.0,
                "cash_dividend_per_share": 0.5,
            }
        ],
        store,
    )

    position = positions[SYMBOLS[0]]
    assert applied[0]["shares_added"] == 1_000
    assert account["cash"] == 100_500
    assert position["shares"] == 2_000
    assert position["cost_basis_total"] == 9_500
    assert position["avg_price"] == 4.75
    assert account["cash"] + position["shares"] * 4.75 == 110_000
    reconstructed = store.positions_as_of("default", "2025-06-20")[0]
    assert reconstructed["shares"] == 2_000
    assert reconstructed["avg_price"] == 4.75
    assert store.dashboard("default")["corporate_actions"]
    store.close()


def test_position_caps_redistribute_instead_of_losing_exposure() -> None:
    weights = _allocate_capped_weights(
        {"A": 100.0, "B": 1.0, "C": 1.0},
        target_exposure=0.60,
        position_cap=0.22,
    )

    assert abs(sum(weights.values()) - 0.60) < 1e-9
    assert weights["A"] == 0.22
    assert all(weight <= 0.22 for weight in weights.values())


def test_weight_allocator_reserves_an_executable_board_lot() -> None:
    weights = _allocate_capped_weights(
        {"A": 1.0, "B": 1.0, "C": 1.0},
        target_exposure=0.75,
        position_cap=0.30,
        minimum_weights={"A": 0.18, "B": 0.05, "C": 0.02},
    )

    assert abs(sum(weights.values()) - 0.75) < 1e-9
    assert weights["A"] >= 0.18
    assert weights["B"] >= 0.05
    assert weights["C"] >= 0.02
    assert all(weight <= 0.30 for weight in weights.values())


def test_aggressive_profile_raises_defensive_exposure() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        params,
        {},
        100_000,
        "moving_average",
        {"technical_breadth": {"composite": 0.10, "coverage": 0.95}},
    )

    assert result["market_regime"] == "防守"
    assert result["requested_exposure"] == 0.35
    assert result["allocated_exposure"] > 0.30


def test_configured_minimum_exposure_is_suspended_in_defensive_regime() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "minimum_exposure": 0.70,
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        params,
        {},
        100_000,
        "moving_average",
        {"technical_breadth": {"composite": 0.10, "coverage": 0.95}},
    )

    assert result["market_regime"] == "防守"
    # 防守状态下最低仓位被挂起，避免市场转弱时被迫买入无信号标的。
    assert result["minimum_exposure"] == 0.0
    assert result["minimum_suspended_reason"]
    assert result["requested_exposure"] == 0.35
    assert result["allocated_exposure"] > 0.30


def test_configured_minimum_exposure_applies_in_offensive_regime() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "minimum_exposure": 0.70,
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        params,
        {},
        100_000,
        "moving_average",
        {"technical_breadth": {"composite": 0.90, "coverage": 0.95}},
    )

    assert result["market_regime"] == "进攻"
    assert result["minimum_exposure"] == 0.70
    assert result["minimum_suspended_reason"] is None
    assert result["requested_exposure"] >= 0.70
    assert result["allocated_exposure"] >= 0.70


def test_etf_fallback_fills_gap_when_stock_pool_is_too_small() -> None:
    from backend.paper_trading import ETF_FALLBACK_POOL
    stock_symbols = SYMBOLS[:3]
    frames = {
        symbol: market_frame(index) for index, symbol in enumerate(stock_symbols)
    }
    for index, symbol in enumerate(ETF_FALLBACK_POOL):
        frames[symbol] = market_frame(index)
    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "minimum_exposure": 0.70,
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "etf_volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(stock_symbols),
        params,
        {},
        100_000,
        "moving_average",
    )

    # 只有3只个股（最多3×25%=75%）时，缺口由趋势成立的宽基ETF补足；
    # 趋势不成立的ETF（如本数据中的510500）不会被买入，剩余缺口留现金。
    assert result["allocated_exposure"] >= 0.80
    assert result["etf_fallback_used"]
    assert "159919.SZ" in result["target_weights"]
    assert "159915.SZ" in result["target_weights"]
    assert "510500.SH" not in result["target_weights"]
    assert any(
        symbol in result["target_weights"] for symbol in ETF_FALLBACK_POOL
    )
    assert any(
        item["symbol"] in ETF_FALLBACK_POOL for item in result["plan"]
    )


def test_candidate_pool_etf_still_participates_in_stock_selection() -> None:
    # 159611.SZ 这类主题ETF在候选池中时应保持原有个股化选股行为，
    # 只有兜底池的宽基ETF(159919/510500/159915)才走独立兜底路径。
    pool_symbols = [*SYMBOLS[:5], "159611.SZ"]
    frames = {
        symbol: market_frame(index) for index, symbol in enumerate(pool_symbols)
    }
    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "minimum_exposure": 0.70,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(pool_symbols),
        params,
        {},
        100_000,
        "moving_average",
        {"technical_breadth": {"composite": 0.90, "coverage": 0.95}},
    )

    assert "159611.SZ" in result["features"]
    assert result["etf_fallback_used"] == []


def test_feature_row_reports_adx_and_golden_cross() -> None:
    params = VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]]

    # 金叉日：ma_cross_up=True，趋势启动 ADX 应达到阈值
    cross_frame = ma_cross_frame(cross_today=True, cross_day_volume_ratio=1.0)
    feature = _feature_row(
        cross_frame,
        pd.Timestamp(cross_frame["trade_date"].iloc[-1]),
        params,
    )
    assert feature is not None
    assert feature["ma_cross_up"] is True
    assert feature["adx"] >= float(params["adx_min"])

    # 金叉 15 天后：持续多头，非金叉日，ADX 仍高
    held_frame = ma_cross_frame(cross_today=False)
    feature_held = _feature_row(
        held_frame,
        pd.Timestamp(held_frame["trade_date"].iloc[-1]),
        params,
    )
    assert feature_held is not None
    assert feature_held["ma_cross_up"] is False
    assert feature_held["adx"] >= float(params["adx_min"])


def test_ma_golden_cross_requires_volume_confirmation() -> None:
    """金叉日必须放量（量比≥volume_confirm_ratio）才允许进场。"""
    params = VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]]
    target = SYMBOLS[0]
    others = SYMBOLS[1:5]

    def run_analysis(target_frame: pd.DataFrame) -> set[str]:
        frames = {symbol: ma_cross_frame(cross_today=False) for symbol in others}
        frames[target] = target_frame
        trade_date = pd.Timestamp(target_frame["trade_date"].iloc[-1])
        result = _analyze(
            trade_date,
            frames,
            FakeProvider().fetch_industries(SYMBOLS[:5]),
            params,
            {},
            100_000,
            "moving_average",
            {"technical_breadth": {"composite": 0.90, "coverage": 0.95}},
        )
        return set(result["selected_symbols"])

    # 金叉日缩量（量比 1.0 < 1.5）→ 视为震荡假金叉，不进场
    low_volume = ma_cross_frame(cross_today=True, cross_day_volume_ratio=1.0)
    assert target not in run_analysis(low_volume)

    # 金叉日放量（量比 2.0 ≥ 1.5）→ 确认有效，进场
    high_volume = ma_cross_frame(cross_today=True, cross_day_volume_ratio=2.0)
    assert target in run_analysis(high_volume)


def test_ma_entry_cannot_bypass_failed_cross_confirmation_next_day() -> None:
    """低量金叉被拒后，次日仍不得仅凭多头排列绕过确认。"""
    params = VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]]
    target = SYMBOLS[0]
    target_frame = ma_cross_frame(cross_today=True, cross_day_volume_ratio=1.0)
    next_row = target_frame.iloc[-1].copy()
    next_row["trade_date"] = pd.Timestamp(next_row["trade_date"]) + pd.offsets.BDay(1)
    next_row["volume"] = 8_000_000
    next_row["amount"] = float(next_row["close"]) * float(next_row["volume"])
    target_frame = pd.concat(
        [target_frame, pd.DataFrame([next_row])],
        ignore_index=True,
    )
    frames = {
        symbol: ma_cross_frame(cross_today=False)
        for symbol in SYMBOLS[1:5]
    }
    frames[target] = target_frame
    trade_date = pd.Timestamp(target_frame["trade_date"].iloc[-1])

    result = _analyze(
        trade_date,
        frames,
        FakeProvider().fetch_industries(SYMBOLS[:5]),
        params,
        {},
        100_000,
        "moving_average",
        {"technical_breadth": {"composite": 0.90, "coverage": 0.95}},
    )

    feature = result["features"][target]
    assert not bool(feature["ma_cross_up"])
    assert feature["days_since_cross"] == 1
    assert feature["cross_volume_ratio"] < float(params["volume_confirm_ratio"])
    assert not bool(feature["eligible"])
    assert target not in result["selected_symbols"]


def test_ma_entry_requires_adx_direction_and_respects_reentry_cooldown() -> None:
    target = SYMBOLS[0]
    target_frame = ma_cross_frame(cross_today=True, cross_day_volume_ratio=2.0)
    trade_date = pd.Timestamp(target_frame["trade_date"].iloc[-1])
    frames = {
        symbol: ma_cross_frame(cross_today=False)
        for symbol in SYMBOLS[1:5]
    }
    frames[target] = target_frame
    industries = FakeProvider().fetch_industries(SYMBOLS[:5])
    params = VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]]

    high_adx_floor = {**params, "adx_min": 101}
    adx_blocked = _analyze(
        trade_date,
        frames,
        industries,
        high_adx_floor,
        {},
        100_000,
        "moving_average",
    )
    assert target not in adx_blocked["selected_symbols"]

    cooldown_blocked = _analyze(
        trade_date,
        frames,
        industries,
        params,
        {},
        100_000,
        "moving_average",
        last_exit_dates={
            target: pd.Timestamp(target_frame["trade_date"].iloc[-2]).date().isoformat()
        },
    )
    assert target not in cooldown_blocked["selected_symbols"]


def test_ma_shallow_death_cross_requires_confirmation_before_close() -> None:
    target = SYMBOLS[0]
    target_frame = ma_cross_frame(cross_today=False).copy()
    close = target_frame["adj_close"].astype(float)
    equality_price = (
        float(close.iloc[-20:-1].sum())
        - 4 * float(close.iloc[-5:-1].sum())
    ) / 3
    shallow_price = equality_price - 0.01
    last_index = target_frame.index[-1]
    for column in ("open", "close", "adj_open", "adj_close"):
        target_frame.loc[last_index, column] = shallow_price
    for column in ("high", "adj_high"):
        target_frame.loc[last_index, column] = shallow_price * 1.01
    for column in ("low", "adj_low"):
        target_frame.loc[last_index, column] = shallow_price * 0.99
    target_frame.loc[last_index, "amount"] = (
        shallow_price * float(target_frame.loc[last_index, "volume"])
    )

    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "minimum_score": 0.0,
        "minimum_amount": 0.0,
    }
    positions = {
        target: {
            "shares": 1_000,
            "avg_price": 1.0,
            "name": "测试股票",
            "sector": "行业0",
            "entry_date": "2025-01-01",
        }
    }
    frames = {
        symbol: ma_cross_frame(cross_today=False)
        for symbol in SYMBOLS[1:5]
    }
    frames[target] = target_frame
    industries = FakeProvider().fetch_industries(SYMBOLS[:5])
    first_date = pd.Timestamp(target_frame["trade_date"].iloc[-1])

    first = _analyze(
        first_date,
        frames,
        industries,
        params,
        positions,
        100_000,
        "moving_average",
    )
    first_feature = first["features"][target]
    assert first_feature["fast_ma"] < first_feature["slow_ma"]
    assert first_feature["fast_ma"] > first_feature["slow_ma"] * 0.995
    assert first_feature["below_slow_days"] == 1
    assert not any(
        item["symbol"] == target and item["action"] == "CLOSE"
        for item in first["plan"]
    )

    second_row = target_frame.iloc[-1].copy()
    second_row["trade_date"] = first_date + pd.offsets.BDay(1)
    target_frame = pd.concat(
        [target_frame, pd.DataFrame([second_row])],
        ignore_index=True,
    )
    frames[target] = target_frame
    second = _analyze(
        pd.Timestamp(target_frame["trade_date"].iloc[-1]),
        frames,
        industries,
        params,
        positions,
        100_000,
        "moving_average",
    )
    assert second["features"][target]["below_slow_days"] >= 2
    assert any(
        item["symbol"] == target
        and item["action"] == "CLOSE"
        and "确认跌破" in item["reason"]
        for item in second["plan"]
    )


def test_analysis_skips_symbols_whose_board_lot_exceeds_position_cap() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    for frame in frames.values():
        for column in (
            "open",
            "high",
            "low",
            "close",
            "adj_open",
            "adj_high",
            "adj_low",
            "adj_close",
        ):
            frame[column] *= 50
        frame["amount"] *= 50

    params = {
        **VERSION_LIBRARY[RISK_PROFILE_INITIAL_VERSION["aggressive"]],
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        params,
        {},
        100_000,
        "moving_average",
    )

    assert not result["target_weights"]
    assert result["unaffordable_symbols"]
    assert result["exposure_constraint"] == "board_lot_affordability"


def test_full_market_medium_term_breadth_controls_regime() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS),
        VERSION_LIBRARY["v1.0-balanced"],
        {},
        500_000,
        "moving_average",
        {
            "technical_breadth": {
                "composite": 0.10,
                "coverage": 0.95,
            }
        },
    )

    assert result["candidate_breadth"] > 0.50
    assert result["breadth"] == 0.10
    assert result["breadth_source"] == "full_market_ma20_ma60"
    assert result["market_regime"] == "防守"


def test_execution_records_partial_fill_and_allocation_gap(tmp_path) -> None:
    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, SYMBOLS[:2], "v1.0-balanced")
    account = store.account("default")
    assert account is not None
    account["pending_plan"] = [
        {
            "symbol": symbol,
            "name": symbol,
            "sector": "测试行业",
            "action": "BUY",
            "target_weight": 0.80,
            "reason": "测试资金竞争",
        }
        for symbol in SYMBOLS[:2]
    ]
    frames = {
        symbol: market_frame(index)
        for index, symbol in enumerate(SYMBOLS[:2])
    }

    executions, outcomes = _execute_pending(
        account,
        {},
        pd.Timestamp("2025-12-31"),
        frames,
        FakeProvider().fetch_industries(SYMBOLS[:2]),
        store,
        PaperCosts(),
    )

    assert len(executions) == 2
    assert any(item["fill_ratio"] < 1 for item in outcomes)
    partial = next(item for item in outcomes if item["fill_ratio"] < 1)
    assert partial["constraint_reason"] == "insufficient_cash_partial_fill"
    assert partial["allocation_gap"] < 0
    store.close()


def test_successful_close_records_reentry_cooldown_date(tmp_path) -> None:
    symbol = SYMBOLS[0]
    trade_date = pd.Timestamp("2025-12-31")
    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, [symbol], "v1.0-balanced")
    account = store.account("default")
    assert account is not None
    account["pending_plan"] = [
        {
            "symbol": symbol,
            "name": symbol,
            "sector": "测试行业",
            "action": "CLOSE",
            "target_weight": 0.0,
            "reason": "测试冷却期",
        }
    ]
    positions = {
        symbol: {
            "name": symbol,
            "sector": "测试行业",
            "shares": 1_000,
            "avg_price": 10.0,
            "cost_basis_total": 10_000.0,
            "entry_date": "2025-01-01",
        }
    }

    executions, _ = _execute_pending(
        account,
        positions,
        trade_date,
        {symbol: market_frame(0)},
        FakeProvider().fetch_industries([symbol]),
        store,
        PaperCosts(),
    )

    assert executions[0]["action"] == "CLOSE"
    assert symbol not in positions
    assert account["last_exit_dates"][symbol] == "2025-12-31"
    store.close()


def test_locked_limit_up_blocks_buy_and_locked_limit_down_blocks_close(
    tmp_path,
) -> None:
    trade_date = pd.Timestamp("2025-12-31")
    frames = {
        symbol: market_frame(index)
        for index, symbol in enumerate(SYMBOLS[:2])
    }
    buy_symbol, sell_symbol = SYMBOLS[:2]
    for symbol, limit_column in ((buy_symbol, "up_limit"), (sell_symbol, "down_limit")):
        frame = frames[symbol]
        row_index = frame.index[frame["trade_date"] == trade_date][0]
        limit_price = float(frame.loc[row_index, "open"])
        frame.loc[row_index, ["open", "high", "low", "close"]] = limit_price
        frame.loc[row_index, "up_limit"] = (
            limit_price if limit_column == "up_limit" else limit_price + 2
        )
        frame.loc[row_index, "down_limit"] = (
            limit_price if limit_column == "down_limit" else limit_price - 2
        )

    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, SYMBOLS[:2], "v1.0-balanced")
    account = store.account("default")
    assert account is not None
    account["pending_plan"] = [
        {
            "symbol": buy_symbol,
            "name": buy_symbol,
            "sector": "测试行业",
            "action": "BUY",
            "target_weight": 0.20,
            "reason": "测试一字涨停",
        },
        {
            "symbol": sell_symbol,
            "name": sell_symbol,
            "sector": "测试行业",
            "action": "CLOSE",
            "target_weight": 0.0,
            "reason": "测试一字跌停",
        },
    ]
    positions = {
        sell_symbol: {
            "name": sell_symbol,
            "sector": "测试行业",
            "shares": 1_000,
            "avg_price": 10.0,
            "cost_basis_total": 10_000.0,
            "entry_date": "2025-01-01",
        }
    }

    executions, outcomes = _execute_pending(
        account,
        positions,
        trade_date,
        frames,
        FakeProvider().fetch_industries(SYMBOLS[:2]),
        store,
        PaperCosts(),
    )

    assert executions == []
    reasons = {item["symbol"]: item["constraint_reason"] for item in outcomes}
    assert reasons[buy_symbol] == "limit_up_locked"
    assert reasons[sell_symbol] == "limit_down_locked"
    assert positions[sell_symbol]["shares"] == 1_000
    store.close()


def test_opened_limit_board_clamps_slippage_to_legal_price(tmp_path) -> None:
    symbol = SYMBOLS[0]
    trade_date = pd.Timestamp("2025-12-31")
    frame = market_frame(0)
    row_index = frame.index[frame["trade_date"] == trade_date][0]
    frame.loc[row_index, "open"] = 11.0
    frame.loc[row_index, "high"] = 11.0
    frame.loc[row_index, "low"] = 10.8
    frame.loc[row_index, "up_limit"] = 11.0
    frame.loc[row_index, "down_limit"] = 9.0

    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, [symbol], "v1.0-balanced")
    account = store.account("default")
    assert account is not None
    account["pending_plan"] = [
        {
            "symbol": symbol,
            "name": symbol,
            "sector": "测试行业",
            "action": "BUY",
            "target_weight": 0.20,
            "reason": "测试开板涨停",
        }
    ]

    executions, outcomes = _execute_pending(
        account,
        {},
        trade_date,
        {symbol: frame},
        FakeProvider().fetch_industries([symbol]),
        store,
        PaperCosts(),
    )

    assert outcomes[0]["constraint_reason"] is None
    assert executions[0]["price"] == 11.0
    store.close()


def test_version_evaluation_uses_only_matching_daily_market_context() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    dates = list(pd.bdate_range("2025-01-01", "2025-12-31"))
    industries = FakeProvider().fetch_industries(SYMBOLS)
    params = {
        **VERSION_LIBRARY["v1.0-balanced"],
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    without_context = _evaluate_version(
        frames,
        industries,
        dates,
        params,
        "moving_average",
    )
    defensive_context = {
        day.normalize(): {
            "technical_breadth": {"composite": 0.10, "coverage": 1.0}
        }
        for day in dates
    }
    with_context = _evaluate_version(
        frames,
        industries,
        dates,
        params,
        "moving_average",
        defensive_context,
    )

    assert with_context["annualized_return"] < without_context["annualized_return"]


def test_version_evaluation_is_invariant_to_adjusted_price_anchor() -> None:
    frames = {symbol: market_frame(index) for index, symbol in enumerate(SYMBOLS)}
    dates = list(pd.bdate_range("2025-01-01", "2025-12-31"))
    industries = FakeProvider().fetch_industries(SYMBOLS)
    params = {
        **VERSION_LIBRARY["v1.0-balanced"],
        "adx_min": 0,
        "volume_confirm_ratio": 1.0,
        "cross_valid_days": 500,
    }
    baseline = _evaluate_version(
        frames,
        industries,
        dates,
        params,
        "moving_average",
    )
    reanchored = {symbol: frame.copy() for symbol, frame in frames.items()}
    for frame in reanchored.values():
        for column in ("adj_open", "adj_high", "adj_low", "adj_close"):
            frame[column] = frame[column] * 0.37

    result = _evaluate_version(
        reanchored,
        industries,
        dates,
        params,
        "moving_average",
    )

    assert result == baseline
    assert baseline["estimated_transaction_cost"] > 0
    assert abs(
        baseline["estimated_transaction_cost"]
        - baseline["estimated_commission"]
        - baseline["estimated_stamp_tax"]
        - baseline["estimated_slippage"]
    ) <= 0.02


def test_advance_forces_existing_holdings_into_requested_universe(tmp_path) -> None:
    requested_symbols: list[str] = []
    requested_start_dates: list[date] = []

    class CapturingProvider(FakeProvider):
        def fetch_daily(
            self,
            symbol: str,
            start_date: date,
            end_date: date,
        ) -> MarketData:
            requested_symbols.append(symbol)
            requested_start_dates.append(start_date)
            return super().fetch_daily(symbol, start_date, end_date)

    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account(
        "default",
        100_000,
        SYMBOLS,
        "v1.0-balanced",
        {
            "strategy_id": "moving_average",
            "universe_mode": "fixed",
            "backtest_start_date": "2025-05-03",
        },
    )
    account = store.account("default")
    assert account is not None
    account["last_date"] = "2025-12-29"
    store.save_account(account)
    store.save_positions(
        "default",
        {
            SYMBOLS[0]: {
                "name": SYMBOLS[0],
                "sector": "测试行业",
                "shares": 1_000,
                "avg_price": 10.0,
                "cost_basis_total": 10_000.0,
                "entry_date": "2025-12-01",
            }
        },
    )

    result = advance_paper_simulation(
        PaperAdvanceRequest(
            account_id="default",
            symbols=SYMBOLS[1:],
            as_of_date=date(2025, 12, 31),
        ),
        CapturingProvider(),
        store,
    )

    assert SYMBOLS[0] in requested_symbols
    assert set(requested_start_dates) == {date(2025, 5, 3)}
    assert result["run"]["processed_days"] == 2
    store.close()


def test_multiday_advance_loads_actions_for_symbols_bought_during_catchup(
    tmp_path,
) -> None:
    captured_symbols: list[str] = []

    class BulkActionProvider(FakeProvider):
        def fetch_corporate_actions_for_period(
            self,
            symbols: list[str],
            start_date: date,
            end_date: date,
        ) -> pd.DataFrame:
            captured_symbols.extend(symbols)
            return pd.DataFrame(
                [
                    {
                        "ts_code": SYMBOLS[0],
                        "end_date": "20241231",
                        "record_date": "20251229",
                        "ex_date": pd.Timestamp("2025-12-30"),
                        "pay_date": "20251230",
                        "div_listdate": "20251230",
                        "stk_div": 0.10,
                        "cash_div": 0.20,
                    }
                ]
            )

    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account(
        "default",
        100_000,
        SYMBOLS,
        "v1.0-balanced",
        {
            "strategy_id": "moving_average",
            "universe_mode": "fixed",
            "backtest_start_date": "2024-01-01",
        },
    )
    account = store.account("default")
    assert account is not None
    account["last_date"] = "2025-12-26"
    account["pending_plan"] = [
        {
            "symbol": SYMBOLS[0],
            "name": "新买股票",
            "sector": "测试行业",
            "action": "BUY",
            "target_weight": 0.20,
            "reason": "补算首日买入",
        }
    ]
    store.save_account(account)

    result = advance_paper_simulation(
        PaperAdvanceRequest(
            account_id="default",
            symbols=SYMBOLS,
            as_of_date=date(2025, 12, 31),
        ),
        BulkActionProvider(),
        store,
    )

    assert captured_symbols == SYMBOLS
    assert result["run"]["processed_days"] == 3
    assert result["corporate_actions"]
    assert result["corporate_actions"][0]["symbol"] == SYMBOLS[0]
    assert result["corporate_actions"][0]["shares_added"] > 0
    store.close()


def test_board_helpers_only_main_and_etf_tradeable() -> None:
    from backend.models import (
        board_lot_size,
        board_of,
        can_buy_board,
        is_tradeable_board,
    )

    assert board_of("688008.SH") == "star"
    assert board_of("300308.SZ") == "chinext"
    assert board_of("920786.BJ") == "bse"
    assert board_of("600000.SH") == "main"
    assert board_of("159919.SZ") == "etf"
    assert board_lot_size("688008.SH") == 200
    assert board_lot_size("600000.SH") == 100
    # 无论资金多少，仅主板/ETF 可买
    assert is_tradeable_board("600000.SH") is True
    assert is_tradeable_board("159919.SZ") is True
    assert is_tradeable_board("688008.SH") is False
    assert is_tradeable_board("300308.SZ") is False
    assert is_tradeable_board("920786.BJ") is False
    assert can_buy_board("600000.SH", 100_000) is True
    assert can_buy_board("159919.SZ", 100_000) is True
    assert can_buy_board("688008.SH", 1_000_000) is False
    assert can_buy_board("300308.SZ", 1_000_000) is False
    assert can_buy_board("920786.BJ", 1_000_000) is False


def test_execute_rejects_star_buy_when_equity_below_threshold(tmp_path) -> None:
    symbol = "688008.SH"
    trade_date = pd.Timestamp("2025-12-31")
    frame = market_frame(0).copy()
    # Rename is not needed; reuse OHLCV shape under STAR code.
    store = PaperStore(tmp_path / "paper.txt")
    store.reset_account("default", 100_000, [symbol], "v1.0-balanced")
    account = store.account("default")
    assert account is not None
    account["pending_plan"] = [
        {
            "symbol": symbol,
            "name": "科创测试",
            "sector": "半导体",
            "action": "BUY",
            "target_weight": 0.20,
            "reason": "测试科创板门槛",
        }
    ]
    executions, outcomes = _execute_pending(
        account,
        {},
        trade_date,
        {symbol: frame},
        {symbol: {"name": "科创测试", "sector_name": "半导体"}},
        store,
        PaperCosts(),
    )
    assert executions == []
    assert outcomes[0]["constraint_reason"] == "board_eligibility"
    store.close()


def test_analyze_skips_non_main_boards() -> None:
    star = "688008.SH"
    chinext = "300308.SZ"
    main = SYMBOLS[0]
    frames = {
        main: market_frame(0),
        star: market_frame(1),
        chinext: market_frame(2),
        SYMBOLS[1]: market_frame(3),
        SYMBOLS[2]: market_frame(4),
        SYMBOLS[3]: market_frame(5),
    }
    industries = {
        main: {"name": "主板", "sector_name": "银行", "sector_code": "801780"},
        star: {"name": "科创", "sector_name": "半导体", "sector_code": "801080"},
        chinext: {"name": "创业", "sector_name": "电子", "sector_code": "801080"},
        SYMBOLS[1]: {"name": "A", "sector_name": "银行", "sector_code": "801780"},
        SYMBOLS[2]: {"name": "B", "sector_name": "银行", "sector_code": "801780"},
        SYMBOLS[3]: {"name": "C", "sector_name": "银行", "sector_code": "801780"},
    }
    result = _analyze(
        pd.Timestamp("2025-12-31"),
        frames,
        industries,
        VERSION_LIBRARY["v1.0-aggressive"],
        {},
        1_000_000,  # 资金再多也不买创业/科创
        "moving_average",
        {"technical_breadth": {"composite": 0.9, "coverage": 0.95}},
    )
    assert star not in result["selected_symbols"]
    assert chinext not in result["selected_symbols"]
    assert star not in result["target_weights"]
    assert chinext not in result["target_weights"]
    buy_symbols = {item["symbol"] for item in result["plan"] if item["action"] == "BUY"}
    assert star not in buy_symbols
    assert chinext not in buy_symbols
