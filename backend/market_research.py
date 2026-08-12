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
    technical: pd.DataFrame | None = None,
) -> MarketResearchResult:
    """Cross-sectionally score the complete current A-share snapshot.

    Stage 1 uses the full market cross-section (factors + optional whole-market
    technicals). Multi-year OHLCV history is fetched later only for the
    shortlisted names. Candidate selection uses two sleeves so hot-money factor
    leaders cannot monopolize the detailed pool:
    - factor sleeve: top sectors by multi-factor / hybrid score
    - trend sleeve: whole-market names above MA20 ranked by technical strength
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

    technical_coverage = 0.0
    if technical is not None and not technical.empty and "ts_code" in technical.columns:
        tech = technical[["ts_code"]].copy()
        for column in ("close_qfq", "ma_qfq_20", "ma_qfq_60"):
            tech[column] = pd.to_numeric(
                technical[column] if column in technical.columns else np.nan,
                errors="coerce",
            )
        tech["above_ma20"] = (
            tech["close_qfq"].notna()
            & tech["ma_qfq_20"].notna()
            & (tech["ma_qfq_20"] > 0)
            & (tech["close_qfq"] > tech["ma_qfq_20"])
        )
        tech["above_ma60"] = (
            tech["close_qfq"].notna()
            & tech["ma_qfq_60"].notna()
            & (tech["ma_qfq_60"] > 0)
            & (tech["close_qfq"] > tech["ma_qfq_60"])
        )
        tech["ma20_extension"] = tech["close_qfq"] / tech["ma_qfq_20"] - 1.0
        tech["ma60_extension"] = tech["close_qfq"] / tech["ma_qfq_60"] - 1.0
        tech = tech.drop_duplicates("ts_code", keep="last")
        eligible = eligible.merge(
            tech[
                [
                    "ts_code",
                    "above_ma20",
                    "above_ma60",
                    "ma20_extension",
                    "ma60_extension",
                ]
            ],
            on="ts_code",
            how="left",
        )
        eligible["above_ma20"] = eligible["above_ma20"].fillna(False).astype(bool)
        eligible["above_ma60"] = eligible["above_ma60"].fillna(False).astype(bool)
        technical_coverage = float(
            eligible["ma20_extension"].notna().mean()
        ) if "ma20_extension" in eligible else 0.0
        eligible["technical_score"] = (
            _neutral_rank(eligible["ma20_extension"].clip(-0.15, 0.30)) * 0.55
            + _neutral_rank(eligible["ma60_extension"].clip(-0.25, 0.45)) * 0.45
        )
        # 站上双均线的标的在趋势袖套里优先，但不直接替代因子分。
        eligible.loc[eligible["above_ma20"] & eligible["above_ma60"], "technical_score"] = (
            eligible.loc[
                eligible["above_ma20"] & eligible["above_ma60"],
                "technical_score",
            ]
            * 1.08
        ).clip(upper=1.0)
    else:
        eligible["above_ma20"] = False
        eligible["above_ma60"] = False
        eligible["technical_score"] = 0.5

    eligible["hybrid_score"] = (
        eligible["factor_score"] * 0.62 + eligible["technical_score"] * 0.38
    )

    sector_table = (
        eligible.groupby("industry", dropna=False)
        .agg(
            score=("hybrid_score", "mean"),
            breadth=("pct_chg", lambda values: float((values > 0).mean())),
            net_flow_ratio=("net_flow_ratio", "mean"),
            quality=("quality_score", "mean"),
            members=("ts_code", "size"),
        )
        .query("members >= 3")
        .sort_values(["score", "breadth"], ascending=False)
    )
    top_sector_names = list(sector_table.head(config.top_sector_count).index)

    total_slots = max(int(config.detailed_candidate_count), 1)
    # 因子袖约 60%，趋势袖约 40%；趋势袖从全市场站上 MA20 的股票里挑，
    # 不限制在领先板块，避免“只在已经热门的池子里打转”。
    factor_slots = max(int(round(total_slots * 0.60)), 1)
    trend_slots = max(total_slots - factor_slots, 0)

    def _pick(
        pool: pd.DataFrame,
        *,
        score_column: str,
        limit: int,
        sector_counts: dict[str, int],
        prefer_columns: list[str] | None = None,
    ) -> list[Any]:
        if limit <= 0 or pool.empty:
            return []
        sort_columns = list(prefer_columns or []) + [score_column]
        ascending = [False] * len(sort_columns)
        ordered = pool.sort_values(sort_columns, ascending=ascending)
        picked: list[Any] = []
        for index, row in ordered.iterrows():
            sector = str(row["industry"])
            if sector_counts.get(sector, 0) >= config.max_candidates_per_sector:
                continue
            picked.append(index)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if len(picked) >= limit:
                break
        return picked

    sector_counts: dict[str, int] = {}
    sector_pool = eligible[eligible["industry"].isin(top_sector_names)]
    factor_indices = _pick(
        sector_pool,
        score_column="hybrid_score",
        limit=factor_slots,
        sector_counts=sector_counts,
    )
    remaining = eligible.loc[~eligible.index.isin(factor_indices)]
    trend_pool = remaining.loc[remaining["above_ma20"]]
    if trend_pool.empty:
        trend_pool = remaining
    trend_indices = _pick(
        trend_pool,
        score_column="technical_score",
        limit=trend_slots,
        sector_counts=sector_counts,
        prefer_columns=["above_ma60", "above_ma20"],
    )
    # 若趋势袖不足，用全市场 hybrid 补齐，仍受板块上限约束。
    selected_indices = factor_indices + trend_indices
    if len(selected_indices) < total_slots:
        filler_pool = eligible.loc[~eligible.index.isin(selected_indices)]
        selected_indices.extend(
            _pick(
                filler_pool,
                score_column="hybrid_score",
                limit=total_slots - len(selected_indices),
                sector_counts=sector_counts,
            )
        )

    candidates = eligible.loc[selected_indices].copy()
    candidates = candidates.sort_values("hybrid_score", ascending=False)
    candidates["selection_sleeve"] = np.where(
        candidates.index.isin(trend_indices) & ~candidates.index.isin(factor_indices),
        "trend",
        "factor",
    )

    factor_columns = [
        "valuation_score",
        "quality_score",
        "turnover_score",
        "fund_flow_score",
        "liquidity_score",
        "price_strength_score",
        "technical_score",
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
                "hybrid_score": round(float(row["hybrid_score"]), 4),
                "technical_score": round(float(row["technical_score"]), 4),
                "selection_sleeve": str(row.get("selection_sleeve", "factor")),
                "above_ma20": bool(row.get("above_ma20", False)),
                "above_ma60": bool(row.get("above_ma60", False)),
                "pct_chg": round(float(row["pct_chg"]), 4)
                if pd.notna(row["pct_chg"])
                else None,
                "net_flow_ratio": round(float(row["net_flow_ratio"]), 6)
                if pd.notna(row["net_flow_ratio"])
                else None,
                "factors": {
                    column.removesuffix("_score"): round(float(row[column]), 4)
                    for column in factor_columns
                    if column in row.index and pd.notna(row[column])
                },
            }
        )

    summary = {
        "mode": "full_market",
        "trade_date": trade_date.isoformat(),
        "market_count": int(len(table)),
        "eligible_count": int(len(eligible)),
        "detailed_candidate_count": int(len(candidates)),
        "factor_sleeve_count": int(len(factor_indices)),
        "trend_sleeve_count": int(len(trend_indices)),
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
            "technical": round(technical_coverage, 4),
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
    technical_frame: pd.DataFrame | None = None
    technical_breadth: dict[str, Any] | None = None
    technical_errors: list[str] = []
    try:
        technical_frame = provider.fetch_market_technical_frame(trade_date)
        technical_breadth = provider._technical_breadth_from_frame(
            technical_frame,
            trade_date,
        )
    except Exception as exc:  # noqa: BLE001 - 技术面失败时仍可用因子袖套
        technical_errors.append(
            "全市场20/60日趋势截面获取失败，选股将仅使用多因子袖套，"
            f"市场状态降级使用详细候选池：{type(exc).__name__}: {exc}"
        )

    result = score_market_snapshot(
        snapshot,
        trade_date,
        config,
        technical=technical_frame,
    )
    result.summary["warnings"].extend(errors)
    result.summary["warnings"].extend(technical_errors)
    result.summary["technical_breadth"] = technical_breadth
    return result
