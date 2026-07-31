from datetime import date

import numpy as np
import pandas as pd

from backend.data.providers import MarketData
from backend.matrix import _metrics, run_strategy_matrix
from backend.models import StrategyMatrixRequest


def trending_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2020-01-01", "2025-12-31")
    index = np.arange(len(dates), dtype=float)
    close = 10 + index * 0.01 + np.sin(index / 20) * 0.4
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
            "volume": 10_000_000,
            "amount": close * 10_000_000,
        }
    )


class FakeProvider:
    def fetch_daily(self, symbol: str, start_date: date, end_date: date) -> MarketData:
        return MarketData(trending_frame(), "fixture")


def test_matrix_deduplicates_and_normalizes_symbols() -> None:
    request = StrategyMatrixRequest(
        symbols=["159611", "002317", "600183", "600183"],
    )
    assert request.symbols == ["159611.SZ", "002317.SZ", "600183.SH"]


def test_sharpe_uses_actual_observation_spacing() -> None:
    dates = pd.date_range("2020-01-01", periods=60, freq="20D")
    values = 100_000 * np.cumprod(
        1 + np.tile(np.array([0.02, -0.01, 0.015, -0.005]), 15)
    )
    spaced = _metrics(pd.Series(values, index=dates))
    daily = _metrics(
        pd.Series(values, index=pd.date_range("2020-01-01", periods=60, freq="D"))
    )
    assert spaced["sharpe"] < daily["sharpe"]


def test_matrix_returns_ranked_available_cells() -> None:
    request = StrategyMatrixRequest(
        symbols=["002317", "600183"],
        start_date=date(2020, 1, 1),
        end_date=date(2025, 12, 31),
    )
    result = run_strategy_matrix(request, FakeProvider())

    assert result["best"] is not None
    assert len(result["ranking"]) == 12
    assert result["best"]["available"] is True
    hourly = [row for row in result["rows"] if row["frequency_id"] == "60min"]
    assert hourly and all(row["available"] is False for row in hourly)
