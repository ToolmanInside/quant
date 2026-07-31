from datetime import date

import pandas as pd

import backend.data.providers as providers
from backend.data.providers import TushareDataProvider


class FakePro:
    def stk_limit(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": kwargs["ts_code"],
                    "trade_date": "20250102",
                    "pre_close": 10.0,
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
            ]
        )

    def trade_cal(self, **kwargs):
        return pd.DataFrame(
            [
                {"cal_date": "20250102", "is_open": 1},
                {"cal_date": "20250103", "is_open": 1},
            ]
        )

    def dividend(self, **kwargs):
        if kwargs.get("ex_date") != "20250103":
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20241231",
                    "ann_date": "20241201",
                    "div_proc": "实施",
                    "stk_div": 0.1,
                    "cash_div": 0.2,
                    "cash_div_tax": 0.25,
                    "record_date": "20250102",
                    "ex_date": "20250103",
                    "pay_date": "20250103",
                    "div_listdate": "20250103",
                    "imp_ann_date": "20241220",
                }
            ]
        )


def _provider() -> TushareDataProvider:
    provider = object.__new__(TushareDataProvider)
    provider._pro = FakePro()
    return provider


def test_price_limits_are_merged_by_trade_date(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(providers, "CACHE_DIR", tmp_path)
    frame = pd.DataFrame(
        [{"trade_date": pd.Timestamp("2025-01-02"), "open": 10.5}]
    )

    result = _provider()._merge_price_limits(
        frame,
        "000001.SZ",
        date(2025, 1, 2),
        date(2025, 1, 2),
    )

    assert result.loc[0, "up_limit"] == 11.0
    assert result.loc[0, "down_limit"] == 9.0


def test_corporate_actions_are_queried_by_ex_date_for_whole_universe(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(providers, "CACHE_DIR", tmp_path)

    result = _provider().fetch_corporate_actions_for_period(
        ["000001.SZ", "000002.SZ"],
        date(2025, 1, 2),
        date(2025, 1, 3),
    )

    assert list(result["ts_code"]) == ["000001.SZ"]
    assert result.loc[0, "ex_date"] == pd.Timestamp("2025-01-03")
    assert result.loc[0, "stk_div"] == 0.1
