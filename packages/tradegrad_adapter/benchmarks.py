"""Immutable daily raw-close comparisons, computed outside candidate processes.

This is price change, not total return or a tradable/cost-adjusted portfolio.
The anchor is the first actual scored-session observation, never initial cash.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import math
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .config import BenchmarkConfig, DateWindow, canonical_bytes
from .windows import DEFAULT_LIMITS, _file_path, stage_csv_window

DEFINITION = "daily_close_observed_anchor.v1"
REASON = (
    "Raw close-price change, not total return: no dividends, split adjustment, "
    "benchmark trading costs or investability claim. Strategy observed equity includes "
    "its recorded engine costs; these are not cost-matched portfolios. Both returns "
    "start at the first observed scored-session value, not initial cash."
)


def _number(value, positive=False):
    if (
        type(value) not in (int, float)
        or abs(value) > 1.7976931348623157e308
        or not math.isfinite(value)
        or (positive and value <= 0)
    ):
        raise ValueError(
            "Benchmark requires finite values and a positive observed anchor"
        )
    return value


def _raw_semantics(manifest):
    semantics = manifest.get("price_semantics", {})
    if (
        not isinstance(semantics, dict)
        or semantics.get("split_basis") != "unadjusted"
        or semantics.get("dividend_basis") != "unadjusted"
        or not semantics.get("native_mode")
        or semantics["native_mode"] == "unknown"
        or manifest.get("quality", {}).get("unknown_flags")
        or manifest.get("evaluation_compatibility", {}).get("compatible") is False
        or manifest.get("request", {}).get("interval") != "1d"
    ):
        raise ValueError(
            "Benchmark comparison requires explicit compatible daily raw, unadjusted close prices on both datasets"
        )
    return semantics


def _sessions(manifest, listing, window):
    records = [
        r
        for r in manifest.get("coverage", [])
        if r.get("listing_id") == listing["listing_id"]
    ]
    if (
        len(records) != 1
        or records[0].get("calendar") != listing["calendar"]
        or not records[0].get("calendar_version")
    ):
        raise ValueError(
            "Benchmark requires versioned pinned calendar coverage for each listing"
        )
    coverage = records[0]
    values = coverage.get("expected_sessions", [])
    if (
        not isinstance(values, list)
        or len(values) > DEFAULT_LIMITS.max_rows
        or values != sorted(set(values))
    ):
        raise ValueError(
            "Benchmark calendar sessions must be bounded, unique and ordered"
        )
    sessions = [day for day in values if str(window.start) <= day < str(window.end)]
    if len(sessions) < 2 or any(
        date.fromisoformat(day).isoformat() != day for day in sessions
    ):
        raise ValueError(
            "Benchmark comparison requires at least two exact scored sessions"
        )
    if any(
        str(window.start) <= day < str(window.end)
        for day in coverage.get("provisional_sessions", [])
    ):
        raise ValueError("Benchmark cannot use provisional scored sessions")
    return sessions, coverage["calendar_version"]


def prepare_benchmark(
    reference,
    manifest,
    strategy_manifest,
    read_file,
    window,
    market,
    *,
    is_synthetic=None,
):
    """Read only selected listing; return exact scored-window inputs, never holdout implicitly."""
    reference = BenchmarkConfig.model_validate(reference)
    window = DateWindow.model_validate({k: window[k] for k in ("start", "end")})
    if (
        hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        != reference.dataset.sha256
    ):
        raise ValueError("Benchmark immutable manifest hash changed")
    semantics = _raw_semantics(manifest)
    _raw_semantics(strategy_manifest)
    selected = [
        r
        for r in manifest.get("listings", [])
        if r.get("listing_id") == reference.listing_id
    ]
    if len(selected) != 1:
        raise ValueError("Benchmark listing is absent or ambiguous in selected dataset")
    listing = selected[0]
    expected = {
        "currency": "CNY" if market == "CN" else "USD",
        "timezone": "Asia/Shanghai" if market == "CN" else "America/New_York",
        "calendar": "XSHG" if market == "CN" else "XNYS",
    }
    if any(listing.get(k) != value for k, value in expected.items()):
        raise ValueError(
            "Benchmark currency, timezone or calendar differs from strategy market"
        )
    sessions, calendar_version = _sessions(manifest, listing, window)
    if not strategy_manifest.get("listings"):
        raise ValueError(
            "Strategy dataset lacks explicit listing semantics for comparison"
        )
    for strategy_listing in strategy_manifest["listings"]:
        if any(strategy_listing.get(k) != value for k, value in expected.items()):
            raise ValueError(
                "Strategy listing semantics are incompatible with benchmark"
            )
        strategy_sessions, strategy_calendar = _sessions(
            strategy_manifest, strategy_listing, window
        )
        if strategy_sessions != sessions or strategy_calendar != calendar_version:
            raise ValueError(
                "Benchmark and strategy must use identical versioned scored-session calendars"
            )
    if market not in {"CN", "US"}:
        raise ValueError("Unsupported benchmark market")
    source_as_of = manifest.get("request", {}).get("as_of")
    if (
        not isinstance(source_as_of, str)
        or datetime.fromisoformat(source_as_of).tzinfo is None
    ):
        raise ValueError("Benchmark snapshot must declare an explicit as-of timestamp")
    symbol = manifest.get("evaluator_symbol_mapping", {}).get(reference.listing_id)
    name = f"data/{symbol}.csv"
    _file_path(name)
    digest = manifest.get("files", {}).get(name)
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Benchmark listing needs an immutable close-price file hash")
    staged = stage_csv_window(
        read_file(name),
        listing_id=reference.listing_id,
        window=window,
        warmup_sessions=0,
        expected_sha256=digest,
        expected_sessions=sessions,
    )
    rows = list(csv.DictReader(io.StringIO(staged.content.decode())))
    return {
        "reference": reference.model_dump(mode="json"),
        "source_file": name,
        "source_file_sha256": digest,
        "source_as_of": source_as_of,
        "source_classification": "synthetic"
        if is_synthetic is True
        else "not_declared_synthetic"
        if is_synthetic is False
        else "unclassified",
        "currency": expected["currency"],
        "timezone": expected["timezone"],
        "calendar": expected["calendar"],
        "calendar_version": calendar_version,
        "price_semantics": semantics,
        "window": window.model_dump(mode="json"),
        "observations": [[r["date"], float(r["close"])] for r in rows],
    }


def account_observations(bundle):
    if bundle.get("evaluation_mode") == "portable":
        return [(row["native_at"], row["equity"]) for row in bundle["account_series"]]
    return bundle["equity"]["rows"]


def build_comparison(prepared, account):
    zone = ZoneInfo(prepared["timezone"])
    observed = []
    for timestamp, equity in account:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            raise ValueError("Benchmark account timestamps must carry timezone")
        observed.append(
            (str(parsed.astimezone(zone).date()), timestamp, _number(equity))
        )
    prices = prepared["observations"]
    if len(prices) < 2 or [row[0] for row in observed] != [row[0] for row in prices]:
        raise ValueError(
            "Benchmark comparison requires exactly one observed account value per scored session; no gaps, duplicates, or forward fill"
        )
    first_equity, first_close = (
        _number(observed[0][2], True),
        _number(prices[0][1], True),
    )
    rows = []
    for (day, timestamp, equity), (price_day, close) in zip(
        observed, prices, strict=True
    ):
        _number(close, True)
        strategy_return, benchmark_return = (
            (equity / first_equity - 1) * 100,
            (close / first_close - 1) * 100,
        )
        _number(strategy_return)
        _number(benchmark_return)
        _number(strategy_return - benchmark_return)
        rows.append(
            [
                day,
                timestamp,
                equity,
                close,
                strategy_return,
                benchmark_return,
                strategy_return - benchmark_return,
            ]
        )
    return {
        "status": "available",
        "definition_version": DEFINITION,
        "reason": REASON,
        "input": prepared,
        "normalization": {
            "session": prices[0][0],
            "strategy_equity": first_equity,
            "benchmark_close": first_close,
        },
        "series": {
            "columns": [
                {"name": name, "dtype": dtype, "unit": unit}
                for name, dtype, unit in (
                    ("session_date", "date", "exchange_session"),
                    ("strategy_observed_at", "string", "timestamp_iso8601"),
                    ("strategy_equity", "number", prepared["currency"]),
                    ("benchmark_close", "number", prepared["currency"]),
                    ("strategy_observed_return", "number", "percent"),
                    ("benchmark_price_return", "number", "percent"),
                    ("excess_observed_return", "number", "percentage_points"),
                )
            ],
            "rows": rows,
        },
        "metrics": [
            {
                "name": name,
                "value": rows[-1][index],
                "unit": unit,
                "definition_version": DEFINITION,
                "calculation_basis": formula,
            }
            for name, index, unit, formula in (
                (
                    "strategy_observed_return",
                    4,
                    "percent",
                    "100 * (last observed equity / first observed equity - 1)",
                ),
                (
                    "benchmark_price_return",
                    5,
                    "percent",
                    "100 * (last scored close / first scored close - 1); raw price change only",
                ),
                (
                    "excess_observed_return",
                    6,
                    "percentage_points",
                    "strategy_observed_return - benchmark_price_return; not annualized or cost matched",
                ),
            )
        ],
    }


def validate_comparison(value, bundle):
    """Strict, mathematical reconstruction; original snapshot binding is checked by API loader."""
    if not isinstance(value, dict) or value.get("status") != "available":
        raise ValueError("Invalid benchmark comparison")
    prepared = value.get("input")
    required = {
        "reference",
        "source_file",
        "source_file_sha256",
        "source_as_of",
        "source_classification",
        "currency",
        "timezone",
        "calendar",
        "calendar_version",
        "price_semantics",
        "window",
        "observations",
    }
    if not isinstance(prepared, dict) or set(prepared) != required:
        raise ValueError("Invalid benchmark evidence schema")
    BenchmarkConfig.model_validate(prepared["reference"])
    _file_path(prepared["source_file"])
    if (
        not isinstance(prepared["source_file_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", prepared["source_file_sha256"]) is None
        or not isinstance(prepared["calendar_version"], str)
        or not 1 <= len(prepared["calendar_version"]) <= 500
        or prepared["source_classification"]
        not in {"synthetic", "not_declared_synthetic", "unclassified"}
        or not isinstance(prepared["source_as_of"], str)
        or datetime.fromisoformat(prepared["source_as_of"]).tzinfo is None
    ):
        raise ValueError("Invalid benchmark immutable source evidence")
    semantics = prepared["price_semantics"]
    _raw_semantics({"price_semantics": semantics, "request": {"interval": "1d"}})
    market = bundle.get("manifest", {}).get(
        "market", bundle.get("engine", {}).get("currency")
    )
    cn = market in {"CN", "CNY"}
    if (
        prepared["currency"] != ("CNY" if cn else "USD")
        or prepared["timezone"] != ("Asia/Shanghai" if cn else "America/New_York")
        or prepared["calendar"] != ("XSHG" if cn else "XNYS")
    ):
        raise ValueError("Benchmark result market semantics differ")
    if prepared["window"] != {k: bundle["window"][k] for k in ("start", "end")}:
        raise ValueError("Benchmark scored window differs from result")
    observations = prepared["observations"]
    if (
        not isinstance(observations, list)
        or not 2 <= len(observations) <= DEFAULT_LIMITS.max_rows
        or any(
            not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str)
            for row in observations
        )
    ):
        raise ValueError("Invalid benchmark close observations")
    for day, close in observations:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("Invalid benchmark session date")
        _number(close, True)
    days = [r[0] for r in prepared["observations"]]
    if (
        days != sorted(set(days))
        or len(days) > DEFAULT_LIMITS.max_rows
        or any(
            not prepared["window"]["start"] <= day < prepared["window"]["end"]
            for day in days
        )
    ):
        raise ValueError(
            "Benchmark observations must be unique ordered scored sessions"
        )
    if value != build_comparison(prepared, account_observations(bundle)):
        raise ValueError(
            "Benchmark comparison does not reconstruct from observed values"
        )
    return value


def attach_comparison(bundle, prepared):
    """A new result, without mutating runner evidence or historical None bundles."""
    if prepared is None:
        return bundle
    result = copy.deepcopy(bundle)
    result["benchmark"] = build_comparison(prepared, account_observations(bundle))
    if result.get("evaluation_mode") == "native_akquant":
        from .results import bundle_sections, canonical_json_bytes

        result["capabilities"]["benchmark"] = True
        result["artifacts"] = [
            {
                "section": name,
                "media_type": "application/json",
                "sha256": hashlib.sha256(
                    canonical_json_bytes(result[name])
                ).hexdigest(),
                "bytes": len(canonical_json_bytes(result[name])),
            }
            for name in bundle_sections(result)
        ]
    return result


def require_comparison_binding(bundle, config, *, allow_pending=False):
    source = getattr(config, "source_config", config)
    reference = source.benchmark
    value = bundle.get("benchmark")
    if reference is None:
        if value is not None and value.get("status") != "not_configured":
            raise ValueError("Unrequested benchmark evidence is forbidden")
    elif allow_pending and (value is None or value.get("status") == "not_configured"):
        return
    elif (
        not value
        or value.get("status") != "available"
        or value["input"]["reference"] != reference.model_dump(mode="json")
    ):
        raise ValueError("Benchmark result differs from immutable selected input")
