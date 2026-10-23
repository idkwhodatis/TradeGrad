"""Additional deterministic SDK-shaped rows, never upstream observations.

The distinct [2023-07-03, 2024-01-02) interval supports warm-up plus two scored
windows. Existing 2024 fixture rows/counts remain byte-for-byte unchanged.
Prices are indexed from this fixed calendar window, never request-relative.
"""

from functools import lru_cache
from math import sin

VERSION = "provider-research.synthetic.v1"
START, END = "2023-07-03", "2024-01-02"


@lru_cache(maxsize=4)
def rows(calendar, symbol):
    import exchange_calendars as xc

    dates = xc.get_calendar(calendar).sessions_in_range(START, "2024-01-01")
    base = {"600000": 14, "000001": 16, "AAPL": 100, "MSFT": 200}[symbol]
    direction = -1 if symbol == "000001" else 1
    result = []
    for index, session in enumerate(dates):
        close = round(
            base + index * 0.015 + direction * (base * 0.12) * sin(index / 4), 4
        )
        opening = round(close - sin(index) * 0.1, 4)
        result.append(
            {
                "date": session.date().isoformat(),
                "open": opening,
                "high": round(max(opening, close) + 0.5, 4),
                "low": round(min(opening, close) - 0.5, 4),
                "close": close,
                "volume": 10000 + index * 11,
            }
        )
    return tuple(result)


def metadata():
    return {
        "fixture_version": VERSION,
        "fixture_type": "generated_synthetic_exchange_sessions",
        "fixture_scope": f"[{START},{END})",
        "evidence": "synthetic_fixture_not_live",
    }
