from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from backend.data.providers import TushareDataProvider
from backend.matrix import _metrics
from backend.models import PaperAdvanceRequest, PaperSimulationRequest, is_etf
from backend.paper_store import PaperStore


logger = logging.getLogger("uvicorn.error")

STRATEGY_NAMES = {
    "moving_average": "双均线趋势",
    "momentum": "价格动量",
    "breakout": "通道突破",
}

VERSION_LIBRARY: dict[str, dict[str, Any]] = {
    "v1.0-balanced": {
        "name": "均衡型中短期趋势",
        "fast_window": 5,
        "slow_window": 20,
        "long_window": 60,
        "momentum_short": 20,
        "momentum_long": 60,
        "breakout_window": 20,
        "breadth_full": 0.50,
        "breadth_reduced": 0.35,
        "full_exposure": 0.90,
        "reduced_exposure": 0.45,
        "defensive_exposure": 0.15,
        "max_positions": 5,
        "max_per_sector": 2,
        "max_position_weight": 0.22,
        "stop_loss": 0.08,
        "minimum_amount": 20_000_000,
        "minimum_score": 0.52,
    },
    "v1.1-defensive": {
        "name": "防守型趋势",
        "fast_window": 10,
        "slow_window": 30,
        "long_window": 90,
        "momentum_short": 30,
        "momentum_long": 90,
        "breakout_window": 30,
        "breadth_full": 0.58,
        "breadth_reduced": 0.42,
        "full_exposure": 0.78,
        "reduced_exposure": 0.35,
        "defensive_exposure": 0.10,
        "max_positions": 4,
        "max_per_sector": 2,
        "max_position_weight": 0.20,
        "stop_loss": 0.06,
        "minimum_amount": 30_000_000,
        "minimum_score": 0.58,
    },
    "v1.2-responsive": {
        "name": "灵敏型趋势",
        "fast_window": 3,
        "slow_window": 15,
        "long_window": 45,
        "momentum_short": 15,
        "momentum_long": 45,
        "breakout_window": 15,
        "breadth_full": 0.46,
        "breadth_reduced": 0.32,
        "full_exposure": 0.88,
        "reduced_exposure": 0.45,
        "defensive_exposure": 0.15,
        "max_positions": 5,
        "max_per_sector": 2,
        "max_position_weight": 0.20,
        "stop_loss": 0.07,
        "minimum_amount": 20_000_000,
        "minimum_score": 0.50,
    },
}


@dataclass(frozen=True)
class PaperCosts:
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_rate: float = 0.0005
    slippage_bps: float = 2.0


def _load_frames(
    provider: TushareDataProvider,
    symbols: list[str],
    start_date: date,
    end_date: date,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, str]]]:
    frames: dict[str, pd.DataFrame] = {}
    errors: list[dict[str, str]] = []
    for index, symbol in enumerate(symbols, start=1):
        logger.info("Paper trading data %s/%s: %s", index, len(symbols), symbol)
        try:
            frame = provider.fetch_daily(symbol, start_date, end_date).frame
            frames[symbol] = frame.sort_values("trade_date").reset_index(drop=True)
        except Exception as exc:
            logger.exception("Paper trading data failed for %s", symbol)
            errors.append({"symbol": symbol, "message": str(exc)})
    if len(frames) < 5:
        raise ValueError("有效行情少于5个标的，无法构建分散组合")
    return frames, errors


