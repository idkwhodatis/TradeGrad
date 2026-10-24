"""CSV staging -> production evaluator -> actual pinned-engine session evidence."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAYS = {
    "january": (
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
        "2024-01-05",
        "2024-01-08",
        "2024-01-09",
        "2024-01-10",
        "2024-01-11",
    ),
    "spring": (
        "2024-03-05",
        "2024-03-06",
        "2024-03-07",
        "2024-03-08",
        "2024-03-11",
        "2024-03-12",
        "2024-03-13",
        "2024-03-14",
    ),
    "fall": (
        "2024-10-29",
        "2024-10-30",
        "2024-10-31",
        "2024-11-01",
        "2024-11-04",
        "2024-11-05",
        "2024-11-06",
        "2024-11-07",
    ),
}


def execute(name, market, days):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Native session tests require the pinned interpreter")
        pytest.skip("Actual native session tests require a pinned interpreter")
    process = subprocess.run(
        [
            python,
            str(ROOT / "tests/research_trace/native_session_fixture.py"),
            json.dumps([name, market, days]),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "PYTHONHASHSEED": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr + process.stdout[-2000:]
    return json.loads(
        next(
            line.removeprefix("SESSION_RESULT:")
            for line in process.stdout.splitlines()
            if line.startswith("SESSION_RESULT:")
        )
    )


def features(record):
    return {
        (feature["instrument_id"], feature["feature_id"]): feature
        for stage in record["stages"]
        for feature in stage["features"]
    }


def without_generated_ids(value):
    ignored = {
        "decision_id",
        "id",
        "order_id",
        "trade_id",
        "execution_id",
        "child_order_ids",
    }
    if isinstance(value, dict):
        return {
            k: without_generated_ids(v) for k, v in value.items() if k not in ignored
        }
    if isinstance(value, list):
        return [without_generated_ids(v) for v in value]
    return value


@pytest.mark.parametrize("name", ["daily_timing", "cross_sectional"])
@pytest.mark.parametrize("market", ["CN", "US"])
@pytest.mark.parametrize("season", DAYS)
def test_real_csv_source_callbacks_trace_and_economics_keep_market_sessions(
    name, market, season
):
    days = DAYS[season]
    runs = execute(name, market, days)
    actual, legacy = runs["corrected"], runs["legacy"]
    records = actual["records"]
    zone = ZoneInfo(runs["zone"])
    expected = [datetime.fromisoformat(day).replace(tzinfo=zone) for day in days[2:]]
    stamps = [datetime.fromisoformat(row["decision_time"]) for row in records]
    assert stamps == expected
    assert len(records) == len({row["data_event_id"] for row in records}) == 6
    assert [
        datetime.fromisoformat(value) for value in actual["equity_times"]
    ] == expected
    assert actual["zone"] == runs["zone"]
    for row, stamp in zip(records, expected):
        assert datetime.fromisoformat(row["information_cutoff"]) == stamp
        assert row["market_timezone"] == runs["zone"]
        assert row["data_event_id"] == (
            f"daily_slice:{int(stamp.timestamp()) * 1_000_000_000}"
            if name == "cross_sectional"
            else f"daily_bar:{int(stamp.timestamp()) * 1_000_000_000}:target_symbol"
        )
        assert all(
            datetime.fromisoformat(f["information_cutoff"]) == stamp
            for f in features(row).values()
        )
    if market == "US" and season != "january":
        assert expected[0].utcoffset() != expected[-1].utcoffset()
    if market == "CN":
        assert len({stamp.utcoffset() for stamp in expected}) == 1
        assert all(
            stamp.astimezone(ZoneInfo("UTC")).date() < stamp.date()
            for stamp in expected
        )

    first = features(records[0])
    if name == "cross_sectional":
        assert (
            len(first) == 8
        )  # All four symbols, one complete event, two features each.
        assert first[("B", "trailing_return")]["value"] == pytest.approx(0.2)
        assert first[("C", "trailing_return")]["value"] == pytest.approx(0.5)
        assert first[("C", "average_dollar_volume")]["value"] == pytest.approx(370 / 3)
        # Membership changes apply on the source session itself, not yesterday.
        universe = [
            p
            for s in records[3]["stages"]
            for p in s["predicates"]
            if p["condition_id"] == "universe_membership"
        ]
        assert {p["instrument_id"]: p["state"] for p in universe} == {
            "A": "true",
            "B": "false",
            "C": "true",
            "D": "true",
        }
        assert (
            next(
                p["state"]
                for s in records[2]["stages"]
                for p in s["predicates"]
                if p["condition_id"] == "universe_membership"
                and p["instrument_id"] == "B"
            )
            == "true"
        )
    else:
        assert first[("target_symbol", "fast_mean")]["value"] == 10.5
        assert first[("target_symbol", "slow_mean")]["value"] == 11

    # Staging supplies exactly two prior rows and excludes its end-exclusive bar.
    for content in actual["staged"].values():
        assert [row["date"] for row in csv.DictReader(io.StringIO(content))] == list(
            days
        )
        assert all(
            float(row["close"]) != 999 for row in csv.DictReader(io.StringIO(content))
        )
    assert all(f["validity"] == "valid" for f in first.values())
    cold = runs["cold"]["records"]
    assert [r["decision_time"] for r in cold] == [r["decision_time"] for r in records]
    assert all(
        f["validity"] == "warmup_insufficient" for f in features(cold[0]).values()
    )
    assert all(f["validity"] == "valid" for f in features(cold[2]).values())

    assert actual["economics"] == runs["off"]["economics"]
    assert runs["off"]["records"] == []
    assert without_generated_ids(records[:-1]) == without_generated_ids(
        runs["future"]["records"][:-1]
    )
    assert features(records[-1]) != features(runs["future"]["records"][-1])
    # The explicitly historical profile is byte/value-equivalent to the old
    # raw-loader + engine path, including its original implicit timezone.
    assert legacy["economics"] == runs["legacy_reference"]["economics"]
    assert without_generated_ids(legacy["records"]) == without_generated_ids(
        runs["legacy_reference"]["records"]
    )
