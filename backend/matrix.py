from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from backend.data.providers import TushareDataProvider
from backend.models import StrategyMatrixRequest, is_etf


logger = logging.getLogger("uvicorn.error")

STRATEGIES = {
    "moving_average": "双均线趋势",
    "momentum": "价格动量",
    "breakout": "通道突破",
    "rsi_reversion": "RSI均值回归",
}

FREQUENCIES = [
    {"id": "60min", "name": "1小时", "bars": None},
    {"id": "120min", "name": "2小时", "bars": None},
    {"id": "180min", "name": "3小时", "bars": None},
    {"id": "1d", "name": "每日", "bars": 1},
    {"id": "5d", "name": "每5个交易日", "bars": 5},
    {"id": "20d", "name": "每20个交易日", "bars": 20},
]


@dataclass
class SimulationCosts:
    initial_cash: float
    commission_rate: float
    minimum_commission: float
    stamp_tax_rate: float
    slippage_bps: float


def _resample_bars(frame: pd.DataFrame, bar_size: int) -> pd.DataFrame:
    data = frame.sort_values("trade_date").reset_index(drop=True).copy()
    if bar_size == 1:
        return data

    data["_group"] = np.arange(len(data)) // bar_size
    aggregations = {
        "trade_date": "last",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "adj_open": "first",
        "adj_high": "max",
        "adj_low": "min",
        "adj_close": "last",
        "volume": "sum",
        "amount": "sum",
    }
    return data.groupby("_group", as_index=False).agg(aggregations)


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + relative_strength)


def _state_from_entries(
    entries: pd.Series,
    exits: pd.Series,
) -> pd.Series:
    state = False
    values: list[bool] = []
    for entry, exit_signal in zip(entries.fillna(False), exits.fillna(False)):
        if state and bool(exit_signal):
            state = False
        elif not state and bool(entry):
            state = True
        values.append(state)
    return pd.Series(values, index=entries.index, dtype=bool)


def _strategy_target(data: pd.DataFrame, strategy_id: str) -> pd.Series:
    close = data["adj_close"]
    if strategy_id == "moving_average":
        return close.rolling(5).mean() > close.rolling(20).mean()
    if strategy_id == "momentum":
        return (close.pct_change(20) > 0) & (close > close.rolling(10).mean())
    if strategy_id == "breakout":
        entry = close > data["adj_high"].shift(1).rolling(20).max()
        exit_signal = close < data["adj_low"].shift(1).rolling(10).min()
        return _state_from_entries(entry, exit_signal)
    if strategy_id == "rsi_reversion":
        rsi = _rsi(close)
        return _state_from_entries(rsi < 30, rsi > 55)
    raise ValueError(f"未知策略：{strategy_id}")


def _commission(gross: float, costs: SimulationCosts) -> float:
    return max(costs.minimum_commission, gross * costs.commission_rate)


def _metrics(equity: pd.Series) -> dict:
    if len(equity) < 3:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
        }

    equity = equity.astype(float)
    start_value = float(equity.iloc[0])
    end_value = float(equity.iloc[-1])
    elapsed_days = max((equity.index[-1] - equity.index[0]).days, 1)
    years = max(elapsed_days / 365.25, 1 / 252)
    total_return = end_value / start_value - 1
    annualized_return = (
        (end_value / start_value) ** (1 / years) - 1 if end_value > 0 else -1
    )
    daily_equity = equity.groupby(equity.index.normalize()).last()
    daily_returns = daily_equity.pct_change().dropna()
    standard_deviation = float(daily_returns.std(ddof=0))
    observed_dates = daily_equity.index.to_series().sort_values()
    median_spacing_days = float(
        observed_dates.diff().dropna().dt.total_seconds().median() / 86_400
    )
    periods_per_year = min(
        252.0,
        365.25 / max(median_spacing_days, 1.0),
    )
    sharpe = (
        float(daily_returns.mean() / standard_deviation * math.sqrt(periods_per_year))
        if standard_deviation > 0
        else 0.0
    )
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = (
        annualized_return / abs(max_drawdown)
        if max_drawdown < -1e-9
        else 0.0
    )
    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "calmar": calmar,
    }


