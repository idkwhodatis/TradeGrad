"""Trusted SDK-only tests. Never import or invoke either policy source module."""

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.strategy_sdk import (
    AccountView,
    BarView,
    CancelIntent,
    DataRequirements,
    EngineProfile,
    EventDecision,
    EventReply,
    ListingView,
    OpenOrderView,
    OrderCapabilities,
    OrderIntent,
    PortfolioReply,
    PositionView,
    PriceAssumptions,
    Score,
    StateCodec,
    StateField,
    StateSchema,
    StrategyManifest,
    Target,
    TargetPortfolio,
    UniverseAssumptions,
    build_observation,
    canonical_json,
    hash_json,
    observation_hash,
    parse_manifest,
    parse_observation,
    parse_reply,
    stable_signal_id,
    validate_observation,
)

L1 = str(UUID(int=1))
L2 = str(UUID(int=2))
L3 = str(UUID(int=3))
STRATEGY = str(UUID(int=100))
HASH = "a" * 64
DAY = date(2025, 1, 3)
NOW = datetime(2025, 1, 3, 21, tzinfo=UTC)
NEXT = datetime(2025, 1, 6, 14, 30, tzinfo=UTC)
SCHEMA = StateSchema(
    name="test", version=1, fields=(StateField(name="count", kind="integer"),)
)


def make_manifest(kind="portfolio", **changes):
    fields = {
        "strategy_kind": kind,
        "strategy_id": STRATEGY,
        "code_sha256": HASH,
        "parameters_sha256": hash_json({}),
        "market": "US",
        "currency": "USD",
        "calendar": "XNYS",
        "timezone": "America/New_York",
        "data": DataRequirements(lookback_bars=2, warmup_bars=2, max_listings=2),
        "universe": UniverseAssumptions(
            semantics="user_fixed", snapshot_sha256=HASH, survivorship_bias="possible"
        ),
        "prices": PriceAssumptions(actions_snapshot_sha256=HASH),
        "state_schema": SCHEMA,
        "engine_profile": EngineProfile(profile_sha256=HASH, environment_sha256=HASH),
    }
    fields.update(changes)
    return StrategyManifest(**fields)


def make_listing(listing_id=L1, **changes):
    fields = {
        "listing_id": listing_id,
        "instrument_id": listing_id,
        "symbol": "AAPL",
        "mic": "XNAS",
        "currency": "USD",
        "calendar": "XNYS",
        "timezone": "America/New_York",
        "effective_from": date(2020, 1, 1),
        "known_at": datetime(2020, 1, 1, tzinfo=UTC),
        "lot_size": 1,
    }
    fields.update(changes)
    return ListingView(**fields)


def make_bar(listing_id=L1, days_ago=0, **changes):
    fields = {
        "listing_id": listing_id,
        "session_date": DAY - timedelta(days=days_ago),
        "completed_at": NOW - timedelta(days=days_ago),
        "available_at": NOW - timedelta(days=days_ago),
        "open": 10.0,
        "high": 12.0,
        "low": 9.0,
        "close": 11.0,
        "volume": 1000.0,
    }
    fields.update(changes)
    return BarView(**fields)


def make_observation(manifest=None, **changes):
    fields = {
        "manifest": manifest or make_manifest(),
        "decision_time": NOW,
        "next_open_time": NEXT,
        "session_date": DAY,
        "listings": (make_listing(),),
        "history": (make_bar(days_ago=1), make_bar()),
        "account": AccountView(as_of=NOW, currency="USD", cash=1000.0, equity=1000.0),
        "state": StateCodec(SCHEMA).encode({"count": 0}),
    }
    fields.update(changes)
    return build_observation(**fields)


def make_target(**changes):
    fields = {
        "signal_id": "signal:1",
        "effective_at": NEXT,
        "expires_at": None,
        "action": "rebalance",
        "basis": "target_weight",
        "targets": (Target(listing_id=L1, value=0.5),),
        "omitted_positions": "liquidate",
    }
    fields.update(changes)
    return TargetPortfolio(**fields)


