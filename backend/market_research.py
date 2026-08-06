from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from backend.data.providers import TushareDataProvider


DEFAULT_FACTOR_WEIGHTS = {
    "valuation": 0.20,
    "quality": 0.25,
    "turnover": 0.15,
    "fund_flow": 0.20,
    "liquidity": 0.10,
    "price_strength": 0.10,
}


def _column(table: pd.DataFrame, name: str) -> pd.Series:
    if name in table:
        return table[name]
    return pd.Series(np.nan, index=table.index, dtype=float)


@dataclass(frozen=True)
class MarketUniverseConfig:
    mode: str = "fixed"
    minimum_listing_days: int = 180
    minimum_daily_amount: float = 50_000_000
    detailed_candidate_count: int = 40
    top_sector_count: int = 8
    max_candidates_per_sector: int = 6
    always_include_symbols: list[str] = field(default_factory=list)
    factor_weights: dict[str, float] = field(
        default_factory=lambda: DEFAULT_FACTOR_WEIGHTS.copy()
    )


@dataclass(frozen=True)
class MarketResearchResult:
    summary: dict[str, Any]
    candidates: pd.DataFrame


def _neutral_rank(
    values: pd.Series,
    *,
    ascending: bool = True,
) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan)
    valid = numeric.notna()
    result = pd.Series(0.5, index=values.index, dtype=float)
    if valid.sum() == 1:
        result.loc[valid] = 0.5
    elif valid.sum() > 1:
        result.loc[valid] = numeric.loc[valid].rank(
            method="average",
            pct=True,
            ascending=ascending,
        )
    return result


