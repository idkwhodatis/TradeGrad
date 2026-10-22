"""Regenerate the committed synthetic M0 data. No network, credentials, or market-data claims."""
import csv
import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "tests/baseline/fixtures"


def main():
    days = []
    day = date(2024, 1, 2)
    while len(days) < 84:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    for market in ("CN", "US"):
        symbols = [f"sh.{600000+i}" if i % 2 == 0 else f"sz.{i:06}" for i in range(40)] if market == "CN" else [f"FIX{i:02}" for i in range(40)]
        for i, symbol in enumerate(symbols + ["INDEX"]):
            path = ROOT / market / f"{symbol}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            fields = ["date", "open", "high", "low", "close", "volume"] + (["amount", "turn"] if market == "CN" else [])
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
                writer.writeheader()
                for t, day in enumerate(days):
                    close = round(30 + i * 0.7 + 0.04*t + 3*math.sin(t*0.35+i*0.5), 4)
                    opening = round(close + 0.12*math.cos(t*0.3+i), 4)
                    row = dict(date=day, open=opening, high=round(max(opening,close)+0.3,4), low=round(min(opening,close)-0.3,4), close=close, volume=1000000+1000*t)
                    if market == "CN":
                        row.update(amount=round(close*row["volume"],2), turn=1.5)
                    writer.writerow(row)
        universe = ROOT / f"{market}_list"
        universe.mkdir(exist_ok=True)
        with (universe / "fixture_list_2024-01.csv").open("w", newline="") as handle:
            writer=csv.writer(handle, lineterminator="\n")
            writer.writerow(["updateDate", "code", "code_name"])
            writer.writerows([[days[0], symbol, f"Synthetic {symbol}"] for symbol in symbols])
    manifest = {"schema_version": 1, "description": "Synthetic business-day OHLCV for engine smoke tests, not actual trading sessions or investment data", "source_commit": "baf73ee56353de8996731139f6721b62f4b26da7", "start": days[0], "end": days[-1], "bars_per_symbol": len(days), "symbols_per_market": 40, "files": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(ROOT.rglob("*.csv"))}}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")


if __name__ == "__main__":
    main()
