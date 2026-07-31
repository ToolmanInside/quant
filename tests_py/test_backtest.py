from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from backend.backtest import run_moving_average_backtest
from backend.models import BacktestRequest


def sample_market_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", "2025-01-01")
    index = np.arange(len(dates), dtype=float)
    close = 10 + index * 0.012 + np.sin(index / 11) * 0.9
    open_price = np.roll(close, 1)
    open_price[0] = close[0]
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": open_price,
            "high": np.maximum(open_price, close) * 1.01,
            "low": np.minimum(open_price, close) * 0.99,
            "close": close,
            "adj_close": close,
            "volume": 10_000_000,
            "amount": close * 10_000_000,
        }
    )


def test_backtest_executes_only_after_long_window() -> None:
    request = BacktestRequest(
        start_date=date(2024, 1, 1),
        end_date=date(2025, 1, 1),
        short_window=5,
        long_window=20,
    )
    frame = sample_market_frame()
    result = run_moving_average_backtest(frame, request, "test fixture")

    assert result["summary"]["final_equity"] > 0
    assert len(result["series"]) == len(frame)
    assert result["trades"]
    earliest_trade = result["trades"][0]["date"]
    minimum_date = frame.iloc[request.long_window]["trade_date"].strftime("%Y-%m-%d")
    assert earliest_trade >= minimum_date


def test_all_trades_use_board_lots() -> None:
    request = BacktestRequest()
    result = run_moving_average_backtest(
        sample_market_frame(),
        request,
        "test fixture",
    )
    assert all(trade["quantity"] % 100 == 0 for trade in result["trades"])


@pytest.mark.parametrize(
    ("raw_symbol", "normalized_symbol"),
    [
        ("002317", "002317.SZ"),
        ("600000", "600000.SH"),
        ("430047", "430047.BJ"),
        (" 002317.sz ", "002317.SZ"),
    ],
)
def test_symbol_is_normalized(
    raw_symbol: str,
    normalized_symbol: str,
) -> None:
    assert BacktestRequest(symbol=raw_symbol).symbol == normalized_symbol


def test_wrong_exchange_is_rejected() -> None:
    with pytest.raises(ValidationError, match="应使用后缀 .SZ"):
        BacktestRequest(symbol="002317.SH")