def _low_positive_score(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    positive = numeric.where(numeric > 0)
    result = _neutral_rank(positive, ascending=False)
    result.loc[numeric.notna() & (numeric <= 0)] = 0.10
    return result


def _mean_available(table: pd.DataFrame, columns: list[str]) -> pd.Series:
    available = [column for column in columns if column in table]
    if not available:
        return pd.Series(0.5, index=table.index, dtype=float)
    return table[available].mean(axis=1, skipna=True).fillna(0.5)


def score_market_snapshot(
    snapshot: pd.DataFrame,
    trade_date: date,
    config: MarketUniverseConfig,
) -> MarketResearchResult:
    """Cross-sectionally score the complete current A-share snapshot.

    This stage intentionally uses only information available on or before the
    snapshot date. Multi-year price history is fetched later only for the
    shortlisted names.
    """
    table = snapshot.copy()
    required = {"ts_code", "name", "trade_date", "close", "amount"}
    missing = sorted(required.difference(table.columns))
    if missing:
        raise ValueError(f"全市场快照缺少字段：{', '.join(missing)}")

    table["name"] = table["name"].fillna(table["ts_code"]).astype(str)
    table["industry"] = table.get(
        "industry",
        pd.Series("未分类", index=table.index),
    ).fillna("未分类")
    table["amount_yuan"] = pd.to_numeric(table["amount"], errors="coerce") * 1_000
    list_dates = pd.to_datetime(
        _column(table, "list_date").astype(str),
        format="mixed",
        errors="coerce",
    ).astype("datetime64[ns]")
    normalized_trade_date = pd.Timestamp(trade_date).as_unit("ns")
    listing_days = (
        normalized_trade_date - list_dates
    ).dt.days.fillna(config.minimum_listing_days)
    table["listing_days"] = listing_days
    table["pct_chg"] = pd.to_numeric(_column(table, "pct_chg"), errors="coerce")

    base_filter = (
        ~table["name"].str.upper().str.contains("ST", regex=False)
        & (table["listing_days"] >= config.minimum_listing_days)
        & (table["amount_yuan"] >= config.minimum_daily_amount)
        & (pd.to_numeric(table["close"], errors="coerce") > 0)
    )
    eligible = table.loc[base_filter].copy()
    if len(eligible) < 20:
        raise ValueError(
            f"全市场基础过滤后仅 {len(eligible)} 只股票，无法形成可靠候选池"
        )

    valuation_parts = pd.DataFrame(
        {
            "pe": _low_positive_score(_column(eligible, "pe_ttm")),
            "pb": _low_positive_score(_column(eligible, "pb")),
        },
        index=eligible.index,
    )
    eligible["valuation_score"] = valuation_parts.mean(axis=1)

    quality_parts: dict[str, pd.Series] = {}
    for column in ("roe", "grossprofit_margin", "q_netprofit_margin", "q_salescash_to_or"):
        if column in eligible:
            quality_parts[column] = _neutral_rank(eligible[column])
    if "debt_to_assets" in eligible:
        quality_parts["debt_to_assets"] = _neutral_rank(
            eligible["debt_to_assets"],
            ascending=False,
        )
    quality_frame = pd.DataFrame(quality_parts, index=eligible.index)
    eligible["quality_score"] = (
        quality_frame.mean(axis=1, skipna=True).fillna(0.5)
        if not quality_frame.empty
        else 0.5
    )

    turnover = pd.to_numeric(
        (
            eligible["turnover_rate_f"]
            if "turnover_rate_f" in eligible
            else _column(eligible, "turnover_rate")
        ),
        errors="coerce",
    )
    volume_ratio = pd.to_numeric(_column(eligible, "volume_ratio"), errors="coerce")
    turnover_score = _neutral_rank(turnover.clip(upper=20))
    turnover_score.loc[turnover > 25] *= 0.55
    eligible["turnover_score"] = (
        turnover_score * 0.70
        + _neutral_rank(volume_ratio.clip(lower=0, upper=3)) * 0.30
    )

    daily_amount_wan = eligible["amount_yuan"] / 10_000
    net_flow = pd.to_numeric(_column(eligible, "net_mf_amount"), errors="coerce")
    large_flow = (
        pd.to_numeric(_column(eligible, "buy_lg_amount"), errors="coerce").fillna(0)
        + pd.to_numeric(_column(eligible, "buy_elg_amount"), errors="coerce").fillna(0)
        - pd.to_numeric(_column(eligible, "sell_lg_amount"), errors="coerce").fillna(0)
        - pd.to_numeric(_column(eligible, "sell_elg_amount"), errors="coerce").fillna(0)
    )
    eligible["net_flow_ratio"] = (net_flow / daily_amount_wan).clip(-0.30, 0.30)
    eligible["large_flow_ratio"] = (large_flow / daily_amount_wan).clip(-0.30, 0.30)
    eligible["fund_flow_score"] = (
        _neutral_rank(eligible["net_flow_ratio"]) * 0.60
        + _neutral_rank(eligible["large_flow_ratio"]) * 0.40
    )
    if net_flow.notna().sum() == 0:
        eligible["fund_flow_score"] = 0.5

    eligible["liquidity_score"] = _neutral_rank(np.log1p(eligible["amount_yuan"]))
    eligible["price_strength_score"] = _neutral_rank(
        eligible["pct_chg"].clip(-9.5, 9.5)
    )
    eligible.loc[eligible["pct_chg"] > 9.7, "price_strength_score"] *= 0.70

    configured_weights = {
        key: max(0.0, float(config.factor_weights.get(key, 0)))
        for key in DEFAULT_FACTOR_WEIGHTS
    }
    total_weight = sum(configured_weights.values())
    if total_weight <= 0:
        raise ValueError("market_universe.factor_weights 总和必须大于 0")
    weights = {key: value / total_weight for key, value in configured_weights.items()}
    eligible["factor_score"] = (
        eligible["valuation_score"] * weights["valuation"]
        + eligible["quality_score"] * weights["quality"]
        + eligible["turnover_score"] * weights["turnover"]
        + eligible["fund_flow_score"] * weights["fund_flow"]
        + eligible["liquidity_score"] * weights["liquidity"]
        + eligible["price_strength_score"] * weights["price_strength"]
    )

    sector_table = (
        eligible.groupby("industry", dropna=False)
        .agg(
            score=("factor_score", "mean"),
            breadth=("pct_chg", lambda values: float((values > 0).mean())),
            net_flow_ratio=("net_flow_ratio", "mean"),
            quality=("quality_score", "mean"),
            members=("ts_code", "size"),
        )
        .query("members >= 3")
        .sort_values(["score", "breadth"], ascending=False)
    )
    top_sector_names = list(sector_table.head(config.top_sector_count).index)
    sector_candidates = eligible[eligible["industry"].isin(top_sector_names)].sort_values(
        "factor_score",
        ascending=False,
    )

    selected_indices: list[int] = []
    sector_counts: dict[str, int] = {}
    for index, row in sector_candidates.iterrows():
        sector = str(row["industry"])
        if sector_counts.get(sector, 0) >= config.max_candidates_per_sector:
            continue
        selected_indices.append(index)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected_indices) >= config.detailed_candidate_count:
            break
    candidates = eligible.loc[selected_indices].copy()
    candidates = candidates.sort_values("factor_score", ascending=False)

    factor_columns = [
        "valuation_score",
        "quality_score",
        "turnover_score",
        "fund_flow_score",
        "liquidity_score",
        "price_strength_score",
    ]
    quality_source_fields = [
        column
        for column in (
            "roe",
            "grossprofit_margin",
            "q_netprofit_margin",
            "debt_to_assets",
            "q_salescash_to_or",
        )
        if column in eligible and eligible[column].notna().any()
    ]
    flow_coverage = float(net_flow.notna().mean())
    quality_coverage = (
        float(eligible[quality_source_fields].notna().any(axis=1).mean())
        if quality_source_fields
        else 0.0
    )

    top_sectors = [
        {
            "name": str(name),
            "score": round(float(row["score"]), 4),
            "breadth": round(float(row["breadth"]), 4),
            "net_flow_ratio": round(float(row["net_flow_ratio"]), 6)
            if pd.notna(row["net_flow_ratio"])
            else None,
            "quality": round(float(row["quality"]), 4),
            "members": int(row["members"]),
        }
        for name, row in sector_table.head(config.top_sector_count).iterrows()
    ]
    candidate_items = []
    for _, row in candidates.iterrows():
        candidate_items.append(
            {
                "symbol": str(row["ts_code"]),
                "name": str(row["name"]),
                "sector": str(row["industry"]),
                "factor_score": round(float(row["factor_score"]), 4),
                "pct_chg": round(float(row["pct_chg"]), 4)
                if pd.notna(row["pct_chg"])
                else None,
                "net_flow_ratio": round(float(row["net_flow_ratio"]), 6)
                if pd.notna(row["net_flow_ratio"])
                else None,
                "factors": {
                    column.removesuffix("_score"): round(float(row[column]), 4)
                    for column in factor_columns
                },
            }
        )

    summary = {
        "mode": "full_market",
        "trade_date": trade_date.isoformat(),
        "market_count": int(len(table)),
        "eligible_count": int(len(eligible)),
        "detailed_candidate_count": int(len(candidates)),
        "factor_weights": weights,
        "factor_coverage": {
            "valuation": round(
                float(
                    (
                        pd.to_numeric(_column(eligible, "pe_ttm"), errors="coerce").notna()
                        | pd.to_numeric(_column(eligible, "pb"), errors="coerce").notna()
                    ).mean()
                ),
                4,
            ),
            "quality": round(quality_coverage, 4),
            "turnover": round(float(turnover.notna().mean()), 4),
            "fund_flow": round(flow_coverage, 4),
        },
        "market_breadth": round(float((eligible["pct_chg"] > 0).mean()), 4),
        "top_sectors": top_sectors,
        "candidates": candidate_items,
        "warnings": [],
    }
    return MarketResearchResult(summary=summary, candidates=candidates)


def research_full_market(
    provider: TushareDataProvider,
    as_of_date: date,
    config: MarketUniverseConfig,
) -> MarketResearchResult:
    trade_date, snapshot, errors = provider.fetch_market_snapshot(as_of_date)
    result = score_market_snapshot(snapshot, trade_date, config)
    result.summary["warnings"].extend(errors)
    try:
        result.summary["technical_breadth"] = (
            provider.fetch_market_technical_breadth(trade_date)
        )
    except Exception as exc:
        result.summary["technical_breadth"] = None
        result.summary["warnings"].append(
            "全市场20/60日趋势宽度获取失败，市场状态将降级使用详细候选池："
            f"{type(exc).__name__}: {exc}"
        )
    return result
