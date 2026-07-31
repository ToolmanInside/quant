from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
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

    def fetch_market_snapshot(
        self,
        as_of_date: date,
    ) -> tuple[date, pd.DataFrame, list[str]]:
        """Return one cross-sectional snapshot for all listed A shares.

        Tushare publishes different tables at slightly different times after
        close. We therefore walk backwards to the latest date with a non-empty
        daily table and tolerate optional factor-table failures. The returned
        errors are surfaced in the daily report instead of being hidden.
        """
        errors: list[str] = []
        stock_basic = self._fetch_stock_basic()
        trade_date, daily = self._fetch_latest_daily(as_of_date)
        date_text = trade_date.strftime("%Y%m%d")

        daily_basic = self._read_or_fetch_snapshot(
            "daily_basic",
            trade_date,
            lambda: self._pro.daily_basic(
                trade_date=date_text,
                fields=(
                    "ts_code,trade_date,turnover_rate,turnover_rate_f,"
                    "volume_ratio,pe_ttm,pb,dv_ttm,total_mv,circ_mv"
                ),
            ),
            errors,
        )
        moneyflow = self._read_or_fetch_snapshot(
            "moneyflow",
            trade_date,
            lambda: self._pro.moneyflow(
                trade_date=date_text,
                fields=(
                    "ts_code,trade_date,buy_lg_amount,sell_lg_amount,"
                    "buy_elg_amount,sell_elg_amount,net_mf_amount"
                ),
            ),
            errors,
        )
        quality = self._fetch_quality_snapshot(trade_date, errors)

        snapshot = daily.merge(stock_basic, on="ts_code", how="left")
        for optional in (daily_basic, moneyflow, quality):
            if optional is not None and not optional.empty:
                duplicate_columns = [
                    column
                    for column in optional.columns
                    if column != "ts_code" and column in snapshot.columns
                ]
                optional = optional.drop(columns=duplicate_columns)
                snapshot = snapshot.merge(optional, on="ts_code", how="left")

        snapshot["trade_date"] = pd.to_datetime(
            snapshot["trade_date"],
            format="%Y%m%d",
        )
        return trade_date, snapshot, errors

    def _fetch_stock_basic(self) -> pd.DataFrame:
        cache_path = CACHE_DIR / "stock_basic_listed.csv"
        if cache_path.exists():
            modified = date.fromtimestamp(cache_path.stat().st_mtime)
            if modified >= date.today() - timedelta(days=7):
                return pd.read_csv(cache_path, dtype={"symbol": str})

        frame = self._pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,market,list_date",
        )
        if frame is None or frame.empty:
            raise ValueError("Tushare 未返回上市股票清单 stock_basic")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        frame.to_csv(cache_path, index=False, encoding="utf-8")
        return frame

    def _fetch_latest_daily(self, as_of_date: date) -> tuple[date, pd.DataFrame]:
        for offset in range(11):
            candidate = as_of_date - timedelta(days=offset)
            cache_path = CACHE_DIR / f"market_daily_{candidate:%Y%m%d}.csv"
            if cache_path.exists():
                frame = pd.read_csv(cache_path)
            else:
                frame = self._pro.daily(
                    trade_date=candidate.strftime("%Y%m%d"),
                    fields="ts_code,trade_date,open,high,low,close,pct_chg,vol,amount",
                )
                if frame is not None and not frame.empty:
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    frame.to_csv(cache_path, index=False, encoding="utf-8")
            if frame is not None and not frame.empty:
                return candidate, frame
        raise ValueError(
            f"Tushare 在 {as_of_date} 及此前 10 天内没有返回全市场日线"
        )

    def _read_or_fetch_snapshot(
        self,
        name: str,
        trade_date: date,
        fetcher,
        errors: list[str],
    ) -> pd.DataFrame:
        cache_path = CACHE_DIR / f"market_{name}_{trade_date:%Y%m%d}.csv"
        if cache_path.exists():
            return pd.read_csv(cache_path)
        try:
            frame = fetcher()
            if frame is None or frame.empty:
                errors.append(f"{name} 在 {trade_date} 没有数据")
                return pd.DataFrame()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            frame.to_csv(cache_path, index=False, encoding="utf-8")
            return frame
        except Exception as exc:
            errors.append(f"{name} 获取失败：{type(exc).__name__}: {exc}")
            return pd.DataFrame()

    def _fetch_quality_snapshot(
        self,
        trade_date: date,
        errors: list[str],
    ) -> pd.DataFrame:
        period = self._latest_fully_reported_period(trade_date)
        cache_path = CACHE_DIR / f"market_quality_{period}.csv"
        if cache_path.exists():
            return pd.read_csv(cache_path)
        try:
            frame = self._pro.fina_indicator_vip(
                period=period,
                fields=(
                    "ts_code,ann_date,end_date,roe,grossprofit_margin,"
                    "q_netprofit_margin,debt_to_assets,q_salescash_to_or"
                ),
            )
            if frame is None or frame.empty:
                errors.append(f"fina_indicator_vip 报告期 {period} 没有数据")
                return pd.DataFrame()
            if "ann_date" in frame.columns:
                frame = frame[
                    frame["ann_date"].astype(str)
                    <= trade_date.strftime("%Y%m%d")
                ]
            frame = (
                frame.sort_values(["ts_code", "ann_date"])
                .drop_duplicates("ts_code", keep="last")
                .reset_index(drop=True)
            )
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            frame.to_csv(cache_path, index=False, encoding="utf-8")
            return frame
        except Exception as exc:
            errors.append(
                "盈利质量因子获取失败，将按中性分处理："
                f"{type(exc).__name__}: {exc}"
            )
            return pd.DataFrame()

    @staticmethod
    def _latest_fully_reported_period(value: date) -> str:
        """Use conservative statutory reporting deadlines to avoid look-ahead."""
        if (value.month, value.day) >= (10, 31):
            return f"{value.year}0930"
        if (value.month, value.day) >= (8, 31):
            return f"{value.year}0630"
        if (value.month, value.day) >= (4, 30):
            return f"{value.year}0331"
        return f"{value.year - 1}1231"

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
