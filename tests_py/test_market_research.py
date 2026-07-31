from datetime import date
import json

import httpx
import pandas as pd

from backend.market_research import (
    MarketUniverseConfig,
    score_market_snapshot,
)
from backend.news_research import NewsResearchConfig, enrich_with_bocha_news


def _snapshot() -> pd.DataFrame:
    rows = []
    for index in range(60):
        rows.append(
            {
                "ts_code": f"{index:06d}.SZ",
                "trade_date": "20260731",
                "name": f"测试{index}",
                "industry": f"行业{index % 4}",
                "list_date": "20200101",
                "close": 10 + index / 10,
                "pct_chg": (index % 11) - 4,
                "amount": 100_000 + index * 1_000,
                "pe_ttm": 8 + index,
                "pb": 1 + index / 30,
                "turnover_rate_f": 1 + index / 10,
                "volume_ratio": 0.8 + index / 100,
                "net_mf_amount": -500 + index * 30,
                "buy_lg_amount": 1_000 + index * 20,
                "sell_lg_amount": 900,
                "buy_elg_amount": 500 + index * 10,
                "sell_elg_amount": 450,
                "roe": 5 + index / 2,
                "grossprofit_margin": 20 + index / 3,
                "q_netprofit_margin": 5 + index / 5,
                "debt_to_assets": 70 - index / 2,
                "q_salescash_to_or": 70 + index,
            }
        )
    return pd.DataFrame(rows)


def test_full_market_factor_screen_has_coverage_sectors_and_candidates() -> None:
    result = score_market_snapshot(
        _snapshot(),
        date(2026, 7, 31),
        MarketUniverseConfig(
            mode="full_market",
            minimum_daily_amount=50_000_000,
            detailed_candidate_count=12,
            top_sector_count=4,
            max_candidates_per_sector=4,
        ),
    )

    assert result.summary["market_count"] == 60
    assert result.summary["eligible_count"] == 60
    assert result.summary["detailed_candidate_count"] == 12
    assert len(result.summary["top_sectors"]) == 4
    assert result.summary["factor_coverage"]["quality"] == 1.0
    assert result.summary["factor_coverage"]["fund_flow"] == 1.0
    assert all(
        set(item["factors"])
        == {
            "valuation",
            "quality",
            "turnover",
            "fund_flow",
            "liquidity",
            "price_strength",
        }
        for item in result.summary["candidates"]
    )


def test_bocha_news_marks_severe_risk_and_keeps_source_links() -> None:
    result = score_market_snapshot(
        _snapshot(),
        date(2026, 7, 31),
        MarketUniverseConfig(
            mode="full_market",
            minimum_daily_amount=50_000_000,
            detailed_candidate_count=5,
            top_sector_count=1,
            max_candidates_per_sector=5,
        ),
    )
    risky_symbol = result.summary["candidates"][0]["symbol"]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if risky_symbol.split(".")[0] in body["query"]:
            title = "公司收到立案调查通知"
        else:
            title = "行业景气保持增长"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "webPages": {
                        "value": [
                            {
                                "name": title,
                                "url": "https://example.com/article",
                                "summary": "测试摘要",
                                "siteName": "测试来源",
                                "datePublished": "2026-07-31",
                            }
                        ]
                    }
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        enriched = enrich_with_bocha_news(
            result,
            "secret-used-only-in-header",
            NewsResearchConfig(
                enabled=True,
                max_sectors=1,
                max_stocks=1,
                results_per_query=1,
            ),
            client=client,
        )

    candidate = next(
        item
        for item in enriched.summary["candidates"]
        if item["symbol"] == risky_symbol
    )
    assert candidate["news_risk_level"] == "high"
    assert candidate["excluded_by_news"]
    assert (
        enriched.summary["news"]["stocks"][0]["items"][0]["url"]
        == "https://example.com/article"
    )
