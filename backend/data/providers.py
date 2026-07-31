from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import json

import pandas as pd

from backend.models import is_etf


CACHE_DIR = Path(__file__).resolve().parent / "cache"

@dataclass(frozen=True)
class MarketData:
    frame: pd.DataFrame
    source: str


class TushareDataProvider:
    name = "tushare"

    def __init__(self, token: str) -> None:
        import tushare as ts

        self._pro = ts.pro_api(token)

    def fetch_daily(self, symbol: str, start_date: date, end_date: date) -> MarketData:
        cache_path = self._cache_path("daily", symbol, start_date, end_date)
        cached = self._read_cache(cache_path, "trade_date")
        if cached is not None:
            return MarketData(frame=cached, source="Tushare Pro（本地缓存）")

        start = start_date.strftime("%Y%m%d")
        end = end_date.strftime("%Y%m%d")
        if is_etf(symbol):
            daily = self._pro.fund_daily(
                ts_code=symbol,
                start_date=start,
                end_date=end,
            )
            factors = self._pro.fund_adj(
                ts_code=symbol,
                start_date=start,
                end_date=end,
            )
        else:
            daily = self._pro.daily(ts_code=symbol, start_date=start, end_date=end)
            factors = self._pro.adj_factor(
                ts_code=symbol,
                start_date=start,
                end_date=end,
            )

        if daily is None or daily.empty:
            raise ValueError(f"Tushare没有返回 {symbol} 在所选区间的日线数据")
        if factors is None or factors.empty:
            raise ValueError(f"Tushare没有返回 {symbol} 的复权因子")

        frame = daily.merge(
            factors[["ts_code", "trade_date", "adj_factor"]],
            on=["ts_code", "trade_date"],
            how="left",
            validate="one_to_one",
        )
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d")
        frame = frame.sort_values("trade_date").reset_index(drop=True)
        frame["adj_factor"] = frame["adj_factor"].ffill().bfill()
        anchor = float(frame["adj_factor"].iloc[-1])
        adjustment = frame["adj_factor"] / anchor
        for column in ("open", "high", "low", "close"):
            frame[f"adj_{column}"] = frame[column] * adjustment

        # Tushare日线成交量单位为手、成交额单位为千元，内部统一成股和元。
        frame["volume"] = frame["vol"] * 100
        frame["amount"] = frame["amount"] * 1_000
        frame = frame[
            [
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "adj_open",
                "adj_high",
                "adj_low",
                "adj_close",
                "volume",
                "amount",
            ]
        ]
        self._write_cache(cache_path, frame)
        return MarketData(frame=frame, source="Tushare Pro")

    def fetch_hourly_cached(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> MarketData | None:
        cache_path = self._cache_path("60min", symbol, start_date, end_date)
        cached = self._read_cache(cache_path, "trade_time")
        if cached is None:
            return None
        return MarketData(frame=cached, source="Tushare Pro 历史分钟（本地缓存）")

    def fetch_industries(self, symbols: list[str]) -> dict[str, dict[str, str]]:
        results: dict[str, dict[str, str]] = {}
        for symbol in symbols:
            cache_path = CACHE_DIR / f"industry_{symbol.replace('.', '_')}.json"
            if cache_path.exists():
                results[symbol] = json.loads(cache_path.read_text(encoding="utf-8"))
                continue

            if is_etf(symbol):
                item = {
                    "sector_code": "ETF",
                    "sector_name": "ETF/其他",
                    "name": symbol,
                }
            else:
                frame = self._pro.index_member_all(ts_code=symbol, is_new="Y")
                if frame is None or frame.empty:
                    item = {
                        "sector_code": "UNKNOWN",
                        "sector_name": "未分类",
                        "name": symbol,
                    }
                else:
                    row = frame.iloc[0]
                    item = {
                        "sector_code": str(row.get("l1_code") or "UNKNOWN"),
                        "sector_name": str(row.get("l1_name") or "未分类"),
                        "name": str(row.get("name") or symbol),
                    }
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(item, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            results[symbol] = item
        return results

    @staticmethod
    def _cache_path(
        frequency: str,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> Path:
        safe_symbol = symbol.replace(".", "_")
        return CACHE_DIR / (
            f"{frequency}_{safe_symbol}_{start_date:%Y%m%d}_{end_date:%Y%m%d}.csv"
        )

    @staticmethod
    def _read_cache(path: Path, time_column: str) -> pd.DataFrame | None:
        if not path.exists():
            return None
        frame = pd.read_csv(path)
        frame[time_column] = pd.to_datetime(frame[time_column])
        return frame

    @staticmethod
    def _write_cache(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path, index=False, encoding="utf-8")