def _calendar(frames: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    counts: dict[pd.Timestamp, int] = {}
    for frame in frames.values():
        for value in _date_values(frame):
            timestamp = pd.Timestamp(value)
            counts[timestamp] = counts.get(timestamp, 0) + 1
    minimum = max(3, math.ceil(len(frames) * 0.5))
    return sorted(day for day, count in counts.items() if count >= minimum)


def _date_values(frame: pd.DataFrame) -> np.ndarray:
    cached = frame.attrs.get("paper_trade_dates")
    if cached is None:
        cached = (
            pd.to_datetime(frame["trade_date"])
            .dt.normalize()
            .to_numpy(dtype="datetime64[ns]")
        )
        frame.attrs["paper_trade_dates"] = cached
    return cached


def _row_on(
    frame: pd.DataFrame,
    trade_date: pd.Timestamp,
) -> pd.Series | None:
    dates = _date_values(frame)
    target = np.datetime64(trade_date.normalize(), "ns")
    index = int(np.searchsorted(dates, target, side="right")) - 1
    if index < 0 or dates[index] != target:
        return None
    return frame.iloc[index]


def _last_row_on_or_before(
    frame: pd.DataFrame,
    trade_date: pd.Timestamp,
) -> pd.Series | None:
    dates = _date_values(frame)
    target = np.datetime64(trade_date.normalize(), "ns")
    index = int(np.searchsorted(dates, target, side="right")) - 1
    return None if index < 0 else frame.iloc[index]


def _feature_row(
    frame: pd.DataFrame,
    trade_date: pd.Timestamp,
    params: dict[str, Any],
) -> dict[str, float] | None:
    dates = _date_values(frame)
    target = np.datetime64(trade_date.normalize(), "ns")
    index = int(np.searchsorted(dates, target, side="right")) - 1
    if index < 0 or dates[index] != target:
        return None
    history = frame.iloc[: index + 1]
    maximum_window = max(
        params["long_window"],
        params["momentum_long"],
        params["breakout_window"],
    )
    if len(history) <= maximum_window:
        return None
    close = history["adj_close"].astype(float)
    last = history.iloc[-1]
    fast_ma = float(close.tail(params["fast_window"]).mean())
    slow_ma = float(close.tail(params["slow_window"]).mean())
    long_ma = float(close.tail(params["long_window"]).mean())
    momentum_short = float(close.iloc[-1] / close.iloc[-1 - params["momentum_short"]] - 1)
    momentum_long = float(close.iloc[-1] / close.iloc[-1 - params["momentum_long"]] - 1)
    breakout_high = float(
        history["adj_high"].astype(float).iloc[-1 - params["breakout_window"] : -1].max()
    )
    exit_low = float(history["adj_low"].astype(float).iloc[-11:-1].min())
    returns = close.pct_change().tail(20).dropna()
    volatility = float(returns.std(ddof=0) * math.sqrt(252))
    average_amount = float(history["amount"].astype(float).tail(20).mean())
    average_volume = float(history["volume"].astype(float).tail(20).mean())
    volume_ratio = (
        float(last["volume"]) / average_volume if average_volume > 0 else 0.0
    )
    return {
        "raw_close": float(last["close"]),
        "adj_close": float(last["adj_close"]),
        "fast_ma": fast_ma,
        "slow_ma": slow_ma,
        "long_ma": long_ma,
        "trend20": float(close.iloc[-1] / slow_ma - 1),
        "momentum_short": momentum_short,
        "momentum_long": momentum_long,
        "breakout_ratio": float(close.iloc[-1] / breakout_high),
        "breakout_high": breakout_high,
        "exit_low": exit_low,
        "volatility": max(volatility, 0.05),
        "average_amount": average_amount,
        "volume_ratio": volume_ratio,
    }


def _rank(series: pd.Series) -> pd.Series:
    if len(series) <= 1:
        return pd.Series(0.5, index=series.index)
    return series.rank(method="average", pct=True)


def _analyze(
    trade_date: pd.Timestamp,
    frames: dict[str, pd.DataFrame],
    industries: dict[str, dict[str, str]],
    params: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    equity: float,
    strategy_id: str,
) -> dict[str, Any]:
    feature_rows: list[dict[str, Any]] = []
    for symbol, frame in frames.items():
        feature = _feature_row(frame, trade_date, params)
        if feature is None:
            continue
        industry = industries.get(
            symbol,
            {"sector_name": "未分类", "sector_code": "UNKNOWN", "name": symbol},
        )
        feature_rows.append(
            {
                "symbol": symbol,
                "name": industry["name"],
                "sector": industry["sector_name"],
                "sector_code": industry["sector_code"],
                **feature,
            }
        )

    classified = sum(
        1
        for symbol in frames
        if industries.get(symbol, {}).get("sector_code") not in (None, "UNKNOWN")
    )
    market_coverage = len(feature_rows) / max(len(frames), 1)
    classification_coverage = classified / max(len(frames), 1)
    data_quality = 0.75 * market_coverage + 0.25 * classification_coverage
    if not feature_rows:
        return {
            "ready": False,
            "target_weights": {},
            "plan": [],
            "selected_symbols": [],
            "top_sectors": [],
            "breadth": 0.0,
            "market_regime": "数据不足",
            "data_quality": data_quality,
            "features": {},
        }

    table = pd.DataFrame(feature_rows).set_index("symbol")
    liquidity_rank = _rank(np.log1p(table["average_amount"]))
    volume_rank = _rank(table["volume_ratio"].clip(upper=3))
    if strategy_id == "moving_average":
        table["score"] = (
            _rank(table["trend20"]) * 0.35
            + _rank(table["fast_ma"] / table["slow_ma"] - 1) * 0.25
            + _rank(table["momentum_short"]) * 0.15
            + _rank(table["momentum_long"]) * 0.10
            + liquidity_rank * 0.10
            + volume_rank * 0.05
        )
        strategy_eligible = (
            (table["adj_close"] > table["slow_ma"])
            & (table["fast_ma"] > table["slow_ma"])
        )
    elif strategy_id == "momentum":
        table["score"] = (
            _rank(table["momentum_short"]) * 0.45
            + _rank(table["momentum_long"]) * 0.30
            + _rank(table["trend20"]) * 0.10
            + liquidity_rank * 0.10
            + volume_rank * 0.05
        )
        strategy_eligible = (
            (table["momentum_short"] > 0)
            & (table["momentum_long"] > 0)
            & (table["adj_close"] > table["fast_ma"])
        )
    elif strategy_id == "breakout":
        table["score"] = (
            _rank(table["breakout_ratio"]) * 0.50
            + volume_rank * 0.15
            + _rank(table["momentum_short"]) * 0.15
            + _rank(table["trend20"]) * 0.10
            + liquidity_rank * 0.10
        )
        held = pd.Series(table.index.isin(positions), index=table.index)
        strategy_eligible = (
            (table["adj_close"] > table["breakout_high"])
            | (held & (table["adj_close"] >= table["exit_low"]))
        )
    else:
        raise ValueError(f"未知模拟策略：{strategy_id}")

    table["eligible"] = (
        strategy_eligible
        & (table["average_amount"] >= params["minimum_amount"])
        & (table["score"] >= params["minimum_score"])
        & ~table["name"].str.upper().str.contains("ST", regex=False)
    )

    breadth = float((table["adj_close"] > table["slow_ma"]).mean())
    median_momentum = float(table["momentum_short"].median())
    if breadth >= params["breadth_full"] and median_momentum > -0.01:
        market_regime = "进攻"
        exposure = params["full_exposure"]
    elif breadth >= params["breadth_reduced"] and median_momentum > -0.04:
        market_regime = "谨慎"
        exposure = params["reduced_exposure"]
    else:
        market_regime = "防守"
        exposure = params["defensive_exposure"]

    sector_table = (
        table.groupby("sector")
        .agg(
            sector_score=("score", "mean"),
            sector_breadth=("eligible", "mean"),
            members=("score", "size"),
        )
        .sort_values(["sector_score", "sector_breadth"], ascending=False)
    )
    top_sectors = [
        {
            "name": sector,
            "score": round(float(row["sector_score"]), 4),
            "breadth": round(float(row["sector_breadth"]), 4),
            "members": int(row["members"]),
        }
        for sector, row in sector_table.head(3).iterrows()
    ]
    allowed_sectors = {item["name"] for item in top_sectors}

    eligible = table[
        table["eligible"] & table["sector"].isin(allowed_sectors)
    ].sort_values("score", ascending=False)
    selected: list[str] = []
    sector_counts: dict[str, int] = {}
    for symbol, row in eligible.iterrows():
        sector = str(row["sector"])
        if sector_counts.get(sector, 0) >= params["max_per_sector"]:
            continue
        selected.append(str(symbol))
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= params["max_positions"]:
            break

    target_weights: dict[str, float] = {}
    if exposure > 0 and selected:
        inverse_volatility = pd.Series(
            {symbol: 1 / float(table.loc[symbol, "volatility"]) for symbol in selected}
        )
        raw_weights = inverse_volatility / inverse_volatility.sum() * exposure
        target_weights = {
            symbol: float(min(weight, params["max_position_weight"]))
            for symbol, weight in raw_weights.items()
        }

    current_weights: dict[str, float] = {}
    for symbol, position in positions.items():
        if symbol in table.index and equity > 0:
            current_weights[symbol] = (
                position["shares"] * float(table.loc[symbol, "raw_close"]) / equity
            )

    plan: list[dict[str, Any]] = []
    held_symbols = set(positions)
    all_symbols = held_symbols | set(target_weights)
    for symbol in sorted(all_symbols):
        current_weight = current_weights.get(symbol, 0.0)
        target_weight = target_weights.get(symbol, 0.0)
        feature = table.loc[symbol] if symbol in table.index else None
        position = positions.get(symbol)
        hard_stop = False
        stop_reason = ""
        if position and feature is not None:
            position_return = float(feature["raw_close"]) / position["avg_price"] - 1
            if position_return <= -params["stop_loss"]:
                hard_stop = True
                stop_reason = f"持仓亏损 {position_return:.1%} 触发止损"
            elif (
                strategy_id == "moving_average"
                and float(feature["fast_ma"]) <= float(feature["slow_ma"])
            ):
                hard_stop = True
                stop_reason = "快均线跌破慢均线"
            elif (
                strategy_id == "momentum"
                and (
                    float(feature["momentum_short"]) <= 0
                    or float(feature["adj_close"]) < float(feature["fast_ma"])
                )
            ):
                hard_stop = True
                stop_reason = "中短期动量转弱"
            elif (
                strategy_id == "breakout"
                and float(feature["adj_close"]) < float(feature["exit_low"])
            ):
                hard_stop = True
                stop_reason = "收盘跌破10日退出通道"
        if hard_stop:
            target_weight = 0.0
            action = "CLOSE"
            reason = stop_reason
        elif position and target_weight <= 0:
            action = "CLOSE"
            reason = "退出板块/个股优选名单"
        elif position and target_weight < current_weight * 0.80:
            action = "SELL"
            reason = "仍保留趋势，但仓位高于风险目标"
        elif target_weight > current_weight + 0.025:
            action = "BUY"
            if position:
                reason = "趋势保持且当前仓位低于目标，模拟加仓"
            else:
                score = float(feature["score"]) if feature is not None else 0.0
                reason = (
                    f"{STRATEGY_NAMES[strategy_id]}信号成立，"
                    f"进入强势板块且个股综合分 {score:.2f}"
                )
        else:
            continue

        source = feature if feature is not None else {}
        industry = industries.get(symbol, {})
        plan.append(
            {
                "symbol": symbol,
                "name": str(source.get("name") or industry.get("name") or symbol),
                "sector": str(
                    source.get("sector") or industry.get("sector_name") or "未分类"
                ),
                "action": action,
                "target_weight": round(target_weight, 6),
                "reason": reason,
                "signal_price": round(float(source.get("raw_close") or 0), 4),
                "score": round(float(source.get("score") or 0), 4),
            }
        )

    return {
        "ready": len(feature_rows) >= 5,
        "target_weights": target_weights,
        "plan": plan,
        "selected_symbols": selected,
        "top_sectors": top_sectors,
        "breadth": breadth,
        "market_regime": market_regime,
        "data_quality": data_quality,
        "features": table.to_dict(orient="index"),
    }


def _commission(gross: float, costs: PaperCosts) -> float:
    return max(costs.minimum_commission, gross * costs.commission_rate)


def _portfolio_value(
    trade_date: pd.Timestamp,
    cash: float,
    positions: dict[str, dict[str, Any]],
    frames: dict[str, pd.DataFrame],
    price_column: str,
) -> tuple[float, float, int]:
    market_value = 0.0
    missing = 0
    for symbol, position in positions.items():
        row = _last_row_on_or_before(frames[symbol], trade_date)
        if row is None:
            missing += 1
            continue
        market_value += position["shares"] * float(row[price_column])
    return cash + market_value, market_value, missing


def _execute_pending(
    account: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    trade_date: pd.Timestamp,
    frames: dict[str, pd.DataFrame],
    industries: dict[str, dict[str, str]],
    store: PaperStore,
    costs: PaperCosts,
) -> list[dict[str, Any]]:
    plan = account.get("pending_plan", [])
    if not plan:
        return []

    equity_open, _, _ = _portfolio_value(
        trade_date,
        account["cash"],
        positions,
        frames,
        "open",
    )
    executions: list[dict[str, Any]] = []
    ordered_plan = sorted(
        plan,
        key=lambda item: 0 if item["action"] in ("CLOSE", "SELL") else 1,
    )
    slippage_rate = costs.slippage_bps / 10_000

    for item in ordered_plan:
        symbol = item["symbol"]
        if symbol not in frames:
            continue
        row = _row_on(frames[symbol], trade_date)
        if row is None:
            continue
        raw_open = float(row["open"])
        current = positions.get(symbol)
        current_shares = int(current["shares"]) if current else 0
        target_shares = (
            int(equity_open * item["target_weight"] / raw_open / 100) * 100
            if item["target_weight"] > 0
            else 0
        )

        if item["action"] in ("CLOSE", "SELL"):
            quantity = (
                current_shares
                if item["action"] == "CLOSE"
                else max(current_shares - target_shares, 0)
            )
            quantity = min(quantity, current_shares)
            if quantity < 100:
                continue
            execution_price = raw_open * (1 - slippage_rate)
            gross = quantity * execution_price
            commission = _commission(gross, costs)
            tax = 0.0 if is_etf(symbol) else gross * costs.stamp_tax_rate
            slippage = quantity * raw_open * slippage_rate
            account["cash"] += gross - commission - tax
            remaining = current_shares - quantity
            if remaining <= 0:
                positions.pop(symbol, None)
            else:
                current["shares"] = remaining
            action = "CLOSE" if remaining <= 0 else "SELL"
        else:
            quantity = max(target_shares - current_shares, 0)
            quantity = int(quantity / 100) * 100
            execution_price = raw_open * (1 + slippage_rate)
            while quantity >= 100:
                gross = quantity * execution_price
                commission = _commission(gross, costs)
                if gross + commission <= account["cash"]:
                    break
                quantity -= 100
            if quantity < 100:
                continue
            gross = quantity * execution_price
            commission = _commission(gross, costs)
            tax = 0.0
            slippage = quantity * raw_open * slippage_rate
            account["cash"] -= gross + commission
            industry = industries.get(symbol, {})
            if current:
                total_shares = current_shares + quantity
                current["avg_price"] = (
                    current["avg_price"] * current_shares + gross
                ) / total_shares
                current["shares"] = total_shares
            else:
                positions[symbol] = {
                    "name": item.get("name") or industry.get("name") or symbol,
                    "sector": item.get("sector")
                    or industry.get("sector_name")
                    or "未分类",
                    "shares": quantity,
                    "avg_price": execution_price,
                    "entry_date": trade_date.date().isoformat(),
                }
            action = "BUY"

        execution = {
            "trade_date": trade_date.date().isoformat(),
            "symbol": symbol,
            "name": item.get("name") or symbol,
            "sector": item.get("sector") or "未分类",
            "action": action,
            "quantity": quantity,
            "price": round(execution_price, 4),
            "gross": round(gross, 2),
            "commission": round(commission, 2),
            "tax": round(tax, 2),
            "slippage": round(slippage, 2),
            "reason": item["reason"],
            "strategy_version": item.get(
                "strategy_version",
                account["current_version"],
            ),
            "signal_price": item.get("signal_price", 0),
        }
        store.add_execution(account["account_id"], execution)
        executions.append(execution)
    return executions


def _reviews_for_day(
    trade_date: pd.Timestamp,
    snapshot: dict[str, Any],
    previous_breadth: float | None,
    executions: list[dict[str, Any]],
    failed_symbols: int,
    total_symbols: int,
) -> list[dict[str, str]]:
    reviews: list[dict[str, str]] = []
    day = trade_date.date().isoformat()
    if snapshot["data_quality"] < 0.90 or failed_symbols:
        reviews.append(
            {
                "trade_date": day,
                "category": "DATA_INCOMPLETE",
                "severity": "warning",
                "diagnosis": "信息不完整或分类覆盖不足",
                "evidence": (
                    f"数据质量 {snapshot['data_quality']:.1%}，"
                    f"失败标的 {failed_symbols}/{total_symbols}"
                ),
                "recommendation": "暂缓自动升级；检查停牌、复权、行业分类和接口返回。",
            }
        )

    large_gaps = [
        execution
        for execution in executions
        if execution.get("signal_price")
        and abs(execution["price"] / execution["signal_price"] - 1) > 0.05
    ]
    if large_gaps:
        reviews.append(
            {
                "trade_date": day,
                "category": "EXECUTION_SURPRISE",
                "severity": "warning",
                "diagnosis": "隔夜跳空使模拟成交偏离信号价格",
                "evidence": "、".join(
                    f"{item['symbol']} 偏离 "
                    f"{item['price'] / item['signal_price'] - 1:.1%}"
                    for item in large_gaps[:3]
                ),
                "recommendation": "检查公告与涨跌停；后续可增加开盘偏离上限。",
            }
        )

    if snapshot["daily_return"] <= -0.03 and snapshot["data_quality"] >= 0.90:
        reviews.append(
            {
                "trade_date": day,
                "category": "STRATEGY_MISS",
                "severity": "high",
                "diagnosis": "数据完整但组合单日损失超出预期",
                "evidence": (
                    f"组合日收益 {snapshot['daily_return']:.2%}，"
                    f"当日市场状态 {snapshot['market_regime']}"
                ),
                "recommendation": "交给防守型挑战者复测，不直接修改冠军策略。",
            }
        )

    breadth_change = (
        snapshot["breadth"] - previous_breadth
        if previous_breadth is not None
        else 0.0
    )
    if snapshot["drawdown"] <= -0.12 or breadth_change <= -0.20:
        reviews.append(
            {
                "trade_date": day,
                "category": "REGIME_SHIFT",
                "severity": "high",
                "diagnosis": "市场环境可能发生切换",
                "evidence": (
                    f"账户回撤 {snapshot['drawdown']:.2%}，"
                    f"市场宽度单日变化 {breadth_change:.1%}"
                ),
                "recommendation": "降低总暴露并比较防守型版本的样本外表现。",
            }
        )
    return reviews


def _build_daily_journal(
    trade_date: pd.Timestamp,
    strategy_id: str,
    due_plan: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    snapshot: dict[str, Any],
    analysis: dict[str, Any],
    next_plan: list[dict[str, Any]],
    positions: dict[str, dict[str, Any]],
    failed_symbols: int,
) -> dict[str, Any]:
    scheduled_symbols = {item["symbol"] for item in due_plan}
    executed_symbols = {item["symbol"] for item in executions}
    unfilled_symbols = sorted(scheduled_symbols - executed_symbols)
    large_gaps = [
        {
            "symbol": item["symbol"],
            "gap": round(item["price"] / item["signal_price"] - 1, 6),
        }
        for item in executions
        if item.get("signal_price")
        and abs(item["price"] / item["signal_price"] - 1) > 0.05
    ]

    if failed_symbols or snapshot["data_quality"] < 0.90:
        category = "DATA"
        conclusion = "当日信息不完整，决策可信度下降。"
        evidence = [
            f"数据质量 {snapshot['data_quality']:.1%}",
            f"异常或缺失标的 {failed_symbols} 个",
        ]
        next_focus = "补齐行情、复权和行业分类后再判断策略是否失效。"
    elif unfilled_symbols or large_gaps:
        category = "EXECUTION"
        conclusion = "计划与实际成交存在偏差，需要检查执行条件。"
        evidence = [
            f"未成交标的：{'、'.join(unfilled_symbols) or '无'}",
            f"大幅跳空标的：{'、'.join(item['symbol'] for item in large_gaps) or '无'}",
        ]
        next_focus = "检查停牌、涨跌停、开盘跳空和资金约束。"
    elif snapshot["daily_return"] <= -0.03:
        category = "STRATEGY"
        conclusion = "数据和执行正常，但当日结果明显低于预期。"
        evidence = [
            f"组合日收益 {snapshot['daily_return']:.2%}",
            f"市场状态 {snapshot['market_regime']}",
        ]
        next_focus = "观察信号是否连续失效，并交给挑战者做样本外比较。"
    elif snapshot["market_regime"] == "防守":
        category = "MARKET"
        conclusion = "市场环境偏弱，保持低仓位或空仓属于主动决策。"
        evidence = [
            f"趋势宽度 {snapshot['breadth']:.1%}",
            f"账户回撤 {snapshot['drawdown']:.1%}",
        ]
        next_focus = "等待趋势宽度恢复，不因无成交而放宽入场条件。"
    else:
        category = "NORMAL"
        conclusion = "当日数据、执行和策略表现未发现显著异常。"
        evidence = [
            f"组合日收益 {snapshot['daily_return']:.2%}",
            f"执行 {len(executions)}/{len(due_plan)} 条到期计划",
        ]
        next_focus = "继续观察市场宽度、持仓趋势和次日开盘偏差。"

    if next_plan:
        decision_summary = "；".join(
            f"{item['action']} {item['symbol']}：{item['reason']}"
            for item in next_plan
        )
    elif positions:
        decision_summary = (
            "无操作：防守状态下现有低仓位与风险目标接近，继续持有。"
            if snapshot["market_regime"] == "防守"
            else "无操作：现有持仓与目标仓位接近，继续持有。"
        )
    elif snapshot["market_regime"] == "防守":
        decision_summary = "无操作：防守状态下没有标的满足策略入场条件。"
    else:
        decision_summary = "无操作：当日没有候选标的同时满足策略和风控条件。"

    return {
        "trade_date": trade_date.date().isoformat(),
        "strategy_id": strategy_id,
        "strategy_name": STRATEGY_NAMES[strategy_id],
        "review": {
            "scheduled_count": len(due_plan),
            "executed_count": len(executions),
            "scheduled_actions": [
                {
                    "symbol": item["symbol"],
                    "action": item["action"],
                    "reason": item["reason"],
                }
                for item in due_plan
            ],
            "executions": [
                {
                    "symbol": item["symbol"],
                    "action": item["action"],
                    "price": item["price"],
                    "quantity": item["quantity"],
                    "reason": item["reason"],
                }
                for item in executions
            ],
            "unfilled_symbols": unfilled_symbols,
            "daily_return": snapshot["daily_return"],
            "drawdown": snapshot["drawdown"],
        },
        "analysis": {
            "market_regime": snapshot["market_regime"],
            "breadth": snapshot["breadth"],
            "data_quality": snapshot["data_quality"],
            "top_sectors": analysis["top_sectors"],
            "selected_symbols": analysis["selected_symbols"],
            "position_count": len(positions),
            "equity": snapshot["equity"],
            "cash": snapshot["cash"],
        },
        "decision": {
            "action_count": len(next_plan),
            "actions": next_plan,
            "summary": decision_summary,
            "execution_timing": "当日收盘确认，下一交易日开盘模拟执行",
        },
        "reflection": {
            "category": category,
            "conclusion": conclusion,
            "evidence": evidence,
            "next_focus": next_focus,
        },
    }


def _process_dates(
    store: PaperStore,
    account: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    industries: dict[str, dict[str, str]],
    dates: list[pd.Timestamp],
    failed_symbols: int,
    strategy_id: str,
) -> int:
    positions = {
        item["symbol"]: {
            "name": item["name"],
            "sector": item["sector"],
            "shares": item["shares"],
            "avg_price": item["avg_price"],
            "entry_date": item["entry_date"],
        }
        for item in store.positions(account["account_id"])
    }
    costs = PaperCosts()
    dashboard = store.dashboard(account["account_id"])
    previous_equity = (
        float(dashboard["latest"]["equity"])
        if dashboard["latest"]
        else float(account["initial_cash"])
    )
    previous_breadth = (
        float(dashboard["latest"]["breadth"]) if dashboard["latest"] else None
    )
    processed = 0

    for trade_date in dates:
        params = VERSION_LIBRARY[account["current_version"]]
        due_plan = list(account.get("pending_plan", []))
        executions = _execute_pending(
            account,
            positions,
            trade_date,
            frames,
            industries,
            store,
            costs,
        )
        equity, market_value, missing_positions = _portfolio_value(
            trade_date,
            account["cash"],
            positions,
            frames,
            "close",
        )
        account["peak_equity"] = max(account["peak_equity"], equity)
        drawdown = equity / account["peak_equity"] - 1
        daily_return = equity / previous_equity - 1 if previous_equity > 0 else 0.0
        analysis = _analyze(
            trade_date,
            frames,
            industries,
            params,
            positions,
            equity,
            strategy_id,
        )
        plan = [
            {
                **item,
                "strategy_version": account["current_version"],
                "signal_date": trade_date.date().isoformat(),
            }
            for item in analysis["plan"]
        ]
        snapshot = {
            "trade_date": trade_date.date().isoformat(),
            "equity": round(equity, 2),
            "cash": round(account["cash"], 2),
            "market_value": round(market_value, 2),
            "daily_return": round(daily_return, 8),
            "drawdown": round(drawdown, 8),
            "breadth": round(analysis["breadth"], 6),
            "market_regime": analysis["market_regime"],
            "data_quality": round(analysis["data_quality"], 6),
            "top_sectors": analysis["top_sectors"],
            "selected_symbols": analysis["selected_symbols"],
            "strategy_version": account["current_version"],
        }
        store.add_snapshot(account["account_id"], snapshot)
        for review in _reviews_for_day(
            trade_date,
            snapshot,
            previous_breadth,
            executions,
            failed_symbols + missing_positions,
            len(account["universe"]),
        ):
            store.add_review(account["account_id"], review)
        store.add_daily_journal(
            account["account_id"],
            _build_daily_journal(
                trade_date,
                strategy_id,
                due_plan,
                executions,
                snapshot,
                analysis,
                plan,
                positions,
                failed_symbols + missing_positions,
            ),
        )

        account["last_date"] = trade_date.date().isoformat()
        account["pending_plan"] = plan
        store.save_positions(account["account_id"], positions)
        store.save_account(account)
        previous_equity = equity
        previous_breadth = analysis["breadth"]
        processed += 1
    return processed


def _evaluate_version(
    frames: dict[str, pd.DataFrame],
    industries: dict[str, dict[str, str]],
    dates: list[pd.Timestamp],
    params: dict[str, Any],
    strategy_id: str,
) -> dict[str, Any]:
    equity = 1.0
    equity_values: list[float] = []
    equity_dates: list[pd.Timestamp] = []
    weights: dict[str, float] = {}
    previous_closes: dict[str, float] = {}
    rebalances = 0
    total_turnover = 0.0
    cost_rate = 0.0006

    for trade_date in dates:
        current_closes: dict[str, float] = {}
        for symbol, frame in frames.items():
            row = _row_on(frame, trade_date)
            if row is not None:
                current_closes[symbol] = float(row["adj_close"])
        if previous_closes:
            portfolio_return = sum(
                weight
                * (
                    current_closes.get(symbol, previous_closes[symbol])
                    / previous_closes[symbol]
                    - 1
                )
                for symbol, weight in weights.items()
                if symbol in previous_closes
            )
            equity *= max(1 + portfolio_return, 0.01)

        shadow_positions = {
            symbol: {
                "shares": 0,
                "avg_price": current_closes.get(symbol, 1.0),
                "name": symbol,
                "sector": "",
                "entry_date": "",
            }
            for symbol in weights
        }
        analysis = _analyze(
            trade_date,
            frames,
            industries,
            params,
            shadow_positions,
            equity,
            strategy_id,
        )
        new_weights = analysis["target_weights"]
        turnover = sum(
            abs(new_weights.get(symbol, 0) - weights.get(symbol, 0))
            for symbol in set(weights) | set(new_weights)
        )
        if turnover > 0.05:
            rebalances += 1
        total_turnover += turnover
        equity *= max(1 - turnover * cost_rate, 0.99)
        weights = new_weights
        previous_closes = current_closes
        equity_values.append(equity)
        equity_dates.append(trade_date)

    series = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates))
    split = max(int(len(series) * 0.7), 1)
    full = _metrics(series)
    out_of_sample = _metrics(series.iloc[split:])
    return {
        "annualized_return": round(full["annualized_return"], 6),
        "max_drawdown": round(full["max_drawdown"], 6),
        "sharpe": round(full["sharpe"], 4),
        "calmar": round(full["calmar"], 4),
        "oos_annualized_return": round(out_of_sample["annualized_return"], 6),
        "oos_max_drawdown": round(out_of_sample["max_drawdown"], 6),
        "oos_sharpe": round(out_of_sample["sharpe"], 4),
        "oos_calmar": round(out_of_sample["calmar"], 4),
        "rebalances": rebalances,
        "turnover": round(total_turnover, 4),
    }


