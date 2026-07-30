"""Helpers for the low-latency single-stock analysis bundle."""

from typing import Any, Dict, Optional


def _number(value: Any, multiplier: float = 1.0) -> Optional[float]:
    """Convert a Tencent quote field to a float without leaking invalid values."""
    if value in (None, "", "--"):
        return None
    try:
        return float(value) * multiplier
    except (TypeError, ValueError):
        return None


def parse_tencent_quote_payload(raw: str, symbol: str) -> Dict[str, Any]:
    """Parse Tencent's ``qt.gtimg.cn`` payload into ai-stock friendly fields."""
    if not raw or "~" not in raw:
        return {}

    quoted = raw.split('"', 1)
    content = quoted[1].rsplit('"', 1)[0] if len(quoted) > 1 else raw
    fields = content.split("~")
    if len(fields) < 47:
        return {}

    returned_symbol = fields[2].strip()
    if returned_symbol and returned_symbol != symbol:
        return {}

    return {
        "symbol": symbol,
        "name": fields[1].strip(),
        "current_price": _number(fields[3]),
        "prev_close": _number(fields[4]),
        "open": _number(fields[5]),
        "volume": _number(fields[6]),
        "bid": _number(fields[9]),
        "ask": _number(fields[19]),
        "change_amount": _number(fields[31]),
        "change_pct": _number(fields[32]),
        "high": _number(fields[33]),
        "low": _number(fields[34]),
        # Tencent reports amount in ten-thousand CNY and market caps in 100m CNY.
        "amount": _number(fields[37], 10_000),
        "turnover_rate": _number(fields[38]),
        "amplitude": _number(fields[43]),
        "float_market_cap": _number(fields[44], 100_000_000),
        "total_market_cap": _number(fields[45], 100_000_000),
        "pb": _number(fields[46]),
        "quote_time": fields[30].strip() or None,
        "source": "tencent",
    }


def normalize_tencent_kline_date(value: str) -> str:
    """Tencent K-line accepts ISO dates while akshare callers use YYYYMMDD."""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text
