from __future__ import annotations

from datetime import date
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def normalize_ts_code(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("证券代码必须是字符串")

    symbol = value.strip().upper()
    match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", symbol)
    if not match:
        raise ValueError("证券代码格式错误，例如：002317、002317.SZ 或 159611.SZ")

    code, supplied_exchange = match.groups()
    if code.startswith(("5", "6")):
        expected_exchange = "SH"
    elif code.startswith(("0", "1", "2", "3")):
        expected_exchange = "SZ"
    elif code.startswith(("4", "8", "9")):
        expected_exchange = "BJ"
    else:
        raise ValueError("暂不支持该证券代码")

    if supplied_exchange and supplied_exchange != expected_exchange:
        raise ValueError(
            f"证券代码与交易所不匹配，{code} 应使用后缀 .{expected_exchange}"
        )
    return f"{code}.{expected_exchange}"


def is_etf(symbol: str) -> bool:
    code = symbol.split(".", 1)[0]
    return code.startswith(("15", "16", "50", "51", "52", "53", "56", "58"))


DEFAULT_MATRIX_SYMBOLS = [
    "159611.SZ",
    "002317.SZ",
    "600183.SH",
    "603738.SH",
    "600367.SH",
    "000811.SZ",
    "002714.SZ",
    "300308.SZ",
    "300502.SZ",
    "688498.SH",
    "300394.SZ",
    "002371.SZ",
    "688008.SH",
]


class PaperSimulationRequest(BaseModel):
    account_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    strategy_id: Literal["moving_average", "momentum", "breakout"] = (
        "moving_average"
    )
    universe_mode: Literal["fixed", "full_market"] = "fixed"
    risk_profile: Literal["balanced", "aggressive"] = "balanced"
    minimum_invested_ratio: float = Field(default=0.0, ge=0.0, le=0.95)
    symbols: list[str] = Field(default_factory=lambda: DEFAULT_MATRIX_SYMBOLS.copy())
    backtest_start_date: date = date(2024, 1, 1)
    backtest_end_date: date = date(2025, 12, 31)
    simulation_start_date: date = date(2026, 1, 1)
    simulation_end_date: date = Field(default_factory=date.today)
    initial_cash: float = Field(default=500_000, ge=50_000, le=100_000_000)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            raw_symbols = [part for part in re.split(r"[\s,，;；]+", value) if part]
        elif isinstance(value, list):
            raw_symbols = value
        else:
            raise ValueError("标的池必须是证券代码列表")
        normalized = list(dict.fromkeys(normalize_ts_code(item) for item in raw_symbols))
        if len(normalized) < 5:
            raise ValueError("模拟组合至少需要 5 个候选标的")
        if len(normalized) > 120:
            raise ValueError("单次最多使用 120 个候选标的")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> "PaperSimulationRequest":
        if self.backtest_start_date >= self.backtest_end_date:
            raise ValueError("回测开始日期必须早于回测结束日期")
        if (self.backtest_end_date - self.backtest_start_date).days < 365:
            raise ValueError("回测区间至少需要 365 天")
        if self.backtest_end_date >= self.simulation_start_date:
            raise ValueError("模拟盘开始日期必须晚于回测结束日期")
        if self.simulation_start_date > self.simulation_end_date:
            raise ValueError("模拟盘开始日期不能晚于模拟截至日期")
        return self


class PaperAdvanceRequest(BaseModel):
    account_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    symbols: list[str] = Field(default_factory=lambda: DEFAULT_MATRIX_SYMBOLS.copy())
    as_of_date: date = Field(default_factory=date.today)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: object) -> list[str]:
        if isinstance(value, str):
            raw_symbols = [part for part in re.split(r"[\s,，;；]+", value) if part]
        elif isinstance(value, list):
            raw_symbols = value
        else:
            raise ValueError("标的池必须是证券代码列表")
        normalized = list(dict.fromkeys(normalize_ts_code(item) for item in raw_symbols))
        if len(normalized) < 5:
            raise ValueError("模拟组合至少需要 5 个候选标的")
        return normalized
