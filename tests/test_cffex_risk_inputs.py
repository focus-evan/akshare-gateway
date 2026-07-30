import pandas as pd

from cffex_risk_inputs import (
    fetch_cffex_risk_inputs,
    normalize_broker_positions,
    normalize_market_rows,
)


def _rank_frame():
    return pd.DataFrame(
        [
            {
                "rank": 1,
                "long_party_name": "其他期货",
                "long_open_interest": 800,
                "long_open_interest_chg": 20,
                "short_party_name": "中信期货",
                "short_open_interest": 1200,
                "short_open_interest_chg": 300,
            },
            {
                "rank": 2,
                "long_party_name": "中信期货",
                "long_open_interest": 500,
                "long_open_interest_chg": 100,
                "short_party_name": "其他期货",
                "short_open_interest": 700,
                "short_open_interest_chg": -10,
            },
            {
                "rank": 999,
                "long_party_name": None,
                "long_open_interest": 9999,
                "long_open_interest_chg": 9999,
                "short_party_name": None,
                "short_open_interest": 9999,
                "short_open_interest_chg": 9999,
            },
        ]
    )


def test_normalize_broker_positions_keeps_disclosure_flags():
    rows = normalize_broker_positions(
        {"IF2608": _rank_frame()},
        "中信期货",
        "20260729",
    )

    assert rows == [
        {
            "date": "20260729",
            "symbol": "IF2608",
            "variety": "IF",
            "long_disclosed": True,
            "long_open_interest": 500,
            "long_open_interest_chg": 100,
            "short_disclosed": True,
            "short_open_interest": 1200,
            "short_open_interest_chg": 300,
        }
    ]


def test_normalize_market_rows_only_keeps_index_futures():
    rows = normalize_market_rows(
        pd.DataFrame(
            [
                {
                    "symbol": "IF2608",
                    "variety": "IF",
                    "close": 4000,
                    "pre_settle": 4040,
                    "volume": 10000,
                    "open_interest": 20000,
                },
                {
                    "symbol": "T2609",
                    "variety": "T",
                    "close": 110,
                    "pre_settle": 109,
                },
            ]
        ),
        "20260729",
    )

    assert len(rows) == 1
    assert rows[0]["symbol"] == "IF2608"
    assert rows[0]["open_interest"] == 20000


class _FakeAk:
    @staticmethod
    def get_cffex_rank_table(date, vars_list):
        if date != "20260729":
            return {}
        return {"IF2608": _rank_frame()}

    @staticmethod
    def futures_hist_daily_cffex(date):
        return pd.DataFrame(
            [
                {
                    "symbol": "IF2608",
                    "variety": "IF",
                    "close": 4000,
                    "pre_settle": 4040,
                    "volume": 10000,
                    "open_interest": 20000,
                }
            ]
        )

    @staticmethod
    def index_zh_a_hist(**kwargs):
        return pd.DataFrame([{"日期": "2026-07-29", "收盘": 4010}])


def test_fetch_cffex_risk_inputs_skips_non_trading_days():
    result = fetch_cffex_risk_inputs(
        _FakeAk,
        target_date="20260730",
        broker="中信期货",
        lookback_sessions=1,
    )

    assert result["latest_trading_date"] == "20260729"
    assert result["session_count"] == 1
    assert result["sessions"][0]["spot_close"]["IF"] == 4010
    assert result["is_proprietary_position"] is False
