"""Synthetic SDK-schema examples. Never captured/frozen live Nasdaq responses.

Small hand-authored samples stay separate from the six-month generated fixture.
The latter uses a fixed window and actual exchange sessions, not requested dates.
"""
from copy import deepcopy
from functools import lru_cache
from math import sin

DIRECTORY = [
    {"symbol": "AAPL", "name": "Apple Inc. - Common Stock", "exchange": None,
     "Listing Exchange": "Q", "market_category": "Q", "etf": "N", "test_issue": "N",
     "nasdaq_traded": "Y", "nasdaq_symbol": "AAPL", "cqs_symbol": None},
    {"symbol": "MSFT", "name": "Microsoft Corporation - Common Stock", "exchange": None,
     "Listing Exchange": "Q", "market_category": "Q", "etf": "N", "test_issue": "N",
     "nasdaq_traded": "Y", "nasdaq_symbol": "MSFT", "cqs_symbol": None},
]
BARS = {
    "AAPL": [
        {"date": "2024-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
        {"date": "2024-01-03", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1100},
        {"date": "2024-01-04", "open": 102, "high": 104, "low": 101, "close": 103, "volume": 1200},
        {"date": "2024-01-05", "open": 103, "high": 105, "low": 102, "close": 104, "volume": 1300},
    ],
    "MSFT": [
        {"date": "2024-01-02", "open": 200, "high": 202, "low": 199, "close": 201, "volume": 2000},
        {"date": "2024-01-03", "open": 201, "high": 203, "low": 200, "close": 202, "volume": 2100},
        {"date": "2024-01-04", "open": 202, "high": 204, "low": 201, "close": 203, "volume": 2200},
        {"date": "2024-01-05", "open": 203, "high": 205, "low": 202, "close": 204, "volume": 2300},
    ],
}


@lru_cache(maxsize=1)
def generated_bars() -> dict[str, list[dict]]:
    import exchange_calendars as xc
    sessions = xc.get_calendar("XNYS", start="2024-01-02", end="2024-07-01").sessions_in_range(
        "2024-01-02", "2024-06-30")
    result = {}
    for symbol, base in (("AAPL", 100), ("MSFT", 200)):
        rows = []
        for index, session in enumerate(sessions):
            close = round(base + index * 0.03 + sin(index / 4) * 7, 4)
            opening = round(close - sin(index) * 0.4, 4)
            rows.append({"date": session.date().isoformat(), "open": opening,
                         "high": round(max(opening, close) + 1, 4),
                         "low": round(min(opening, close) - 1, 4), "close": close,
                         "volume": 1000 + index * 11})
        # Preserve the tiny contract sample verbatim for existing boundary tests.
        rows[:4] = deepcopy(BARS[symbol])
        result[symbol] = rows
    return result


def fixture_call(command: str, params: dict) -> dict:
    import re
    if command == "search":
        rows = [row for row in DIRECTORY if re.search(params["query"], row["symbol"], re.IGNORECASE)]
    elif command == "historical":
        rows = [row for row in generated_bars().get(params["symbol"], [])
                if params["start_date"] <= row["date"] <= params["end_date"]]
    else:
        raise ValueError("unsupported fixture endpoint")
    return deepcopy({"results": rows, "provider": "nasdaq", "warnings": [], "extra": {
        "evidence": "synthetic_fixture_not_live", "fixture_version": "openbb-nasdaq-schema.v2",
        "fixture_type": "generated_synthetic_exchange_sessions",
        "fixture_scope": "AAPL/MSFT [2024-01-02,2024-07-01)",
    }})
