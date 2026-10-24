"""Copy trusted portable observations without inspecting or executing policy code.

The portable ABI exposes replies, not internal filters, features or predicates.
Those missing internals stay explicitly unsupported. Native receipt/fill facts are
published as later execution-result events, never added retroactively to a
streamed decision as if they were known at its information cutoff.
"""

from __future__ import annotations

from datetime import datetime
from functools import wraps

from packages.strategy_sdk import PortfolioReply, canonical_json, hash_json

INTERNALS_MISSING = (
    "The portable.v1 reply does not expose internal features, predicates, "
    "eligibility, ranking, tie-breaks or reasons. They were not inferred from "
    "scores, targets, order intents or later fills."
)


def _observation_only(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.recorder is None or (
            self.recorder.config.mode == "off" and method.__name__ != "begin"
        ):
            return None
        try:
            return method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - trace failure must not change economics
            self.recorder._failed(
                "A portable runtime observation could not be collected; no value "
                "was reconstructed and execution continued unchanged."
            )
            return None

    return guarded


def _feature(feature_id, value, cutoff, *, unit="json", source="adapter_observed"):
    return {
        "feature_id": feature_id,
        "value": value,
        "validity": "valid",
        "unit": unit,
        "calculation_window": "already_observed_protocol_or_native_record",
        "information_cutoff": cutoff,
        "source": source,
    }


def _source_event(observation):
    # snapshot_id also includes account/state and therefore cannot pair variants.
    # Hash only the complete current market event, not policy parameters, account,
    # lookback requirements or a whole-file checksum containing future bars.
    return hash_json(
        {
            "schema": "portable.complete_market_event.v1",
            "event": observation.event,
            "session_date": observation.session_date.isoformat(),
            "decision_time": observation.decision_time.isoformat(),
            "listings": [
                listing.model_dump(mode="json") for listing in observation.listings
            ],
            "bars": [
                bar.model_dump(mode="json")
                for bar in observation.history
                if bar.session_date == observation.session_date
            ],
        }
    )


def _execution_status(orders):
    statuses = {order["status"] for order in orders}
    if statuses == {"filled"}:
        return "filled", None
    if any(order["filled_quantity"] > 0 for order in orders):
        return "partially_filled", None
    if statuses == {"rejected"}:
        reasons = [
            order["reject_reason"]
            for order in orders
            if order["status"] == "rejected" and order["reject_reason"]
        ]
        # An unavailable native reason must not be invented to satisfy a schema.
        combined = "; ".join(reasons)
        return (
            ("rejected", combined)
            if len(reasons) == len(orders) and len(combined) <= 500
            else ("unknown", None)
        )
    if statuses == {"cancelled"}:
        return "cancelled", None
    if statuses <= {"accepted", "expired", "cancelled"}:
        return "unfilled", None
    return "unknown", None


class PortableCapture:
    """Bounded adapter around the caller-owned streaming or in-memory Recorder."""

    def __init__(self, recorder):
        self.recorder = recorder
        # One small origin per already bounded (<= 5,000) controller callback.
        # No policy objects, observations, decisions or result rows are retained.
        self._origins = {}
        self._cutoff = None
        self._native_available = False

    @_observation_only
    def begin(self, controller, observation, broker):
        sequence = len(controller.trace)
        if self.recorder.config.mode == "off":
            # Count disabled callbacks without hashing or copying their detail.
            self.recorder.begin_decision(
                data_event_id=observation.snapshot_id,
                event_seq=sequence,
                scope="portfolio",
                callback_phase="portable.complete_daily_cross_section",
                decision_time=observation.decision_time,
                information_cutoff=observation.decision_time,
                market_timezone=controller.inputs.manifest.timezone,
            )
            return
        event_id = _source_event(observation)
        self._origins[sequence] = event_id
        self._cutoff = observation.decision_time
        self._native_available = broker is not None
        open_orders = []
        for order in observation.open_orders:
            item = order.model_dump(mode="json")
            item["native_order_ids"] = list(
                controller.pending[order.order_id]["native_order_ids"]
            )
            open_orders.append(item)
        # The SDK's warmup account is a declared cash-only projection, not a
        # native broker read. Do not present that projection as engine evidence.
        pre_state = {
            "cash": float(broker.cash) if broker is not None else None,
            "equity": float(broker.equity) if broker is not None else None,
            "positions": [
                position.model_dump(mode="json")
                for position in observation.account.positions
            ]
            if broker is not None
            else None,
            "open_orders": open_orders if broker is not None else None,
            "strategy_state": observation.state.model_dump(mode="json"),
            "coverage": "complete" if broker is not None else "partial",
            "missing_reason": None
            if broker is not None
            else (
                "Warmup is state-only replay before native broker startup; "
                "no native account, positions or open orders were observed."
            ),
            "source": "engine_observed" if broker is not None else "adapter_observed",
        }
        self.recorder.begin_decision(
            data_event_id=event_id,
            event_seq=sequence,
            scope="portfolio",
            callback_phase="portable.complete_daily_cross_section",
            decision_time=observation.decision_time,
            information_cutoff=observation.decision_time,
            market_timezone=controller.inputs.manifest.timezone,
            pre_state=pre_state,
        )
        self.recorder.emit(
            "portable.policy_internals",
            {"coverage": "unsupported", "missing_reason": INTERNALS_MISSING},
        )
        self.recorder.emit(
            "portable.observation",
            {
                "features": [
                    _feature(
                        "portable.observation.snapshot_sha256",
                        observation.snapshot_id,
                        self._cutoff,
                        unit="sha256",
                    ),
                    _feature(
                        "portable.observation.warmup",
                        observation.warmup,
                        self._cutoff,
                        unit="boolean",
                    ),
                    _feature(
                        "portable.observation.order_events",
                        canonical_json(
                            [
                                event.model_dump(mode="json")
                                for event in observation.order_events
                            ]
                        ).decode(),
                        self._cutoff,
                    ),
                ]
            },
        )

    @_observation_only
    def received(self, observation, reply):
        portfolio = isinstance(reply, PortfolioReply)
        scores = (
            {row.listing_id: row.value for row in reply.decision.scores}
            if portfolio
            else {}
        )
        holdings = {row.listing_id for row in observation.account.positions}
        candidates = [
            {
                "instrument_id": listing.listing_id,
                "universe_member": True,
                "eligibility": "unknown",
                "reason": "not_collected",
                "reason_detail": INTERNALS_MISSING,
                "score": scores.get(listing.listing_id),
                "score_validity": "valid"
                if listing.listing_id in scores
                else "not_collected",
                "selected": None,
                "prior_holding": listing.listing_id in holdings
                if self._native_available
                else None,
                "source": "adapter_observed",
            }
            for listing in observation.listings
        ]
        targets = (
            [
                {**target.model_dump(mode="json"), "basis": reply.decision.basis}
                for target in reply.decision.targets
            ]
            if portfolio
            else None
        )
        self.recorder.emit(
            "portable.policy_reply",
            {
                "coverage": "partial",
                "missing_reason": INTERNALS_MISSING,
                "features": [
                    _feature(
                        "portable.reply.json",
                        canonical_json(reply).decode(),
                        self._cutoff,
                    ),
                    _feature(
                        "portable.reply.sha256",
                        hash_json(reply),
                        self._cutoff,
                        unit="sha256",
                    ),
                ],
                "candidates": candidates,
                "raw_targets": targets,
            },
        )
        if not portfolio:
            self.recorder.emit(
                "portable.order_intents",
                {
                    # These are the exact received order/cancel intents, not fills.
                    "constrained_plan": [
                        intent.model_dump(mode="json")
                        for intent in reply.decision.intents
                    ]
                },
            )

    @_observation_only
    def planned(self, controller, plans, logical_ids, outcome):
        submitted = {
            logical_id: controller.pending[logical_id] for logical_id in logical_ids
        }
        self.recorder.emit(
            "portable.controller_plan",
            {
                "coverage": "partial" if logical_ids else "complete",
                "missing_reason": (
                    "Submission receipts expose actual native child IDs, not final "
                    "status, creation time or fills. Those are observed separately "
                    "from the native execution result."
                )
                if logical_ids
                else None,
                "features": [
                    _feature(
                        "portable.controller.outcome",
                        outcome,
                        self._cutoff,
                        unit="enum",
                    )
                ],
                "constrained_plan": [
                    {"listing_id": key, "side": side, "quantity": quantity}
                    for key, side, quantity in plans
                ],
                "order_links": [
                    {
                        "logical_order_id": logical_id,
                        "child_order_ids": submitted[logical_id]["native_order_ids"],
                        "fill_ids": [],
                        "instrument_id": submitted[logical_id]["listing_id"],
                        "submitted_at": None,
                        "fill_times": [],
                        "status": "unknown",
                        "source": "adapter_observed",
                    }
                    for logical_id in logical_ids
                ],
            },
        )

    @_observation_only
    def end(self):
        self.recorder.end_decision()

    @_observation_only
    def finalize(self, controller, bundle):
        """Use only the already validated native export, never derived fills."""
        self.recorder.end_decision()
        orders, fills = {}, {}
        for order in bundle["orders"]:
            orders.setdefault(order["logical_order_id"], []).append(order)
        for fill in bundle["fills"]:
            fills.setdefault(fill["logical_order_id"], []).append(fill)
        completion = max(
            [controller.current_session.close_time]
            + [
                datetime.fromisoformat(row["native_updated_at"])
                for row in bundle["orders"]
            ]
            + [datetime.fromisoformat(row["native_at"]) for row in bundle["fills"]]
        )
        for decision in controller.trace:
            logical_ids = decision["logical_order_ids"]
            if not logical_ids or decision["sequence"] not in self._origins:
                continue
            links, execution_rows, incomplete = [], [], False
            for logical_id in logical_ids:
                native_orders, native_fills = (
                    orders[logical_id],
                    fills.get(logical_id, []),
                )
                status, reason = _execution_status(native_orders)
                incomplete = incomplete or status == "unknown"
                links.append(
                    {
                        "logical_order_id": logical_id,
                        "child_order_ids": [row["order_id"] for row in native_orders],
                        "fill_ids": [row["fill_id"] for row in native_fills],
                        "instrument_id": native_orders[0]["listing_id"],
                        "submitted_at": min(
                            datetime.fromisoformat(row["native_created_at"])
                            for row in native_orders
                        ),
                        "fill_times": [
                            datetime.fromisoformat(row["native_at"])
                            for row in native_fills
                        ],
                        "status": status,
                        "rejection_reason": reason,
                        "source": "engine_observed",
                    }
                )
                execution_rows.append(
                    {
                        "logical_order_id": logical_id,
                        "orders": native_orders,
                        "fills": native_fills,
                    }
                )
            self.recorder.begin_decision(
                data_event_id=self._origins[decision["sequence"]],
                event_seq=decision["sequence"],
                scope="portfolio",
                callback_phase="portable.execution_result",
                decision_time=completion,
                information_cutoff=completion,
                market_timezone=controller.inputs.manifest.timezone,
                pre_state_missing_reason=(
                    "This is a later native execution-result observation, not a "
                    "decision callback. Decision-before state belongs to the "
                    "matching source event's complete_daily_cross_section record."
                ),
            )
            self.recorder.emit(
                "portable.native_execution",
                {
                    "coverage": "partial" if incomplete else "complete",
                    "missing_reason": (
                        "A native rejection/status lacked a bounded, unambiguous "
                        "logical-order outcome; "
                        "no final status or rejection reason was inferred."
                    )
                    if incomplete
                    else None,
                    "features": [
                        _feature(
                            "portable.native_execution.receipts",
                            canonical_json(execution_rows).decode(),
                            completion,
                            source="engine_observed",
                        )
                    ],
                    "order_links": links,
                },
            )
            self.recorder.end_decision()
