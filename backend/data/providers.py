from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import json

import numpy as np
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
            if not {"up_limit", "down_limit"}.issubset(cached.columns):
                cached = self._merge_price_limits(
                    cached,
                    symbol,
                    start_date,
                    end_date,
                )
                self._write_cache(cache_path, cached)
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
        frame = self._merge_price_limits(
            frame,
            symbol,
            start_date,
            end_date,
        )
        frame = frame[
            [
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "up_limit",
                "down_limit",
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

    def _merge_price_limits(
        self,
        frame: pd.DataFrame,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        cache_path = self._cache_path("price_limits", symbol, start_date, end_date)
        if cache_path.exists():
            limits = pd.read_csv(cache_path)
        else:
            limits = self._pro.stk_limit(
                ts_code=symbol,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                fields="ts_code,trade_date,pre_close,up_limit,down_limit",
            )
            if limits is None or limits.empty:
                limits = pd.DataFrame(
                    columns=["ts_code", "trade_date", "pre_close", "up_limit", "down_limit"]
                )
            self._write_cache(cache_path, limits)

        result = frame.drop(columns=["up_limit", "down_limit"], errors="ignore").copy()
        if limits.empty:
            result["up_limit"] = np.nan
            result["down_limit"] = np.nan
            return result
        limits = limits.copy()
        limits["trade_date"] = pd.to_datetime(
            limits["trade_date"], format="%Y%m%d", errors="coerce"
        )
        for column in ("up_limit", "down_limit"):
            limits[column] = pd.to_numeric(limits[column], errors="coerce")
        return result.merge(
            limits[["trade_date", "up_limit", "down_limit"]],
            on="trade_date",
            how="left",
            validate="one_to_one",
        )

    def fetch_open_dates(self, start_date: date, end_date: date) -> list[date]:
        if end_date < start_date:
            return []
        frame = self._pro.trade_cal(
            exchange="SSE",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            is_open="1",
            fields="cal_date,is_open",
        )
        if frame is None or frame.empty:
            return []
        return sorted(
            pd.to_datetime(frame["cal_date"], format="%Y%m%d").dt.date.tolist()
        )

    def fetch_corporate_actions(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Return implemented cash/stock dividends by ex-date.

        The paper ledger accrues both legs on the ex-date.  This is an
        economic-return convention: it keeps the position value continuous
        across the ex-date, while the recorded event still retains the actual
        pay/list dates for audit purposes.
        """
        columns = [
            "ts_code",
            "end_date",
            "ann_date",
            "div_proc",
            "stk_div",
            "cash_div",
            "cash_div_tax",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "imp_ann_date",
        ]
        if is_etf(symbol):
            return pd.DataFrame(columns=columns)

        cache_path = self._cache_path(
            "corporate_actions",
            symbol,
            start_date,
            end_date,
        )
        if cache_path.exists():
            frame = pd.read_csv(cache_path, dtype=str)
        else:
            frame = self._pro.dividend(
                ts_code=symbol,
                fields=",".join(columns),
            )
            if frame is None:
                frame = pd.DataFrame(columns=columns)
            for column in columns:
                if column not in frame.columns:
                    frame[column] = None
            frame = frame[columns]
            self._write_cache(cache_path, frame)

        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame = frame.copy()
        frame = frame[frame["div_proc"].astype(str).str.strip() == "实施"]
        frame["ex_date"] = pd.to_datetime(
            frame["ex_date"], format="%Y%m%d", errors="coerce"
        )
        frame = frame[
            frame["ex_date"].between(
                pd.Timestamp(start_date), pd.Timestamp(end_date), inclusive="both"
            )
        ]
        for column in ("stk_div", "cash_div", "cash_div_tax"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        frame = (
            frame.sort_values(["ex_date", "end_date", "imp_ann_date"])
            .drop_duplicates(["ex_date", "end_date"], keep="last")
            .reset_index(drop=True)
        )
        return frame

    def fetch_corporate_actions_for_period(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch all implemented actions by ex-date, then filter the universe.

        Tushare accepts ``ex_date`` as a query key, so catch-up processing costs
        one request per trading day instead of one request per candidate symbol.
        """
        columns = [
            "ts_code",
            "end_date",
            "ann_date",
            "div_proc",
            "stk_div",
            "cash_div",
            "cash_div_tax",
            "record_date",
            "ex_date",
            "pay_date",
            "div_listdate",
            "imp_ann_date",
        ]
        wanted = {symbol for symbol in symbols if not is_etf(symbol)}
        if not wanted:
            return pd.DataFrame(columns=columns)

        frames: list[pd.DataFrame] = []
        for trade_date in self.fetch_open_dates(start_date, end_date):
            cache_path = CACHE_DIR / f"corporate_actions_exdate_{trade_date:%Y%m%d}.csv"
            if cache_path.exists():
                frame = pd.read_csv(cache_path, dtype=str)
            else:
                frame = self._pro.dividend(
                    ex_date=trade_date.strftime("%Y%m%d"),
                    fields=",".join(columns),
                )
                if frame is None:
                    frame = pd.DataFrame(columns=columns)
                for column in columns:
                    if column not in frame.columns:
                        frame[column] = None
                frame = frame[columns]
                self._write_cache(cache_path, frame)
            if not frame.empty:
                frames.append(frame[frame["ts_code"].isin(wanted)].copy())

        if not frames:
            return pd.DataFrame(columns=columns)
        result = pd.concat(frames, ignore_index=True)
        result = result[result["div_proc"].astype(str).str.strip() == "实施"]
        result["ex_date"] = pd.to_datetime(
            result["ex_date"], format="%Y%m%d", errors="coerce"
        )
        for column in ("stk_div", "cash_div", "cash_div_tax"):
            result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0.0)
        return (
            result.sort_values(["ex_date", "ts_code", "end_date", "imp_ann_date"])
            .drop_duplicates(["ex_date", "ts_code", "end_date"], keep="last")
            .reset_index(drop=True)
        )

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

    def fetch_market_technical_breadth(
        self,
        trade_date: date,
    ) -> dict[str, float | int | str]:
        """Calculate whole-market medium-term breadth from point-in-time factors."""
        cache_path = CACHE_DIR / f"market_technical_{trade_date:%Y%m%d}.csv"
        if cache_path.exists():
            frame = pd.read_csv(cache_path)
        else:
            frame = self._pro.stk_factor_pro(
                trade_date=trade_date.strftime("%Y%m%d"),
                fields="ts_code,trade_date,close_qfq,ma_qfq_20,ma_qfq_60",
            )
            if frame is None or frame.empty:
                raise ValueError(f"stk_factor_pro 在 {trade_date} 没有返回数据")
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            frame.to_csv(cache_path, index=False, encoding="utf-8")

        required = {"close_qfq", "ma_qfq_20", "ma_qfq_60"}
        missing = sorted(required.difference(frame.columns))
        if missing:
            raise ValueError(f"stk_factor_pro 缺少字段：{', '.join(missing)}")
        close = pd.to_numeric(frame["close_qfq"], errors="coerce")
        ma20 = pd.to_numeric(frame["ma_qfq_20"], errors="coerce")
        ma60 = pd.to_numeric(frame["ma_qfq_60"], errors="coerce")
        valid20 = close.notna() & ma20.notna() & (ma20 > 0)
        valid60 = close.notna() & ma60.notna() & (ma60 > 0)
        total = max(len(frame), 1)
        breadth20 = float((close[valid20] > ma20[valid20]).mean()) if valid20.any() else 0.0
        breadth60 = float((close[valid60] > ma60[valid60]).mean()) if valid60.any() else 0.0
        coverage = float(min(valid20.sum(), valid60.sum()) / total)
        return {
            "trade_date": trade_date.isoformat(),
            "above_ma20": round(breadth20, 6),
            "above_ma60": round(breadth60, 6),
            "composite": round(breadth20 * 0.6 + breadth60 * 0.4, 6),
            "coverage": round(coverage, 6),
            "sample_size": int(len(frame)),
        }

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