def make_order(**changes):
    fields = {
        "signal_id": "signal:1",
        "effective_at": NEXT,
        "expires_at": None,
        "listing_id": L1,
        "side": "buy",
        "quantity": 10.0,
        "order_type": "market",
        "time_in_force": "GTC",
        "reduce_only": False,
        "position_effect": "open",
    }
    fields.update(changes)
    return OrderIntent(**fields)


def reply_bytes(decision, state=None):
    cls = PortfolioReply if isinstance(decision, TargetPortfolio) else EventReply
    return canonical_json(
        cls(
            decision=decision,
            next_state=state or StateCodec(SCHEMA).encode({"count": 1}),
        )
    )


def test_manifest_and_observation_roundtrip_immutable():
    manifest = make_manifest()
    observation = make_observation(manifest)
    assert parse_manifest(canonical_json(manifest)) == manifest
    assert parse_observation(canonical_json(observation), manifest) == observation
    assert observation.snapshot_id == observation_hash(observation)
    with pytest.raises(ValidationError):
        observation.warmup = True
    with pytest.raises(ValidationError):
        observation.history[0].close = 0.0
    assert isinstance(observation.history, tuple)
    assert isinstance(manifest.state_schema.fields, tuple)


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_version": "portable.v2"},
        {"market": "EU"},
        {"currency": "CNY"},
        {"timezone": "UTC"},
        {"calendar": "XSHG"},
        {"asset_type": "future"},
        {"decision_timing": "at_close"},
        {"execution_timing": "SameClose"},
        {"code_sha256": "Z" * 64},
        {"parameters_sha256": "../bad"},
        {"strategy_id": "AAPL"},
        {"strategy_id": "00000000-0000-0000-0000-00000000000A"},
        {"conformance": "trusted_execution"},
        {"external_framework_equivalence": "certified"},
    ],
)
def test_manifest_rejects_unsupported_or_ambiguous_semantics(changes):
    with pytest.raises(ValueError):
        make_manifest(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"short": True},
        {"leverage": 2},
        {"account_mode": "hedge"},
        {"oco": True},
        {"trailing_stop": True},
        {"close_today": True},
        {"fractional_shares": True},
        {"quantity_unit": "lots"},
        {"order_types": ("stop",)},
        {"order_types": ()},
        {"order_types": ("market", "market")},
        {"time_in_force": ("DAY",)},
    ],
)
def test_unsupported_capabilities_fail_closed(changes):
    with pytest.raises(ValueError):
        OrderCapabilities(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"interval": "1h"},
        {"history_order": "newest_first"},
        {"bar_complete": "intrabar"},
        {"snapshot_rule": "first_bar"},
        {"missing_bar_policy": "skip"},
        {"lookback_bars": 253},
        {"warmup_bars": 3},
        {"max_listings": 101},
        {"lookback_bars": True},
    ],
)
def test_data_requirements_bound_and_pin_semantics(changes):
    fields = {"lookback_bars": 2, "warmup_bars": 2, "max_listings": 2}
    fields.update(changes)
    with pytest.raises(ValueError):
        DataRequirements(**fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"order_type": "stop"},
        {"quantity_unit": "lots"},
        {"time_in_force": "DAY"},
        {"quantity": 0.0},
        {"quantity": -2.0},
        {"quantity": 1.5},
        {"quantity": "10"},
        {"quantity": True},
        {"quantity": float("nan")},
        {"limit_price": 2.0},
        {"order_type": "limit"},
        {"stop_price": 2.0},
        {"position_effect": "close_today"},
        {"reduce_only": True},
        {"side": "sell"},
        {"expires_at": NEXT + timedelta(days=1)},
        {"signal_id": "../signal"},
        {"signal_id": "signal\n1"},
    ],
)
def test_order_rejects_malformed_unsupported_or_short_intent(changes):
    with pytest.raises(ValueError):
        make_order(**changes)