def _automatic_upgrade(
    store: PaperStore,
    account: dict[str, Any],
    frames: dict[str, pd.DataFrame],
    industries: dict[str, dict[str, str]],
    dates: list[pd.Timestamp],
    strategy_id: str,
) -> None:
    if len(dates) < 252:
        return
    evaluation_dates = dates[-min(len(dates), 756) :]
    metrics_by_version: dict[str, dict[str, Any]] = {}
    for version, params in VERSION_LIBRARY.items():
        metrics = _evaluate_version(
            frames,
            industries,
            evaluation_dates,
            params,
            strategy_id,
        )
        metrics_by_version[version] = metrics
        store.save_version(
            account["account_id"],
            version,
            "champion" if version == account["current_version"] else "challenger",
            params,
            metrics,
            "固定规则挑战者；仅在模拟样本外满足门槛时晋级。",
        )

    current = account["current_version"]
    champion = metrics_by_version[current]
    candidates = sorted(
        (
            (version, metrics)
            for version, metrics in metrics_by_version.items()
            if version != current
        ),
        key=lambda item: (
            item[1]["oos_sharpe"],
            item[1]["oos_calmar"],
        ),
        reverse=True,
    )
    target, candidate = candidates[0]
    sample_sufficient = len(frames) >= 20 and len(evaluation_dates) >= 504
    qualifies = (
        sample_sufficient
        and
        candidate["oos_sharpe"] >= champion["oos_sharpe"] + 0.15
        and candidate["oos_max_drawdown"]
        >= champion["oos_max_drawdown"] - 0.02
        and candidate["oos_annualized_return"] > 0
        and candidate["rebalances"] >= 10
    )
    if qualifies:
        decision = "PROMOTED"
        reason = (
            f"挑战者样本外夏普 {candidate['oos_sharpe']:.2f} 高于冠军 "
            f"{champion['oos_sharpe']:.2f}，且回撤未明显恶化。"
        )
        store.promote_version(account["account_id"], target)
        account["current_version"] = target
    else:
        decision = "REJECTED"
        if not sample_sufficient:
            reason = (
                f"只有 {len(frames)} 个标的、{len(evaluation_dates)} 个交易日；"
                "自动晋级至少需要20个标的和504个交易日，以降低样本偏差。"
            )
        else:
            reason = (
                f"最佳挑战者样本外夏普 {candidate['oos_sharpe']:.2f}，"
                f"未同时满足领先0.15、回撤和交易次数门槛。"
            )
    store.add_upgrade_event(
        account["account_id"],
        {
            "trade_date": account["last_date"],
            "from_version": current,
            "to_version": target,
            "decision": decision,
            "reason": reason,
            "metrics": {
                "champion": champion,
                "challenger": candidate,
            },
        },
    )
    if not sample_sufficient:
        store.add_review(
            account["account_id"],
            {
                "trade_date": account["last_date"],
                "category": "UNIVERSE_BIAS",
                "severity": "high",
                "diagnosis": "候选池或历史长度不足，升级结果可能过拟合",
                "evidence": (
                    f"当前仅 {len(frames)} 个标的、"
                    f"{len(evaluation_dates)} 个交易日，挑战者暂不晋级"
                ),
                "recommendation": (
                    "扩展到至少20只且包含退市/历史成分股，并积累至少两年数据。"
                ),
            },
        )
    if candidate["oos_annualized_return"] > 1.0:
        store.add_review(
            account["account_id"],
            {
                "trade_date": account["last_date"],
                "category": "UNEXPECTED_RESULT",
                "severity": "high",
                "diagnosis": "样本外收益高得异常，优先怀疑选样偏差而非策略突破",
                "evidence": (
                    f"{target} 样本外年化 "
                    f"{candidate['oos_annualized_return']:.1%}，"
                    f"候选池仅 {len(frames)} 个标的"
                ),
                "recommendation": (
                    "检查幸存者偏差、事后选股和行情复权，再做滚动样本外检验。"
                ),
            },
        )
    store.save_account(account)


