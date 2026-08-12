from datetime import date

import pandas as pd

from backend.market_research import MarketUniverseConfig, score_market_snapshot


def _snapshot_rows() -> pd.DataFrame:
    """构造 24 只股票：3 个板块，其中趋势强票落在非领先板块。"""
    rows = []
    # 板块A/B 因涨幅和资金流成为因子领先；板块C 因子弱但均线趋势强
    for index in range(24):
        if index < 8:
            industry = "板块A"
            pct = 4.0
            amount = 80_000.0  # 千元
        elif index < 16:
            industry = "板块B"
            pct = 3.0
            amount = 70_000.0
        else:
            industry = "板块C"
            pct = -1.0
            amount = 60_000.0
        rows.append(
            {
                "ts_code": f"{index:06d}.SZ",
                "name": f"股票{index}",
                "trade_date": pd.Timestamp("2026-08-11"),
                "close": 10.0 + index * 0.1,
                "amount": amount,
                "list_date": "20100101",
                "pct_chg": pct,
                "industry": industry,
                "pe_ttm": 15.0 + index,
                "pb": 1.5,
                "turnover_rate_f": 3.0,
                "volume_ratio": 1.0,
                "net_mf_amount": 100.0 if industry != "板块C" else -50.0,
                "buy_lg_amount": 200.0,
                "sell_lg_amount": 100.0,
                "buy_elg_amount": 50.0,
                "sell_elg_amount": 20.0,
                "roe": 12.0,
            }
        )
    return pd.DataFrame(rows)


def test_trend_sleeve_pulls_names_outside_factor_leader_sectors() -> None:
    snapshot = _snapshot_rows()
    # 仅板块C 的 000020/000021 站上双均线且扩展更大
    technical_rows = []
    for index in range(24):
        close = 10.0 + index * 0.1
        if index in (20, 21):
            ma20, ma60 = close * 0.90, close * 0.85
        else:
            ma20, ma60 = close * 1.05, close * 1.10
        technical_rows.append(
            {
                "ts_code": f"{index:06d}.SZ",
                "close_qfq": close,
                "ma_qfq_20": ma20,
                "ma_qfq_60": ma60,
            }
        )
    technical = pd.DataFrame(technical_rows)
    config = MarketUniverseConfig(
        mode="full_market",
        detailed_candidate_count=10,
        top_sector_count=2,
        max_candidates_per_sector=6,
        minimum_daily_amount=50_000_000,
    )

    result = score_market_snapshot(
        snapshot,
        date(2026, 8, 11),
        config,
        technical=technical,
    )

    symbols = {item["symbol"] for item in result.summary["candidates"]}
    assert "000020.SZ" in symbols or "000021.SZ" in symbols
    assert result.summary["trend_sleeve_count"] >= 1
    assert result.summary["factor_sleeve_count"] >= 1
    assert result.summary["factor_coverage"]["technical"] > 0.9
    sleeves = {item["selection_sleeve"] for item in result.summary["candidates"]}
    assert "trend" in sleeves
