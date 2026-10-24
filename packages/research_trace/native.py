"""Actual native order/fill observations, without reconstructing Python intent.

Controlled source emits its own pre-state and already-computed operands through
current_recorder. Generic sources expose only the native result's real orders.
Completion observations are separate events: later fills never become evidence
that was available at the earlier decision callback.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta

MAX_LINKS = 100_000


def _time(ns):
    return (
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(ns) // 1000)
    ).isoformat()


def _status(value):
    name = str(value).split(".")[-1].lower().replace("_", "")
    return {
        "new": "accepted",
        "accepted": "accepted",
        "submitted": "accepted",
        "filled": "filled",
        "partiallyfilled": "partially_filled",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "rejected": "rejected",
        "expired": "unfilled",
    }.get(name, "unknown")


class NativeCapture:
    def __init__(self, recorder, sink):
        self.recorder = recorder
        self.sink = sink
        self.order_origins = {}

    def append(self, record):
        self.sink(record)
        # Native is intentionally not a trust boundary. This registry only
        # preserves explicit emitted associations, never matches by timestamp.
        for stage in record.stages:
            for link in stage.order_links:
                for child in link.child_order_ids:
                    if len(self.order_origins) >= MAX_LINKS:
                        self.recorder._failed(
                            "Explicit order association capacity was reached; unmatched orders retain observable-only identity."
                        )
                        return
                    prior = self.order_origins.get(child)
                    if child in self.order_origins and prior is None:
                        # A conflict is a permanent tombstone, not an absent mapping.
                        continue
                    origin = (
                        record.data_event_id,
                        record.event_seq,
                        record.scope,
                        record.instrument_id,
                        record.callback_phase,
                        link.logical_order_id,
                    )
                    if prior is not None and prior != origin:
                        self.recorder._failed(
                            "Conflicting strategy-emitted child order associations were not resolved by guessing."
                        )
                        self.order_origins[child] = None
                    else:
                        self.order_origins[child] = origin

    def finalize(self, result, *, market_timezone):
        """Read the actual pinned engine result. Never reevaluate candidate code."""
        if self.recorder.config.mode == "off":
            return
        try:
            orders = result._raw.orders
            fills = result._raw.executions
            by_order = defaultdict(list)
            for fill in fills:
                by_order[str(fill.order_id)].append(fill)
            groups = {}
            for index, order in enumerate(orders):
                order_id = str(order.id)
                origin = self.order_origins.get(order_id)
                if origin is None:
                    key = (
                        f"native_order:{order_id}",
                        index,
                        "instrument",
                        str(order.symbol),
                        None,
                    )
                    logical_id = f"observed-native:{order_id}"
                else:
                    key = origin[:5]
                    logical_id = origin[5]
                group = groups.setdefault(
                    key,
                    {
                        "links": {},
                        "time": max(int(order.created_at), int(order.updated_at or 0)),
                        "mapped": origin is not None,
                    },
                )
                group["time"] = max(
                    group["time"], int(order.created_at), int(order.updated_at or 0)
                )
                reason = (
                    order.reject_reason
                    if type(order.reject_reason) is str and order.reject_reason
                    else None
                )
                status = _status(order.status)
                if status == "rejected" and (reason is None or len(reason) > 500):
                    status = "unknown"
                    reason = None
                    self.recorder._failed(
                        "A native rejected order had no bounded observable rejection reason; its association is retained with unknown linked status."
                    )
                link = group["links"].setdefault(
                    logical_id,
                    {
                        "logical_order_id": logical_id,
                        "child_order_ids": [],
                        "fill_ids": [],
                        "instrument_id": str(order.symbol),
                        "submitted_at": _time(order.created_at),
                        "fill_times": [],
                        "status": status,
                        "rejection_reason": reason,
                        "source": "engine_observed",
                    },
                )
                link["child_order_ids"].append(order_id)
                link["submitted_at"] = min(
                    link["submitted_at"], _time(order.created_at)
                )
                if link["status"] != status:
                    link["status"] = (
                        "partially_filled"
                        if float(order.filled_quantity) > 0 or link["fill_ids"]
                        else "unknown"
                    )
                for fill in by_order.get(order_id, ()):
                    link["fill_ids"].append(str(fill.id))
                    link["fill_times"].append(_time(fill.timestamp))
                    group["time"] = max(group["time"], int(fill.timestamp))
            for key, group in groups.items():
                event, sequence, scope, instrument, original_phase = key
                phase = "native.execution_result"
                if original_phase is not None:
                    phase += ":" + (
                        original_phase
                        if len(original_phase) <= 170
                        else hashlib.sha256(original_phase.encode()).hexdigest()
                    )
                moment = _time(group["time"])
                self.recorder.begin_decision(
                    data_event_id=event,
                    event_seq=sequence,
                    scope=scope,
                    instrument_id=instrument,
                    callback_phase=phase,
                    decision_time=moment,
                    information_cutoff=moment,
                    market_timezone=market_timezone,
                    pre_state=None,
                    pre_state_missing_reason="This is the final observable native execution snapshot, not a new strategy callback; original decision pre-state is available only when explicitly emitted.",
                )
                reason = (
                    "Actual final native order states and fills are linked by emitted child IDs; intermediate order transitions are not collected."
                    if group["mapped"]
                    else "Only actual native orders and fills are observable. The native order ID is an unpaired execution-event identity, not a recovered market-data callback or strategy intent; internal conditions and decision pre-state are unavailable."
                )
                self.recorder.emit(
                    "execution_result",
                    {
                        "coverage": "partial",
                        "missing_reason": reason,
                        "order_links": list(group["links"].values()),
                    },
                )
                self.recorder.end_decision()
        except Exception:  # noqa: BLE001 - observer must never alter economic execution
            self.recorder._failed(
                "Native execution linkage could not be fully collected; no missing event was inferred."
            )
