"""Controlled daily, long-only native AKQuant research template.

This is executable strategy code, not an executable StrategySpec. It only reads
engine history available at the callback and emits values already used below.
The evaluator, not this strategy, supplies immutable execution identity.
"""

import math
from datetime import UTC, datetime

from akquant import Strategy

RESEARCH_TEMPLATE = {
    "template_id": "daily_cross_section.v1",
    "strategy_symbol": "AlphaStrategy",
    "mode": "native",
    "parameters": {
        "lookback": 3,
        "liquidity_enabled": True,
        "min_average_dollar_volume": 1000.0,
        "top_n": 1,
        "retention_rank_buffer": 2,
        "max_positions": 2,
        "gross_target": 0.8,
        "max_weight": 0.4,
        "cash_reserve": 0.05,
    },
    "rules": [
        {
            "stage_id": "universe",
            "symbol": "AlphaStrategy.on_cross_section",
            "description": "Use the membership snapshot effective on this trading date; show existing holdings outside it.",
        },
        {
            "stage_id": "features",
            "symbol": "AlphaStrategy._read_inputs",
            "description": "Trailing close return and mean close times volume, oldest to newest, including this completed daily slice.",
        },
        {
            "stage_id": "liquidity",
            "symbol": "AlphaStrategy._read_inputs",
            "description": "When enabled, require trailing mean dollar volume at or above the threshold; unavailable inputs are not false.",
        },
        {
            "stage_id": "ranking",
            "symbol": "AlphaStrategy._rank",
            "description": "Rank eligible symbols by descending return, breaking ties by ascending instrument identifier.",
        },
        {
            "stage_id": "allocation",
            "symbol": "AlphaStrategy._allocate",
            "description": "Retain eligible existing holdings inside top_n plus retention buffer before admitting top_n names; cap slots, gross, per-name weight and cash without assuming sale proceeds.",
        },
        {
            "stage_id": "execution",
            "symbol": "AlphaStrategy._submit",
            "description": "Submit reductions before increases through native order_target; record returned order IDs, not inferred fills.",
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
    # Metadata only: no predicate, feature or user callback is re-evaluated.
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
    # AKQuant left-pads history with NaN. Only a leading missing prefix is
    # classified as warmup; internal/trailing holes remain missing input.
    first = next((i for i, valid in enumerate(finite) if valid), len(values))
    if first and all(finite[first:]):
        return "warmup_insufficient", "insufficient_history"
    return "input_missing", "nonfinite_history_input"


class AlphaStrategy(Strategy):
    def __init__(self):
        super().__init__()
        self._research_event_seq = 0

    def on_start(self):
        values = dict(RESEARCH_TEMPLATE["parameters"])
        supplied = getattr(self, "tradegrad_parameters", {})
        if not isinstance(supplied, dict) or set(supplied) - set(values):
            raise ValueError("Unknown controlled template parameter")
        values.update(supplied)
        self._parameters = values
        for name in ("lookback", "top_n", "max_positions"):
            if type(values[name]) is not int or not 1 <= values[name] <= 252:
                raise ValueError("Invalid controlled template parameter: " + name)
        if values["lookback"] < 2:
            raise ValueError("lookback must be at least two completed daily bars")
        if (
            type(values["retention_rank_buffer"]) is not int
            or not 0 <= values["retention_rank_buffer"] <= 252
        ):
            raise ValueError("Invalid retention_rank_buffer")
        if type(values["liquidity_enabled"]) is not bool:
            raise ValueError("liquidity_enabled must be boolean")
        for name in ("gross_target", "max_weight", "cash_reserve"):
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
        for symbol in sorted(self.all_symbols):
            self.subscribe(symbol)

    def _read_inputs(self, symbols, cutoff):
        p = self._parameters
        window = p["lookback"]
        closes = self.get_history_map(count=window, symbols=symbols, field="close")
        volumes = self.get_history_map(count=window, symbols=symbols, field="volume")
        inputs, features, predicates = {}, [], []
        source = _source(self._read_inputs)
        for symbol in symbols:
            close = [float(value) for value in closes[symbol]]
            volume = [float(value) for value in volumes[symbol]]
            validity, missing = _validity(close, window)
            volume_validity, volume_missing = _validity(volume, window)
            if validity == "valid" and any(value <= 0 for value in close):
                validity, missing = "calculation_error", "nonpositive_close"
            if volume_validity == "valid" and any(value < 0 for value in volume):
                volume_validity, volume_missing = "calculation_error", "negative_volume"
            score = close[-1] / close[0] - 1 if validity == "valid" else None
            liquidity_validity = validity if validity != "valid" else volume_validity
            liquidity_missing = missing if validity != "valid" else volume_missing
            liquidity = (
                sum(a * b for a, b in zip(close, volume)) / window
                if liquidity_validity == "valid"
                else None
            )
            liquid = (
                liquidity >= p["min_average_dollar_volume"]
                if liquidity is not None and p["liquidity_enabled"]
                else None
            )
            liquidity_state = (
                "not_evaluated"
                if not p["liquidity_enabled"]
                else ("true" if liquid else "false")
                if liquid is not None
                else "unknown"
            )
            inputs[symbol] = {
                "score": score,
                "price": close[-1]
                if close and math.isfinite(close[-1]) and close[-1] > 0
                else None,
                "validity": validity,
                "liquid": liquid,
                "liquidity_validity": liquidity_validity,
            }
            for feature_id, value, unit, state, reason in (
                ("trailing_return", score, "fraction", validity, missing),
                (
                    "average_dollar_volume",
                    liquidity,
                    "quote_currency_per_session",
                    liquidity_validity,
                    liquidity_missing,
                ),
            ):
                features.append(
                    {
                        "instrument_id": symbol,
                        "feature_id": feature_id,
                        "value": value,
                        "unit": unit,
                        "calculation_window": f"{window} completed daily bars",
                        "validity": state,
                        "missing_reason": reason,
                        "information_cutoff": cutoff,
                        "source_ref": source,
                    }
                )
            predicates.append(
                {
                    "instrument_id": symbol,
                    "condition_id": "liquidity_minimum",
                    "operator": "gte",
                    "operands": [liquidity] if liquidity is not None else None,
                    "threshold": p["min_average_dollar_volume"],
                    "state": liquidity_state,
                    "missing_reason": "liquidity_filter_disabled"
                    if not p["liquidity_enabled"]
                    else liquidity_missing,
                    "source_ref": source,
                }
            )
        return inputs, features, predicates

    def _rank(self, symbols, universe, inputs, positions):
        p = self._parameters
        candidates, universe_predicates = [], []
        for symbol in symbols:
            value = inputs[symbol]
            universe_member = symbol in universe
            universe_predicates.append(
                {
                    "instrument_id": symbol,
                    "condition_id": "universe_membership",
                    "operator": "eq",
                    "operands": [universe_member],
                    "threshold": True,
                    "state": "true" if universe_member else "false",
                    "missing_reason": None,
                    "source_ref": _source(self._rank),
                }
            )
            if not universe_member:
                eligible, stage, reason = False, "universe", "outside_universe"
            elif value["validity"] != "valid":
                eligible, stage, reason = None, "features", value["validity"]
            elif p["liquidity_enabled"] and value["liquid"] is None:
                eligible, stage, reason = (
                    None,
                    "liquidity",
                    "liquidity_input_unavailable",
                )
            elif p["liquidity_enabled"] and not value["liquid"]:
                eligible, stage, reason = (
                    False,
                    "liquidity",
                    "liquidity_below_threshold",
                )
            else:
                eligible, stage, reason = True, None, None
            candidates.append(
                {
                    "instrument_id": symbol,
                    "universe_member": universe_member,
                    "eligibility": "eligible"
                    if eligible
                    else "ineligible"
                    if eligible is False
                    else "unknown",
                    "elimination_stage": stage,
                    "reason": "outside_universe"
                    if not universe_member
                    else "feature_unavailable"
                    if eligible is None
                    else "eligibility_filter"
                    if eligible is False
                    else "selected",
                    "reason_detail": reason,
                    "score": value["score"],
                    "score_validity": value["validity"],
                    "rank": None,
                    "ranking_direction": "descending",
                    "tie_break": "instrument_id_ascending",
                    "selected": False,
                    "prior_holding": positions.get(symbol, 0.0) > 0,
                    "source_ref": _source(self._rank),
                }
            )
        ranking = sorted(
            (row for row in candidates if row["eligibility"] == "eligible"),
            key=lambda row: (-row["score"], row["instrument_id"]),
        )
        for rank, row in enumerate(ranking, 1):
            row["rank"] = rank
        retained = [
            row
            for row in ranking
            if row["prior_holding"]
            and row["rank"] <= p["top_n"] + p["retention_rank_buffer"]
        ][: p["max_positions"]]
        selected = list(retained)
        for row in ranking[: p["top_n"]]:
            if row not in selected and len(selected) < p["max_positions"]:
                selected.append(row)
        for row in ranking:
            row["selected"] = row in selected
            if row in retained:
                row["reason"] = "holding_retained"
            elif row in selected:
                row["reason"] = "selected"
            else:
                row["elimination_stage"] = (
                    "allocation" if row["rank"] <= p["top_n"] else "ranking"
                )
                row["reason"] = (
                    "position_slots"
                    if row["rank"] <= p["top_n"]
                    else "rank_not_selected"
                )
        return candidates, universe_predicates

    def _allocate(self, candidates, inputs, positions, cash, equity, pending):
        p = self._parameters
        selected = [row for row in candidates if row["selected"]]
        raw, plan, predicates = [], [], []
        available_cash = max(0.0, cash - equity * p["cash_reserve"])
        unpriced_holding = any(
            quantity and (symbol not in inputs or inputs[symbol]["price"] is None)
            for symbol, quantity in positions.items()
        )
        retained_value = sum(
            positions[row["instrument_id"]] * inputs[row["instrument_id"]]["price"]
            for row in selected
            if row["reason"] == "holding_retained"
        )
        available_gross = max(0.0, equity * p["gross_target"] - retained_value)
        retained_gross_budget = max(0.0, equity * p["gross_target"])
        for row in sorted(
            candidates,
            key=lambda value: (
                not value["selected"],
                value["rank"] or 100000,
                value["instrument_id"],
            ),
        ):
            symbol, price = row["instrument_id"], inputs[row["instrument_id"]]["price"]
            current = positions.get(symbol, 0.0)
            retained = row["reason"] == "holding_retained"
            desired_value = (
                current * price
                if retained
                else equity * p["gross_target"] / max(1, p["top_n"])
                if row["selected"]
                else 0.0
            )
            raw_quantity = desired_value / price if price else None
            reason = row["reason"]
            constraints = []
            if (
                pending
                or unpriced_holding
                or price is None
                or row["eligibility"] == "unknown"
            ):
                quantity = current
                constraints.append(
                    "pending_orders"
                    if pending
                    else "unpriced_holding"
                    if unpriced_holding
                    else "price_unavailable"
                    if price is None
                    else "feature_unavailable"
                )
            elif retained:
                # Retention never overrides the per-name risk cap.
                retention_cap = min(equity * p["max_weight"], retained_gross_budget)
                weight_ok = current * price <= retention_cap
                predicates.append(
                    {
                        "instrument_id": symbol,
                        "condition_id": "within_retention_risk_budget",
                        "operator": "lte",
                        "operands": [current * price],
                        "threshold": retention_cap,
                        "state": "true" if weight_ok else "false",
                        "missing_reason": None,
                        "source_ref": _source(self._allocate),
                    }
                )
                quantity = current if weight_ok else retention_cap / price
                retained_gross_budget = max(
                    0.0, retained_gross_budget - quantity * price
                )
                if not weight_ok:
                    constraints.append("retention_risk_cap")
            elif row["selected"]:
                cap = min(desired_value, equity * p["max_weight"], available_gross)
                risk_ok = cap >= desired_value
                predicates.append(
                    {
                        "instrument_id": symbol,
                        "condition_id": "within_risk_budget",
                        "operator": "lte",
                        "operands": [desired_value],
                        "threshold": min(equity * p["max_weight"], available_gross),
                        "state": "true" if risk_ok else "false",
                        "missing_reason": None,
                        "source_ref": _source(self._allocate),
                    }
                )
                if not risk_ok:
                    constraints.append("risk_cap")
                cash_cap = current * price + available_cash
                cash_ok = cap <= cash_cap
                predicates.append(
                    {
                        "instrument_id": symbol,
                        "condition_id": "within_cash_budget",
                        "operator": "lte",
                        "operands": [cap],
                        "threshold": cash_cap,
                        "state": "true" if cash_ok else "false",
                        "missing_reason": None,
                        "source_ref": _source(self._allocate),
                    }
                )
                if not cash_ok:
                    cap = cash_cap
                    constraints.append("cash_reserve")
                quantity = cap / price
            else:
                quantity = 0.0
            lot = (
                self.lot_size.get(symbol, 1)
                if isinstance(self.lot_size, dict)
                else self.lot_size
            )
            if price and not pending and not unpriced_holding:
                quantity = math.floor(quantity / lot) * lot
            target_value = quantity * price if price else None
            if row["selected"] and not retained and target_value is not None:
                available_gross = max(0.0, available_gross - target_value)
                available_cash = max(
                    0.0, available_cash - max(0.0, quantity - current) * price
                )
            raw.append(
                {
                    "instrument_id": symbol,
                    "target_quantity": raw_quantity,
                    "target_weight": desired_value / equity if equity > 0 else None,
                    "reason": reason,
                }
            )
            plan.append(
                {
                    "instrument_id": symbol,
                    "target_quantity": float(quantity),
                    "target_weight": target_value / equity
                    if target_value is not None and equity > 0
                    else None,
                    "reason": reason,
                    "constraints": constraints,
                }
            )
        return raw, plan, predicates

    def _submit(self, plan, positions, recorder, cutoff):
        links = []
        for row in sorted(
            plan,
            key=lambda value: (
                value["target_quantity"] > positions.get(value["instrument_id"], 0),
                value["instrument_id"],
            ),
        ):
            symbol, target = row["instrument_id"], row["target_quantity"]
            if target == positions.get(symbol, 0.0):
                continue
            order_id = self.order_target(symbol=symbol, target=target)
            links.append(
                {
                    "instrument_id": symbol,
                    "logical_order_id": f"cross:{self._research_event_seq}:{symbol}",
                    "child_order_ids": [str(order_id)] if order_id is not None else [],
                    "fill_ids": [],
                    "submitted_at": cutoff,
                    "fill_times": [],
                    "status": "intended",
                }
            )
        if recorder is not None:
            recorder.emit("execution", {"order_links": links})

    def on_cross_section(self, trading_date, timestamp):
        cutoff = _timestamp(timestamp)
        universe = set(self.date2symbols[trading_date])
        positions = {
            symbol: float(quantity)
            for symbol, quantity in self.positions.items()
            if quantity
        }
        symbols = sorted(universe | set(positions))
        cash, equity = float(self.cash), float(self.equity)
        pending = [
            {"order_id": str(order.id), "instrument_id": str(order.symbol)}
            for order in self.get_open_orders()
        ]
        inputs, features, predicates = self._read_inputs(symbols, cutoff)
        candidates, universe_predicates = self._rank(
            symbols, universe, inputs, positions
        )
        raw, plan, allocation_predicates = self._allocate(
            candidates, inputs, positions, cash, equity, pending
        )
        self._research_event_seq += 1
        recorder = _recorder()
        if recorder is not None:
            recorder.begin_decision(
                data_event_id=f"daily_slice:{timestamp}",
                event_seq=self._research_event_seq,
                scope="portfolio",
                callback_phase="on_cross_section",
                decision_time=cutoff,
                information_cutoff=cutoff,
                market_timezone=self.timezone,
                pre_state={
                    "cash": cash,
                    "equity": equity,
                    "positions": [
                        {"instrument_id": symbol, "quantity": quantity}
                        for symbol, quantity in sorted(positions.items())
                    ],
                    "open_orders": pending,
                    "strategy_state": {"parameters": dict(self._parameters)},
                    "coverage": "complete",
                    "source": "strategy_emitted",
                },
            )
        try:
            if recorder is not None:
                recorder.emit("universe", {"predicates": universe_predicates})
                recorder.emit("features", {"features": features})
                recorder.emit("liquidity", {"predicates": predicates})
                recorder.emit("ranking", {"candidates": candidates})
                recorder.emit(
                    "allocation",
                    {
                        "predicates": allocation_predicates,
                        "raw_targets": raw,
                        "constrained_plan": plan,
                    },
                )
            self._submit(plan, positions, recorder, cutoff)
        finally:
            if recorder is not None:
                recorder.end_decision()
