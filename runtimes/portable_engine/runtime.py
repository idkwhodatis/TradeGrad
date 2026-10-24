"""Reviewed AKQuant adapter, isolated from policy source and policy objects.

Only ``execute`` imports AKQuant/pandas, and it must run inside the engine
container. The controller and sizing routines are testable using inert fakes.
No exec, policy import, pickle, host backtest fallback, or synthetic liquidation.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.portable import PortableCapture
from packages.strategy_sdk import (
    AccountView,
    BarView,
    CancelIntent,
    JsonLimits,
    ListingView,
    OpenOrderView,
    OrderEvent,
    PortfolioReply,
    PositionView,
    StateCodec,
    StateSnapshot,
    StrategyManifest,
    build_observation,
    canonical_json,
    hash_json,
    parse_json,
    parse_manifest,
    parse_reply,
)
from packages.strategy_sdk.base import utc_timestamp

REQUEST_VERSION = "portable-engine-request.v1"
MAX_DATA_BYTES = 32 * 1024 * 1024
DATA_LIMITS = JsonLimits(
    max_bytes=MAX_DATA_BYTES, max_nodes=2_000_000, max_items=100_000
)
MAX_SESSIONS = 5000


class NativeVolumeLimit:
    """Apply the reviewed cap through the pinned public pre-run analyzer API.

    AKQuant 0.3.27's wrapper skips its setter for exactly 0.25 although the
    native execution client's default is unlimited. Do not perturb the value
    or replace native matching. The wrapper catches analyzer startup errors,
    so every native callback and the final return must verify this latch.
    """

    name = "tradegrad_reviewed_volume_limit"

    def __init__(self, volume_limit_pct):
        if type(volume_limit_pct) is not float or volume_limit_pct != 0.25:
            raise ValueError("unreviewed native volume limit")
        self.volume_limit_pct = volume_limit_pct
        self.applied = False

    def on_start(self, context):
        self.applied = False
        try:
            context["engine"].set_volume_limit(self.volume_limit_pct)
        except Exception:  # noqa: BLE001 - upstream logs hook errors; emit no native payload
            raise ValueError("native volume-limit configuration failed") from None
        self.applied = True

    def require_applied(self):
        if not self.applied:
            raise ValueError("native volume-limit configuration is unverified")

    def on_bar(self, context):
        pass

    def on_trade(self, context):
        pass

    def on_finish(self, context):
        self.require_applied()
        return {"volume_limit_configured": True}


def reviewed_profile(market: str, initial_cash: float) -> dict:
    if (
        market not in {"CN", "US"}
        or type(initial_cash) not in {int, float}
        or not math.isfinite(initial_cash)
        or not 0 < initial_cash <= 1e12
    ):
        raise ValueError("invalid cash/market")
    return {
        "profile_version": "akquant.portable.cash-daily.controller-t1.v3",
        "engine": "akquant",
        "engine_version": "0.3.27",
        "market": market,
        "initial_cash": float(initial_cash),
        "commission_rate": 0.0005,
        "stamp_tax_rate": 0.001 if market == "CN" else 0.0,
        "transfer_fee_rate": 0.0,
        "min_commission": 0.0,
        "slippage": {"type": "percent", "value": 0.0005},
        "volume_limit_pct": 0.25,
        # Only the native settlement switch is T+0. CN T+1 is the separate,
        # explicitly versioned controller constraint, not claimed native parity.
        "t_plus_one": False,
        "engine_settlement_mode": "T+0",
        "application_settlement_enforcement": "trusted_controller_daily_next_open_inventory.v1",
        "application_t_plus_one": market == "CN",
        "initial_positions": "flat",
        "policy_callback": "complete_daily_cross_section_after_close",
        "intraday_policy": "unsupported",
        "external_funding": "unsupported",
        "corporate_actions": "reject_window_with_actions",
        "limit_execution": "reject_non_open_or_limit_breaching_native_fill",
        "lot_size": 100 if market == "CN" else 1,
        "timezone": "Asia/Shanghai" if market == "CN" else "America/New_York",
        "risk_config": {"account_mode": "cash", "enable_short_sell": False},
        "fill_policy": "NextOpen",
        "time_in_force": "GTC",
        "sizing_price": "decision_close",
        "sizing_equity": "decision_close_native_equity",
        "cash_budget": "available_cash_without_anticipated_sale_proceeds",
        "sell_budget": "decision_close_holdings_less_pending_sells",
        "sellability_observation": "next_open_unreserved_holdings",
        "unsettled_next_open_sell": "admit_after_close_with_controller_t1",
        "pending_rebalance": "skip_until_all_native_legs_terminal",
        "final_liquidation": False,
    }


def profile_sha256(market: str, initial_cash: float) -> str:
    return hash_json(reviewed_profile(market, initial_cash))


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(f"invalid {label} fields")


def _hash(value):
    return (
        type(value) is str
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def from_ns(value: int) -> datetime:
    # Python datetime is microsecond resolution; original integer ns retained in exports.
    if type(value) is not int:
        raise ValueError("native nanosecond timestamp required")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value // 1000)


def to_ns(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000


@dataclass(frozen=True)
class Session:
    session_date: date
    open_time: datetime
    close_time: datetime
    next_open_time: datetime

    @classmethod
    def parse(cls, raw):
        _keys(
            raw,
            {"session_date", "open_time", "close_time", "next_open_time"},
            "session",
        )
        item = cls(
            date.fromisoformat(raw["session_date"]),
            *(
                utc_timestamp(raw[k])
                for k in ("open_time", "close_time", "next_open_time")
            ),
        )
        if not item.open_time < item.close_time < item.next_open_time:
            raise ValueError("invalid exchange session times")
        return item


def verify_calendar(manifest, sessions):
    """Independently reject missing/non-session rows and invented next opens."""
    import exchange_calendars as xc

    template = xc.get_calendar(manifest.calendar)
    calendar = type(template)(
        start=sessions[0].session_date - timedelta(days=10),
        end=sessions[-1].session_date + timedelta(days=20),
    )
    expected = [
        v.date()
        for v in calendar.sessions_in_range(
            sessions[0].session_date, sessions[-1].session_date
        )
    ]
    if expected != [s.session_date for s in sessions]:
        raise ValueError("incomplete exchange calendar session range")
    for item in sessions:
        if (
            calendar.session_open(item.session_date).to_pydatetime(),
            calendar.session_close(item.session_date).to_pydatetime(),
            calendar.session_open(
                calendar.next_session(item.session_date)
            ).to_pydatetime(),
        ) != (item.open_time, item.close_time, item.next_open_time):
            raise ValueError("staged times differ from independent exchange calendar")


@dataclass
class Inputs:
    manifest: StrategyManifest
    window: dict
    initial_cash: float
    state: StateSnapshot
    identity: dict
    listings: tuple[ListingView, ...]
    bars: tuple[BarView, ...]
    sessions: tuple[Session, ...]
    profile: dict
    by_session: dict = field(default_factory=dict)
    capture_config: CaptureConfig | None = None

    def effective(self, session: Session):
        return tuple(
            l
            for l in self.listings
            if l.effective_from <= session.session_date
            and (l.effective_to is None or session.session_date < l.effective_to)
            and l.known_at <= session.close_time
        )


def load_inputs(
    request_raw: bytes, market_raw: bytes, *, calendar_verifier=verify_calendar
) -> Inputs:
    request = parse_json(request_raw)
    _keys(
        request,
        {
            "schema_version",
            "manifest",
            "window",
            "initial_cash",
            "initial_state",
            "input_identity",
        }
        | (
            {"research_capture"}
            if type(request) is dict and "research_capture" in request
            else set()
        )
        | (
            {"replay_rng_state"}
            if type(request) is dict and "replay_rng_state" in request
            else set()
        ),
        "engine request",
    )
    if request["schema_version"] != REQUEST_VERSION:
        raise ValueError("unsupported engine request")
    if "replay_rng_state" in request:
        from packages.tradegrad_adapter.runtime_observation import validate_rng_state

        # This state belongs exclusively to the policy process. The trusted
        # engine validates staging but never consumes or restores policy RNG.
        validate_rng_state(request["replay_rng_state"], include_numpy=False)
    manifest = parse_manifest(canonical_json(request["manifest"]))
    _keys(request["window"], {"start", "end"}, "window")
    start, end = (date.fromisoformat(request["window"][k]) for k in ("start", "end"))
    if end <= start:
        raise ValueError("empty half-open evaluation window")
    profile = reviewed_profile(manifest.market, request["initial_cash"])
    if manifest.engine_profile.profile_sha256 != hash_json(profile):
        raise ValueError("manifest does not identify reviewed engine profile")
    state = StateSnapshot.model_validate_json(canonical_json(request["initial_state"]))
    StateCodec(manifest.state_schema).decode(state)
    identity = request["input_identity"]
    _keys(identity, {"run_config_sha256", "market_data_sha256"}, "input identity")
    if (
        not all(_hash(v) for v in identity.values())
        or identity["market_data_sha256"] != hashlib.sha256(market_raw).hexdigest()
    ):
        raise ValueError("market artifact checksum mismatch")
    market = parse_json(market_raw, limits=DATA_LIMITS)
    _keys(market, {"listings", "bars", "sessions", "corporate_actions"}, "market data")
    if market[
        "corporate_actions"
    ] != [] or manifest.prices.actions_snapshot_sha256 != hash_json([]):
        raise ValueError(
            "corporate actions unsupported; reject window, never ignore actions"
        )
    if manifest.universe.snapshot_sha256 != hash_json(market["listings"]):
        raise ValueError("universe snapshot checksum mismatch")
    listings = tuple(
        ListingView.model_validate_json(canonical_json(v)) for v in market["listings"]
    )
    bars = tuple(BarView.model_validate_json(canonical_json(v)) for v in market["bars"])
    sessions = tuple(Session.parse(v) for v in market["sessions"])
    if not 1 <= len(listings) <= manifest.data.max_listings or len(
        {l.listing_id for l in listings}
    ) != len(listings):
        raise ValueError("invalid or duplicate listing universe")
    # AKQuant symbol is the immutable listing ID, not a mutable/reused ticker.
    if any(
        (l.currency, l.calendar, l.timezone, l.lot_size)
        != (
            manifest.currency,
            manifest.calendar,
            manifest.timezone,
            profile["lot_size"],
        )
        for l in listings
    ):
        raise ValueError("listing market/lot semantics mismatch")
    if not 1 <= len(sessions) <= MAX_SESSIONS or any(
        a.session_date >= b.session_date for a, b in itertools.pairwise(sessions)
    ):
        raise ValueError("session range is empty, unsorted, or oversized")
    if (
        sessions[-1].session_date >= end
        or sessions[-1].session_date < start
        or not any(start <= s.session_date < end for s in sessions)
    ):
        raise ValueError("sessions must cover only warmup and nonempty active window")
    # Verify the entire active calendar range, including a missing final session.
    calendar_verifier(manifest, sessions)
    if calendar_verifier is verify_calendar:
        import exchange_calendars as xc

        cal = xc.get_calendar(
            manifest.calendar,
            start=start - timedelta(days=10),
            end=end + timedelta(days=10),
        )
        expected_active = [
            v.date() for v in cal.sessions_in_range(start, end - timedelta(days=1))
        ]
        if expected_active != [
            s.session_date for s in sessions if s.session_date >= start
        ]:
            raise ValueError("active window has missing exchange sessions")
    inputs = Inputs(
        manifest,
        request["window"],
        float(request["initial_cash"]),
        state,
        identity,
        listings,
        bars,
        sessions,
        profile,
        capture_config=(
            CaptureConfig.model_validate(request["research_capture"])
            if "research_capture" in request
            else None
        ),
    )
    known_sessions = {s.session_date: s for s in sessions}
    for bar in bars:
        s = known_sessions.get(bar.session_date)
        if (
            s is None
            or bar.completed_at != s.close_time
            or bar.available_at > s.close_time
        ):
            raise ValueError(
                "bar is unavailable at decision close or outside staged calendar"
            )
        per_session = inputs.by_session.setdefault(bar.session_date, {})
        if bar.listing_id in per_session:
            raise ValueError("duplicate listing/session bar")
        per_session[bar.listing_id] = bar
    for s in sessions:
        effective = inputs.effective(s)
        if not effective or set(inputs.by_session.get(s.session_date, {})) != {
            l.listing_id for l in effective
        }:
            raise ValueError(
                "incomplete effective cross-section; missingness cannot shrink universe"
            )
    return inputs


def lot_floor(quantity: float, lot: int) -> int:
    if (
        type(quantity) not in {float, int}
        or not math.isfinite(quantity)
        or quantity < 0
    ):
        raise ValueError("invalid quantity")
    return (
        int(
            (Decimal(str(quantity)) / Decimal(lot)).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        * lot
    )


def next_open_sell_capacity(observation, profile):
    """Held shares minus every observed sell leaf; never anticipated buys.

    This is next-session order capacity, NOT current-session CN sellability.
    Both returned capacity and the labelled SDK view must agree. The engine's
    T+0 availability is deliberately not exposed as a T+1 observation.
    """
    if profile != reviewed_profile(profile["market"], profile["initial_cash"]):
        raise ValueError(
            "next-open capacity requires exact reviewed controller profile"
        )
    capacity = {p.listing_id: p.quantity for p in observation.account.positions}
    for order in observation.open_orders:
        if order.side == "sell":
            capacity[order.listing_id] = (
                capacity.get(order.listing_id, 0.0) - order.remaining_quantity
            )
    if any(quantity < 0 for quantity in capacity.values()):
        raise ValueError("pending sells exceed decision-close holdings")
    if any(
        p.available_quantity != capacity[p.listing_id]
        for p in observation.account.positions
    ):
        raise ValueError(
            "SDK next-open capacity disagrees with native holdings/reservations"
        )
    return capacity


def validate_sell_plans(plans, observation, profile):
    """Whole-reply reservation induction, independent of native T+0 availability.

    At close d all sell leaves for future sessions fit H(d), with no pending buy
    credit. A partial sell reduces held and reserved equally; newly filled buys
    can enlarge capacity only at the next complete close. No financial ledger
    is reconstructed here: each H(d) is read from AKQuant itself.
    """
    capacity = next_open_sell_capacity(observation, profile)
    for key, side, quantity in plans:
        if side == "sell":
            if quantity > capacity.get(key, 0.0):
                raise ValueError("sell plan exceeds next-open unreserved holdings")
            capacity[key] -= quantity


def target_orders(decision, observation, profile):
    """Deterministic close-sized deltas; sell proceeds are never a buy budget."""
    if decision.action in {"hold", "no_decision"}:
        return []
    prices = {
        b.listing_id: b.close
        for b in observation.history
        if b.session_date == observation.session_date
    }
    holdings = {p.listing_id: p for p in observation.account.positions}
    sell_capacity = next_open_sell_capacity(observation, profile)
    desired = {t.listing_id: t.value for t in decision.targets}
    lot = profile["lot_size"]
    available = Decimal(str(observation.account.cash))
    plans = []
    for listing in sorted(observation.listings, key=lambda l: l.listing_id):
        key = listing.listing_id
        position = holdings.get(key)
        held = position.quantity if position else 0.0
        if key not in desired:
            raw_target = held if decision.omitted_positions == "preserve" else 0.0
        elif decision.basis == "target_quantity":
            raw_target = desired[key]
        elif decision.basis == "target_notional":
            raw_target = desired[key] / prices[key]
        else:
            raw_target = desired[key] * observation.account.equity / prices[key]
        target = lot_floor(raw_target, lot)
        delta = target - held
        if delta < 0:
            qty = lot_floor(min(-delta, sell_capacity.get(key, 0.0)), lot)
            if qty:
                plans.append((key, "sell", qty))
        elif delta > 0:
            # Includes reviewed commission/slippage reserve; an adverse future gap
            # remains the engine's reject/partial-fill decision, not a future peek.
            unit = (
                Decimal(str(prices[key]))
                * (1 + Decimal(str(profile["slippage"]["value"])))
                * (1 + Decimal(str(profile["commission_rate"])))
            )
            qty = min(
                lot_floor(delta, lot),
                int((available / unit / lot).to_integral_value(rounding=ROUND_FLOOR))
                * lot,
            )
            if qty:
                plans.append((key, "buy", qty))
                available -= Decimal(qty) * unit
    return sorted(plans, key=lambda item: (item[1] != "sell", item[0]))


def status_name(value) -> str:
    name = str(value).split(".")[-1].replace("_", "").lower()
    mapping = {
        "new": "accepted",
        "submitted": "accepted",
        "accepted": "accepted",
        "partiallyfilled": "partial_fill",
        "filled": "filled",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "rejected": "rejected",
        "expired": "expired",
    }
    if name not in mapping:
        raise ValueError(f"unsupported native order status: {value}")
    return mapping[name]


class Controller:
    """Trusted sequencing, account projection, deduplication and order planning."""

    def __init__(self, inputs: Inputs, exchange: Callable, research_recorder=None):
        if inputs.profile != reviewed_profile(
            inputs.manifest.market, inputs.initial_cash
        ):
            raise ValueError("controller requires exact reviewed portable profile")
        self.inputs, self.exchange = inputs, exchange
        self.state = inputs.state
        self.trace, self.logical_orders = [], []
        self.signals, self.completed = set(), set()
        self.pending, self.native_to_logical = {}, {}
        self.order_events = []
        self.current_session = None
        self.native_start_verified = False
        self.research = PortableCapture(research_recorder)

    def verify_native_start(self, broker):
        """Called before the first native bar callback; no seeded inventory.

        AKQuant 0.3.27 exposes Strategy.positions as a property, not the removed
        Strategy.get_positions method. Keep the reviewed native API explicit.
        """
        if self.native_start_verified:
            return
        if (
            any(broker.positions.values())
            or self.pending
            or float(broker.cash) != self.inputs.initial_cash
            or float(broker.equity) != self.inputs.initial_cash
        ):
            raise ValueError(
                "controller profile requires a flat native cash-only start"
            )
        self.native_start_verified = True

    def _refresh(self, broker):
        events = []
        for logical_id, record in list(self.pending.items()):
            statuses, filled, native_quantity, remaining = [], 0.0, 0.0, 0.0
            for leg_index, oid in enumerate(record["native_order_ids"]):
                native = broker.get_order(oid)
                if native is None:
                    raise ValueError("native order receipt cannot be resolved")
                status = status_name(native.status)
                statuses.append(status)
                quantity, leg_filled = (
                    float(native.quantity),
                    float(native.filled_quantity),
                )
                previous = record["last_events"].get(oid)
                if (
                    not math.isfinite(quantity)
                    or not math.isfinite(leg_filled)
                    or quantity <= 0
                    or not 0 <= leg_filled <= quantity
                    or (previous is not None and leg_filled < previous[1])
                    or (status == "filled" and leg_filled != quantity)
                ):
                    raise ValueError("invalid or regressing native receipt quantities")
                native_quantity += quantity
                filled += leg_filled
                if status not in {"filled", "cancelled", "rejected", "expired"}:
                    remaining += quantity - leg_filled
                if leg_filled > (previous[1] if previous else 0.0):
                    day = (
                        from_ns(int(native.updated_at))
                        .astimezone(
                            __import__("zoneinfo").ZoneInfo(
                                self.inputs.manifest.timezone
                            )
                        )
                        .date()
                    )
                    if day <= date.fromisoformat(record["decision_session"]):
                        raise ValueError(
                            "native fill violates strictly-later session boundary"
                        )
                    if day != self.current_session.session_date:
                        raise ValueError(
                            "unobserved native fill crossed a decision barrier"
                        )
                    bar = self.inputs.by_session[day][record["listing_id"]]
                    if record["order_type"] == "limit" and (
                        (record["side"] == "buy" and bar.open > record["limit_price"])
                        or (
                            record["side"] == "sell"
                            and bar.open < record["limit_price"]
                        )
                    ):
                        raise ValueError("unsupported native intrabar-only limit fill")
                marker = (status, leg_filled, int(native.updated_at))
                if record["last_events"].get(oid) != marker:
                    occurred = from_ns(int(native.updated_at))
                    if occurred > self.current_session.close_time:
                        raise ValueError("native order event is future-dated")
                    events.append(
                        OrderEvent(
                            event_id=hash_json(
                                [
                                    logical_id,
                                    leg_index,
                                    marker[0],
                                    marker[1],
                                    str(marker[2]),
                                ]
                            ),
                            order_id=logical_id,
                            signal_id=record["signal_id"],
                            listing_id=record["listing_id"],
                            occurred_at=occurred,
                            status=status,
                            filled_quantity=float(native.filled_quantity),
                        )
                    )
                    record["last_events"][oid] = marker
            if native_quantity != record["quantity"]:
                raise ValueError(
                    "native receipt leg quantities do not match submitted order"
                )
            record["filled_quantity"] = filled
            record["remaining_quantity"] = remaining
            if all(
                s in {"filled", "cancelled", "rejected", "expired"} for s in statuses
            ):
                record["terminal"] = True
                del self.pending[logical_id]
        if len(events) > 100:
            raise ValueError("native order event budget exceeded")
        self.order_events = events

    def _account(self, session, broker):
        if broker is None:
            return AccountView(
                as_of=session.close_time,
                currency=self.inputs.manifest.currency,
                cash=self.inputs.initial_cash,
                equity=self.inputs.initial_cash,
            )
        current = self.inputs.by_session[session.session_date]
        positions = []
        reserved = {}
        for rec in self.pending.values():
            if rec["side"] == "sell":
                reserved[rec["listing_id"]] = (
                    reserved.get(rec["listing_id"], 0.0) + rec["remaining_quantity"]
                )
        native_positions = dict(broker.positions)
        if any(q > native_positions.get(key, 0.0) for key, q in reserved.items()):
            raise ValueError("pending sells exceed native decision-close holdings")
        for key, quantity in native_positions.items():
            if not quantity:
                continue
            if key not in current or quantity < 0:
                raise ValueError(
                    "held position left effective universe or became short"
                )
            positions.append(
                PositionView(
                    listing_id=key,
                    quantity=float(quantity),
                    available_quantity=float(quantity) - reserved.get(key, 0.0),
                    market_value=float(quantity) * current[key].close,
                )
            )
        cash, equity = float(broker.cash), float(broker.equity)
        if cash < -1e-7:
            raise ValueError("cash account cannot borrow")
        return AccountView(
            as_of=session.close_time,
            currency=self.inputs.manifest.currency,
            cash=max(0.0, cash),
            equity=equity,
            positions=tuple(sorted(positions, key=lambda p: p.listing_id)),
        )

    def _open_orders(self):
        result = []
        for key, rec in self.pending.items():
            remaining = rec["remaining_quantity"]
            if remaining > 0:
                result.append(
                    OpenOrderView(
                        order_id=key,
                        signal_id=rec["signal_id"],
                        listing_id=rec["listing_id"],
                        side=rec["side"],
                        remaining_quantity=remaining,
                        order_type=rec["order_type"],
                        limit_price=rec["limit_price"],
                        time_in_force="GTC",
                        reduce_only=rec["side"] == "sell",
                        position_effect="close" if rec["side"] == "sell" else "open",
                        submitted_at=utc_timestamp(rec["decision_time"]),
                        expires_at=None,
                    )
                )
        return tuple(result)

    def on_session(self, session, broker=None, aq=None):
        if session.session_date in self.completed:
            raise ValueError("duplicate engine decision session")
        if (
            len(self.completed) >= len(self.inputs.sessions)
            or session != self.inputs.sessions[len(self.completed)]
        ):
            raise ValueError(
                "decision sessions must follow complete trusted calendar order"
            )
        self.current_session = session
        forced_warmup = session.session_date < date.fromisoformat(
            self.inputs.window["start"]
        )
        if broker is not None:
            self._refresh(broker)
        account = self._account(session, broker)
        observation = build_observation(
            manifest=self.inputs.manifest,
            decision_time=session.close_time,
            next_open_time=session.next_open_time,
            session_date=session.session_date,
            listings=self.inputs.listings,
            history=self.inputs.bars,
            account=account,
            state=self.state,
            open_orders=self._open_orders(),
            order_events=tuple(self.order_events),
            warmup=True if forced_warmup else None,
        )
        expected = {l.listing_id for l in self.inputs.effective(session)}
        if {l.listing_id for l in observation.listings} != expected:
            raise ValueError(
                "SDK projection must preserve independent effective universe"
            )
        # Copy the exact before-state before calling even the isolated exchange.
        # Capture is observation-only and cannot alter the validated SDK input.
        self.research.begin(self, observation, broker)
        raw = self.exchange(observation)
        reply = parse_reply(canonical_json(raw), self.inputs.manifest, observation)
        self.research.received(observation, reply)
        decision = reply.decision
        before = self.state.payload_sha256
        outcome, logical_ids, plans = "no_orders", [], []
        if isinstance(reply, PortfolioReply):
            intents = (decision,)
        else:
            intents = decision.intents
        # All signals, including hold/no-decision, are unique across invocations.
        if any(intent.signal_id in self.signals for intent in intents):
            raise ValueError("replayed signal ID")
        self.signals.update(intent.signal_id for intent in intents)
        if not observation.warmup:
            if broker is None or aq is None:
                raise ValueError("active decision requires trusted native broker")
            if isinstance(reply, PortfolioReply):
                if self.pending and decision.action not in {"hold", "no_decision"}:
                    outcome = "skipped_pending_rebalance"
                else:
                    plans = target_orders(decision, observation, self.inputs.profile)
                    validate_sell_plans(plans, observation, self.inputs.profile)
                    for key, side, quantity in plans:
                        logical_ids.append(
                            self._submit(
                                broker,
                                aq,
                                observation,
                                decision.signal_id,
                                key,
                                side,
                                quantity,
                                "market",
                                None,
                            )
                        )
                    if plans:
                        outcome = "submitted"
            else:
                # No implicit resizing of explicit event orders. Reserve current
                # cash/next-open holdings for every plan, including pending orders.
                cash = observation.account.cash
                positions = next_open_sell_capacity(observation, self.inputs.profile)
                for rec in self.pending.values():
                    remaining = rec["remaining_quantity"]
                    if rec["side"] == "buy":
                        cash -= remaining * rec["budget_price"]
                prices = {
                    b.listing_id: b.close
                    for b in observation.history
                    if b.session_date == session.session_date
                }
                # Validate the whole reply before submitting any leg or cancel.
                # A cancel request cannot release its reservation in this reply.
                plans = []
                for intent in decision.intents:
                    if isinstance(intent, CancelIntent):
                        continue
                    lot = self.inputs.profile["lot_size"]
                    if lot_floor(intent.quantity, lot) != intent.quantity:
                        raise ValueError("order quantity must be exact market lot")
                    cost = self._budget_price(
                        intent.limit_price or prices[intent.listing_id]
                    )
                    if intent.side == "buy":
                        if intent.quantity * cost > cash + 1e-8:
                            raise ValueError("event buy exceeds unreserved cash")
                        cash -= intent.quantity * cost
                    else:
                        if intent.quantity > positions.get(intent.listing_id, 0.0):
                            raise ValueError(
                                "event sell exceeds next-open unreserved holdings"
                            )
                        positions[intent.listing_id] -= intent.quantity
                    plans.append((intent.listing_id, intent.side, intent.quantity))
                validate_sell_plans(plans, observation, self.inputs.profile)
                for intent in decision.intents:
                    if isinstance(intent, CancelIntent):
                        record = self.pending[intent.order_id]
                        for oid in record["native_order_ids"]:
                            broker.cancel_order(oid)
                        outcome = "cancel_requested"
                        continue
                    logical_ids.append(
                        self._submit(
                            broker,
                            aq,
                            observation,
                            intent.signal_id,
                            intent.listing_id,
                            intent.side,
                            intent.quantity,
                            intent.order_type,
                            intent.limit_price,
                        )
                    )
                    outcome = "submitted"
        else:
            outcome = "warmup_state_only"
        self.research.planned(self, plans, logical_ids, outcome)
        self.state = reply.next_state
        self.trace.append(
            {
                "sequence": len(self.trace),
                "session_date": session.session_date.isoformat(),
                "decision_time": iso(session.close_time),
                "next_open_time": iso(session.next_open_time),
                "warmup": observation.warmup,
                "snapshot_sha256": observation.snapshot_id,
                "observation_sha256": hash_json(observation),
                "reply_sha256": hash_json(reply),
                "state_before_sha256": before,
                "state_after_sha256": self.state.payload_sha256,
                "decision": decision.model_dump(mode="json"),
                "logical_order_ids": logical_ids,
                "outcome": outcome,
            }
        )
        self.completed.add(session.session_date)
        self.research.end()

    def _budget_price(self, price):
        return (
            price
            * (1 + self.inputs.profile["slippage"]["value"])
            * (1 + self.inputs.profile["commission_rate"])
        )

    def _submit(
        self, broker, aq, observation, signal_id, key, side, quantity, order_type, price
    ):
        logical_id = hash_json([self.inputs.manifest.strategy_id, signal_id, key, side])
        if logical_id in {o["logical_order_id"] for o in self.logical_orders}:
            raise ValueError("duplicate logical order")
        receipt = broker.submit_order(
            symbol=key,
            side=side.title(),
            quantity=float(quantity),
            price=price,
            order_type=order_type,
            time_in_force=aq.TimeInForce.GTC,
            fill_mode=aq.NextOpen(),
            tag=logical_id,
            position_effect="close" if side == "sell" else "open",
            reduce_only=side == "sell",
            asset_type="stock",
        )
        order_ids = tuple(receipt.order_ids)
        if (
            not order_ids
            or len(set(order_ids)) != len(order_ids)
            or any(
                type(oid) is not str or not oid or oid in self.native_to_logical
                for oid in order_ids
            )
        ):
            raise ValueError("invalid or duplicated native order receipt legs")
        close = next(
            b.close
            for b in observation.history
            if b.listing_id == key and b.session_date == observation.session_date
        )
        rec = {
            "logical_order_id": logical_id,
            "signal_id": signal_id,
            "listing_id": key,
            "side": side,
            "quantity": float(quantity),
            "order_type": order_type,
            "limit_price": price,
            "decision_time": iso(observation.decision_time),
            "decision_session": observation.session_date.isoformat(),
            "effective_at": iso(observation.next_open_time),
            "native_order_ids": list(order_ids),
            "filled_quantity": 0.0,
            "remaining_quantity": float(quantity),
            "terminal": False,
            "budget_price": self._budget_price(price or close),
            "last_events": {},
        }
        self.pending[logical_id] = rec
        self.logical_orders.append(rec)
        self.native_to_logical.update({oid: logical_id for oid in order_ids})
        return logical_id


class DailyDecisionBarrier:
    """Only a complete trusted daily cross-section may invoke either policy ABI.

    Native bar/order/timer/pre-open callbacks never invoke policy code. AKQuant's
    framework cross-section timer is the sole after-bar entry, independently
    verified against every expected symbol and the staged calendar here.
    """

    def __init__(self, controller):
        self.controller = controller
        self.by_timestamp = {
            to_ns(s.close_time): s
            for s in controller.inputs.sessions
            if s.session_date >= date.fromisoformat(controller.inputs.window["start"])
        }
        self.seen = {}

    def on_bar(self, bar, broker):
        self.controller.verify_native_start(broker)
        timestamp = int(bar.timestamp)
        session = self.by_timestamp.get(timestamp)
        if session is None:
            raise ValueError("native bar outside reviewed active calendar")
        if session.session_date in self.controller.completed:
            raise ValueError("native bar arrived after its complete decision barrier")
        expected_index = len(self.controller.completed)
        if (
            expected_index >= len(self.controller.inputs.sessions)
            or session != self.controller.inputs.sessions[expected_index]
        ):
            raise ValueError("native bar skipped an unfinished daily decision barrier")
        current = self.seen.setdefault(timestamp, set())
        if bar.symbol in current:
            raise ValueError("duplicate native bar")
        current.add(bar.symbol)
        expected = {l.listing_id for l in self.controller.inputs.effective(session)}
        if not current <= expected:
            raise ValueError("native event outside expected universe")

    def on_cross_section(self, trading_date, timestamp, broker, aq):
        session = self.by_timestamp.get(int(timestamp))
        if session is None or trading_date != session.session_date:
            raise ValueError("native cross-section outside reviewed session")
        expected = {l.listing_id for l in self.controller.inputs.effective(session)}
        if self.seen.get(int(timestamp), set()) != expected:
            raise ValueError("native cross-section missing expected listings")
        self.controller.on_session(session, broker, aq)


def execute(
    inputs: Inputs, exchange: Callable, research_recorder=None, *, runtime_observer=None
):
    """Container-only real native execution. Never called by host fallbacks."""
    if os.environ.get("TRADEGRAD_PORTABLE_ENGINE_CONTAINER") != "1":
        raise RuntimeError(
            "Native portable execution requires the isolated engine image"
        )
    import importlib.metadata

    import akquant as aq

    if importlib.metadata.version("akquant") != "0.3.27":
        raise ValueError("unreviewed AKQuant runtime version")
    controller = Controller(inputs, exchange, research_recorder=research_recorder)
    start = date.fromisoformat(inputs.window["start"])
    active = [s for s in inputs.sessions if s.session_date >= start]
    # Explicit state-only replay avoids AKQuant's suppressed pre-active callbacks.
    for session in inputs.sessions:
        if session.session_date < start:
            controller.on_session(session)
    barrier = DailyDecisionBarrier(controller)
    callback_errors = []
    native_configuration = NativeVolumeLimit(inputs.profile["volume_limit_pct"])

    class ReviewedStrategy(aq.Strategy):
        def on_error(self, error, source, payload=None):
            callback_errors.append((str(source), type(error).__name__))
            raise error

        def on_bar(self, bar):
            native_configuration.require_applied()
            barrier.on_bar(bar, self)

        def on_cross_section(self, trading_date, timestamp):
            native_configuration.require_applied()
            barrier.on_cross_section(trading_date, timestamp, self, aq)

        def on_order(self, order):
            # The next complete barrier reads authoritative receipts. No policy.
            native_configuration.require_applied()

        def on_trade(self, trade):
            native_configuration.require_applied()

        def on_timer(self, payload):
            raise ValueError("policy timers are unsupported in the daily profile")

        def on_tick(self, tick):
            raise ValueError("intraday data is unsupported in the daily profile")

    bars = [
        aq.Bar(
            timestamp=to_ns(s.close_time),
            symbol=key,
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=b.volume,
        )
        for s in active
        for key, b in sorted(inputs.by_session[s.session_date].items())
    ]
    profile = inputs.profile
    broker = ReviewedStrategy()
    result = aq.run_backtest(
        data=bars,
        strategy=broker,
        symbols=[l.listing_id for l in inputs.listings],
        initial_cash=inputs.initial_cash,
        commission_rate=profile["commission_rate"],
        stamp_tax_rate=profile["stamp_tax_rate"],
        transfer_fee_rate=0.0,
        min_commission=0.0,
        slippage=profile["slippage"],
        volume_limit_pct=profile["volume_limit_pct"],
        t_plus_one=profile["t_plus_one"],
        lot_size=profile["lot_size"],
        timezone=profile["timezone"],
        risk_config=profile["risk_config"],
        fill_policy=aq.NextOpen(),
        history_depth=inputs.manifest.data.lookback_bars,
        warmup_period=0,
        show_progress=False,
        start_time=active[0].open_time,
        # Native cross-section hooks occur one nanosecond after the final bar.
        end_time=active[-1].close_time + timedelta(microseconds=1),
        strategy_runtime_config={"error_mode": "raise", "re_raise_on_error": True},
        analyzer_plugins=[native_configuration],
        **(
            {"on_event": runtime_observer, "stream_mode": "audit"}
            if runtime_observer is not None
            else {}
        ),
    )
    native_configuration.require_applied()
    if callback_errors:
        raise ValueError("native callback error prevented a complete result")
    if controller.completed != {s.session_date for s in inputs.sessions}:
        raise ValueError("native engine omitted expected decision callbacks")
    controller.current_session = active[-1]
    controller._refresh(broker)
    from .export import export_result

    bundle = export_result(result, controller)
    controller.research.finalize(controller, bundle)
    return bundle
