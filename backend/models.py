from __future__ import annotations

from datetime import date
import math
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


def board_of(symbol: str) -> str:
    """Return A-share board bucket for trading-rule gates.

    - ``etf``: 场内基金
    - ``star``: 科创板（688）
    - ``chinext``: 创业板（300/301）
    - ``bse``: 北交所（4/8 开头或 .BJ）
    - ``main``: 沪深主板及其他
    """
    if is_etf(symbol):
        return "etf"
    code, _, exchange = symbol.upper().partition(".")
    if code.startswith("688"):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    if exchange == "BJ" or code.startswith(("4", "8", "9")):
        # 北交所新代码 920xxx / 旧三板 43/83/87 等
        if code.startswith(("15", "16")):
            return "etf"
        return "bse"
    return "main"


# 本模拟盘硬性可交易板块：仅沪深主板个股 + 场内 ETF。
# 创业板 / 科创板 / 北交所一律禁止新开与加仓（已持仓仍可减/平）。
TRADEABLE_BOARDS: frozenset[str] = frozenset({"main", "etf"})

# 保留资产门槛常量供展示/对照；当前策略不再按权益放开创业板/科创/北交。
BOARD_ASSET_THRESHOLDS: dict[str, float] = {
    "main": 0.0,
    "etf": 0.0,
    "chinext": 100_000.0,
    "star": 500_000.0,
    "bse": 500_000.0,
}

BOARD_NAMES: dict[str, str] = {
    "main": "主板",
    "etf": "ETF",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
}


def board_asset_threshold(symbol: str) -> float:
    return float(BOARD_ASSET_THRESHOLDS.get(board_of(symbol), 0.0))


def board_lot_size(symbol: str) -> int:
    """Minimum buy board lot. STAR Market requires 200 shares."""
    if board_of(symbol) == "star":
        return 200
    return 100


def is_tradeable_board(symbol: str) -> bool:
    """Whether the symbol's board is in the allowed trading universe."""
    return board_of(symbol) in TRADEABLE_BOARDS


def can_buy_board(symbol: str, equity: float | None = None) -> bool:
    """Whether a new buy/add is allowed on this board.

    Policy: only main-board stocks and ETFs. ChiNext / STAR / BSE are blocked
    regardless of account equity. ``equity`` is kept for API compatibility and
    future optional thresholds.
    """
    if not is_tradeable_board(symbol):
        return False
    if equity is None:
        return True
    try:
        assets = float(equity)
    except (TypeError, ValueError):
        return False
    return math.isfinite(assets) and assets > 0


def board_buy_block_reason(symbol: str, equity: float | None = None) -> str | None:
    """Human-readable buy block reason, or None if buy is allowed."""
    if can_buy_board(symbol, equity):
        return None
    board = board_of(symbol)
    name = BOARD_NAMES.get(board, board)
    if board not in TRADEABLE_BOARDS:
        return f"{name}不在可交易范围（仅主板/ETF）"
    return f"{name}当前不可买入"


DEFAULT_MATRIX_SYMBOLS = [
    "159611.SZ",
    "002317.SZ",
    "600183.SH",
    "603738.SH",
    "600367.SH",
    "000811.SZ",
    "002714.SZ",
    "600036.SH",
    "601318.SH",
    "000858.SZ",
    "600519.SH",
    "002371.SZ",
    "600276.SH",
]


class PaperSimulationRequest(BaseModel):
    account_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,32}$")
    strategy_id: Literal["moving_average", "momentum", "breakout"] = (
        "moving_average"
    )
    universe_mode: Literal["fixed", "full_market"] = "fixed"
    risk_profile: Literal["balanced", "aggressive"] = "balanced"
    minimum_invested_ratio: float = Field(default=0.0, ge=0.0, le=0.95)
    adx_window: int = Field(default=14, ge=5, le=60)
    adx_min: float = Field(default=20.0, ge=5.0, le=60.0)
    volume_confirm_ratio: float = Field(default=1.5, ge=1.0, le=5.0)
    cross_valid_days: int = Field(default=3, ge=1, le=20)
    death_cross_confirm_days: int = Field(default=2, ge=1, le=10)
    death_cross_buffer: float = Field(default=0.005, ge=0.0, le=0.05)
    reentry_cooldown_days: int = Field(default=5, ge=0, le=60)
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
