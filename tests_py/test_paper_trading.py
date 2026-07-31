from datetime import date

import numpy as np
import pandas as pd

from backend.data.providers import MarketData
from backend.models import PaperAdvanceRequest, PaperSimulationRequest
from backend.paper_store import PaperStore
from backend.paper_trading import (
    PaperCosts,
    VERSION_LIBRARY,
    _allocate_capped_weights,
    _apply_corporate_actions,
    _analyze,
    _execute_pending,
    _evaluate_version,
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
    without_context = _evaluate_version(
        frames,
        industries,
        dates,
        VERSION_LIBRARY["v1.0-balanced"],
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
        VERSION_LIBRARY["v1.0-balanced"],
        "moving_average",
        defensive_context,
    )

    assert with_context["annualized_return"] < without_context["annualized_return"]


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
