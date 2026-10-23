"""Controlled daily native AKQuant moving-average timing research template.

No explanation re-runs a condition. The native engine is the only simulator;
this file submits real native target orders and records actual calculated inputs.
"""

import math
from datetime import UTC, datetime

from akquant import Strategy

RESEARCH_TEMPLATE = {
    "template_id": "daily_timing.v1",
    "strategy_symbol": "TimeStrategy",
    "mode": "native",
    "parameters": {
        "fast_window": 2,
        "slow_window": 3,
        "liquidity_enabled": True,
        "min_average_dollar_volume": 1000.0,
        "exit_buffer": 0.01,
        "target_weight": 0.8,
        "max_weight": 0.4,
        "cash_reserve": 0.05,
    },
    "rules": [
        {
            "stage_id": "features",
            "symbol": "TimeStrategy._features",
            "description": "Compute fast and slow means from trailing completed daily closes; require the entire slow window.",
        },
        {
            "stage_id": "signal",
            "symbol": "TimeStrategy._decision",
            "description": "Enter only when fast mean exceeds slow mean. A false entry signal is not an order failure.",
        },
        {
            "stage_id": "liquidity",
            "symbol": "TimeStrategy._decision",
            "description": "Optional trailing mean close times volume threshold; disabled filters are not evaluated.",
        },
        {
            "stage_id": "allocation",
            "symbol": "TimeStrategy._decision",
            "description": "Retain existing shares while fast mean stays above slow mean times one minus exit buffer; otherwise exit. Apply weight and cash limits without leverage.",
        },
        {
            "stage_id": "execution",
            "symbol": "TimeStrategy.on_bar",
            "description": "Submit native target quantity orders. Returned IDs are explicit order links, never proof of acceptance or fill.",
        },
    ],
}


def _timestamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1_000_000_000, UTC).isoformat()
    return value.isoformat()


def _recorder():
    try:
        from packages.research_trace.recorder import current_recorder
    except ImportError:
        return None
    return current_recorder()


def _source(method):
    line = method.__func__.__code__.co_firstlineno
    return {
        "file": "strategy.py",
        "symbol": method.__qualname__,
        "line_start": line,
        "line_end": line,
    }


def _validity(values, window):
    if len(values) < window:
        return "warmup_insufficient", "insufficient_history"
    finite = [math.isfinite(value) for value in values]
    if all(finite):
        return "valid", None
    first = next((i for i, valid in enumerate(finite) if valid), len(values))
    if first and all(finite[first:]):
        return "warmup_insufficient", "insufficient_history"
    return "input_missing", "nonfinite_history_input"


