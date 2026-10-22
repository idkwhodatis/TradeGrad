"""Synthetic, fixed SDK-schema examples, NOT captured or live market quotes.

The fixed generated window is [2024-01-02,2024-07-01). Dates come only from
exchange_calendars sessions. These prices are artificial, NOT captured returns.
Other dates stay missing, never filled.
"""

from math import sin

import pandas as pd


def fixture_days(calendar: str):
    import exchange_calendars as xc

    return [
        item.date().isoformat()
        for item in xc.get_calendar(calendar).sessions_in_range(
            "2024-01-02", "2024-06-30"
        )
    ]


DAYS = fixture_days("XNAS")


def akshare_resolve():
    return pd.DataFrame(
        {"code": ["600000", "000001"], "name": ["Fixture Shanghai", "Fixture Shenzhen"]}
    )


def akshare_bars(
    symbol: str, adjust: str, start_date: str = "20240102", end_date: str = "20240630"
):
    if symbol not in {"600000", "000001"}:
        return pd.DataFrame()
    factor = {"": 1.0, "qfq": 0.9, "hfq": 1.1}[adjust]
    rows = []
    for i, day in enumerate(fixture_days("XSHG")):
        # Use the absolute fixed-window session index, never the request-relative
        # offset: retry/date-block overlaps must return exactly the same prices.
        if not start_date <= day.replace("-", "") <= end_date:
            continue
        if i < 4:
            # Keep the tiny Jan 2-5 endpoint contract samples unchanged.
            opening, high, low, close = 10 + i, 12 + i, 9 + i, 11 + i
        else:
            # Distinct deterministic synthetic paths exercise basket rebalancing.
            # Identical rising paths produced no completed trades in the original
            # two-listing cross seed. These are not historical price observations.
            direction = 1 if symbol == "600000" else -1
            close = round(14 + (i - 3) * 0.04 + direction * 3 * sin((i - 3) / 4), 4)
            opening = round(close - 0.2 * sin(i), 4)
            high, low = (
                round(max(opening, close) + 1, 4),
                round(min(opening, close) - 1, 4),
            )
        rows.append(
            {
                "日期": day,
                "股票代码": symbol,
                "开盘": opening * factor,
                "最高": high * factor,
                "最低": low * factor,
                "收盘": close * factor,
                "成交量": 1000 + i,
                "成交额": 1000000 + i,
            }
        )
    return pd.DataFrame(rows)


def yfinance_resolve(symbol: str):
    if symbol not in {"AAPL", "MSFT"}:
        return {"quotes": [], "metadata": {}}
    return {
        "quotes": [
            {"symbol": symbol, "shortname": f"Fixture {symbol}", "quoteType": "EQUITY"}
        ],
        "metadata": {
            "symbol": symbol,
            "currency": "USD",
            "exchangeName": "NMS",
            "exchangeTimezoneName": "America/New_York",
            "instrumentType": "EQUITY",
        },
    }


def yfinance_bars(symbol: str, start: str = "2024-01-02", end: str = "2024-07-01"):
    if symbol not in {"AAPL", "MSFT"}:
        return pd.DataFrame()
    days = [day for day in DAYS if start <= day < end]
    return pd.DataFrame(
        [
            {
                "Open": 100 + i,
                "High": 102 + i,
                "Low": 99 + i,
                "Close": 101 + i,
                "Adj Close": 100.5 + i,
                "Volume": 10000 + i,
                "Dividends": 0.25 if i == 1 else 0.0,
                "Stock Splits": 2.0 if i == 2 else 0.0,
            }
            for i, day in enumerate(DAYS)
            if day in days
        ],
        index=pd.DatetimeIndex(days, tz="America/New_York", name="Date"),
    )