def replay_paper_simulation(
    request: PaperSimulationRequest,
    provider: TushareDataProvider,
    store: PaperStore,
) -> dict[str, Any]:
    logger.info(
        (
            "Paper replay started: account=%s, symbols=%s, "
            "backtest=%s..%s, simulation=%s..%s"
        ),
        request.account_id,
        len(request.symbols),
        request.backtest_start_date,
        request.backtest_end_date,
        request.simulation_start_date,
        request.simulation_end_date,
    )
    frames, errors = _load_frames(
        provider,
        request.symbols,
        request.backtest_start_date,
        request.simulation_end_date,
    )
    industries = provider.fetch_industries(list(frames))
    configuration = {
        "strategy_id": request.strategy_id,
        "strategy_name": STRATEGY_NAMES[request.strategy_id],
        "frequency": "1d",
        "universe_mode": request.universe_mode,
        "backtest_start_date": request.backtest_start_date.isoformat(),
        "backtest_end_date": request.backtest_end_date.isoformat(),
        "simulation_start_date": request.simulation_start_date.isoformat(),
        "simulation_end_date": request.simulation_end_date.isoformat(),
    }
    store.reset_account(
        request.account_id,
        request.initial_cash,
        request.symbols,
        "v1.0-balanced",
        configuration,
    )
    store.save_version(
        request.account_id,
        "v1.0-balanced",
        "champion",
        VERSION_LIBRARY["v1.0-balanced"],
        {},
        "初始可解释基线版本。",
    )
    account = store.account(request.account_id)
    assert account is not None
    all_dates = _calendar(frames)
    backtest_dates = [
        day
        for day in all_dates
        if request.backtest_start_date
        <= day.date()
        <= request.backtest_end_date
    ]
    simulation_dates = [
        day
        for day in all_dates
        if request.simulation_start_date
        <= day.date()
        <= request.simulation_end_date
    ]
    if len(backtest_dates) < 120:
        raise ValueError("回测期有效交易日不足120天")
    if not simulation_dates:
        raise ValueError("模拟期没有可用交易日")

    # 回测期只评估版本，不创建持仓、成交或资金快照。
    account["last_date"] = backtest_dates[-1].date().isoformat()
    store.save_account(account)
    if request.universe_mode == "fixed":
        _automatic_upgrade(
            store,
            account,
            frames,
            industries,
            backtest_dates,
            request.strategy_id,
        )
    account = store.account(request.account_id)
    assert account is not None
    account["last_date"] = None
    account["pending_plan"] = []
    store.save_account(account)

    processed = _process_dates(
        store,
        account,
        frames,
        industries,
        simulation_dates,
        len(errors),
        request.strategy_id,
    )
    dashboard = store.dashboard(request.account_id)
    dashboard["run"] = {
        "mode": "replay",
        "processed_days": processed,
        "backtest_days": len(backtest_dates),
        "simulation_days": len(simulation_dates),
        "data_errors": errors,
        "message": (
            f"回测期评估 {len(backtest_dates)} 个交易日；"
            f"模拟期逐日推演 {processed} 个交易日，并生成 {processed} 份决策日志。"
        ),
    }
    logger.info("Paper replay completed: %s days", processed)
    return dashboard


