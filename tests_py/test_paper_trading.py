from datetime import date

import numpy as np
import pandas as pd

from backend.data.providers import MarketData
from backend.models import PaperAdvanceRequest, PaperSimulationRequest
from backend.paper_store import PaperStore
from backend.paper_trading import (
    VERSION_LIBRARY,
    _analyze,
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