def _simulate(
    frame: pd.DataFrame,
    strategy_id: str,
    costs: SimulationCosts,
    etf: bool,
) -> dict:
    data = frame.copy().sort_values("trade_date").reset_index(drop=True)
    if len(data) < 45:
        raise ValueError("有效K线不足45根")

    data["target"] = _strategy_target(data, strategy_id).shift(1).fillna(False)
    cash = costs.initial_cash
    shares = 0
    buy_date: pd.Timestamp | None = None
    trade_count = 0
    total_cost = 0.0
    equity_values: list[float] = []
    slippage = costs.slippage_bps / 10_000

    for row in data.itertuples(index=False):
        current_time = pd.Timestamp(row.trade_date)
        target_long = bool(row.target)
        open_price = float(row.adj_open)

        if target_long and shares == 0 and math.isfinite(open_price):
            execution_price = open_price * (1 + slippage)
            candidate = int((cash * 0.98) / execution_price / 100) * 100
            while candidate >= 100:
                gross = candidate * execution_price
                commission = _commission(gross, costs)
                if gross + commission <= cash:
                    break
                candidate -= 100
            if candidate >= 100:
                gross = candidate * execution_price
                commission = _commission(gross, costs)
                cash -= gross + commission
                shares = candidate
                buy_date = current_time.normalize()
                total_cost += commission + candidate * open_price * slippage
                trade_count += 1

        elif (
            not target_long
            and shares > 0
            and buy_date is not None
            and current_time.normalize() > buy_date
        ):
            execution_price = open_price * (1 - slippage)
            gross = shares * execution_price
            commission = _commission(gross, costs)
            tax = 0.0 if etf else gross * costs.stamp_tax_rate
            cash += gross - commission - tax
            total_cost += commission + tax + shares * open_price * slippage
            shares = 0
            buy_date = None
            trade_count += 1

        equity_values.append(cash + shares * float(row.adj_close))

    equity = pd.Series(equity_values, index=pd.DatetimeIndex(data["trade_date"]))
    split_index = max(int(len(equity) * 0.7), 1)
    out_of_sample = equity.iloc[split_index:]
    result = _metrics(equity)
    result["oos"] = _metrics(out_of_sample)
    result["trade_count"] = trade_count
    result["total_cost"] = total_cost
    result["cost_rate"] = total_cost / costs.initial_cash
    return result