def test_explicit_long_only_limit_and_reduce_only_close():
    assert make_order(order_type="limit", limit_price=10.0).limit_price == 10.0
    assert make_order(
        side="sell", position_effect="close", reduce_only=True
    ).reduce_only


@pytest.mark.parametrize(
    "basis,value",
    [("target_weight", 0.5), ("target_quantity", 10.0), ("target_notional", 200.0)],
)
def test_explicit_target_basis_roundtrips(basis, value):
    manifest = make_manifest()
    decision = make_target(basis=basis, targets=(Target(listing_id=L1, value=value),))
    assert (
        parse_reply(reply_bytes(decision), manifest, make_observation()).decision.basis
        == basis
    )


@pytest.mark.parametrize(
    "action,omitted",
    [("hold", "preserve"), ("liquidate", "liquidate"), ("no_decision", "preserve")],
)
def test_empty_semantics_explicit_and_distinct(action, omitted):
    assert (
        make_target(action=action, targets=(), omitted_positions=omitted).action
        == action
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"targets": ()},
        {"action": "hold"},
        {"action": "hold", "targets": ()},
        {"action": "no_decision", "targets": ()},
        {"action": "liquidate", "targets": (), "omitted_positions": "preserve"},
        {
            "targets": (
                Target(listing_id=L1, value=0.6),
                Target(listing_id=L2, value=0.5),
            )
        },
        {
            "targets": (
                Target(listing_id=L1, value=0.2),
                Target(listing_id=L1, value=0.2),
            )
        },
        {"basis": "target_quantity", "targets": (Target(listing_id=L1, value=0.5),)},
    ],
)
def test_ambiguous_empty_and_invalid_target_semantics_rejected(changes):
    with pytest.raises(ValueError):
        make_target(**changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("ledger", {}),
        ("metrics", {"sharpe": 99}),
        ("fills", []),
        ("orders", []),
        ("portfolio_value", 9999),
        ("contract_version", "portable.v1"),
    ],
)
def test_policy_cannot_submit_accounting_or_extra_reply_fields(field, value):
    payload = PortfolioReply(
        decision=make_target(), next_state=StateCodec(SCHEMA).encode({"count": 1})
    ).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValueError):
        parse_reply(canonical_json(payload), make_manifest(), make_observation())


def test_wrong_kind_future_timing_and_scope_reject():
    manifest = make_manifest()
    observation = make_observation()
    for decision in [
        make_target(effective_at=NOW),
        make_target(targets=(Target(listing_id=L3, value=0.5),)),
        make_target(scores=(Score(listing_id=L3, value=1.0),)),
    ]:
        with pytest.raises(ValueError):
            parse_reply(reply_bytes(decision), manifest, observation)
    with pytest.raises(ValueError):
        parse_reply(reply_bytes(EventDecision()), manifest, observation)


def test_event_reply_capabilities_signal_dedup_and_cancel_scope():
    manifest = make_manifest(
        "event", capabilities=OrderCapabilities(order_types=("market",))
    )
    observation = make_observation(manifest)
    assert parse_reply(
        reply_bytes(EventDecision(intents=(make_order(),))), manifest, observation
    )
    with pytest.raises(ValueError):
        EventDecision(intents=(make_order(), make_order()))
    for intent in [
        make_order(order_type="limit", limit_price=10.0),
        CancelIntent(
            signal_id="cancel:1",
            order_id="order:unknown",
            listing_id=L1,
            effective_at=NEXT,
            expires_at=None,
        ),
    ]:
        with pytest.raises(ValueError):
            parse_reply(
                reply_bytes(EventDecision(intents=(intent,))), manifest, observation
            )
    order = OpenOrderView(
        order_id="order:logical",
        signal_id="old:signal",
        listing_id=L1,
        side="buy",
        remaining_quantity=10.0,
        order_type="market",
        reduce_only=False,
        position_effect="open",
        submitted_at=NOW,
        expires_at=None,
    )
    observation = make_observation(manifest, open_orders=(order,))
    cancel = CancelIntent(
        signal_id="cancel:1",
        order_id=order.order_id,
        listing_id=L1,
        effective_at=NEXT,
        expires_at=None,
    )
    assert parse_reply(
        reply_bytes(EventDecision(intents=(cancel,))), manifest, observation
    )