def advance_paper_simulation(
    request: PaperAdvanceRequest,
    provider: TushareDataProvider,
    store: PaperStore,
) -> dict[str, Any]:
    account = store.account(request.account_id)
    if account is None or not account.get("last_date"):
        raise ValueError("模拟账户尚未初始化，请先运行历史回放")

    last_date = date.fromisoformat(account["last_date"])
    if request.as_of_date <= last_date:
        dashboard = store.dashboard(request.account_id)
        dashboard["run"] = {
            "mode": "advance",
            "processed_days": 0,
            "data_errors": [],
            "message": "没有发现新的交易日数据，账户未重复执行。",
        }
        return dashboard

    configured_start = account.get("configuration", {}).get("backtest_start_date")
    strategy_id = account.get("configuration", {}).get(
        "strategy_id",
        "moving_average",
    )
    history_start = (
        date.fromisoformat(configured_start)
        if configured_start
        else last_date - timedelta(days=180)
    )
    frames, errors = _load_frames(
        provider,
        request.symbols,
        history_start,
        request.as_of_date,
    )
    industries = provider.fetch_industries(list(frames))
    dates = [
        day
        for day in _calendar(frames)
        if day.date() > last_date and day.date() <= request.as_of_date
    ]
    processed = _process_dates(
        store,
        account,
        frames,
        industries,
        dates,
        len(errors),
        strategy_id,
    )
    account = store.account(request.account_id)
    assert account is not None
    if (
        dates
        and account.get("configuration", {}).get("universe_mode", "fixed")
        == "fixed"
    ):
        _automatic_upgrade(
            store,
            account,
            frames,
            industries,
            _calendar(frames),
            strategy_id,
        )
    dashboard = store.dashboard(request.account_id)
    dashboard["run"] = {
        "mode": "advance",
        "processed_days": processed,
        "data_errors": errors,
        "message": (
            f"已补算 {processed} 个新交易日。"
            if processed
            else "没有发现新的交易日数据，账户未重复执行。"
        ),
    }
    return dashboard