class TimeStrategy(Strategy):
    def __init__(self):
        super().__init__()
        self.target_symbol = "target_symbol"
        self._research_event_seq = 0

    def on_start(self):
        values = dict(RESEARCH_TEMPLATE["parameters"])
        supplied = getattr(self, "tradegrad_parameters", {})
        if not isinstance(supplied, dict) or set(supplied) - set(values):
            raise ValueError("Unknown controlled template parameter")
        values.update(supplied)
        self._parameters = values
        if (
            any(
                type(values[name]) is not int for name in ("fast_window", "slow_window")
            )
            or not 1 <= values["fast_window"] < values["slow_window"] <= 252
        ):
            raise ValueError("Require 1 <= fast_window < slow_window <= 252")
        if type(values["liquidity_enabled"]) is not bool:
            raise ValueError("liquidity_enabled must be boolean")
        for name in ("exit_buffer", "target_weight", "max_weight", "cash_reserve"):
            if (
                type(values[name]) not in (float, int)
                or not math.isfinite(values[name])
                or not 0 <= values[name] <= 1
            ):
                raise ValueError("Invalid controlled template parameter: " + name)
        threshold = values["min_average_dollar_volume"]
        if (
            type(threshold) not in (float, int)
            or not math.isfinite(threshold)
            or threshold < 0
        ):
            raise ValueError("Invalid min_average_dollar_volume")
        self.subscribe(self.target_symbol)

    def _features(self, cutoff):
        p = self._parameters
        close = [
            float(value)
            for value in self.get_history(
                count=p["slow_window"], symbol=self.target_symbol, field="close"
            )
        ]
        volume = [
            float(value)
            for value in self.get_history(
                count=p["slow_window"], symbol=self.target_symbol, field="volume"
            )
        ]
        validity, missing = _validity(close, p["slow_window"])
        vstate, vreason = _validity(volume, p["slow_window"])
        if validity == "valid" and any(value <= 0 for value in close):
            validity, missing = "calculation_error", "nonpositive_close"
        if vstate == "valid" and any(value < 0 for value in volume):
            vstate, vreason = "calculation_error", "negative_volume"
        fast = (
            sum(close[-p["fast_window"] :]) / p["fast_window"]
            if validity == "valid"
            else None
        )
        slow = sum(close) / p["slow_window"] if validity == "valid" else None
        liquidity_validity = validity if validity != "valid" else vstate
        liquidity_missing = missing if validity != "valid" else vreason
        liquidity = (
            sum(a * b for a, b in zip(close, volume)) / p["slow_window"]
            if liquidity_validity == "valid"
            else None
        )
        features = []
        for name, value, unit, window, state, reason in (
            ("fast_mean", fast, "quote_currency", p["fast_window"], validity, missing),
            ("slow_mean", slow, "quote_currency", p["slow_window"], validity, missing),
            (
                "average_dollar_volume",
                liquidity,
                "quote_currency_per_session",
                p["slow_window"],
                liquidity_validity,
                liquidity_missing,
            ),
        ):
            features.append(
                {
                    "instrument_id": self.target_symbol,
                    "feature_id": name,
                    "value": value,
                    "unit": unit,
                    "calculation_window": f"{window} completed daily bars",
                    "validity": state,
                    "missing_reason": reason,
                    "information_cutoff": cutoff,
                    "source_ref": _source(self._features),
                }
            )
        return {
            "fast": fast,
            "slow": slow,
            "liquidity": liquidity,
            "validity": validity,
            "liquidity_validity": liquidity_validity,
            "price": close[-1]
            if close and math.isfinite(close[-1]) and close[-1] > 0
            else None,
        }, features

    def _decision(self, inputs, current, cash, equity, pending):
        p = self._parameters
        fast, slow, liquidity, price = (
            inputs[name] for name in ("fast", "slow", "liquidity", "price")
        )
        ready = inputs["validity"] == "valid"
        entry = fast > slow if ready else None
        retained = (
            fast >= slow * (1 - p["exit_buffer"]) if ready and current > 0 else None
        )
        liquid = (
            liquidity >= p["min_average_dollar_volume"]
            if p["liquidity_enabled"] and liquidity is not None
            else None
        )
        predicates = [
            {
                "instrument_id": self.target_symbol,
                "condition_id": "entry_signal",
                "operator": "gt",
                "operands": [fast] if ready else None,
                "threshold": slow,
                "state": "true"
                if entry
                else "false"
                if ready
                else "not_evaluated"
                if inputs["validity"] == "warmup_insufficient"
                else "unknown",
                "missing_reason": None if ready else inputs["validity"],
                "source_ref": _source(self._decision),
            },
            {
                "instrument_id": self.target_symbol,
                "condition_id": "liquidity_minimum",
                "operator": "gte",
                "operands": [liquidity] if liquidity is not None else None,
                "threshold": p["min_average_dollar_volume"],
                "state": "not_evaluated"
                if not p["liquidity_enabled"]
                else "true"
                if liquid
                else "false"
                if liquid is not None
                else "unknown",
                "missing_reason": "liquidity_filter_disabled"
                if not p["liquidity_enabled"]
                else inputs["liquidity_validity"]
                if liquid is None
                else None,
                "source_ref": _source(self._decision),
            },
            {
                "instrument_id": self.target_symbol,
                "condition_id": "retention_band",
                "operator": "gte",
                "operands": [fast] if retained is not None else None,
                "threshold": slow * (1 - p["exit_buffer"]) if ready else None,
                "state": "true"
                if retained
                else "false"
                if retained is not None
                else "not_evaluated",
                "missing_reason": None
                if retained is not None
                else "no_prior_holding"
                if current <= 0
                else inputs["validity"],
                "source_ref": _source(self._decision),
            },
        ]
        liquidity_pass = not p["liquidity_enabled"] or liquid is True
        if not ready:
            raw_quantity, reason, eligibility = (
                current,
                "feature_unavailable",
                "unknown",
            )
        elif retained:
            raw_quantity, reason, eligibility = current, "holding_retained", "eligible"
        elif entry and liquidity_pass:
            raw_quantity, reason, eligibility = (
                equity * p["target_weight"] / price,
                "selected",
                "eligible",
            )
        elif entry:
            raw_quantity, reason, eligibility = (
                0.0,
                "eligibility_filter" if liquid is False else "feature_unavailable",
                "ineligible" if liquid is False else "unknown",
            )
        else:
            raw_quantity, reason, eligibility = (
                0.0,
                "exit" if current > 0 else "timing_not_triggered",
                "eligible",
            )
        quantity, constraints = raw_quantity, []
        if pending or not ready:
            quantity = current
            constraints.append("pending_orders" if pending else "feature_unavailable")
        elif price:
            cap = equity * p["max_weight"] / price
            weight_ok = quantity <= cap
            predicates.append(
                {
                    "instrument_id": self.target_symbol,
                    "condition_id": "within_max_weight",
                    "operator": "lte",
                    "operands": [quantity * price / equity if equity > 0 else 0.0],
                    "threshold": p["max_weight"],
                    "state": "true" if weight_ok else "false",
                    "missing_reason": None,
                    "source_ref": _source(self._decision),
                }
            )
            if not weight_ok:
                quantity = cap
                constraints.append("max_weight")
            cash_cap = current + max(0.0, cash - equity * p["cash_reserve"]) / price
            cash_ok = quantity <= cash_cap
            predicates.append(
                {
                    "instrument_id": self.target_symbol,
                    "condition_id": "within_cash_budget",
                    "operator": "lte",
                    "operands": [quantity],
                    "threshold": cash_cap,
                    "state": "true" if cash_ok else "false",
                    "missing_reason": None,
                    "source_ref": _source(self._decision),
                }
            )
            if not cash_ok:
                quantity = cash_cap
                constraints.append("cash_reserve")
            lot = (
                self.lot_size.get(self.target_symbol, 1)
                if isinstance(self.lot_size, dict)
                else self.lot_size
            )
            quantity = math.floor(quantity / lot) * lot
        raw = {
            "instrument_id": self.target_symbol,
            "target_quantity": raw_quantity,
            "target_weight": raw_quantity * price / equity
            if price and equity > 0
            else None,
            "reason": reason,
        }
        plan = {
            "instrument_id": self.target_symbol,
            "target_quantity": float(quantity),
            "target_weight": quantity * price / equity
            if price and equity > 0
            else None,
            "reason": reason,
            "constraints": constraints,
        }
        candidate = {
            "instrument_id": self.target_symbol,
            "universe_member": True,
            "eligibility": eligibility,
            "elimination_stage": "features"
            if not ready
            else "liquidity"
            if entry and not liquidity_pass and not retained
            else None,
            "reason": reason,
            "score": fast - slow if ready else None,
            "score_validity": inputs["validity"],
            "rank": None,
            "ranking_direction": None,
            "tie_break": None,
            "selected": reason in ("selected", "holding_retained"),
            "prior_holding": current > 0,
            "source_ref": _source(self._decision),
        }
        return predicates, candidate, raw, plan

    def on_bar(self, bar):
        if bar.symbol != self.target_symbol:
            return
        cutoff = _timestamp(bar.timestamp)
        inputs, features = self._features(cutoff)
        current, cash, equity = (
            float(self.get_position(self.target_symbol)),
            float(self.cash),
            float(self.equity),
        )
        pending = [
            {"order_id": str(order.id), "instrument_id": str(order.symbol)}
            for order in self.get_open_orders()
        ]
        predicates, candidate, raw, plan = self._decision(
            inputs, current, cash, equity, pending
        )
        self._research_event_seq += 1
        recorder = _recorder()
        if recorder is not None:
            recorder.begin_decision(
                data_event_id=f"daily_bar:{bar.timestamp}:{bar.symbol}",
                event_seq=self._research_event_seq,
                scope="instrument",
                instrument_id=self.target_symbol,
                callback_phase="on_bar",
                decision_time=cutoff,
                information_cutoff=cutoff,
                market_timezone=self.timezone,
                pre_state={
                    "cash": cash,
                    "equity": equity,
                    "positions": [
                        {"instrument_id": self.target_symbol, "quantity": current}
                    ]
                    if current
                    else [],
                    "open_orders": pending,
                    "strategy_state": {"parameters": dict(self._parameters)},
                    "coverage": "complete",
                    "source": "strategy_emitted",
                },
            )
        try:
            if recorder is not None:
                recorder.emit("features", {"features": features})
                recorder.emit(
                    "signal", {"predicates": [predicates[0]], "candidates": [candidate]}
                )
                recorder.emit("liquidity", {"predicates": [predicates[1]]})
                recorder.emit(
                    "allocation",
                    {
                        "predicates": predicates[2:],
                        "raw_targets": [raw],
                        "constrained_plan": [plan],
                    },
                )
            links = []
            if plan["target_quantity"] != current:
                order_id = self.order_target(
                    symbol=self.target_symbol, target=plan["target_quantity"]
                )
                links.append(
                    {
                        "instrument_id": self.target_symbol,
                        "logical_order_id": f"timing:{self._research_event_seq}:{self.target_symbol}",
                        "child_order_ids": [str(order_id)]
                        if order_id is not None
                        else [],
                        "fill_ids": [],
                        "submitted_at": cutoff,
                        "fill_times": [],
                        "status": "intended",
                    }
                )
            if recorder is not None:
                recorder.emit("execution", {"order_links": links})
        finally:
            if recorder is not None:
                recorder.end_decision()