def test_warmup_can_prime_full_window_but_cannot_trade():
    observation = make_observation(warmup=True)
    validate_observation(observation, make_manifest())
    with pytest.raises(ValueError):
        parse_reply(reply_bytes(make_target()), make_manifest(), observation)
    no_decision = make_target(
        action="no_decision", targets=(), omitted_positions="preserve"
    )
    assert parse_reply(reply_bytes(no_decision), make_manifest(), observation)
    with pytest.raises(ValueError):
        make_observation(history=(make_bar(),), warmup=False)
    event_manifest = make_manifest("event")
    with pytest.raises(ValueError):
        parse_reply(
            reply_bytes(EventDecision(intents=(make_order(),))),
            event_manifest,
            make_observation(event_manifest, warmup=True),
        )
    assert parse_reply(
        reply_bytes(EventDecision()),
        event_manifest,
        make_observation(event_manifest, warmup=True),
    )


def test_builder_prefix_future_price_membership_and_source_order_invariance():
    manifest = make_manifest()
    prefix = [make_bar(days_ago=1), make_bar()]
    original = make_observation(manifest, history=prefix)
    future = make_bar(days_ago=-3, close=1000.0, high=1000.0)
    future_listing = make_listing(
        L2, effective_from=DAY + timedelta(days=3), known_at=NOW + timedelta(days=3)
    )
    past_out_of_window = make_bar(days_ago=2)
    extended = make_observation(
        manifest,
        history=[future, *reversed(prefix), past_out_of_window],
        listings=(future_listing, make_listing()),
    )
    assert canonical_json(extended) == canonical_json(original)
    perturbed = make_observation(
        manifest, history=[make_bar(days_ago=-4, close=5000.0, high=5000.0), *prefix]
    )
    assert perturbed.snapshot_id == original.snapshot_id
    assert stable_signal_id(STRATEGY, original, "buy") == stable_signal_id(
        STRATEGY, extended, "buy"
    )
    assert stable_signal_id(STRATEGY, original, "buy") != stable_signal_id(
        STRATEGY, original, "sell"
    )


def test_builder_requires_complete_universe_and_filters_late_available_rows():
    with pytest.raises(ValueError, match="incomplete cross-sectional"):
        make_observation(listings=(make_listing(), make_listing(L2)))
    with pytest.raises(ValueError, match="incomplete cross-sectional"):
        make_observation(history=(make_bar(available_at=NOW + timedelta(seconds=1)),))
    with pytest.raises(ValueError, match="duplicate"):
        make_observation(history=(make_bar(), make_bar()))


def test_observation_rejects_future_history_and_unordered_duplicate_inputs():
    original = make_observation().model_dump(mode="json")
    for bars in [
        [make_bar(days_ago=-1)],
        [make_bar(), make_bar(days_ago=1)],
        [make_bar(), make_bar()],
    ]:
        payload = dict(original, history=[b.model_dump(mode="json") for b in bars])
        with pytest.raises(ValueError):
            parse_observation(canonical_json(payload), make_manifest())


@pytest.mark.parametrize(
    "timestamp",
    [
        "2025-01-03",
        "2025-01-03T21:00:00",
        "2025-01-03T21:00:00+00:00",
        1735938000,
        "2025-01-03T21:00:00-00:00",
    ],
)
def test_wire_timestamp_requires_unambiguous_canonical_utc(timestamp):
    payload = make_observation().model_dump(mode="json")
    payload["decision_time"] = timestamp
    with pytest.raises(ValueError):
        parse_observation(canonical_json(payload), make_manifest())


