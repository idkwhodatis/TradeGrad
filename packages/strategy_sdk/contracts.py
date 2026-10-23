"""portable.v1: decisions, not fills, metrics, or an execution certification.

The initial declared capability subset is daily, long-only, cash equities,
market/limit, GTC, next-open. A schema match is never a sandbox or a broker
capability certificate. Runtime capability checks remain mandatory.
"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import Field, model_serializer, model_validator

from .base import (
    Contract,
    Currency,
    Hash,
    Identity,
    Name,
    NonNegative,
    Positive,
    SessionDate,
    Timestamp,
)
from .json import REPLY_LIMITS, canonical_json, hash_json, parse_json
from .state import StateCodec, StateSchema, StateSnapshot

CONTRACT_VERSION = "portable.v1"
MAX_LISTINGS = 100
MAX_LOOKBACK = 252
MAX_INTENTS = 100


class OrderCapabilities(Contract):
    order_types: tuple[Literal["market", "limit"], ...] = ("market", "limit")
    time_in_force: tuple[Literal["GTC"], ...] = ("GTC",)
    quantity_unit: Literal["shares"] = "shares"
    short: Literal[False] = False
    leverage: Literal[1] = 1
    account_mode: Literal["cash_long_only"] = "cash_long_only"
    fractional_shares: Literal[False] = False
    oco: Literal[False] = False
    trailing_stop: Literal[False] = False
    close_today: Literal[False] = False

    @model_validator(mode="after")
    def nonempty_unique(self):
        if not self.order_types or len(set(self.order_types)) != len(self.order_types):
            raise ValueError("order capabilities must be nonempty and unique")
        if self.time_in_force != ("GTC",):
            raise ValueError("only GTC time-in-force is supported")
        return self


class DataRequirements(Contract):
    interval: Literal["1d"] = "1d"
    lookback_bars: int = Field(ge=1, le=MAX_LOOKBACK)
    warmup_bars: int = Field(ge=1, le=MAX_LOOKBACK)
    history_order: Literal["oldest_first"] = "oldest_first"
    bar_complete: Literal["exchange_session_closed"] = "exchange_session_closed"
    snapshot_rule: Literal["all_effective_listings_complete"] = (
        "all_effective_listings_complete"
    )
    missing_bar_policy: Literal["reject_snapshot"] = "reject_snapshot"
    max_listings: int = Field(ge=1, le=MAX_LISTINGS)

    @model_validator(mode="after")
    def bounded_warmup(self):
        if self.warmup_bars > self.lookback_bars:
            raise ValueError("warmup cannot exceed bounded lookback")
        return self


class UniverseAssumptions(Contract):
    semantics: Literal[
        "user_fixed",
        "current_snapshot",
        "historical_membership",
        "point_in_time_verified",
    ]
    snapshot_sha256: Hash
    survivorship_bias: Literal["possible", "membership_evidence_required"]

    @model_validator(mode="after")
    def no_pit_inference(self):
        expected = (
            "membership_evidence_required"
            if self.semantics == "point_in_time_verified"
            else "possible"
        )
        if self.survivorship_bias != expected:
            raise ValueError("universe semantics must preserve survivorship caveats")
        return self


class PriceAssumptions(Contract):
    price_basis: Literal["unadjusted"] = "unadjusted"
    corporate_actions: Literal["reject_window_with_actions"] = (
        "reject_window_with_actions"
    )
    actions_snapshot_sha256: Hash
    # This limited implementation may not silently run adjusted prices or
    # ignore split/dividend events. Supporting them requires another profile.


class EngineProfile(Contract):
    profile_version: Literal["akquant.portable.cash-daily.controller-t1.v3"] = (
        "akquant.portable.cash-daily.controller-t1.v3"
    )
    engine_settlement_mode: Literal["T+0"] = "T+0"
    application_settlement_enforcement: Literal[
        "trusted_controller_daily_next_open_inventory.v1"
    ] = "trusted_controller_daily_next_open_inventory.v1"
    engine: Literal["akquant"] = "akquant"
    version: Literal["0.3.27"] = "0.3.27"
    profile_sha256: Hash
    environment_sha256: Hash


class UserPolicyDeclaration(Contract):
    """User claims only; parsed data is never a policy safety certificate."""

    source_kind: Literal["user_authored"] = "user_authored"
    parameter_schema: StateSchema
    initial_state: StateSnapshot
    evidence: Literal["user_declared_not_code_verified"] = (
        "user_declared_not_code_verified"
    )


class StrategyManifest(Contract):
    contract_version: Literal["portable.v1"] = CONTRACT_VERSION
    strategy_kind: Literal["portfolio", "event"]
    strategy_id: Identity
    code_sha256: Hash
    parameters_sha256: Hash
    market: Literal["US", "CN"]
    asset_type: Literal["equity"] = "equity"
    currency: Currency
    calendar: Literal["XNYS", "XSHG"]
    timezone: str = Field(min_length=1, max_length=64)
    data: DataRequirements
    decision_timing: Literal["after_close"] = "after_close"
    execution_timing: Literal["NextOpen"] = "NextOpen"
    universe: UniverseAssumptions
    prices: PriceAssumptions
    capabilities: OrderCapabilities = OrderCapabilities()
    state_schema: StateSchema
    engine_profile: EngineProfile
    authoring: UserPolicyDeclaration | None = None
    reference_result_sha256: Hash | None = None
    conformance: Literal["interface_only"] = "interface_only"
    external_framework_equivalence: Literal["not_certified"] = "not_certified"

    @model_serializer(mode="wrap")
    def historical_manifest(self, serializer):
        value = serializer(self)
        if self.authoring is None:
            value.pop("authoring", None)
        return value

    @model_validator(mode="after")
    def market_semantics(self):
        if self.authoring is not None:
            StateCodec(self.state_schema).decode(self.authoring.initial_state)
        expected = {
            "US": ("USD", "XNYS", "America/New_York"),
            "CN": ("CNY", "XSHG", "Asia/Shanghai"),
        }[self.market]
        if (self.currency, self.calendar, self.timezone) != expected:
            raise ValueError("market/currency/calendar/timezone mismatch")
        ZoneInfo(self.timezone)
        return self


class ListingView(Contract):
    listing_id: Identity
    instrument_id: Identity
    symbol: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9][A-Z0-9.\-]*$")
    mic: Literal["XNYS", "XNAS", "XASE", "ARCX", "BATS", "IEXG", "XSHG", "XSHE"]
    currency: Currency
    calendar: Literal["XNYS", "XSHG"]
    timezone: str = Field(min_length=1, max_length=64)
    effective_from: SessionDate
    effective_to: SessionDate | None = None
    effective_to_known_at: Timestamp | None = None
    known_at: Timestamp
    lot_size: int = Field(ge=1, le=1_000_000)

    @model_validator(mode="after")
    def identity_scope(self):
        expected = (
            ("CNY", "XSHG", "Asia/Shanghai")
            if self.mic in {"XSHG", "XSHE"}
            else ("USD", "XNYS", "America/New_York")
        )
        if (self.currency, self.calendar, self.timezone) != expected:
            raise ValueError("listing venue/currency/calendar/timezone mismatch")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("empty listing effective interval")
        return self


class BarView(Contract):
    listing_id: Identity
    session_date: SessionDate
    completed_at: Timestamp
    available_at: Timestamp
    open: Positive
    high: Positive
    low: Positive
    close: Positive
    volume: NonNegative
    volume_unit: Literal["shares"] = "shares"
    complete: Literal[True] = True

    @model_validator(mode="after")
    def coherent_prices(self):
        if self.high < max(self.open, self.close, self.low) or self.low > min(
            self.open, self.close
        ):
            raise ValueError("inconsistent OHLC prices")
        if self.available_at < self.completed_at:
            raise ValueError("bar cannot be available before completion")
        return self


class PositionView(Contract):
    listing_id: Identity
    quantity: NonNegative
    # Next-session unreserved sell capacity; NEVER native current T+0 or T+1
    # sellability. Remaining pending buys are excluded, pending sells deducted.
    available_quantity: NonNegative
    available_quantity_semantics: Literal["next_open_unreserved_holdings"] = (
        "next_open_unreserved_holdings"
    )
    quantity_unit: Literal["shares"] = "shares"
    market_value: NonNegative

    @model_validator(mode="after")
    def long_only(self):
        if self.available_quantity > self.quantity:
            raise ValueError("available quantity exceeds position")
        if not self.quantity.is_integer() or not self.available_quantity.is_integer():
            raise ValueError("fractional shares are unsupported")
        return self


class AccountView(Contract):
    as_of: Timestamp
    currency: Currency
    cash: NonNegative
    equity: NonNegative
    positions: tuple[PositionView, ...] = Field(default=(), max_length=MAX_LISTINGS)

    @model_validator(mode="after")
    def unique_positions(self):
        if len({p.listing_id for p in self.positions}) != len(self.positions):
            raise ValueError("duplicate positions")
        return self


class IntentTiming(Contract):
    signal_id: Name
    effective_at: Timestamp
    expires_at: None = None

    @model_validator(mode="after")
    def valid_interval(self):
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("signal expiration must follow its effective time")
        return self


class OrderIntent(IntentTiming):
    kind: Literal["order"] = "order"
    listing_id: Identity
    side: Literal["buy", "sell"]
    quantity: Positive
    quantity_unit: Literal["shares"] = "shares"
    order_type: Literal["market", "limit"]
    limit_price: Positive | None = None
    stop_price: None = None
    time_in_force: Literal["GTC"] = "GTC"
    reduce_only: bool
    position_effect: Literal["open", "close"]

    @model_validator(mode="after")
    def supported_order(self):
        if (self.order_type == "limit") != (self.limit_price is not None):
            raise ValueError("limit price is required only for limit orders")
        if not self.quantity.is_integer():
            raise ValueError("fractional shares are unsupported")
        if self.side == "buy" and (self.position_effect != "open" or self.reduce_only):
            raise ValueError("long-only buys must open, not reduce")
        if self.side == "sell" and (
            self.position_effect != "close" or not self.reduce_only
        ):
            raise ValueError("long-only sells must be reduce-only closes")
        return self


class CancelIntent(IntentTiming):
    kind: Literal["cancel"] = "cancel"
    order_id: Name
    listing_id: Identity


class OpenOrderView(Contract):
    order_id: Name
    signal_id: Name
    listing_id: Identity
    side: Literal["buy", "sell"]
    remaining_quantity: Positive
    quantity_unit: Literal["shares"] = "shares"
    order_type: Literal["market", "limit"]
    limit_price: Positive | None = None
    time_in_force: Literal["GTC"] = "GTC"
    reduce_only: bool
    position_effect: Literal["open", "close"]
    submitted_at: Timestamp
    expires_at: None = None

    @model_validator(mode="after")
    def order_contract(self):
        OrderIntent(
            signal_id=self.signal_id,
            listing_id=self.listing_id,
            side=self.side,
            quantity=self.remaining_quantity,
            order_type=self.order_type,
            limit_price=self.limit_price,
            reduce_only=self.reduce_only,
            position_effect=self.position_effect,
            effective_at=self.submitted_at,
            expires_at=self.expires_at,
        )
        return self


class OrderEvent(Contract):
    event_id: Name
    order_id: Name
    signal_id: Name
    listing_id: Identity
    occurred_at: Timestamp
    status: Literal[
        "accepted", "partial_fill", "filled", "cancelled", "rejected", "expired"
    ]
    filled_quantity: NonNegative = 0.0
    # Engine-originated observations only; policy replies cannot include these.


class Observation(Contract):
    contract_version: Literal["portable.v1"] = CONTRACT_VERSION
    snapshot_id: Hash
    decision_time: Timestamp
    next_open_time: Timestamp
    session_date: SessionDate
    listings: tuple[ListingView, ...] = Field(min_length=1, max_length=MAX_LISTINGS)
    history: tuple[BarView, ...] = Field(max_length=MAX_LISTINGS * MAX_LOOKBACK)
    account: AccountView
    open_orders: tuple[OpenOrderView, ...] = Field(default=(), max_length=MAX_INTENTS)
    order_events: tuple[OrderEvent, ...] = Field(default=(), max_length=MAX_INTENTS)
    state: StateSnapshot
    warmup: bool
    event: Literal["bar_complete"] = "bar_complete"

    @model_validator(mode="after")
    def past_only(self):
        if self.next_open_time <= self.decision_time:
            raise ValueError("NextOpen must follow decision time")
        ids = {listing.listing_id for listing in self.listings}
        if len(ids) != len(self.listings):
            raise ValueError("duplicate listing identity")
        for listing in self.listings:
            if listing.effective_to is not None and (
                listing.effective_to_known_at is None
                or listing.effective_to_known_at > self.decision_time
            ):
                raise ValueError(
                    "listing end date requires already-known announcement evidence"
                )
            if (
                listing.known_at > self.decision_time
                or listing.effective_from > self.session_date
                or (
                    listing.effective_to is not None
                    and self.session_date >= listing.effective_to
                )
            ):
                raise ValueError("listing not effective and known at decision")
        seen = set()
        previous = None
        for bar in self.history:
            if (
                bar.listing_id not in ids
                or bar.session_date > self.session_date
                or bar.completed_at > self.decision_time
                or bar.available_at > self.decision_time
            ):
                raise ValueError("history contains future or out-of-scope observations")
            key = (bar.session_date, bar.listing_id)
            if key in seen or (previous is not None and key <= previous):
                raise ValueError(
                    "history must be unique and oldest-first by session/listing"
                )
            seen.add(key)
            previous = key
        if self.account.as_of > self.decision_time:
            raise ValueError("future account snapshot")
        if any(p.listing_id not in ids for p in self.account.positions):
            raise ValueError("account position outside declared observation scope")
        if len({o.order_id for o in self.open_orders}) != len(self.open_orders):
            raise ValueError("duplicate open order identity")
        for order in self.open_orders:
            if (
                order.listing_id not in ids
                or order.submitted_at > self.decision_time
                or (
                    order.expires_at is not None
                    and order.expires_at <= self.decision_time
                )
            ):
                raise ValueError("invalid open order time or listing scope")
        reserved = {}
        for order in self.open_orders:
            if order.side == "sell":
                reserved[order.listing_id] = (
                    reserved.get(order.listing_id, 0.0) + order.remaining_quantity
                )
        positions = {p.listing_id: p for p in self.account.positions}
        if any(
            key not in positions or value > positions[key].quantity
            for key, value in reserved.items()
        ):
            raise ValueError("pending sells exceed current native holdings")
        if any(
            p.available_quantity != p.quantity - reserved.get(p.listing_id, 0.0)
            for p in self.account.positions
        ):
            raise ValueError("available_quantity must be next-open unreserved holdings")
        if len({e.event_id for e in self.order_events}) != len(self.order_events):
            raise ValueError("duplicate order event identity")
        if any(
            e.occurred_at > self.decision_time or e.listing_id not in ids
            for e in self.order_events
        ):
            raise ValueError("future or out-of-scope order event")
        return self


class Target(Contract):
    listing_id: Identity
    value: NonNegative


class Score(Contract):
    listing_id: Identity
    value: float = Field(ge=-1e15, le=1e15)


class TargetPortfolio(IntentTiming):
    kind: Literal["target_portfolio"] = "target_portfolio"
    action: Literal["rebalance", "hold", "liquidate", "no_decision"]
    basis: Literal["target_weight", "target_quantity", "target_notional"]
    targets: tuple[Target, ...] = Field(default=(), max_length=MAX_LISTINGS)
    omitted_positions: Literal["preserve", "liquidate"]
    rebalance_rule: Literal["next_open"] = "next_open"
    scores: tuple[Score, ...] = Field(default=(), max_length=MAX_LISTINGS)

    @model_validator(mode="after")
    def explicit_empty_semantics(self):
        if len({t.listing_id for t in self.targets}) != len(self.targets) or len(
            {s.listing_id for s in self.scores}
        ) != len(self.scores):
            raise ValueError("duplicate target or score listing")
        if self.action == "rebalance" and not self.targets:
            raise ValueError(
                "empty rebalance is ambiguous; choose hold/liquidate/no_decision"
            )
        if self.action != "rebalance" and self.targets:
            raise ValueError("non-rebalance action cannot contain targets")
        if (
            self.action in {"hold", "no_decision"}
            and self.omitted_positions != "preserve"
        ):
            raise ValueError("hold/no_decision must preserve omitted positions")
        if self.action == "liquidate" and self.omitted_positions != "liquidate":
            raise ValueError("liquidate must explicitly clear omitted positions")
        if (
            self.basis == "target_weight"
            and sum(t.value for t in self.targets) > 1.0 + 1e-12
        ):
            raise ValueError("long-only target weights cannot exceed one")
        if self.basis == "target_quantity" and any(
            not t.value.is_integer() for t in self.targets
        ):
            raise ValueError("fractional target quantity unsupported")
        return self


Intent = Annotated[OrderIntent | CancelIntent, Field(discriminator="kind")]


class EventDecision(Contract):
    kind: Literal["event"] = "event"
    intents: tuple[Intent, ...] = Field(default=(), max_length=MAX_INTENTS)

    @model_validator(mode="after")
    def unique_signals(self):
        if len({intent.signal_id for intent in self.intents}) != len(self.intents):
            raise ValueError("duplicate signal IDs in reply")
        return self


class PortfolioReply(Contract):
    decision: TargetPortfolio
    next_state: StateSnapshot


class EventReply(Contract):
    decision: EventDecision
    next_state: StateSnapshot


class PortfolioPolicy(Protocol):
    def decide(self, observation: Observation) -> PortfolioReply: ...


class EventPolicy(Protocol):
    def on_event(self, observation: Observation) -> EventReply: ...


def parse_manifest(raw: bytes | str) -> StrategyManifest:
    return StrategyManifest.model_validate_json(canonical_json(parse_json(raw)))


def validate_observation(
    observation: Observation, manifest: StrategyManifest
) -> Observation:
    observation = Observation.model_validate(observation)
    manifest = StrategyManifest.model_validate(manifest)
    StateCodec(manifest.state_schema).decode(observation.state)
    if len(observation.listings) > manifest.data.max_listings:
        raise ValueError("observation exceeds manifest universe bound")
    if observation.account.currency != manifest.currency:
        raise ValueError("account/manifest currency mismatch")
    counts = {}
    latest = {}
    for bar in observation.history:
        counts[bar.listing_id] = counts.get(bar.listing_id, 0) + 1
        latest[bar.listing_id] = bar.session_date
    for listing in observation.listings:
        if (listing.currency, listing.calendar, listing.timezone) != (
            manifest.currency,
            manifest.calendar,
            manifest.timezone,
        ):
            raise ValueError("listing/manifest market mismatch")
        if counts.get(listing.listing_id, 0) > manifest.data.lookback_bars:
            raise ValueError("history exceeds manifest lookback")
        if latest.get(listing.listing_id) != observation.session_date:
            raise ValueError(
                "incomplete cross-sectional snapshot; never compute a partial universe"
            )
    expected_warmup = any(
        counts.get(l.listing_id, 0) < manifest.data.warmup_bars
        for l in observation.listings
    )
    if expected_warmup and not observation.warmup:
        raise ValueError("insufficient history must be marked warmup")
    if (
        observation.decision_time.astimezone(ZoneInfo(manifest.timezone)).date()
        != observation.session_date
    ):
        raise ValueError("decision must be on its exchange-local session")
    if (
        observation.next_open_time.astimezone(ZoneInfo(manifest.timezone)).date()
        <= observation.session_date
    ):
        raise ValueError("NextOpen must be on a later exchange-local session")
    for bar in observation.history:
        if (
            bar.completed_at.astimezone(ZoneInfo(manifest.timezone)).date()
            != bar.session_date
        ):
            raise ValueError("bar completion/session date mismatch")
    return observation


def parse_observation(raw: bytes | str, manifest: StrategyManifest) -> Observation:
    value = Observation.model_validate_json(canonical_json(parse_json(raw)))
    expected = hash_json(value.model_dump(mode="json", exclude={"snapshot_id"}))
    if value.snapshot_id != expected:
        raise ValueError("observation prefix checksum mismatch")
    return validate_observation(value, manifest)


def parse_reply(
    raw: bytes | str, manifest: StrategyManifest, observation: Observation
) -> PortfolioReply | EventReply:
    """Validate shape, schema and scope, never trust policy accounting claims.

    Cash, price, T+1, sellability, order lifecycle, dedup across invocations,
    rounding, and exchange calendars are enforced by the trusted runtime.
    """
    validate_observation(observation, manifest)
    data = canonical_json(parse_json(raw, limits=REPLY_LIMITS), limits=REPLY_LIMITS)
    cls = PortfolioReply if manifest.strategy_kind == "portfolio" else EventReply
    reply = cls.model_validate_json(data)
    StateCodec(manifest.state_schema).decode(reply.next_state)
    ids = {listing.listing_id for listing in observation.listings}
    if isinstance(reply, PortfolioReply):
        decision = reply.decision
        if any(
            target.listing_id not in ids
            for target in (*decision.targets, *decision.scores)
        ):
            raise ValueError("target/score outside effective universe")
        if observation.warmup and decision.action != "no_decision":
            raise ValueError("warmup may update state but cannot submit decisions")
        intents = (decision,)
    else:
        intents = reply.decision.intents
        if observation.warmup and intents:
            raise ValueError("warmup cannot submit orders or cancels")
        open_orders = {order.order_id: order for order in observation.open_orders}
        for intent in intents:
            if intent.listing_id not in ids:
                raise ValueError("intent outside effective universe")
            if (
                isinstance(intent, OrderIntent)
                and intent.order_type not in manifest.capabilities.order_types
            ):
                raise ValueError("order type absent from manifest capabilities")
            if isinstance(intent, CancelIntent) and (
                intent.order_id not in open_orders
                or open_orders[intent.order_id].listing_id != intent.listing_id
            ):
                raise ValueError("cancel must identify an observed open order")
    for intent in intents:
        if intent.effective_at != observation.next_open_time:
            raise ValueError("signal must explicitly take effect at declared NextOpen")
        if (
            intent.expires_at is not None
            and intent.expires_at <= observation.next_open_time
        ):
            raise ValueError("signal already expired at NextOpen")
    return reply


def stable_signal_id(strategy_id: str, observation: Observation, label: str) -> str:
    """Same immutable input, strategy and label give the same retry identity."""
    # Callers must still persist deduplication records outside policy state.
    return hash_json(
        {
            "strategy_id": strategy_id,
            "snapshot_id": observation.snapshot_id,
            "session_date": observation.session_date.isoformat(),
            "label": label,
        }
    )
