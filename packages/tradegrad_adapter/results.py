"""Versioned, bounded native AKQuant snapshots, not an independently trusted ledger.

Only export_native_result imports the research runtime. Validators and canonical
serialization are standard-library-only so the coordinator never imports a
candidate, AKQuant, pandas, pickle, or an engine object.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

BUNDLE_VERSION = "tradegrad.result-bundle.v1"
ENVELOPE_VERSION = "tradegrad.evaluator-envelope.v2"
RESULT_PREFIX_V2 = "TRADEGRAD_RESULT_V2:"
MAX_WIRE_BYTES = 8 * 1_048_576
TRUST = "native_strategy_and_engine_share_process"
ENGINE_VERSION = "0.3.27"
ENGINE_FIELDS = frozenset(
    {
        "initial_cash",
        "commission_rate",
        "stamp_tax_rate",
        "slippage",
        "history_depth",
        "t_plus_one",
        "risk_config",
        "timezone",
        "lot_size",
    }
)
SECTIONS = (
    "identity",
    "window",
    "engine",
    "metrics",
    "native_metrics",
    "legacy_performance",
    "equity",
    "drawdown",
    "cash",
    "margin",
    "positions",
    "orders",
    "fills",
    "closed_trades",
    "fees",
    "benchmark",
    "decision_trace",
    "model_usage",
    "capabilities",
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def _keys(value: Any, keys: set[str], label: str):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"Invalid {label} schema")


def _json_tree(value: Any, depth: int = 0):
    if depth > 32:
        raise ValueError("Result JSON nesting exceeds limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_tree(item, depth + 1)
        return
    if type(value) is dict and all(isinstance(k, str) for k in value):
        for item in value.values():
            _json_tree(item, depth + 1)
        return
    raise ValueError("Result contains a non-JSON or nonfinite value")


def validate_engine_overrides(value: dict) -> dict:
    """Reject silently ignored kwargs; the immutable RunConfig performs policy validation."""
    if not isinstance(value, dict) or set(value) - ENGINE_FIELDS:
        raise ValueError("Unsupported evaluation engine override")
    for key in ("initial_cash", "commission_rate", "stamp_tax_rate"):
        if key in value and (
            not _finite_number(value[key])
            or value[key] < 0
            or (key == "initial_cash" and value[key] == 0)
        ):
            raise ValueError(f"Invalid engine override: {key}")
    for key in ("history_depth", "lot_size"):
        if key in value and (
            type(value[key]) is not int or not 1 <= value[key] <= 100000
        ):
            raise ValueError(f"Invalid engine override: {key}")
    if "t_plus_one" in value and type(value["t_plus_one"]) is not bool:
        raise ValueError("Invalid t_plus_one")
    if "timezone" in value:
        from zoneinfo import ZoneInfo

        if not isinstance(value["timezone"], str):
            raise ValueError("Invalid timezone")
        try:
            ZoneInfo(value["timezone"])
        except (KeyError, ValueError) as exc:
            raise ValueError("Invalid timezone") from exc
    if "slippage" in value:
        item = value["slippage"]
        _keys(item, {"type", "value"}, "slippage")
        if (
            item["type"] != "percent"
            or not _finite_number(item["value"])
            or not 0 <= item["value"] < 1
        ):
            raise ValueError("Unsupported slippage")
    if "risk_config" in value:
        item = value["risk_config"]
        _keys(item, {"account_mode", "enable_short_sell"}, "risk_config")
        if (
            item["account_mode"] not in ("cash", "margin")
            or type(item["enable_short_sell"]) is not bool
        ):
            raise ValueError("Unsupported risk_config")
        if item["enable_short_sell"] and item["account_mode"] != "margin":
            raise ValueError("Short selling requires margin account mode")
    _json_tree(value)
    return json.loads(canonical_json_bytes(value))


def artifact_inventory(root: Path) -> list[dict]:
    """Hash exact staged bytes; these attest content, never execution trust."""
    root = Path(root)
    items = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in evaluation inputs")
        if path.is_file():
            data = path.read_bytes()
            items.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": sha256_bytes(data),
                    "bytes": len(data),
                }
            )
    return items


def _scalar(value: Any) -> Any:
    # Engine-facing only: normalize numpy/pandas scalar representations losslessly.
    import pandas as pd

    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, pd.Timedelta):
        return value.value  # integer nanoseconds, not lossy total_seconds()
    if isinstance(value, timedelta):
        return (
            value.days * 86400 + value.seconds
        ) * 1_000_000_000 + value.microseconds * 1000
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if type(value) is float and not math.isfinite(value):
        return None
    if type(value) in (str, bool, int, float):
        return value
    raise ValueError(f"Unsupported native result scalar: {type(value).__name__}")


UNITS = {
    "quantity": "shares",
    "filled_quantity": "shares",
    "long_shares": "shares",
    "short_shares": "shares",
    "price": "currency_per_share",
    "close": "currency_per_share",
    "entry_price": "currency_per_share",
    "exit_price": "currency_per_share",
    "avg_price": "currency_per_share",
    "limit_price": "currency_per_share",
    "stop_price": "currency_per_share",
    "commission": "account_currency",
    "equity": "account_currency",
    "cash": "account_currency",
    "margin": "account_currency",
    "market_value": "account_currency",
    "unrealized_pnl": "account_currency",
    "pnl": "account_currency",
    "net_pnl": "account_currency",
    "filled_value": "account_currency",
    "entry_portfolio_value": "account_currency",
    "duration": "nanoseconds",
    "duration_bars": "bars",
    "bar_index": "bars",
    "return_pct": "percent",
    "max_drawdown_pct": "percent",
    "mae": "percent",
    "mfe": "percent",
    "created_at": "timestamp_iso8601",
    "updated_at": "timestamp_iso8601",
    "timestamp": "timestamp_iso8601",
    "date": "timestamp_iso8601",
    "entry_time": "timestamp_iso8601",
    "exit_time": "timestamp_iso8601",
    "drawdown": "fraction",
}


def _table(frame: Any) -> dict:
    return {
        "columns": [
            {
                "name": str(name),
                "dtype": str(frame[name].dtype),
                "unit": UNITS.get(str(name), "label"),
            }
            for name in frame.columns
        ],
        "rows": [
            [_scalar(v) for v in row]
            for row in frame.itertuples(index=False, name=None)
        ],
    }


def _series(series: Any, name: str) -> dict:
    frame = series.rename(name).rename_axis("timestamp").reset_index()
    return _table(frame)


def _metric(
    name: str,
    value: Any,
    unit: str,
    definition: str,
    *,
    version: str = "tradegrad.metrics.v1",
) -> dict:
    scalar = _scalar(value)
    return {
        "name": name,
        "value": scalar,
        "unit": unit,
        "definition_version": version,
        "calculation_basis": definition,
        "unavailable_reason": "native_nonfinite_or_missing" if scalar is None else None,
    }


def _native_metric_unit(name: str) -> str:
    if name in ("start_time", "end_time"):
        return "timestamp_iso8601"
    if name == "duration":
        return "nanoseconds"
    if name.endswith("_pct") or name in ("win_rate", "loss_rate"):
        return "percent"
    if name in (
        "initial_market_value",
        "end_market_value",
        "total_pnl",
        "unrealized_pnl",
        "total_profit",
        "total_loss",
        "total_commission",
        "max_drawdown_value",
        "avg_pnl",
        "avg_profit",
        "avg_loss",
        "largest_win",
        "largest_loss",
        "std_error",
    ):
        return "account_currency"
    if name.endswith("_count") or name in (
        "winning_trades",
        "losing_trades",
        "max_wins",
        "max_losses",
    ):
        return "count"
    if name.endswith("_bars") or name == "total_bars":
        return "bars"
    if name in (
        "annualized_return",
        "max_drawdown",
        "volatility",
        "var_95",
        "var_99",
        "cvar_95",
        "cvar_99",
    ):
        return "fraction"
    return "dimensionless_native"


def export_native_result(
    result: Any,
    legacy_performance: dict,
    *,
    identity: dict,
    window: dict,
    engine_kwargs: dict,
    currency: str,
    reward_parameters: dict | None = None,
) -> dict:
    """Snapshot actual pinned object APIs while the native result is still alive."""
    from importlib.metadata import version

    if version("akquant") != ENGINE_VERSION:
        raise ValueError("ResultBundle exporter requires pinned AKQuant 0.3.27")
    native = result.metrics_df["value"]
    duration_days = native["duration"].days
    frequency = float(native["closed_trade_count"]) / (duration_days / 365 * 243)
    metrics = [
        _metric(
            "sharpe_ratio",
            native["sharpe_ratio"],
            "dimensionless",
            "AKQuant 0.3.27 metrics_df.sharpe_ratio; native engine statistic, not independently recomputed.",
        ),
        _metric(
            "annualized_return",
            native["annualized_return"] * 100,
            "percent",
            "AKQuant 0.3.27 metrics_df.annualized_return (fraction) multiplied by 100; before legacy rounding.",
        ),
        _metric(
            "max_drawdown",
            native["max_drawdown_pct"],
            "percent",
            "AKQuant 0.3.27 metrics_df.max_drawdown_pct; before legacy rounding.",
        ),
        _metric(
            "trade_per_day",
            frequency,
            "closed_trades_per_estimated_trading_day",
            "closed_trade_count / (duration.days / 365 * 243); duration.days truncates fractional days, matching original reward.",
        ),
        _metric(
            "win_rate",
            native["win_rate"],
            "percent",
            "AKQuant 0.3.27 metrics_df.win_rate; before legacy rounding.",
        ),
        _metric(
            "return_per_trade",
            native["avg_return_pct"],
            "percent",
            "AKQuant 0.3.27 metrics_df.avg_return_pct; before legacy rounding.",
        ),
        _metric(
            "combined_score",
            legacy_performance["combined_score"],
            "score_0_100",
            "Legacy geometric logistic return/drawdown reward with trade-frequency gate. Six inputs rounded to 2 decimal places before reward; score rounded to 2 decimal places. Parameters stored in legacy_performance.reward_parameters.",
        ),
    ]
    equity = result.equity_curve
    running_max = equity.cummax()
    drawdown = 1 - equity / running_max.where(running_max != 0)
    orders, fills = result.orders_df, result.executions_df
    fees = {
        "unit": currency,
        "definition_version": "akquant.0.3.27.execution_commission",
        "total_from_fills": float(fills["commission"].sum())
        if "commission" in fills.columns
        else 0.0,
        "native_total_commission": _scalar(native["total_commission"]),
        "calculation_basis": "sum(executions_df.commission); retained separately from native metrics_df.total_commission. No additional fee is inferred or added.",
        "commission_stamp_transfer_breakdown": None,
        "breakdown_status": "unsupported_by_execution_report",
        "slippage_cost": None,
        "slippage_cost_status": "not_separately_reported_by_native_engine",
    }
    # The actual engine policy is exported, not replaced with an assumed daily policy.
    engine = {
        "name": "akquant",
        "version": ENGINE_VERSION,
        "requested_kwargs": engine_kwargs,
        "resolved_execution_policy": result.resolved_execution_policy,
        "resolved_config": result.resolved_config,
        "currency": currency,
        "dependencies": {
            name: version(name) for name in ("akquant", "numpy", "pandas")
        },
        "exporter_version": BUNDLE_VERSION,
        "series_semantics": {
            "equity": "AKQuant BacktestResult.equity_curve including its documented native snapshot/PnL fallback behavior; not a separate ledger.",
            "drawdown": "Positive fraction 1 - equity / cumulative_max(equity); null when the running peak is zero. No synthetic initial bar is inserted.",
            "positions": "Native positions_df snapshots only; missing rows are not silently filled or claimed to be zero positions.",
            "fills": "executions_df execution reports, not closed trades or order quantities.",
            "closed_trades": "trades_df closed-trade records; kept separate from individual executions.",
            "timestamps": "ISO 8601 with the native result timezone offset; durations are integer nanoseconds.",
        },
    }
    order_columns = list(orders.columns)
    trace = {
        "scope": "observable_native_orders_only",
        "portable_contract": False,
        "input_snapshot_sha256": identity.get("input_manifest_sha256"),
        "input_snapshot_scope": "staged_input_artifacts_not_asof_strategy_view",
        "strategy_sha256": identity["strategy_sha256"],
        "decisions": [],
        "scores": None,
        "signals": None,
        "target_portfolio": None,
        "state_digest": None,
        "unobservable_reason": "Arbitrary native strategy internal intent/state is not exposed by the AKQuant result API.",
    }
    for index, row in enumerate(orders.itertuples(index=False, name=None)):
        item = dict(zip(order_columns, row))
        trace["decisions"].append(
            {
                "observed_at": _scalar(item["created_at"]),
                "order_id": _scalar(item["id"]),
                "orders_row": index,
                "reject_reason": _scalar(item.get("reject_reason")),
            }
        )
    bundle = {
        "schema_version": BUNDLE_VERSION,
        "evaluation_mode": "native_akquant",
        "result_trust": TRUST,
        "identity": identity,
        "window": window,
        "engine": engine,
        "metrics": metrics,
        "native_metrics": [
            _metric(
                str(name),
                value,
                _native_metric_unit(str(name)),
                f"AKQuant 0.3.27 metrics_df.{name}; engine-defined statistic, not independently recomputed.",
                version="akquant.0.3.27.metrics_df",
            )
            for name, value in native.items()
        ],
        "legacy_performance": {
            "value": legacy_performance,
            "reward_parameters": reward_parameters or {},
            "rounding": "Six numeric inputs round(value, 2) before reward; combined_score round(value, 2). Display rounding must not change objective.",
        },
        "equity": _series(equity, "equity"),
        "drawdown": _series(drawdown, "drawdown"),
        "cash": _series(result.cash_curve, "cash"),
        "margin": _series(result.margin_curve, "margin"),
        "positions": _table(result.positions_df),
        "orders": _table(orders),
        "fills": _table(fills),
        "closed_trades": _table(result.trades_df),
        "fees": fees,
        "benchmark": {
            "status": "not_configured",
            "series": None,
            "reason": "No separate benchmark input was provided. Strategy data is not silently substituted for a benchmark.",
        },
        "decision_trace": trace,
        "model_usage": {
            "status": "not_applicable_to_evaluator",
            "calls": [],
            "cost": None,
        },
        "capabilities": {
            "equity": True,
            "drawdown": True,
            "positions": True,
            "orders": True,
            "fills": True,
            "closed_trades": True,
            "fees_aggregate": True,
            "fee_component_breakdown": False,
            "benchmark": False,
            "observable_order_trace": True,
            "internal_decisions": False,
            "trusted_ledger": False,
        },
    }
    bundle["artifacts"] = [
        {
            "section": name,
            "media_type": "application/json",
            "sha256": _sha(bundle[name]),
            "bytes": len(canonical_json_bytes(bundle[name])),
        }
        for name in SECTIONS
    ]
    validate_result_bundle(bundle)
    return bundle


def _validate_table(table: Any):
    _keys(table, {"columns", "rows"}, "native table")
    if not isinstance(table["columns"], list) or not isinstance(table["rows"], list):
        raise ValueError("Invalid native table shape")  # noqa: TRY004 - wire validation
    names = []
    for column in table["columns"]:
        _keys(column, {"name", "dtype", "unit"}, "native table column")
        if not all(isinstance(v, str) and v for v in column.values()):
            raise ValueError("Invalid native column metadata")
        names.append(column["name"])
    if len(names) != len(set(names)) or any(
        not isinstance(row, list) or len(row) != len(names) for row in table["rows"]
    ):
        raise ValueError("Invalid native table row")
    for row in table["rows"]:
        if any(
            cell is not None and type(cell) not in (str, bool, int, float)
            for cell in row
        ):
            raise ValueError("Native table cells must be scalar")


def _validate_native_observations(bundle: dict, start: date, end: date):
    """Completeness/consistency checks even when an untrusted producer rehashes."""
    from zoneinfo import ZoneInfo

    engine = bundle["engine"]
    if not isinstance(engine.get("resolved_config"), dict):
        raise ValueError("Missing native resolved configuration")  # noqa: TRY004 - wire validation
    try:
        timezone = ZoneInfo(engine["resolved_config"]["timezone"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid native result timezone") from exc

    def timestamp(value):
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid native timestamp") from exc
        if (
            parsed.tzinfo is None
            or not start <= parsed.astimezone(timezone).date() < end
        ):
            raise ValueError(
                "Native timestamp outside active window or missing timezone"
            )
        return parsed

    series_times = None
    peak = None
    for name in ("equity", "drawdown", "cash", "margin"):
        rows = bundle[name]["rows"]
        if not rows:
            raise ValueError("Incomplete native account series")
        times = [timestamp(row[0]) for row in rows]
        if any(after <= before for before, after in itertools.pairwise(times)):
            raise ValueError("Native account series timestamps are not increasing")
        if series_times is not None and times != series_times:
            raise ValueError("Native account series timestamps are not aligned")
        series_times = times
        if name != "drawdown" and any(not _finite_number(row[1]) for row in rows):
            raise ValueError("Missing native account value")
    for equity, drawdown in zip(bundle["equity"]["rows"], bundle["drawdown"]["rows"]):
        peak = equity[1] if peak is None else max(peak, equity[1])
        expected = None if peak == 0 else 1 - equity[1] / peak
        if (expected is None) != (drawdown[1] is None) or (
            expected is not None
            and not math.isclose(expected, drawdown[1], rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise ValueError("Drawdown does not reconstruct from equity")

    required = {
        "positions": {
            "date",
            "symbol",
            "long_shares",
            "short_shares",
            "equity",
            "close",
        },
        "orders": {
            "id",
            "symbol",
            "side",
            "quantity",
            "filled_quantity",
            "created_at",
            "updated_at",
            "commission",
            "status",
            "reject_reason",
        },
        "fills": {
            "id",
            "order_id",
            "symbol",
            "side",
            "quantity",
            "price",
            "commission",
            "timestamp",
        },
        "closed_trades": {
            "symbol",
            "entry_time",
            "exit_time",
            "quantity",
            "pnl",
            "net_pnl",
            "commission",
        },
    }
    row_maps = {}
    for name, fields in required.items():
        table = bundle[name]
        names = [column["name"] for column in table["columns"]]
        if not table["rows"]:
            if name != "positions":
                raise ValueError("Incomplete native order/fill/closed-trade history")
        elif not fields.issubset(names):
            raise ValueError(f"Missing required {name} columns")
        for column in table["columns"]:
            if column["unit"] != UNITS.get(column["name"], "label"):
                raise ValueError("Native column has incorrect unit")
        maps = []
        for row in table["rows"]:
            item = dict(zip(names, row))
            for column, value in zip(table["columns"], row):
                unit = column["unit"]
                if unit == "timestamp_iso8601":
                    timestamp(value)
                elif unit == "label":
                    if value is not None and not isinstance(value, str):
                        raise ValueError("Invalid native label")
                elif value is not None and not _finite_number(value):
                    raise ValueError("Invalid native numeric field")
            if name != "positions" and (
                not _finite_number(item["quantity"]) or item["quantity"] <= 0
            ):
                raise ValueError("Invalid native trade quantity")
            maps.append(item)
        row_maps[name] = maps
    for name in ("orders", "fills"):
        ids = [item["id"] for item in row_maps[name]]
        if any(not isinstance(value, str) or not value for value in ids) or len(
            ids
        ) != len(set(ids)):
            raise ValueError("Missing or duplicate native identifier")

    fees = bundle["fees"]
    _keys(
        fees,
        {
            "unit",
            "definition_version",
            "total_from_fills",
            "native_total_commission",
            "calculation_basis",
            "commission_stamp_transfer_breakdown",
            "breakdown_status",
            "slippage_cost",
            "slippage_cost_status",
        },
        "fees",
    )
    if fees["unit"] != engine.get("currency") or fees["unit"] not in ("CNY", "USD"):
        raise ValueError("Invalid fee currency")
    if any(
        not _finite_number(fees[key]) or fees[key] < 0
        for key in ("total_from_fills", "native_total_commission")
    ):
        raise ValueError("Invalid native fee aggregate")
    if any(
        not isinstance(fees[key], str) or not fees[key]
        for key in ("definition_version", "calculation_basis")
    ):
        raise ValueError("Missing fee definition")
    if (
        fees["commission_stamp_transfer_breakdown"] is not None
        or fees["slippage_cost"] is not None
        or fees["breakdown_status"] != "unsupported_by_execution_report"
        or fees["slippage_cost_status"] != "not_separately_reported_by_native_engine"
    ):
        raise ValueError("Native fees cannot claim unreported components")
    native = {item["name"]: item["value"] for item in bundle["native_metrics"]}
    if native.get("closed_trade_count") != len(row_maps["closed_trades"]) or native.get(
        "execution_count"
    ) != len(row_maps["fills"]):
        raise ValueError("Native metric counts do not match trade records")
    if fees["native_total_commission"] != native.get("total_commission"):
        raise ValueError("Native fee metric does not match fee aggregate")
    commissions = [item["commission"] for item in row_maps["fills"]]
    if any(
        not _finite_number(value) or value < 0 for value in commissions
    ) or not math.isclose(
        sum(commissions), fees["total_from_fills"], rel_tol=1e-12, abs_tol=1e-7
    ):
        raise ValueError("Fee aggregate does not match fills")

    trace = bundle["decision_trace"]
    _keys(
        trace,
        {
            "scope",
            "portable_contract",
            "input_snapshot_sha256",
            "input_snapshot_scope",
            "strategy_sha256",
            "decisions",
            "scores",
            "signals",
            "target_portfolio",
            "state_digest",
            "unobservable_reason",
        },
        "decision trace",
    )
    if (
        trace["strategy_sha256"] != bundle["identity"]["strategy_sha256"]
        or trace["input_snapshot_sha256"]
        != bundle["identity"].get("input_manifest_sha256")
        or trace["input_snapshot_scope"]
        != "staged_input_artifacts_not_asof_strategy_view"
    ):
        raise ValueError("Native decision trace identity mismatch")
    for index, (decision, order) in enumerate(
        zip(trace["decisions"], row_maps["orders"])
    ):
        _keys(
            decision,
            {"observed_at", "order_id", "orders_row", "reject_reason"},
            "observed decision",
        )
        if (
            type(decision["orders_row"]) is not int
            or decision["orders_row"] != index
            or decision["order_id"] != order["id"]
            or decision["observed_at"] != order["created_at"]
            or decision["reject_reason"] != order["reject_reason"]
        ):
            raise ValueError("Observed decision does not link to its native order")

    if bundle["benchmark"].get("status") == "not_configured":
        _keys(bundle["benchmark"], {"status", "series", "reason"}, "benchmark")
    _keys(bundle["model_usage"], {"status", "calls", "cost"}, "model usage")
    if bundle["model_usage"] != {
        "status": "not_applicable_to_evaluator",
        "calls": [],
        "cost": None,
    }:
        raise ValueError("Evaluator cannot claim model usage")
    capability_truths = {
        "equity",
        "drawdown",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "fees_aggregate",
        "observable_order_trace",
    }
    capability_falsehoods = {
        "fee_component_breakdown",
        "internal_decisions",
        "trusted_ledger",
    }
    _keys(
        bundle["capabilities"],
        capability_truths | capability_falsehoods | {"benchmark"},
        "native capabilities",
    )
    if any(bundle["capabilities"][key] is not True for key in capability_truths) or any(
        bundle["capabilities"][key] is not False for key in capability_falsehoods
    ):
        raise ValueError("Unsupported native capability declaration")


def validate_result_bundle(bundle: Any) -> dict:
    _keys(
        bundle,
        {"schema_version", "evaluation_mode", "result_trust", "artifacts", *SECTIONS},
        "ResultBundle",
    )
    _json_tree(bundle)
    if (
        bundle["schema_version"] != BUNDLE_VERSION
        or bundle["evaluation_mode"] != "native_akquant"
        or bundle["result_trust"] != TRUST
    ):
        raise ValueError("Unsupported ResultBundle version or trust")
    for name in (
        "engine",
        "capabilities",
        "decision_trace",
        "window",
        "legacy_performance",
        "fees",
        "benchmark",
        "model_usage",
    ):
        if not isinstance(bundle[name], dict):
            raise ValueError(f"Invalid {name} schema")  # noqa: TRY004 - wire validation
    if (
        bundle["engine"].get("name") != "akquant"
        or bundle["engine"].get("version") != ENGINE_VERSION
    ):
        raise ValueError("Unsupported native engine")
    if (
        bundle["capabilities"].get("trusted_ledger") is not False
        or bundle["decision_trace"].get("portable_contract") is not False
    ):
        raise ValueError("Native result cannot claim trusted or portable execution")
    identity = bundle["identity"]
    if not isinstance(identity, dict) or not _hash(identity.get("strategy_sha256")):
        raise ValueError("Invalid strategy identity")
    window = bundle["window"]
    try:
        start, end = (
            date.fromisoformat(window["start"]),
            date.fromisoformat(window["end"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid ResultBundle window") from exc
    if end <= start or window.get("interval") != "half_open":
        raise ValueError("Invalid ResultBundle window interval")
    if "inputs" in identity:
        if not isinstance(identity["inputs"], list):
            raise ValueError("Invalid input manifest")
        paths = set()
        for item in identity["inputs"]:
            _keys(item, {"path", "sha256", "bytes"}, "input artifact")
            path = item["path"]
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or ".." in path.split("/")
                or path in paths
                or not _hash(item["sha256"])
                or type(item["bytes"]) is not int
                or item["bytes"] < 0
            ):
                raise ValueError("Invalid input artifact")
            paths.add(path)
        if identity.get("input_manifest_sha256") != _sha(identity["inputs"]):
            raise ValueError("Input manifest hash mismatch")
    for collection in ("metrics", "native_metrics"):
        entries = bundle[collection]
        if not isinstance(entries, list) or not entries:
            raise ValueError("Missing native metrics")
        names = []
        for metric in entries:
            _keys(
                metric,
                {
                    "name",
                    "value",
                    "unit",
                    "definition_version",
                    "calculation_basis",
                    "unavailable_reason",
                },
                "metric",
            )
            if not all(
                isinstance(metric[key], str) and metric[key]
                for key in ("name", "unit", "definition_version", "calculation_basis")
            ):
                raise ValueError("Invalid metric definition")
            scalar = metric["value"]
            if (
                scalar is not None
                and not _finite_number(scalar)
                and not (
                    collection == "native_metrics"
                    and metric["name"] in ("start_time", "end_time")
                    and isinstance(scalar, str)
                )
            ):
                raise ValueError("Invalid metric value")
            if metric["value"] is None and not metric["unavailable_reason"]:
                raise ValueError("Missing metric availability reason")
            names.append(metric["name"])
        if len(names) != len(set(names)):
            raise ValueError("Duplicate metric")
    expected_metrics = {
        "sharpe_ratio",
        "annualized_return",
        "max_drawdown",
        "trade_per_day",
        "win_rate",
        "return_per_trade",
        "combined_score",
    }
    expected_units = {
        "sharpe_ratio": "dimensionless",
        "annualized_return": "percent",
        "max_drawdown": "percent",
        "trade_per_day": "closed_trades_per_estimated_trading_day",
        "win_rate": "percent",
        "return_per_trade": "percent",
        "combined_score": "score_0_100",
    }
    for item in bundle["metrics"]:
        if (
            item["unit"] != expected_units.get(item["name"])
            or item["definition_version"] != "tradegrad.metrics.v1"
        ):
            raise ValueError("Incorrect objective metric unit or definition version")
    for item in bundle["native_metrics"]:
        if (
            item["unit"] != _native_metric_unit(item["name"])
            or item["definition_version"] != "akquant.0.3.27.metrics_df"
        ):
            raise ValueError("Incorrect native metric unit or definition version")
    precise = {item["name"]: item["value"] for item in bundle["metrics"]}
    if set(precise) != expected_metrics:
        raise ValueError("Invalid objective metric schema")
    legacy = bundle["legacy_performance"].get("value")
    _keys(
        legacy, {"combined_score", "metrics", "market", "profile"}, "legacy performance"
    )
    _keys(legacy["metrics"], expected_metrics - {"combined_score"}, "legacy metrics")
    if legacy["market"] not in ("CN", "US") or legacy["profile"] not in (
        "source_compat",
        "corrected_explicit_config",
    ):
        raise ValueError("Invalid legacy engine profile")
    if (
        not _finite_number(legacy["combined_score"])
        or not 0 <= legacy["combined_score"] <= 100
    ):
        raise ValueError("Invalid legacy reward")
    for name, value in legacy["metrics"].items():
        if (
            not _finite_number(value)
            or not _finite_number(precise[name])
            or value != round(precise[name], 2)
        ):
            raise ValueError("Precise and legacy metric rounding disagree")
    if precise["combined_score"] != legacy["combined_score"]:
        raise ValueError("Precise and legacy reward disagree")
    for name in (
        "equity",
        "drawdown",
        "cash",
        "margin",
        "positions",
        "orders",
        "fills",
        "closed_trades",
    ):
        _validate_table(bundle[name])
    for name, value_name in (
        ("equity", "equity"),
        ("drawdown", "drawdown"),
        ("cash", "cash"),
        ("margin", "margin"),
    ):
        if [c["name"] for c in bundle[name]["columns"]] != ["timestamp", value_name]:
            raise ValueError("Invalid series columns")
        for timestamp, value in bundle[name]["rows"]:
            try:
                parsed = datetime.fromisoformat(timestamp)
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid series timestamp") from exc
            if parsed.tzinfo is None or (
                value is not None and not _finite_number(value)
            ):
                raise ValueError("Invalid series value or timezone")
    if len(bundle["equity"]["rows"]) != len(bundle["drawdown"]["rows"]):
        raise ValueError("Equity and drawdown lengths disagree")
    if bundle["benchmark"].get("status") == "available":
        from .benchmarks import validate_comparison

        validate_comparison(bundle["benchmark"], bundle)
        if bundle["capabilities"]["benchmark"] is not True:
            raise ValueError("Missing benchmark capability")
    elif (
        bundle["benchmark"].get("series") is not None
        or bundle["benchmark"].get("status") != "not_configured"
        or bundle["capabilities"]["benchmark"] is not False
    ):
        raise ValueError("Unsupported native benchmark")
    trace = bundle["decision_trace"]
    if not isinstance(trace.get("decisions"), list) or len(trace["decisions"]) != len(
        bundle["orders"]["rows"]
    ):
        raise ValueError("Invalid observable decision trace")
    if trace.get("scope") != "observable_native_orders_only" or any(
        trace.get(key) is not None
        for key in ("scores", "signals", "target_portfolio", "state_digest")
    ):
        raise ValueError("Native trace cannot claim unobserved strategy intent")
    _validate_native_observations(bundle, start, end)
    manifest = bundle["artifacts"]
    if not isinstance(manifest, list) or len(manifest) != len(SECTIONS):
        raise ValueError("Incomplete bundle artifact manifest")
    seen = set()
    for item in manifest:
        _keys(item, {"section", "media_type", "sha256", "bytes"}, "artifact")
        name = item["section"]
        if (
            name not in SECTIONS
            or name in seen
            or item["media_type"] != "application/json"
        ):
            raise ValueError("Invalid bundle artifact section")
        seen.add(name)
        data = canonical_json_bytes(bundle[name])
        if (
            not _hash(item["sha256"])
            or item["sha256"] != sha256_bytes(data)
            or type(item["bytes"]) is not int
            or item["bytes"] != len(data)
        ):
            raise ValueError("ResultBundle artifact hash or size mismatch")
    return bundle


def make_envelope(metrics: dict, bundle: dict) -> bytes:
    validate_result_bundle(bundle)
    value = {
        "schema_version": ENVELOPE_VERSION,
        "metrics": metrics,
        "bundle": bundle,
        "bundle_sha256": _sha(bundle),
    }
    data = canonical_json_bytes(value)
    validate_envelope(data)
    return data


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def validate_envelope(data: bytes) -> dict:
    if not isinstance(data, bytes) or len(data) > MAX_WIRE_BYTES:
        raise ValueError("Result protocol exceeded its byte budget")
    value = json.loads(
        data,
        object_pairs_hook=_unique_pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")),
    )
    _keys(
        value,
        {"schema_version", "metrics", "bundle", "bundle_sha256"},
        "result envelope",
    )
    if value["schema_version"] != ENVELOPE_VERSION:
        raise ValueError("Unsupported result envelope version")
    validate_result_bundle(value["bundle"])
    if not _hash(value["bundle_sha256"]) or value["bundle_sha256"] != _sha(
        value["bundle"]
    ):
        raise ValueError("ResultBundle envelope hash mismatch")
    if value["metrics"] != value["bundle"]["legacy_performance"]["value"]:
        raise ValueError("Legacy metrics and ResultBundle disagree")
    return value


def wire_record(metrics: dict, bundle: dict) -> str:
    return RESULT_PREFIX_V2 + base64.b64encode(make_envelope(metrics, bundle)).decode(
        "ascii"
    )