def test_naive_datetime_bad_codes_future_accounts_and_positions_reject():
    for changes in [
        {"known_at": datetime(2020, 1, 1)},
        {"symbol": "AAPL\n"},
        {"currency": "usd"},
        {"mic": "????"},
        {"effective_to": date(2019, 1, 1)},
    ]:
        with pytest.raises(ValueError):
            make_listing(**changes)
    for changes in [
        {"quantity": -1.0},
        {"quantity": 1.5},
        {"available_quantity": 20.0},
    ]:
        fields = {
            "listing_id": L1,
            "quantity": 10.0,
            "available_quantity": 10.0,
            "market_value": 100.0,
        }
        fields.update(changes)
        with pytest.raises(ValueError):
            PositionView(**fields)
    with pytest.raises(ValueError):
        make_observation(
            account=AccountView(as_of=NEXT, currency="USD", cash=0.0, equity=0.0)
        )


def test_templates_are_parsed_as_source_never_imported_or_invoked():
    root = (
        Path(__file__).resolve().parents[2] / "packages" / "strategy_sdk" / "templates"
    )
    for name, method in [
        ("portfolio_momentum.py", "decide"),
        ("event_trend.py", "on_event"),
    ]:
        source = (root / name).read_text()
        module = ast.parse(source)
        policy = next(
            n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "Policy"
        )
        assert method in {n.name for n in policy.body if isinstance(n, ast.FunctionDef)}
        assert not any(
            isinstance(n, (ast.Import, ast.ImportFrom))
            and getattr(n, "module", "") in {"akquant", "subprocess", "socket"}
            for n in ast.walk(module)
        )
        assert "never" in source.lower() or "only" in source.lower()


def test_unannounced_future_delisting_is_not_visible_or_hash_relevant():
    original = make_observation()
    for end, known in [
        (DAY + timedelta(days=3), None),
        (DAY + timedelta(days=100), None),
        (DAY + timedelta(days=3), NOW + timedelta(days=1)),
    ]:
        changed = make_observation(
            listings=(make_listing(effective_to=end, effective_to_known_at=known),)
        )
        assert changed.listings[0].effective_to is None
        assert canonical_json(changed) == canonical_json(original)
    announced = make_observation(
        listings=(
            make_listing(
                effective_to=DAY + timedelta(days=3), effective_to_known_at=NOW
            ),
        )
    )
    assert announced.listings[0].effective_to == DAY + timedelta(days=3)
    assert announced.snapshot_id != original.snapshot_id


def test_account_position_order_is_normalized_before_snapshot_hash():
    positions = (
        PositionView(
            listing_id=L1, quantity=10.0, available_quantity=10.0, market_value=100.0
        ),
        PositionView(
            listing_id=L2, quantity=5.0, available_quantity=5.0, market_value=50.0
        ),
    )
    options = {
        "listings": (make_listing(L2), make_listing(L1)),
        "history": (make_bar(L2), make_bar(L1), make_bar(L2, 1), make_bar(L1, 1)),
    }
    account = lambda rows: AccountView(
        as_of=NOW, currency="USD", cash=100.0, equity=250.0, positions=rows
    )
    a = make_observation(**options, account=account(positions))
    b = make_observation(**options, account=account(tuple(reversed(positions))))
    assert canonical_json(a) == canonical_json(b)


@pytest.mark.parametrize(
    "changes", [{"short": 0}, {"oco": 0}, {"leverage": True}, {"leverage": 1.0}]
)
def test_literal_capabilities_do_not_coerce_bool_or_number_types(changes):
    with pytest.raises(ValueError):
        OrderCapabilities(**changes)


def test_bar_complete_requires_boolean_not_numeric_one():
    with pytest.raises(ValueError):
        make_bar(complete=1)


def test_observation_prefix_checksum_cannot_be_reused_for_changed_visible_data():
    original = make_observation().model_dump(mode="json")
    original["account"]["cash"] = 999.0
    with pytest.raises(ValueError, match="prefix checksum"):
        parse_observation(canonical_json(original), make_manifest())