def _percentile(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    ranked = values.rank(method="average", pct=True)
    return ranked if higher_is_better else 1 - ranked + (1 / max(len(values), 1))


def _score_rows(rows: list[dict]) -> None:
    available = [row for row in rows if row["available"]]
    if not available:
        return
    table = pd.DataFrame(available)
    components = {
        "oos_sharpe": (0.30, True),
        "oos_calmar": (0.15, True),
        "median_sharpe": (0.15, True),
        "median_annualized_return": (0.10, True),
        "median_max_drawdown": (0.10, True),
        "positive_rate": (0.10, True),
        "median_cost_rate": (0.05, False),
        "coverage": (0.05, True),
    }
    scores = pd.Series(0.0, index=table.index)
    for column, (weight, higher_is_better) in components.items():
        scores += _percentile(table[column], higher_is_better) * weight * 100

    low_trade_penalty = table["median_trade_count"].apply(
        lambda count: 0.8 if count < 4 else 1.0
    )
    scores *= low_trade_penalty
    for index, row in enumerate(available):
        row["score"] = round(float(scores.iloc[index]), 1)


def _aggregate_cell(
    strategy_id: str,
    frequency_id: str,
    frequency_name: str,
    symbol_results: list[dict],
    expected_symbols: int,
) -> dict:
    if not symbol_results:
        return {
            "strategy_id": strategy_id,
            "strategy_name": STRATEGIES[strategy_id],
            "frequency_id": frequency_id,
            "frequency_name": frequency_name,
            "available": False,
            "score": None,
            "coverage": 0.0,
            "reason": "没有足够数据",
        }

    frame = pd.DataFrame(symbol_results)
    return {
        "strategy_id": strategy_id,
        "strategy_name": STRATEGIES[strategy_id],
        "frequency_id": frequency_id,
        "frequency_name": frequency_name,
        "available": True,
        "score": None,
        "coverage": len(frame) / expected_symbols,
        "tested_symbols": len(frame),
        "median_annualized_return": float(frame["annualized_return"].median()),
        "median_max_drawdown": float(frame["max_drawdown"].median()),
        "median_sharpe": float(frame["sharpe"].median()),
        "median_calmar": float(frame["calmar"].median()),
        "oos_sharpe": float(frame["oos_sharpe"].median()),
        "oos_calmar": float(frame["oos_calmar"].median()),
        "positive_rate": float((frame["total_return"] > 0).mean()),
        "median_trade_count": float(frame["trade_count"].median()),
        "median_cost_rate": float(frame["cost_rate"].median()),
        "symbol_results": symbol_results,
    }


def run_strategy_matrix(
    request: StrategyMatrixRequest,
    provider: TushareDataProvider,
    progress: Callable[[str], None] | None = None,
) -> dict:
    notify = progress or (lambda message: None)
    costs = SimulationCosts(
        initial_cash=request.initial_cash,
        commission_rate=request.commission_rate,
        minimum_commission=request.minimum_commission,
        stamp_tax_rate=request.stamp_tax_rate,
        slippage_bps=request.slippage_bps,
    )
    daily_frames: dict[str, pd.DataFrame] = {}
    errors: list[dict] = []

    for index, symbol in enumerate(request.symbols, start=1):
        notify(f"正在获取日线 {index}/{len(request.symbols)}：{symbol}")
        logger.info(
            "Strategy matrix daily data %s/%s: %s",
            index,
            len(request.symbols),
            symbol,
        )
        try:
            daily_frames[symbol] = provider.fetch_daily(
                symbol,
                request.start_date,
                request.end_date,
            ).frame
        except Exception as exc:
            logger.exception("Strategy matrix data failed for %s", symbol)
            errors.append({"symbol": symbol, "frequency": "daily", "message": str(exc)})

    rows: list[dict] = []
    for frequency in FREQUENCIES:
        bar_size = frequency["bars"]
        if bar_size is None:
            for strategy_id in STRATEGIES:
                rows.append(
                    {
                        "strategy_id": strategy_id,
                        "strategy_name": STRATEGIES[strategy_id],
                        "frequency_id": frequency["id"],
                        "frequency_name": frequency["name"],
                        "available": False,
                        "score": None,
                        "coverage": 0.0,
                        "reason": (
                            "Tushare 历史分钟为单独权限；当前账户返回每小时1次，"
                            "无法完成13个标的的同口径批量测试"
                        ),
                    }
                )
            continue

        resampled = {
            symbol: _resample_bars(frame, int(bar_size))
            for symbol, frame in daily_frames.items()
        }
        for strategy_id in STRATEGIES:
            notify(f"正在评测：{STRATEGIES[strategy_id]} × {frequency['name']}")
            symbol_results: list[dict] = []
            for symbol, frame in resampled.items():
                try:
                    metrics = _simulate(
                        frame,
                        strategy_id,
                        costs,
                        etf=is_etf(symbol),
                    )
                    symbol_results.append(
                        {
                            "symbol": symbol,
                            "total_return": metrics["total_return"],
                            "annualized_return": metrics["annualized_return"],
                            "max_drawdown": metrics["max_drawdown"],
                            "sharpe": metrics["sharpe"],
                            "calmar": metrics["calmar"],
                            "oos_sharpe": metrics["oos"]["sharpe"],
                            "oos_calmar": metrics["oos"]["calmar"],
                            "trade_count": metrics["trade_count"],
                            "cost_rate": metrics["cost_rate"],
                        }
                    )
                except Exception as exc:
                    logger.warning(
                        "Strategy matrix skipped %s %s %s: %s",
                        symbol,
                        strategy_id,
                        frequency["id"],
                        exc,
                    )
                    errors.append(
                        {
                            "symbol": symbol,
                            "strategy": strategy_id,
                            "frequency": frequency["id"],
                            "message": str(exc),
                        }
                    )
            rows.append(
                _aggregate_cell(
                    strategy_id,
                    frequency["id"],
                    frequency["name"],
                    symbol_results,
                    len(request.symbols),
                )
            )

    _score_rows(rows)
    ranked = sorted(
        (row for row in rows if row["available"] and row["score"] is not None),
        key=lambda row: row["score"],
        reverse=True,
    )
    best = ranked[0] if ranked else None
    notify("评测完成")
    return {
        "symbols": request.symbols,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "strategies": [
            {"id": strategy_id, "name": name}
            for strategy_id, name in STRATEGIES.items()
        ],
        "frequencies": FREQUENCIES,
        "rows": rows,
        "ranking": ranked,
        "best": best,
        "errors": errors,
        "methodology": {
            "execution": "信号在当前K线收盘确认，下一根K线开盘成交；股票卖出遵守T+1",
            "parameters": {
                "moving_average": "5/20根K线均线",
                "momentum": "20根K线收益为正且价格高于10根均线",
                "breakout": "突破前20根高点入场，跌破前10根低点离场",
                "rsi_reversion": "RSI(14)<30入场，RSI>55离场",
            },
            "score": (
                "相对评分：样本外夏普30%、样本外Calmar15%、全样本夏普15%、"
                "年化收益10%、最大回撤10%、盈利标的比例10%、成本5%、覆盖率5%；"
                "中位交易少于4笔乘0.8"
            ),
            "out_of_sample": "按时间顺序最后30%作为样本外区间，参数未针对标的优化",
            "costs": {
                "commission_rate": request.commission_rate,
                "minimum_commission": request.minimum_commission,
                "stock_stamp_tax_rate": request.stamp_tax_rate,
                "slippage_bps": request.slippage_bps,
                "etf_stamp_tax_rate": 0,
            },
        },
    }
