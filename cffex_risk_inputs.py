"""CFFEX index-futures inputs used by the A-share market risk gate.

The public member ranking is brokerage-client aggregate data.  This module
therefore exposes disclosed positions and their coverage explicitly instead
of presenting a broker row as the broker's proprietary directional position.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd


INDEX_FUTURES = ("IF", "IH", "IC", "IM")
INDEX_SYMBOLS = {
    "IF": "000300",
    "IH": "000016",
    "IC": "000905",
    "IM": "000852",
}


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or pd.isna(value):
            return None
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None else None


def _party_matches(value: Any, broker: str) -> bool:
    text = str(value or "").replace(" ", "").strip()
    target = str(broker or "").replace(" ", "").strip()
    return bool(text and target and (text == target or target in text))


def _first_matching_row(
    frame: pd.DataFrame,
    party_column: str,
    broker: str,
) -> Optional[pd.Series]:
    if frame is None or frame.empty or party_column not in frame.columns:
        return None
    for _, row in frame.iterrows():
        rank = _integer(row.get("rank"))
        if rank == 999:
            continue
        if _party_matches(row.get(party_column), broker):
            return row
    return None


def normalize_broker_positions(
    rank_tables: Any,
    broker: str,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """Flatten the member's disclosed long/short rows for every contract."""
    if not isinstance(rank_tables, dict):
        return []

    rows: List[Dict[str, Any]] = []
    for raw_symbol, frame in rank_tables.items():
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        symbol = str(raw_symbol or "").strip().upper()
        variety = "".join(char for char in symbol if char.isalpha()).upper()
        if variety not in INDEX_FUTURES:
            continue

        long_row = _first_matching_row(frame, "long_party_name", broker)
        short_row = _first_matching_row(frame, "short_party_name", broker)
        rows.append(
            {
                "date": trading_date,
                "symbol": symbol,
                "variety": variety,
                "long_disclosed": long_row is not None,
                "long_open_interest": (
                    _integer(long_row.get("long_open_interest"))
                    if long_row is not None
                    else None
                ),
                "long_open_interest_chg": (
                    _integer(long_row.get("long_open_interest_chg"))
                    if long_row is not None
                    else None
                ),
                "short_disclosed": short_row is not None,
                "short_open_interest": (
                    _integer(short_row.get("short_open_interest"))
                    if short_row is not None
                    else None
                ),
                "short_open_interest_chg": (
                    _integer(short_row.get("short_open_interest_chg"))
                    if short_row is not None
                    else None
                ),
            }
        )
    return sorted(rows, key=lambda item: item["symbol"])


def normalize_market_rows(
    market_frame: Any,
    trading_date: str,
) -> List[Dict[str, Any]]:
    """Normalize CFFEX daily contract data while retaining official fields."""
    if not isinstance(market_frame, pd.DataFrame) or market_frame.empty:
        return []

    rows: List[Dict[str, Any]] = []
    for _, raw in market_frame.iterrows():
        symbol = str(raw.get("symbol", raw.get("合约代码", ""))).strip().upper()
        variety = str(raw.get("variety", "")).strip().upper()
        if not variety:
            variety = "".join(char for char in symbol if char.isalpha()).upper()
        if variety not in INDEX_FUTURES:
            continue
        rows.append(
            {
                "date": trading_date,
                "symbol": symbol,
                "variety": variety,
                "open": _number(raw.get("open")),
                "high": _number(raw.get("high")),
                "low": _number(raw.get("low")),
                "close": _number(raw.get("close")),
                "settle": _number(raw.get("settle")),
                "pre_settle": _number(raw.get("pre_settle")),
                "volume": _integer(raw.get("volume")),
                "open_interest": _integer(raw.get("open_interest")),
            }
        )
    return sorted(rows, key=lambda item: item["symbol"])


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip().replace("-", "")
    return text[:8]


def _spot_close_history(
    ak_module: Any,
    start_date: str,
    end_date: str,
) -> Dict[str, Dict[str, float]]:
    """Best-effort spot closes; ranking/market data remain usable if it fails."""
    result: Dict[str, Dict[str, float]] = {}
    for variety, index_symbol in INDEX_SYMBOLS.items():
        try:
            frame = ak_module.index_zh_a_hist(
                symbol=index_symbol,
                period="daily",
                start_date=start_date,
                end_date=end_date,
            )
        except Exception:
            continue
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        date_column = next(
            (column for column in frame.columns if "日期" in str(column) or str(column).lower() == "date"),
            None,
        )
        close_column = next(
            (column for column in frame.columns if "收盘" in str(column) or str(column).lower() == "close"),
            None,
        )
        if date_column is None or close_column is None:
            continue
        result[variety] = {}
        for _, row in frame.iterrows():
            date_key = _date_text(row.get(date_column))
            close = _number(row.get(close_column))
            if date_key and close is not None and close > 0:
                result[variety][date_key] = close
    return result


def _candidate_dates(target_date: datetime, max_calendar_days: int) -> Iterable[datetime]:
    for offset in range(max_calendar_days):
        candidate = target_date - timedelta(days=offset)
        if candidate.weekday() < 5:
            yield candidate


def fetch_cffex_risk_inputs(
    ak_module: Any,
    *,
    target_date: str,
    broker: str = "中信期货",
    lookback_sessions: int = 5,
) -> Dict[str, Any]:
    """Fetch recent official CFFEX member rankings and contract market data."""
    clean_date = _date_text(target_date)
    target = datetime.strptime(clean_date, "%Y%m%d")
    lookback = min(max(int(lookback_sessions), 1), 10)
    sessions: List[Dict[str, Any]] = []
    errors: List[str] = []

    for candidate in _candidate_dates(target, max(lookback * 4, 14)):
        if len(sessions) >= lookback:
            break
        day = candidate.strftime("%Y%m%d")
        try:
            rank_tables = ak_module.get_cffex_rank_table(
                date=day,
                vars_list=list(INDEX_FUTURES),
            )
        except Exception as exc:
            errors.append(f"{day}:rank:{exc}")
            continue
        positions = normalize_broker_positions(rank_tables, broker, day)
        if not positions:
            continue

        market_rows: List[Dict[str, Any]] = []
        try:
            market_frame = ak_module.futures_hist_daily_cffex(date=day)
            market_rows = normalize_market_rows(market_frame, day)
        except Exception as exc:
            errors.append(f"{day}:market:{exc}")

        sessions.append(
            {
                "date": day,
                "positions": positions,
                "market": market_rows,
            }
        )

    if sessions:
        oldest = sessions[-1]["date"]
        newest = sessions[0]["date"]
        spot_history = _spot_close_history(ak_module, oldest, newest)
        for session in sessions:
            day = session["date"]
            session["spot_close"] = {
                variety: values[day]
                for variety, values in spot_history.items()
                if day in values
            }

    return {
        "broker": broker,
        "requested_date": clean_date,
        "latest_trading_date": sessions[0]["date"] if sessions else None,
        "sessions": sessions,
        "session_count": len(sessions),
        "source": "CFFEX official disclosures via AKShare",
        "disclosure_scope": "top20 brokerage-client aggregate",
        "is_proprietary_position": False,
        "errors": errors[-10:],
    }
