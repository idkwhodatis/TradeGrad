"""Export only actual native records from the trusted engine process."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from numbers import Integral, Real

from packages.strategy_sdk import hash_json
from packages.tradegrad_adapter.portable_results import validate_portable_result

from .runtime import from_ns, iso, status_name


def _number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite native accounting record")
    return result


def _duration_ns(value):
    """Preserve native duration exactly, including pandas sub-microsecond data."""
    scalar = getattr(value, "value", value)
    if isinstance(scalar, Integral) and not isinstance(scalar, bool):
        return int(scalar)
    if isinstance(value, timedelta):
        return (
            value.days * 86400 + value.seconds
        ) * 1_000_000_000 + value.microseconds * 1000
    raise ValueError("unsupported native duration representation")


def _json(value):
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, Real):
        return _number(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("native timestamp lacks timezone")
        return value.isoformat()
    if isinstance(value, dict):
        if any(type(k) is not str for k in value):
            raise ValueError("native configuration key is not a string")
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    raise ValueError("unsupported native config value; no repr serialization")


def _side(value):
    name = str(value).split(".")[-1].lower()
    if name not in {"buy", "sell"}:
        raise ValueError("unsupported native side")
    return name


def export_result(result, controller):
    inputs = controller.inputs
    raw = result._raw
    sessions = {s.session_date: s for s in inputs.sessions}
    timezone = __import__("zoneinfo").ZoneInfo(inputs.manifest.timezone)

    def timestamp(ns):
        return {"native_timestamp_ns": str(int(ns)), "native_at": iso(from_ns(int(ns)))}

    def session_for(ns):
        day = from_ns(int(ns)).astimezone(timezone).date()
        if day not in sessions:
            raise ValueError("native record outside staged sessions")
        return sessions[day]

    # Raw ledger curves only: never BacktestResult's heuristic PnL/equity fallback.
    equity, cash = list(raw.equity_curve), list(raw.cash_curve)
    if not equity or [r[0] for r in equity] != [r[0] for r in cash]:
        raise ValueError("native equity/cash curves unavailable or not aligned")
    account, peak = [], inputs.initial_cash
    for (ns, value), (_, cash_value) in zip(equity, cash):
        value = _number(value)
        peak = max(peak, value)
        account.append(
            {
                **timestamp(ns),
                "session_date": session_for(ns).session_date.isoformat(),
                "equity": value,
                "cash": _number(cash_value),
                "drawdown": 1 - value / peak,
            }
        )
    positions = []
    for ns, records in raw.snapshots:
        for p in records:
            positions.append(
                {
                    **timestamp(ns),
                    "listing_id": p.symbol,
                    "quantity": _number(p.long_shares),
                    "short_quantity": _number(p.short_shares),
                    "close": _number(p.close),
                    "market_value": _number(p.market_value),
                    "entry_price": _number(p.entry_price),
                    "unrealized_pnl": _number(p.unrealized_pnl),
                }
            )
    logical = [
        {
            k: v
            for k, v in r.items()
            if k not in {"last_events", "budget_price", "remaining_quantity"}
        }
        for r in controller.logical_orders
    ]
    logical_by_id = {r["logical_order_id"]: r for r in logical}
    orders = []
    for o in raw.orders:
        logical_id = controller.native_to_logical.get(o.id)
        if logical_id is None or str(o.tag) != logical_id:
            raise ValueError(
                "unattributed native order; only reviewed adapter may submit"
            )
        rec = logical_by_id[logical_id]
        orders.append(
            {
                "order_id": o.id,
                "logical_order_id": logical_id,
                "listing_id": o.symbol,
                "side": _side(o.side),
                "quantity": _number(o.quantity),
                "filled_quantity": _number(o.filled_quantity),
                "order_type": str(o.order_type).split(".")[-1].lower(),
                "limit_price": None if o.price is None else _number(o.price),
                "time_in_force": str(o.time_in_force).split(".")[-1].upper(),
                "status": status_name(o.status),
                "reject_reason": str(o.reject_reason),
                "commission": _number(o.commission),
                "native_created_ns": str(int(o.created_at)),
                "native_created_at": iso(from_ns(int(o.created_at))),
                "native_updated_ns": str(int(o.updated_at)),
                "native_updated_at": iso(from_ns(int(o.updated_at))),
                "decision_time": rec["decision_time"],
                "effective_at": rec["effective_at"],
            }
        )
    fills = []
    for fill in raw.executions:
        logical_id = controller.native_to_logical.get(fill.order_id)
        if logical_id is None:
            raise ValueError("unattributed native fill")
        rec = logical_by_id[logical_id]
        session = session_for(fill.timestamp)
        if session.session_date.isoformat() <= rec[
            "decision_session"
        ] or session.open_time < datetime.fromisoformat(rec["effective_at"]):
            raise ValueError("NextOpen fill not strictly later than decision session")
        if rec["order_type"] == "limit":
            opening = inputs.by_session[session.session_date][rec["listing_id"]].open
            limit = rec["limit_price"]
            price = _number(fill.price)
            if (rec["side"] == "buy" and (opening > limit or price > limit)) or (
                rec["side"] == "sell" and (opening < limit or price < limit)
            ):
                raise ValueError("unsupported non-open or limit-breaching native fill")
        fills.append(
            {
                **timestamp(fill.timestamp),
                "fill_id": fill.id,
                "order_id": fill.order_id,
                "logical_order_id": logical_id,
                "listing_id": fill.symbol,
                "side": _side(fill.side),
                "quantity": _number(fill.quantity),
                "price": _number(fill.price),
                "commission": _number(fill.commission),
                "session_date": session.session_date.isoformat(),
                "economic_reference_time": iso(session.open_time),
                "economic_reference": "session_open_with_native_matching_and_slippage",
                "decision_session": rec["decision_session"],
            }
        )
    trades = []
    for t in raw.trades:
        trades.append(
            {
                "listing_id": t.symbol,
                "entry_native_ns": str(int(t.entry_time)),
                "entry_native_at": iso(from_ns(int(t.entry_time))),
                "exit_native_ns": str(int(t.exit_time)),
                "exit_native_at": iso(from_ns(int(t.exit_time))),
                "quantity": _number(t.quantity),
                "entry_price": _number(t.entry_price),
                "exit_price": _number(t.exit_price),
                "pnl": _number(t.pnl),
                "net_pnl": _number(t.net_pnl),
                "commission": _number(t.commission),
            }
        )
    metrics = []
    for name, value in result.metrics_df["value"].items():
        unit, missing = "native_engine_defined", None
        if name in {"start_time", "end_time"}:
            scalar = value.isoformat() if hasattr(value, "isoformat") else str(value)
            unit = "native_timestamp"
        elif name == "duration":
            scalar = str(_duration_ns(value))
            unit = "nanoseconds"
        else:
            try:
                scalar = float(value)
            except (TypeError, ValueError):
                scalar = None
            if scalar is None or not math.isfinite(scalar):
                scalar, missing = None, "undefined_or_unavailable_in_native_result"
        metrics.append(
            {
                "name": str(name),
                "value": scalar,
                "unit": unit,
                "unavailable_reason": missing,
            }
        )
    summary = {
        "initial_cash": inputs.initial_cash,
        "final_equity": account[-1]["equity"],
        "total_return_pct": (account[-1]["equity"] / inputs.initial_cash - 1) * 100,
        "max_drawdown_pct": max(row["drawdown"] for row in account) * 100,
        "execution_count": len(fills),
        "closed_trade_count": len(trades),
        "total_commission": sum(f["commission"] for f in fills),
    }
    bundle = {
        "schema_version": "tradegrad.portable-result.v1",
        "evaluation_mode": "portable",
        "result_trust": "isolated_policy_trusted_akquant_engine",
        "ledger_origin": "trusted_engine",
        "runtime_certified": False,
        "identity": {
            "strategy_sha256": inputs.manifest.code_sha256,
            "parameters_sha256": inputs.manifest.parameters_sha256,
            "manifest_sha256": hash_json(inputs.manifest),
            **inputs.identity,
        },
        "manifest": inputs.manifest.model_dump(mode="json"),
        "window": {**inputs.window, "interval": "half_open"},
        "engine": {
            "name": "akquant",
            "version": "0.3.27",
            "profile": inputs.profile,
            "resolved_config": _json(result.resolved_config),
            "resolved_execution_policy": _json(result.resolved_execution_policy),
            "timestamp_semantics": "native_close_timestamp_preserved; economic_reference_time identifies later-session open, not a rewritten native timestamp",
            "forced_final_liquidation": False,
        },
        "account_series": account,
        "positions": positions,
        "orders": orders,
        "fills": fills,
        "closed_trades": trades,
        "native_metrics": metrics,
        "decision_trace": controller.trace,
        "logical_orders": logical,
        "final_state": controller.state.model_dump(mode="json"),
        "summary": summary,
    }
    return validate_portable_result(bundle)
