from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from backend.models import BacktestRequest


@dataclass
class Portfolio:
    cash: float
    shares: int = 0


def _commission(gross: float, request: BacktestRequest) -> float:
    if gross <= 0:
        return 0.0
    return max(request.minimum_commission, gross * request.commission_rate)


def run_moving_average_backtest(
    frame: pd.DataFrame,
    request: BacktestRequest,
    data_source: str,
) -> dict:
    if len(frame) <= request.long_window + 2:
        raise ValueError(f"有效行情不足，至少需要 {request.long_window + 3} 根日线")

    data = frame.copy().sort_values("trade_date").reset_index(drop=True)
    data["short_ma"] = data["adj_close"].rolling(request.short_window).mean()
    data["long_ma"] = data["adj_close"].rolling(request.long_window).mean()

    # 今日信号只能在今日收盘后得到，因此移动一日并在下一交易日开盘执行。
    data["signal_at_close"] = data["short_ma"] > data["long_ma"]
    data["target_at_open"] = data["signal_at_close"].shift(1).fillna(False)

    portfolio = Portfolio(cash=request.initial_cash)
    trades: list[dict] = []
    equity_values: list[float] = []
    total_cost = 0.0
    slippage = request.slippage_bps / 10_000

    for row in data.itertuples(index=False):
        target_long = bool(row.target_at_open)

        if target_long and portfolio.shares == 0:
            execution_price = float(row.open) * (1 + slippage)
            shares = int((portfolio.cash * 0.98) / execution_price / 100) * 100
            while shares >= 100:
                gross = shares * execution_price
                commission = _commission(gross, request)
                if gross + commission <= portfolio.cash:
                    break
                shares -= 100

            if shares >= 100:
                gross = shares * execution_price
                commission = _commission(gross, request)
                slippage_cost = shares * float(row.open) * slippage
                portfolio.cash -= gross + commission
                portfolio.shares = shares
                total_cost += commission + slippage_cost
                trades.append(
                    {
                        "date": row.trade_date.strftime("%Y-%m-%d"),
                        "side": "买入",
                        "price": round(execution_price, 4),
                        "quantity": shares,
                        "gross": round(gross, 2),
                        "commission": round(commission, 2),
                        "tax": 0.0,
                        "slippage": round(slippage_cost, 2),
                    }
                )

        elif not target_long and portfolio.shares > 0:
            execution_price = float(row.open) * (1 - slippage)
            gross = portfolio.shares * execution_price
            commission = _commission(gross, request)
            tax = gross * request.stamp_tax_rate
            slippage_cost = portfolio.shares * float(row.open) * slippage
            portfolio.cash += gross - commission - tax
            total_cost += commission + tax + slippage_cost
            trades.append(
                {
                    "date": row.trade_date.strftime("%Y-%m-%d"),
                    "side": "卖出",
                    "price": round(execution_price, 4),
                    "quantity": portfolio.shares,
                    "gross": round(gross, 2),
                    "commission": round(commission, 2),
                    "tax": round(tax, 2),
                    "slippage": round(slippage_cost, 2),
                }
            )
            portfolio.shares = 0

        equity_values.append(portfolio.cash + portfolio.shares * float(row.close))

    data["equity"] = equity_values
    data["benchmark"] = (
        request.initial_cash * data["adj_close"] / float(data["adj_close"].iloc[0])
    )
    data["drawdown"] = data["equity"] / data["equity"].cummax() - 1

    daily_return = data["equity"].pct_change().dropna()
    final_equity = float(data["equity"].iloc[-1])
    total_return = final_equity / request.initial_cash - 1
    years = max(len(data) / 252, 1 / 252)
    annualized_return = (final_equity / request.initial_cash) ** (1 / years) - 1
    volatility = float(daily_return.std(ddof=0) * math.sqrt(252))
    sharpe = (
        float(daily_return.mean() / daily_return.std(ddof=0) * math.sqrt(252))
        if len(daily_return) > 1 and daily_return.std(ddof=0) > 0
        else 0.0
    )

    series = []
    for row in data.itertuples(index=False):
        series.append(
            {
                "date": row.trade_date.strftime("%Y-%m-%d"),
                "close": round(float(row.close), 4),
                "adj_close": round(float(row.adj_close), 4),
                "short_ma": None if pd.isna(row.short_ma) else round(float(row.short_ma), 4),
                "long_ma": None if pd.isna(row.long_ma) else round(float(row.long_ma), 4),
                "equity": round(float(row.equity), 2),
                "benchmark": round(float(row.benchmark), 2),
                "drawdown": round(float(row.drawdown), 6),
            }
        )

    return {
        "data_source": data_source,
        "strategy": {
            "name": "双均线趋势",
            "description": "短均线上穿长均线后，下一交易日开盘买入；反向时卖出。",
            "short_window": request.short_window,
            "long_window": request.long_window,
            "execution": "次日开盘",
        },
        "summary": {
            "initial_cash": round(request.initial_cash, 2),
            "final_equity": round(final_equity, 2),
            "total_return": round(total_return, 6),
            "benchmark_return": round(float(data["benchmark"].iloc[-1]) / request.initial_cash - 1, 6),
            "annualized_return": round(float(annualized_return), 6),
            "max_drawdown": round(float(data["drawdown"].min()), 6),
            "volatility": round(volatility, 6),
            "sharpe": round(sharpe, 4),
            "trade_count": len(trades),
            "total_cost": round(total_cost, 2),
            "ending_cash": round(portfolio.cash, 2),
            "ending_shares": portfolio.shares,
        },
        "series": series,
        "trades": trades,
        "warnings": [
            "当前版本仅用于研究与回测，不会连接券商或发送真实订单。",
            "模拟成交未建模涨跌停封单、停牌和部分成交，后续阶段补充。",
            "历史表现不代表未来收益。",
        ],
    }
