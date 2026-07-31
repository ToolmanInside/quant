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


class BacktestRequest(BaseModel):
    symbol: str = Field(default="000001.SZ")
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2025, 12, 31)
    short_window: int = Field(default=5, ge=2, le=120)
    long_window: int = Field(default=20, ge=3, le=250)
    initial_cash: float = Field(default=100_000, ge=10_000, le=100_000_000)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    minimum_commission: float = Field(default=5.0, ge=0, le=100)
    stamp_tax_rate: float = Field(default=0.0005, ge=0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: object) -> str:
        return normalize_ts_code(value)

    @model_validator(mode="after")
    def validate_range(self) -> "BacktestRequest":
        if self.start_date >= self.end_date:
            raise ValueError("开始日期必须早于结束日期")
        if self.short_window >= self.long_window:
            raise ValueError("短均线周期必须小于长均线周期")
        return self


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


class StrategyMatrixRequest(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: DEFAULT_MATRIX_SYMBOLS.copy())
    start_date: date = date(2020, 1, 1)
    end_date: date = date(2025, 12, 31)
    initial_cash: float = Field(default=100_000, ge=10_000, le=100_000_000)
    commission_rate: float = Field(default=0.0003, ge=0, le=0.01)
    minimum_commission: float = Field(default=5.0, ge=0, le=100)
    stamp_tax_rate: float = Field(default=0.0005, ge=0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)

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
        if not normalized:
            raise ValueError("标的池不能为空")
        if len(normalized) > 50:
            raise ValueError("单次最多评测 50 个标的")
        return normalized

    @model_validator(mode="after")
    def validate_range(self) -> "StrategyMatrixRequest":
        if self.start_date >= self.end_date:
            raise ValueError("开始日期必须早于结束日期")
        if (self.end_date - self.start_date).days < 365:
            raise ValueError("策略矩阵至少需要一年的历史区间")
        return self


class PaperSimulationRequest(BaseModel):
    account_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    strategy_id: Literal["moving_average", "momentum", "breakout"] = (
        "moving_average"
    )
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
        if len(normalized) > 50:
            raise ValueError("单次最多使用 50 个候选标的")
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
