"""Trusted input construction. No strategy import, engine or future metadata.

This builder accepts trusted, normalized records. Its prefix tests verify this
projection, not a candidate's point-in-time honesty or framework equivalence.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

from .contracts import (
    AccountView,
    BarView,
    ListingView,
    Observation,
    OpenOrderView,
    OrderEvent,
    StrategyManifest,
    validate_observation,
)
from .json import canonical_json, hash_json
from .state import StateSnapshot


def observation_hash(observation: Observation) -> str:
    return hash_json(observation.model_dump(mode="json", exclude={"snapshot_id"}))


def build_observation(
    *,
    manifest: StrategyManifest,
    decision_time: datetime,
    next_open_time: datetime,
    session_date: date,
    listings: Iterable[ListingView],
    history: Iterable[BarView],
    account: AccountView,
    state: StateSnapshot,
    open_orders: Iterable[OpenOrderView] = (),
    order_events: Iterable[OrderEvent] = (),
    warmup: bool | None = None,
) -> Observation:
    """Project only effective, known listings and their bounded available bars.

    Sorting and hashing depend only on the visible projection. A longer source,
    future prices, future membership, or future event cannot affect the bytes.
    The caller supplies the trusted calendar's next open, not the next data row.
    Never infer a last bar, test-window end or total row count in the policy view.
    Missing any current listing's complete bar rejects the entire snapshot.
    """
    manifest = StrategyManifest.model_validate(manifest)
    normalized = []
    for listing in listings:
        if listing.known_at > decision_time or listing.effective_from > session_date:
            continue
        end_known = (
            listing.effective_to is not None
            and listing.effective_to_known_at is not None
            and listing.effective_to_known_at <= decision_time
        )
        if end_known and session_date >= listing.effective_to:
            continue
        if not end_known:
            fields = listing.model_dump()
            fields["effective_to"] = None
            fields["effective_to_known_at"] = None
            listing = ListingView.model_validate(fields)
        normalized.append(listing)
    effective = tuple(sorted(normalized, key=lambda listing: listing.listing_id))
    ids = {listing.listing_id for listing in effective}
    bounded = {}
    seen = set()
    for bar in history:
        if (
            bar.listing_id not in ids
            or bar.session_date > session_date
            or bar.completed_at > decision_time
            or bar.available_at > decision_time
        ):
            continue
        key = (bar.session_date, bar.listing_id)
        if key in seen:
            raise ValueError("duplicate visible history record")
        seen.add(key)
        bounded.setdefault(bar.listing_id, []).append(bar)
    selected = []
    counts = {}
    for listing_id, bars in bounded.items():
        bars = sorted(bars, key=lambda bar: bar.session_date)[
            -manifest.data.lookback_bars :
        ]
        counts[listing_id] = len(bars)
        selected.extend(bars)
    selected.sort(key=lambda bar: (bar.session_date, bar.listing_id))
    visible_events = tuple(
        sorted(
            (
                event
                for event in order_events
                if event.occurred_at <= decision_time and event.listing_id in ids
            ),
            key=lambda event: (event.occurred_at, event.event_id),
        )
    )
    # Account and open orders are exact current engine snapshots. Do not silently
    # discard out-of-scope holdings/orders; a universe transition needs an
    # explicit runtime liquidation/retention policy instead.
    observation = Observation(
        snapshot_id="0" * 64,
        decision_time=decision_time,
        next_open_time=next_open_time,
        session_date=session_date,
        listings=effective,
        history=tuple(selected),
        account=AccountView(
            as_of=account.as_of,
            currency=account.currency,
            cash=account.cash,
            equity=account.equity,
            positions=tuple(
                sorted(account.positions, key=lambda position: position.listing_id)
            ),
        ),
        open_orders=tuple(sorted(open_orders, key=lambda order: order.order_id)),
        order_events=visible_events,
        state=state,
        warmup=warmup
        if warmup is not None
        else any(
            counts.get(listing.listing_id, 0) < manifest.data.warmup_bars
            for listing in effective
        ),
    )
    validate_observation(observation, manifest)
    payload = observation.model_dump(mode="json")
    payload["snapshot_id"] = observation_hash(observation)
    result = Observation.model_validate_json(canonical_json(payload))
    canonical_json(result)  # enforce complete transport byte/depth/count budget
    return result
