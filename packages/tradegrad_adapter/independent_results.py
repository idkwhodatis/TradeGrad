"""Native observation admission separate from unchanged optimization eligibility.

Only a trusted staged independent request may select these versioned loaders.
A successful zero-trade backtest is an observation, never a zero reward candidate.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import datetime

from .results import (
    MAX_WIRE_BYTES,
    _finite_number,
    _hash,
    _keys,
    _scalar,
    _sha,
    _unique_pairs,
    _validate_result_bundle,
    canonical_json_bytes,
)
from .results import (
    export_native_result as _export_native_result,
)

RESULT_POLICY = "independent_observation.v1"
BUNDLE_VERSION = "tradegrad.result-bundle.v2"
ENVELOPE_VERSION = "tradegrad.evaluator-envelope.v3"
RESULT_PREFIX_V3 = "TRADEGRAD_RESULT_V3:"
METRICS = {
    "sharpe_ratio",
    "annualized_return",
    "max_drawdown",
    "trade_per_day",
    "win_rate",
    "return_per_trade",
}


def validate_performance(value):
    _keys(
        value,
        {"combined_score", "metrics", "market", "profile", "reward_status"},
        "independent performance",
    )
    _keys(value["metrics"], METRICS, "independent metrics")
    if (
        value["combined_score"] is not None
        or value["reward_status"] != "not_applicable"
        or value["market"] not in ("CN", "US")
        or value["profile"] not in ("source_compat", "corrected_explicit_config")
        or any(
            v is not None and not _finite_number(v) for v in value["metrics"].values()
        )
    ):
        raise ValueError("Invalid independent performance")
    if (
        value["metrics"]["trade_per_day"] is not None
        and value["metrics"]["trade_per_day"] < 0
    ):
        raise ValueError("Invalid independent trade frequency")
    return value


def observation_performance(result, *, market, profile):
    native = result.metrics_df["value"]
    days, trades = native["duration"].days, native["closed_trade_count"]
    metrics = {
        "sharpe_ratio": _scalar(native["sharpe_ratio"]),
        "annualized_return": _scalar(native["annualized_return"] * 100),
        "max_drawdown": _scalar(native["max_drawdown_pct"]),
        "trade_per_day": None if days == 0 else float(trades) / (days / 365 * 243),
        "win_rate": None if trades == 0 else _scalar(native["win_rate"]),
        "return_per_trade": None if trades == 0 else _scalar(native["avg_return_pct"]),
    }
    return validate_performance(
        {
            "combined_score": None,
            "metrics": {
                k: None if v is None else round(v, 2) for k, v in metrics.items()
            },
            "market": market,
            "profile": profile,
            "reward_status": "not_applicable",
        }
    )


def export_native_result(result, performance, **kwargs):
    validate_performance(performance)
    return _export_native_result(result, performance, _independent=True, **kwargs)


def validate_result_bundle(bundle):
    _validate_result_bundle(bundle, independent=True)
    native = {item["name"]: item["value"] for item in bundle["native_metrics"]}
    required = {
        "duration",
        "closed_trade_count",
        "sharpe_ratio",
        "annualized_return",
        "max_drawdown_pct",
        "win_rate",
        "avg_return_pct",
    }
    if (
        not required.issubset(native)
        or not _finite_number(native["duration"])
        or native["duration"] < 0
    ):
        raise ValueError("Independent native metric basis is incomplete")
    days = native["duration"] // 86_400_000_000_000
    trades = native["closed_trade_count"]
    expected = {
        "sharpe_ratio": native["sharpe_ratio"],
        "annualized_return": None
        if native["annualized_return"] is None
        else native["annualized_return"] * 100,
        "max_drawdown": native["max_drawdown_pct"],
        "trade_per_day": None if days == 0 else trades / (days / 365 * 243),
        "win_rate": None if trades == 0 else native["win_rate"],
        "return_per_trade": None if trades == 0 else native["avg_return_pct"],
        "combined_score": None,
    }
    for item in bundle["metrics"]:
        actual, wanted = item["value"], expected[item["name"]]
        if (
            (actual is None) != (wanted is None)
            or wanted is not None
            and not math.isclose(actual, wanted, rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise ValueError(
                "Independent metrics disagree with native observation basis"
            )

    def rows(name):
        table = bundle[name]
        names = [column["name"] for column in table["columns"]]
        return [dict(zip(names, row)) for row in table["rows"]]

    orders = {row["id"]: row for row in rows("orders")}
    quantities = {key: 0.0 for key in orders}
    for fill in rows("fills"):
        order = orders.get(fill["order_id"])
        if (
            order is None
            or any(fill[key] != order[key] for key in ("symbol", "side"))
            or not _finite_number(fill["price"])
            or fill["price"] <= 0
            or datetime.fromisoformat(fill["timestamp"])
            < datetime.fromisoformat(order["created_at"])
        ):
            raise ValueError(
                "Independent fill lacks a consistent explicit order association"
            )
        quantities[fill["order_id"]] += fill["quantity"]
    for key, order in orders.items():
        filled = order["filled_quantity"]
        if (
            not _finite_number(filled)
            or not 0 <= filled <= order["quantity"]
            or not math.isclose(filled, quantities[key], rel_tol=1e-12, abs_tol=1e-8)
        ):
            raise ValueError(
                "Independent native filled quantity disagrees with executions"
            )
    return bundle


def validate_envelope(data):
    if not isinstance(data, bytes) or len(data) > MAX_WIRE_BYTES:
        raise ValueError("Result protocol exceeded its byte budget")
    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite JSON")
            ),
        )
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Independent result JSON complexity exceeds limit") from exc
    _keys(
        value,
        {"schema_version", "metrics", "bundle", "bundle_sha256"},
        "independent envelope",
    )
    if value["schema_version"] != ENVELOPE_VERSION:
        raise ValueError("Unsupported independent result envelope")
    validate_result_bundle(value["bundle"])
    validate_performance(value["metrics"])
    if not _hash(value["bundle_sha256"]) or value["bundle_sha256"] != _sha(
        value["bundle"]
    ):
        raise ValueError("Independent ResultBundle envelope hash mismatch")
    if value["metrics"] != value["bundle"]["legacy_performance"]["value"]:
        raise ValueError("Independent metrics and ResultBundle disagree")
    return value


def make_envelope(metrics, bundle):
    value = {
        "schema_version": ENVELOPE_VERSION,
        "metrics": metrics,
        "bundle": bundle,
        "bundle_sha256": _sha(bundle),
    }
    data = canonical_json_bytes(value)
    validate_envelope(data)
    return data


def wire_record(metrics, bundle):
    return RESULT_PREFIX_V3 + base64.b64encode(make_envelope(metrics, bundle)).decode(
        "ascii"
    )
