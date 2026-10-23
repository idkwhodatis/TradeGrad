"""Pure offline CSV generator; never fetch prices, call an SDK, or run a strategy.

Requires the separately locked data runtime's exchange-calendars==4.13.2.
Regeneration must exactly match the committed profile hash; it cannot silently
refresh the engineering reference. Dates are real calendar sessions, prices are
fully synthetic deterministic Decimal arithmetic, not market evidence.
"""

import csv
import hashlib
import io
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def generate(sessions):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "open", "high", "low", "close", "volume", "amount"])
    for index, day in enumerate(sessions):
        close = (
            Decimal(60) + Decimal(index // 240) + Decimal(20 - abs(index % 80 - 40)) / 4
        )
        opened, volume = close - Decimal("0.10"), 1000000 + index * 100
        writer.writerow(
            [
                day,
                format(opened, ".2f"),
                format(close + Decimal("0.30"), ".2f"),
                format(opened - Decimal("0.30"), ".2f"),
                format(close, ".2f"),
                volume,
                format(close * volume, ".2f"),
            ]
        )
    return output.getvalue().encode()


def main():
    import exchange_calendars as xc

    profile = json.loads(
        (ROOT / "tests/performance/fixtures/cn-daily-six-year-v1.json").read_text()
    )
    if xc.__version__ != profile["calendar_version"]:
        raise ValueError(
            "Pinned calendar version required; do not refresh the reference"
        )
    calendar = xc.get_calendar("XSHG", start="2019-01-01", end="2025-01-07")
    sessions = [
        day.date().isoformat()
        for day in calendar.sessions
        if profile["start"] <= day.date().isoformat() < profile["end"]
    ]
    content = generate(sessions)
    if (
        len(sessions) != profile["rows"]
        or len(content) != profile["csv_bytes"]
        or hashlib.sha256(content).hexdigest() != profile["csv_sha256"]
        or hashlib.sha256(("\n".join(sessions) + "\n").encode()).hexdigest()
        != profile["session_dates_sha256"]
    ):
        raise ValueError("Generated content does not match the frozen reference")
    (ROOT / "tests/performance/fixtures/cn-daily-six-year-v1.csv").write_bytes(content)
    print("Frozen synthetic six-year reference reproduced exactly")


if __name__ == "__main__":
    main()
