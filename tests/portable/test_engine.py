"""Offline tests of reviewed engine logic with inert broker/result fakes only.

No candidate is imported and no AKQuant backtest is executed on the host.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.strategy_sdk import (
    AccountView,
    CancelIntent,
    EngineProfile,
    PositionView,
    StateCodec,  # noqa: F401 - Shared by research-trace controller tests.
    Target,
    TargetPortfolio,
    build_observation,
    canonical_json,
    hash_json,
)
from packages.tradegrad_adapter.portable_results import (
    canonical_json_bytes,
    parse_portable_json,
    validate_portable_result,
)
from runtimes.portable_engine.export import _duration_ns, export_result
from runtimes.portable_engine.runtime import (
    Controller,
    DailyDecisionBarrier,
    NativeVolumeLimit,
    from_ns,
    iso,
    load_inputs,
    lot_floor,
    next_open_sell_capacity,
    reviewed_profile,
    target_orders,
    to_ns,
)

# Preserve the shared helper names for existing inert-broker test consumers.
from tests.portable.engine_fixtures import (
    L1,
    L2,
    SCHEMA,  # noqa: F401 - Shared by research-trace controller tests.
    changed_input,
    event_reply,
    fixture,
    inputs,
    order,
    portfolio_reply,
    state_after,  # noqa: F401 - Preserve the existing shared fixture API.
)


class FakeAQ:
    TimeInForce = SimpleNamespace(GTC="GTC")

    @staticmethod
    def NextOpen():
        return "reviewed_next_open"


class FakeBroker:
    def __init__(
        self,
        session,
        *,
        cash=10000.0,
        equity=10000.0,
        positions=None,
        available=None,
        legs=1,
    ):
        self.cash, self.equity = cash, equity
        self.positions = positions or {}
        self.available = self.positions.copy() if available is None else available
        self.session, self.legs = session, legs
        self.orders, self.submissions, self.cancelled = {}, [], []

    def get_available_position(self, key):
        return self.available.get(key, 0.0)

    def get_order(self, key):
        return self.orders[key]

    def submit_order(self, **kwargs):
        self.submissions.append(kwargs)
        assert "client_order_id" not in kwargs
        assert (
            kwargs["time_in_force"] == "GTC"
            and kwargs["fill_mode"] == "reviewed_next_open"
        )
        ids = []
        for index in range(self.legs):
            oid = f"native:{len(self.submissions)}:{index}"
            ns = to_ns(self.session.close_time)
            self.orders[oid] = SimpleNamespace(
                id=oid,
                tag=kwargs["tag"],
                symbol=kwargs["symbol"],
                side=kwargs["side"],
                order_type=kwargs["order_type"],
                price=kwargs["price"],
                time_in_force="GTC",
                status="Accepted",
                reject_reason="",
                commission=0.0,
                quantity=kwargs["quantity"] / self.legs,
                filled_quantity=0.0,
                created_at=ns,
                updated_at=ns,
            )
            ids.append(oid)
        return SimpleNamespace(order_ids=tuple(ids))

    def cancel_order(self, key):
        self.cancelled.append(key)
        self.orders[key].status = "Cancelled"
        self.orders[key].updated_at = to_ns(self.session.close_time)


def fake_result(controller, broker=None, fills=()):
    sessions = [
        s
        for s in controller.inputs.sessions
        if s.session_date >= date.fromisoformat(controller.inputs.window["start"])
    ]
    raw = SimpleNamespace(
        equity_curve=[(to_ns(s.close_time), 10000.0) for s in sessions],
        cash_curve=[(to_ns(s.close_time), 10000.0) for s in sessions],
        snapshots=[],
        orders=list(broker.orders.values()) if broker else [],
        executions=list(fills),
        trades=[],
    )
    return SimpleNamespace(
        _raw=raw,
        metrics_df={
            "value": {
                "duration": timedelta(days=2, seconds=3, microseconds=7),
                "execution_count": len(fills),
                "closed_trade_count": 0.0,
                "sharpe_ratio": float("nan"),
            }
        },
        resolved_config={
            "timezone": controller.inputs.manifest.timezone,
            "slippage": {"type": "percent", "value": 0.0005},
            "volume_limit_pct": 0.25,
            "commission_policy": {"type": "percent", "value": 0.0005},
            "stamp_tax_rate": 0.001
            if controller.inputs.manifest.market == "CN"
            else 0.0,
            "transfer_fee_rate": 0.0,
            "min_commission": 0.0,
            "t_plus_one": False,
            "history_depth": controller.inputs.manifest.data.lookback_bars,
            "fill_policy": {
                "price_basis": "open",
                "bar_offset": 1,
                "temporal": "same_cycle",
            },
        },
        resolved_execution_policy={
            "price_basis": "open",
            "bar_offset": 1,
            "temporal": "same_cycle",
            "source": "explicit",
        },
    )


def hold_bundle():
    data = inputs()
    controller = Controller(data, lambda obs: portfolio_reply(obs))
    broker = FakeBroker(data.sessions[0])
    for session in data.sessions:
        broker.session = session
        controller.on_session(session, broker, FakeAQ)
    return export_result(fake_result(controller), controller)


def test_runtime_imports_no_akquant_or_pandas():
    root = str(Path(__file__).resolve().parents[2])
    script = "import sys; import runtimes.portable_engine.runtime; import packages.tradegrad_adapter.portable_results; assert 'akquant' not in sys.modules; assert 'pandas' not in sys.modules"
    subprocess.run([sys.executable, "-c", script], cwd=root, check=True)


@pytest.mark.parametrize("market", ["US", "CN"])
def test_valid_data_loads_and_fixed_market_profile(market):
    data = inputs(market)
    assert data.profile["t_plus_one"] is False
    assert data.profile["engine_settlement_mode"] == "T+0"
    assert data.profile["application_t_plus_one"] is (market == "CN")
    assert data.profile["lot_size"] == (100 if market == "CN" else 1)
    assert data.profile["risk_config"] == {
        "account_mode": "cash",
        "enable_short_sell": False,
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "future_availability",
        "actions",
        "wrong_lot",
        "profile",
        "extra",
    ],
)
def test_malformed_or_unsupported_market_fails_closed(mutation):
    req, data = fixture(listing_count=2)
    if mutation == "missing":
        data["bars"].pop()
    elif mutation == "duplicate":
        data["bars"].append(data["bars"][0])
    elif mutation == "future_availability":
        data["bars"][0]["available_at"] = "2025-01-03T21:00:00Z"
    elif mutation == "actions":
        data["corporate_actions"] = [{"type": "split"}]
    elif mutation == "wrong_lot":
        data["listings"][0]["lot_size"] = 100
        req["manifest"]["universe"]["snapshot_sha256"] = hash_json(data["listings"])
    elif mutation == "profile":
        req["manifest"]["engine_profile"]["profile_sha256"] = "0" * 64
    else:
        req["source"] = "never imported"
    with pytest.raises(ValueError):
        changed_input(req, data)


def test_file_identity_and_independent_calendar_are_required():
    req, data = fixture()
    with pytest.raises(ValueError, match="checksum"):
        load_inputs(
            canonical_json(req),
            canonical_json({**data, "bars": []}),
            calendar_verifier=lambda *_: None,
        )

    def reject(*_):
        raise ValueError("independent exchange calendar")

    with pytest.raises(ValueError, match="independent exchange"):
        load_inputs(canonical_json(req), canonical_json(data), calendar_verifier=reject)


def test_event_multi_listing_requires_complete_cross_section():
    req, data = fixture(kind="event", listing_count=2)
    assert len(changed_input(req, data).listings) == 2
    data["bars"].pop()
    with pytest.raises(ValueError, match="incomplete effective cross-section"):
        changed_input(req, data)


def test_explicit_warmup_replay_has_flat_account_and_no_intents():
    data = inputs(prewarm=True)
    observed = []

    def policy(obs):
        observed.append(obs)
        return portfolio_reply(obs)

    controller = Controller(data, policy)
    controller.on_session(data.sessions[0])
    assert observed[0].warmup and not observed[0].account.positions
    assert observed[0].account.equity == data.initial_cash
    assert controller.trace[0]["outcome"] == "warmup_state_only"
    assert controller.state.payload_json == '{"counter":1}'


def test_target_sizing_is_close_equity_lots_and_never_sale_proceeds():
    data = inputs(listing_count=2)
    s = data.sessions[0]
    obs = build_observation(
        manifest=data.manifest,
        decision_time=s.close_time,
        next_open_time=s.next_open_time,
        session_date=s.session_date,
        listings=data.listings,
        history=data.bars,
        state=data.state,
        account=AccountView(
            as_of=s.close_time,
            currency="USD",
            cash=0.0,
            equity=1000.0,
            positions=(
                PositionView(
                    listing_id=L1,
                    quantity=100.0,
                    available_quantity=100.0,
                    market_value=1000.0,
                ),
            ),
        ),
    )
    decision = TargetPortfolio(
        signal_id="sizing",
        effective_at=s.next_open_time,
        expires_at=None,
        action="rebalance",
        basis="target_weight",
        targets=(Target(listing_id=L2, value=1.0),),
        omitted_positions="liquidate",
    )
    assert target_orders(decision, obs, data.profile) == [(L1, "sell", 100)]
    assert lot_floor(299.999, 100) == 200


def test_pending_rebalance_is_not_resubmitted_and_final_order_not_liquidated():
    data = inputs()
    controller = Controller(
        data,
        lambda obs: portfolio_reply(
            obs, "rebalance", (Target(listing_id=L1, value=10.0),)
        ),
    )
    broker = FakeBroker(data.sessions[0])
    for s in data.sessions:
        broker.session = s
        controller.on_session(s, broker, FakeAQ)
    assert len(broker.submissions) == 1
    assert controller.trace[-1]["outcome"] == "skipped_pending_rebalance"
    bundle = export_result(fake_result(controller, broker), controller)
    assert bundle["logical_orders"][0]["terminal"] is False
    assert not bundle["fills"] and not bundle["closed_trades"]
    assert bundle["engine"]["forced_final_liquidation"] is False


def test_cancel_cancels_every_native_receipt_leg():
    data = inputs(kind="event")

    def policy(obs):
        if not obs.open_orders:
            return event_reply(obs, (order(obs),))
        return event_reply(
            obs,
            (
                CancelIntent(
                    signal_id="cancel-all",
                    listing_id=L1,
                    order_id=obs.open_orders[0].order_id,
                    effective_at=obs.next_open_time,
                    expires_at=None,
                ),
            ),
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], legs=2)
    controller.on_session(data.sessions[0], broker, FakeAQ)
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    assert broker.cancelled == ["native:1:0", "native:1:1"]


@pytest.mark.parametrize("case", ["cash", "holdings", "lot"])
def test_event_constraints_fail_closed(case):
    data = inputs("CN", "event")
    broker = FakeBroker(
        data.sessions[0],
        cash=0.0 if case == "cash" else 10000.0,
        positions={L1: 100.0},
        available={L1: 100.0},
    )
    side, qty = (
        ("sell", 200.0)
        if case == "holdings"
        else ("buy", 1.0 if case == "lot" else 100.0)
    )
    controller = Controller(
        data, lambda obs: event_reply(obs, (order(obs, side, qty),))
    )
    with pytest.raises(ValueError, match="cash|holdings|lot"):
        controller.on_session(data.sessions[0], broker, FakeAQ)
    assert not broker.submissions


@pytest.mark.parametrize("kind", ["portfolio", "event"])
def test_cn_controller_admits_earliest_next_open_reversal_not_silent_t2(kind):
    data = inputs("CN", kind)
    seen = []

    def policy(obs):
        seen.append(obs)
        if len(seen) == 1:
            if kind == "portfolio":
                return portfolio_reply(
                    obs, "rebalance", (Target(listing_id=L1, value=100.0),)
                )
            return event_reply(obs, (order(obs, "buy", 100.0),))
        if kind == "portfolio":
            return portfolio_reply(obs, "liquidate")
        return event_reply(obs, (order(obs, "sell", 100.0),))

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0])
    controller.on_session(data.sessions[0], broker, FakeAQ)
    native = next(iter(broker.orders.values()))
    native.status, native.filled_quantity = "Filled", 100.0
    native.updated_at = to_ns(data.sessions[1].close_time)
    broker.positions[L1], broker.available[L1] = 100.0, 0.0
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    # No native availability value can be misread as current T+1 sellability.
    # The separately labelled next-open budget includes already-held shares.
    obs = seen[1]
    assert obs.account.positions[0].available_quantity == 100.0
    assert (
        obs.account.positions[0].available_quantity_semantics
        == "next_open_unreserved_holdings"
    )
    assert next_open_sell_capacity(obs, data.profile) == {L1: 100.0}
    if kind == "portfolio":
        decision = TargetPortfolio.model_validate(
            portfolio_reply(obs, "liquidate")["decision"]
        )
        assert target_orders(decision, obs, data.profile) == [(L1, "sell", 100)]
    assert [o["side"] for o in broker.submissions] == ["Buy", "Sell"]
    assert broker.submissions[1]["quantity"] == 100.0
    assert controller.logical_orders[1]["effective_at"] == iso(
        data.sessions[2].open_time
    )
    assert len(controller.trace) == 2
    assert controller.completed == {s.session_date for s in data.sessions[:2]}


@pytest.mark.parametrize("kind", ["portfolio", "event"])
def test_settled_cn_sell_retains_native_reduce_only_and_rejection_authority(kind):
    data = inputs("CN", kind)
    seen = []

    def policy(obs):
        seen.append(obs)
        if kind == "portfolio":
            return portfolio_reply(obs, "liquidate" if len(seen) == 1 else "hold")
        return event_reply(obs, (order(obs, "sell", 100.0),) if len(seen) == 1 else ())

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 100.0})
    controller.on_session(data.sessions[0], broker, FakeAQ)
    submitted = broker.submissions[0]
    assert submitted["reduce_only"] is True and submitted["position_effect"] == "close"
    assert submitted["fill_mode"] == "reviewed_next_open"
    native = next(iter(broker.orders.values()))
    native.status, native.reject_reason = "Rejected", "trusted kernel rejection"
    native.updated_at = to_ns(data.sessions[1].close_time)
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    assert seen[1].order_events[0].status == "rejected"
    assert seen[1].account.positions[0].quantity == 100.0
    assert not controller.pending
    assert controller.logical_orders[0]["filled_quantity"] == 0.0


@pytest.mark.parametrize("remaining", [100.0, 200.0])
def test_event_gtc_sell_remainders_reserve_holdings_without_double_counting(remaining):
    data = inputs("CN", "event")

    def policy(obs):
        return event_reply(
            obs,
            (
                order(
                    obs,
                    "sell",
                    200.0 if not obs.open_orders else 100.0,
                    order_type="limit",
                    limit_price=1.0,
                ),
            ),
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 300.0})
    controller.on_session(data.sessions[0], broker, FakeAQ)
    native = next(iter(broker.orders.values()))
    native.filled_quantity = 200.0 - remaining
    native.status = "PartiallyFilled" if native.filled_quantity else "Accepted"
    native.updated_at = to_ns(data.sessions[1].close_time)
    broker.positions[L1] -= native.filled_quantity
    broker.available[L1] = broker.positions[L1]
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    assert [row["quantity"] for row in broker.submissions] == [200.0, 100.0]
    broker.session = data.sessions[2]
    with pytest.raises(ValueError, match="next-open unreserved holdings"):
        controller.on_session(data.sessions[2], broker, FakeAQ)
    assert len(broker.submissions) == 2


def test_event_pending_buy_cannot_authorize_anticipated_sell():
    data = inputs("CN", "event")

    def policy(obs):
        side = "sell" if obs.open_orders else "buy"
        return event_reply(obs, (order(obs, side, 100.0),))

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0])
    controller.on_session(data.sessions[0], broker, FakeAQ)
    broker.session = data.sessions[1]
    with pytest.raises(ValueError, match="next-open unreserved holdings"):
        controller.on_session(data.sessions[1], broker, FakeAQ)
    assert len(broker.submissions) == 1


def test_event_cancel_does_not_free_sell_reservation_until_native_terminal():
    data = inputs("CN", "event")

    def policy(obs):
        if not obs.open_orders:
            return event_reply(
                obs, (order(obs, "sell", 100.0, order_type="limit", limit_price=20.0),)
            )
        pending = obs.open_orders[0]
        return event_reply(
            obs,
            (
                CancelIntent(
                    signal_id="cancel",
                    listing_id=L1,
                    order_id=pending.order_id,
                    effective_at=obs.next_open_time,
                    expires_at=None,
                ),
                order(obs, "sell", 100.0),
            ),
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 100.0})
    controller.on_session(data.sessions[0], broker, FakeAQ)
    broker.session = data.sessions[1]
    with pytest.raises(ValueError, match="next-open unreserved holdings"):
        controller.on_session(data.sessions[1], broker, FakeAQ)
    assert not broker.cancelled and len(broker.submissions) == 1


def test_event_oversell_in_one_reply_fails_before_any_native_submission():
    data = inputs("CN", "event")
    controller = Controller(
        data,
        lambda obs: event_reply(
            obs,
            (
                order(obs, "sell", 100.0, signal_id="first"),
                order(obs, "sell", 100.0, signal_id="second"),
            ),
        ),
    )
    broker = FakeBroker(data.sessions[0], positions={L1: 100.0})
    with pytest.raises(ValueError, match="next-open unreserved holdings"):
        controller.on_session(data.sessions[0], broker, FakeAQ)
    assert not broker.submissions


def test_reference_event_template_does_not_confuse_current_sellability_with_next_open():
    # Parse only. This test must never import or invoke policy code on the host.
    source = Path("packages/strategy_sdk/templates/event_trend.py").read_text()
    module = ast.parse(source)
    attributes = [
        node.attr for node in ast.walk(module) if isinstance(node, ast.Attribute)
    ]
    assert "available_quantity" in attributes
    assert "quantity" in attributes and "next_open_time" in attributes


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_legacy_sell_profile_manifest_hash_is_not_silently_reinterpreted(version):
    request, data = fixture("CN")
    legacy = reviewed_profile("CN", 10000.0)
    legacy["profile_version"] = f"akquant.portable.cash-daily.{version}"
    for key in ("sell_budget", "sellability_observation", "unsettled_next_open_sell"):
        del legacy[key]
    request["manifest"]["engine_profile"]["profile_sha256"] = hash_json(legacy)
    with pytest.raises(ValueError, match="reviewed engine profile"):
        changed_input(request, data)


def test_duplicate_signal_rejected_across_sessions():
    data = inputs(kind="event")
    controller = Controller(
        data, lambda obs: event_reply(obs, (order(obs, signal_id="repeat"),))
    )
    broker = FakeBroker(data.sessions[0])
    controller.on_session(data.sessions[0], broker, FakeAQ)
    broker.session = data.sessions[1]
    with pytest.raises(ValueError, match="replayed"):
        controller.on_session(data.sessions[1], broker, FakeAQ)


def test_zero_trade_valid_result_and_stdlib_roundtrip():
    bundle = hold_bundle()
    assert validate_portable_result(canonical_json_bytes(bundle)) == bundle
    assert bundle["summary"]["execution_count"] == 0
    assert bundle["runtime_certified"] is False
    assert bundle["native_metrics"][-1]["unavailable_reason"]
    duration = next(m for m in bundle["native_metrics"] if m["name"] == "duration")
    assert duration["value"] == "172803000007000"
    assert duration["unit"] == "nanoseconds"
    assert duration["unavailable_reason"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "certified",
        "return",
        "negative_cash",
        "state",
        "state_chain",
        "native_time",
        "duplicate_trace",
        "missing_metrics",
    ],
)
def test_result_tampering_rejected(mutation):
    bundle = hold_bundle()
    if mutation == "extra":
        bundle["policy_profit"] = 9999
    elif mutation == "certified":
        bundle["runtime_certified"] = True
    elif mutation == "return":
        bundle["summary"]["total_return_pct"] = 999
    elif mutation == "negative_cash":
        bundle["account_series"][0]["cash"] = -1
    elif mutation == "state":
        bundle["final_state"]["payload_json"] = '{"counter":999}'
    elif mutation == "state_chain":
        bundle["decision_trace"][1]["state_before_sha256"] = "f" * 64
    elif mutation == "native_time":
        bundle["account_series"][0]["native_at"] = "2025-01-02T14:30:00Z"
    elif mutation == "duplicate_trace":
        bundle["decision_trace"].append(bundle["decision_trace"][-1])
    else:
        bundle["native_metrics"] = []
    with pytest.raises(ValueError):
        validate_portable_result(bundle)


@pytest.mark.parametrize(
    "wire",
    [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"x":9007199254740992}'],
)
def test_wire_rejects_ambiguous_or_nonfinite_json(wire):
    with pytest.raises(ValueError):
        parse_portable_json(wire)


def test_native_nanoseconds_roundtrip_without_float_timestamp():
    t = datetime(2025, 1, 2, 21, 0, 0, 123456, tzinfo=UTC)
    assert from_ns(to_ns(t)) == t


def test_container_image_is_source_free_and_dependencies_hash_locked():
    docker = Path("runtimes/portable_engine/Dockerfile").read_text()
    assert "--require-hashes" in docker and "USER 65532:65532" in docker
    assert "COPY src" not in docker and "policy.py" not in docker
    assert "protocol.py" in docker and "calendar-requirements.lock" in docker
    assert "LABEL io.tradegrad.portable_role=engine" in docker


def test_native_random_order_ids_cannot_change_policy_prefix():
    observations = []
    for suffix in ("first-random", "other-random"):
        captured = []
        data = inputs(kind="event")

        def policy(obs, captured=captured):
            captured.append(canonical_json(obs))
            return (
                event_reply(obs, (order(obs),))
                if len(captured) == 1
                else event_reply(obs)
            )

        controller = Controller(data, policy)
        broker = FakeBroker(data.sessions[0])
        original_submit = broker.submit_order

        def submit(
            *, original_submit=original_submit, broker=broker, suffix=suffix, **kwargs
        ):
            receipt = original_submit(**kwargs)
            oid = receipt.order_ids[0]
            native = broker.orders.pop(oid)
            native.id = suffix
            broker.orders[suffix] = native
            return SimpleNamespace(order_ids=(suffix,))

        broker.submit_order = submit
        controller.on_session(data.sessions[0], broker, FakeAQ)
        broker.session = data.sessions[1]
        controller.on_session(data.sessions[1], broker, FakeAQ)
        observations.append(captured)
    assert observations[0] == observations[1]


def filled_bundle():
    data = inputs(kind="event")
    seen = []

    def policy(obs):
        seen.append(obs)
        return event_reply(obs, (order(obs),)) if len(seen) == 1 else event_reply(obs)

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0])
    controller.on_session(data.sessions[0], broker, FakeAQ)
    native = next(iter(broker.orders.values()))
    native.status, native.filled_quantity, native.commission = "Filled", 10.0, 0.05
    native.updated_at = to_ns(data.sessions[1].close_time)
    broker.positions[L1] = broker.available[L1] = 10.0
    for session in data.sessions[1:]:
        broker.session = session
        controller.on_session(session, broker, FakeAQ)
    fill = SimpleNamespace(
        id="fill:1",
        order_id=native.id,
        symbol=L1,
        side="Buy",
        quantity=10.0,
        price=10.0,
        commission=0.05,
        timestamp=native.updated_at,
    )
    return export_result(fake_result(controller, broker, [fill]), controller)


def test_actual_native_fill_timestamp_separate_from_economic_open():
    bundle = filled_bundle()
    fill = bundle["fills"][0]
    assert fill["native_at"] == "2025-01-03T21:00:00Z"
    assert fill["economic_reference_time"] == "2025-01-03T14:30:00Z"
    assert fill["decision_session"] == "2025-01-02"
    assert bundle["summary"]["total_commission"] == 0.05
    assert bundle["logical_orders"][0]["terminal"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        "same_session",
        "missing_fill",
        "quantity",
        "fees",
        "native_id",
        "fake_liquidation",
        "wrong_policy",
    ],
)
def test_native_fill_result_validation_rejects_forgery(mutation):
    bundle = filled_bundle()
    if mutation == "same_session":
        fill = bundle["fills"][0]
        fill["session_date"] = fill["decision_session"]
    elif mutation == "missing_fill":
        bundle["fills"] = []
    elif mutation == "quantity":
        bundle["fills"][0]["quantity"] = 9.0
    elif mutation == "fees":
        bundle["summary"]["total_commission"] = 0.0
    elif mutation == "native_id":
        bundle["fills"][0]["order_id"] = "unknown"
    elif mutation == "fake_liquidation":
        bundle["engine"]["forced_final_liquidation"] = True
    else:
        bundle["engine"]["resolved_execution_policy"]["bar_offset"] = 0
    with pytest.raises(ValueError):
        validate_portable_result(bundle)


def test_native_execution_has_no_accidental_host_fallback(monkeypatch):
    from runtimes.portable_engine.runtime import execute

    monkeypatch.delenv("TRADEGRAD_PORTABLE_ENGINE_CONTAINER", raising=False)
    with pytest.raises(RuntimeError, match="isolated engine image"):
        execute(None, None)


def test_offline_calendar_verification_when_data_calendar_is_installed():
    pytest.importorskip("exchange_calendars")
    for market in ("US", "CN"):
        req, data = fixture(market)
        assert (
            load_inputs(canonical_json(req), canonical_json(data)).manifest.market
            == market
        )


def test_big_integer_result_is_a_validation_error_not_overflow():
    with pytest.raises(ValueError):
        parse_portable_json(b'{"n":' + b"9" * 500 + b"}")


def test_final_state_schema_is_bound_to_manifest():
    bundle = hold_bundle()
    bundle["final_state"]["schema_sha256"] = "d" * 64
    with pytest.raises(ValueError, match="trace/schema"):
        validate_portable_result(bundle)


@pytest.mark.parametrize("kind", ["portfolio", "event"])
@pytest.mark.parametrize("symbol_order", [(L1, L2), (L2, L1)])
def test_complete_daily_barrier_never_exposes_partial_native_cross_section(
    kind, symbol_order
):
    data = inputs("CN", kind, listing_count=2)
    observed = []

    def policy(obs):
        observed.append(obs)
        return portfolio_reply(obs) if kind == "portfolio" else event_reply(obs)

    controller = Controller(data, policy)
    barrier = DailyDecisionBarrier(controller)
    broker = FakeBroker(data.sessions[0])
    for key in symbol_order:
        barrier.on_bar(
            SimpleNamespace(timestamp=to_ns(data.sessions[0].close_time), symbol=key),
            broker,
        )
    assert not observed  # Not even the last per-symbol bar invokes policy.
    barrier.on_cross_section(
        data.sessions[0].session_date,
        to_ns(data.sessions[0].close_time),
        broker,
        FakeAQ,
    )
    broker.session = data.sessions[1]
    for index, key in enumerate(symbol_order):
        broker.positions[key] = 100.0
        broker.cash -= 1000.0
        barrier.on_bar(
            SimpleNamespace(timestamp=to_ns(broker.session.close_time), symbol=key),
            broker,
        )
        assert len(observed) == 1
        if index == 0:
            with pytest.raises(ValueError, match="missing expected listings"):
                barrier.on_cross_section(
                    broker.session.session_date,
                    to_ns(broker.session.close_time),
                    broker,
                    FakeAQ,
                )
    barrier.on_cross_section(
        broker.session.session_date, to_ns(broker.session.close_time), broker, FakeAQ
    )
    assert len(observed) == 2
    assert {p.listing_id: p.quantity for p in observed[-1].account.positions} == {
        L1: 100.0,
        L2: 100.0,
    }
    assert observed[-1].account.cash == 8000.0
    assert all(p.available_quantity == 100.0 for p in observed[-1].account.positions)
    with pytest.raises(ValueError, match="duplicate engine decision session"):
        barrier.on_cross_section(
            broker.session.session_date,
            to_ns(broker.session.close_time),
            broker,
            FakeAQ,
        )


@pytest.mark.parametrize("mutation", ["holdings", "cash", "equity"])
def test_controller_profile_rejects_nonflat_native_start(mutation):
    data = inputs("CN")
    controller = Controller(data, lambda _: pytest.fail("policy must not be called"))
    broker = FakeBroker(data.sessions[0])
    if mutation == "holdings":
        broker.positions[L1] = 100.0
    else:
        setattr(broker, mutation, 9000.0)
    with pytest.raises(ValueError, match="flat native cash-only start"):
        DailyDecisionBarrier(controller).on_bar(
            SimpleNamespace(timestamp=to_ns(data.sessions[0].close_time), symbol=L1),
            broker,
        )


@pytest.mark.parametrize("buy_first", [True, False])
@pytest.mark.parametrize("sell_quantity,allowed", [(100.0, True), (200.0, False)])
def test_same_reply_buy_does_not_increase_controller_sell_budget(
    buy_first, sell_quantity, allowed
):
    data = inputs("CN", "event")

    def policy(obs):
        intents = [
            order(obs, "buy", 100.0, signal_id="new-buy"),
            order(obs, "sell", sell_quantity, signal_id="new-sell"),
        ]
        return event_reply(obs, tuple(intents if buy_first else reversed(intents)))

    controller = Controller(data, policy)
    # Seeded inert broker isolates the admission proof; real runtime separately
    # enforces native flat start before its first bar, tested above.
    broker = FakeBroker(data.sessions[0], positions={L1: 100.0})
    if allowed:
        controller.on_session(data.sessions[0], broker, FakeAQ)
        assert len(broker.submissions) == 2
        assert (
            sum(o["quantity"] for o in broker.submissions if o["side"] == "Sell")
            == 100.0
        )
    else:
        with pytest.raises(ValueError, match="next-open unreserved holdings"):
            controller.on_session(data.sessions[0], broker, FakeAQ)
        assert not broker.submissions


@pytest.mark.parametrize("buy_first", [True, False])
@pytest.mark.parametrize("fill_buy", [False, True])
@pytest.mark.parametrize("fill_sell", [False, True])
def test_pending_buy_sell_permutations_and_partial_fill_reservation_induction(
    buy_first, fill_buy, fill_sell
):
    data = inputs("CN", "event")
    observed = []

    def policy(obs):
        observed.append(obs)
        if len(observed) > 1:
            return event_reply(obs)
        intents = [
            order(obs, "buy", 200.0, signal_id="buy"),
            order(obs, "sell", 200.0, signal_id="sell"),
        ]
        return event_reply(obs, tuple(intents if buy_first else reversed(intents)))

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 300.0})
    controller.on_session(data.sessions[0], broker, FakeAQ)
    for native in broker.orders.values():
        is_buy = native.side == "Buy"
        if fill_buy if is_buy else fill_sell:
            native.status, native.filled_quantity = "PartiallyFilled", 100.0
            native.updated_at = to_ns(data.sessions[1].close_time)
            broker.positions[L1] += 100.0 if is_buy else -100.0
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    capacity = next_open_sell_capacity(observed[-1], data.profile)[L1]
    # Sell fills reduce held and reserved equally. Only buys actually present
    # in the authoritative ledger at this complete close increase next capacity.
    assert capacity == 100.0 + (100.0 if fill_buy else 0.0)
    assert observed[-1].account.positions[0].available_quantity == capacity


@pytest.mark.parametrize("terminal_legs", [0, 1, 2])
def test_cancel_reservations_release_only_native_confirmed_terminal_legs(terminal_legs):
    data = inputs("CN", "event")
    observed = []

    def policy(obs):
        observed.append(obs)
        return (
            event_reply(obs, (order(obs, "sell", 200.0),))
            if len(observed) == 1
            else event_reply(obs)
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 300.0}, legs=2)
    controller.on_session(data.sessions[0], broker, FakeAQ)
    for index, native in enumerate(broker.orders.values()):
        if index < terminal_legs:
            native.status = "Cancelled"
            native.updated_at = to_ns(data.sessions[1].close_time)
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    assert (
        observed[-1].account.positions[0].available_quantity
        == 100.0 + terminal_legs * 100.0
    )
    assert bool(controller.pending) is (terminal_legs != 2)


@pytest.mark.parametrize(
    "bad_fill", ["same_session", "intrabar_only_limit", "overfill", "regression"]
)
def test_controller_rejects_native_counterexamples_before_another_policy_decision(
    bad_fill,
):
    data = inputs("CN", "event")
    observed = []

    def policy(obs):
        observed.append(obs)
        return (
            event_reply(
                obs,
                (
                    order(
                        obs,
                        "buy",
                        200.0,
                        order_type="limit",
                        limit_price=9.0 if bad_fill == "intrabar_only_limit" else 12.0,
                    ),
                ),
            )
            if len(observed) == 1
            else event_reply(obs)
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0])
    controller.on_session(data.sessions[0], broker, FakeAQ)
    native = next(iter(broker.orders.values()))
    native.status, native.filled_quantity = "PartiallyFilled", 100.0
    native.updated_at = to_ns(
        data.sessions[0 if bad_fill == "same_session" else 1].close_time
    )
    if bad_fill == "overfill":
        native.filled_quantity = 300.0
    broker.positions[L1] = 100.0
    broker.session = data.sessions[1]
    if bad_fill == "regression":
        controller.on_session(data.sessions[1], broker, FakeAQ)
        native.filled_quantity = 50.0
        native.updated_at = to_ns(data.sessions[2].close_time)
        broker.session = data.sessions[2]
    before = len(observed)
    with pytest.raises(
        ValueError, match="strictly-later|intrabar-only|receipt quantities"
    ):
        controller.on_session(broker.session, broker, FakeAQ)
    assert len(observed) == before


def test_runtime_has_no_policy_callbacks_from_order_timer_preopen_or_per_symbol_bar():
    module = ast.parse(Path("runtimes/portable_engine/runtime.py").read_text())
    strategy = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.ClassDef) and node.name == "ReviewedStrategy"
    )
    methods = {
        node.name: node for node in strategy.body if isinstance(node, ast.FunctionDef)
    }
    assert "on_pre_open" not in methods  # Keep native no-op; do not register timers.
    for name in ("on_order", "on_timer", "on_bar", "on_tick"):
        assert not any(
            isinstance(node, ast.Attribute)
            and node.attr in {"on_session", "exchange", "decide", "on_event"}
            for node in ast.walk(methods[name])
        )
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "on_cross_section"
        for node in ast.walk(methods["on_cross_section"])
    )


def test_partial_terminal_cancel_releases_only_its_unfilled_leaf():
    data = inputs("CN", "event")
    observed = []

    def policy(obs):
        observed.append(obs)
        return (
            event_reply(obs, (order(obs, "sell", 400.0),))
            if len(observed) == 1
            else event_reply(obs)
        )

    controller = Controller(data, policy)
    broker = FakeBroker(data.sessions[0], positions={L1: 500.0}, legs=2)
    controller.on_session(data.sessions[0], broker, FakeAQ)
    first, second = broker.orders.values()
    first.status, first.filled_quantity = "Cancelled", 100.0
    first.updated_at = to_ns(data.sessions[1].close_time)
    broker.positions[L1] = 400.0
    broker.session = data.sessions[1]
    controller.on_session(data.sessions[1], broker, FakeAQ)
    assert second.status == "Accepted"
    assert observed[-1].account.positions[0].available_quantity == 200.0
    assert observed[-1].open_orders[0].remaining_quantity == 200.0
    assert controller.logical_orders[0]["filled_quantity"] == 100.0
    assert controller.logical_orders[0]["terminal"] is False


def test_result_validator_rejects_limit_price_breach_independently_of_native_export():
    bundle = filled_bundle()
    for row in bundle["orders"] + bundle["logical_orders"]:
        row["order_type"], row["limit_price"] = "limit", 9.0
    with pytest.raises(ValueError, match="breaches declared limit price"):
        validate_portable_result(bundle)


def test_manifest_declares_controller_not_native_t1_and_rejects_stale_profiles():
    req, _ = fixture("CN")
    profile = req["manifest"]["engine_profile"]
    assert profile["engine_settlement_mode"] == "T+0"
    assert (
        profile["application_settlement_enforcement"]
        == "trusted_controller_daily_next_open_inventory.v1"
    )
    for stale in ("akquant.portable.cash-daily.v1", "akquant.portable.cash-daily.v2"):
        with pytest.raises(ValueError):
            EngineProfile.model_validate(profile | {"profile_version": stale})


def test_daily_barrier_rejects_future_bar_before_prior_session_decision():
    data = inputs("CN", "event", listing_count=2)
    controller = Controller(data, lambda _: pytest.fail("policy must not be called"))
    barrier = DailyDecisionBarrier(controller)
    broker = FakeBroker(data.sessions[0])
    barrier.on_bar(
        SimpleNamespace(timestamp=to_ns(data.sessions[0].close_time), symbol=L1), broker
    )
    with pytest.raises(ValueError, match="unfinished daily decision barrier"):
        barrier.on_bar(
            SimpleNamespace(timestamp=to_ns(data.sessions[1].close_time), symbol=L2),
            broker,
        )


def test_inert_broker_matches_pinned_positions_property_not_removed_method():
    import inspect

    assert not hasattr(FakeBroker, "get_positions")
    source = inspect.getsource(Controller)
    assert "broker.get_positions(" not in source
    assert "broker.positions" in source


@pytest.mark.parametrize(
    "value,expected",
    [
        (timedelta(days=2, seconds=3, microseconds=7), 172803000007000),
        (timedelta(microseconds=-1), -1000),
        (SimpleNamespace(value=123456789), 123456789),
        (123456789, 123456789),
    ],
)
def test_native_duration_preserves_exact_nanoseconds_without_engine(value, expected):
    assert _duration_ns(value) == expected


@pytest.mark.parametrize("value", [None, True, 1.5, "123", object()])
def test_native_duration_never_truncates_or_guesses_unsupported_types(value):
    with pytest.raises(ValueError, match="duration representation"):
        _duration_ns(value)


def test_native_volume_limit_is_exact_and_does_not_mutate_input_data():
    import copy

    calls = []
    data = {"bars": [{"open": 104.0, "volume": 400.0}], "symbols": [L1]}
    original = copy.deepcopy(data)
    engine = SimpleNamespace(set_volume_limit=lambda value: calls.append(value))
    context = {"engine": engine, "data": data}
    hook = NativeVolumeLimit(0.25)
    with pytest.raises(ValueError, match="unverified"):
        hook.require_applied()
    hook.on_start(context)
    hook.require_applied()
    hook.on_bar(context)
    hook.on_trade(context)
    assert hook.on_finish(context) == {"volume_limit_configured": True}
    assert calls == [0.25]
    assert data == original and context == {"engine": engine, "data": data}


@pytest.mark.parametrize("failure", ["not_called", "no_engine", "no_setter", "throws"])
def test_unverified_native_configuration_blocks_accounting_decision(failure):
    hook = NativeVolumeLimit(0.25)

    def fail(_):
        raise RuntimeError("inert setter failure")

    if failure != "not_called":
        context = (
            {}
            if failure == "no_engine"
            else {"engine": object()}
            if failure == "no_setter"
            else {"engine": SimpleNamespace(set_volume_limit=fail)}
        )
        with pytest.raises(ValueError, match="configuration failed"):
            hook.on_start(context)
    accounting_calls = []
    with pytest.raises(ValueError, match="unverified"):
        hook.require_applied()
        accounting_calls.append("must not run")
    with pytest.raises(ValueError, match="unverified"):
        hook.on_finish({})
    assert accounting_calls == []


def test_native_configuration_failed_restart_clears_prior_verification():
    hook = NativeVolumeLimit(0.25)
    hook.on_start({"engine": SimpleNamespace(set_volume_limit=lambda _: None)})
    hook.require_applied()
    with pytest.raises(ValueError, match="configuration failed"):
        hook.on_start({})
    with pytest.raises(ValueError, match="unverified"):
        hook.require_applied()


@pytest.mark.parametrize("value", [None, True, 0, 0.0, 0.2500001, "0.25"])
def test_native_configuration_never_substitutes_a_different_limit(value):
    with pytest.raises(ValueError, match="unreviewed"):
        NativeVolumeLimit(value)


def test_native_configuration_guards_precede_each_native_accounting_callback():
    module = ast.parse(Path("runtimes/portable_engine/runtime.py").read_text())
    execute = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "execute"
    )
    strategy = next(
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.ClassDef) and node.name == "ReviewedStrategy"
    )
    for name in ("on_bar", "on_cross_section", "on_order", "on_trade"):
        method = next(
            node for node in strategy.body if getattr(node, "name", None) == name
        )
        first = method.body[0]
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
        assert ast.unparse(first.value.func) == "native_configuration.require_applied"
    backtest = next(
        node
        for node in ast.walk(execute)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "aq.run_backtest"
    )
    keywords = {item.arg: ast.unparse(item.value) for item in backtest.keywords}
    assert keywords["analyzer_plugins"] == "[native_configuration]"
    assert keywords["volume_limit_pct"] == "profile['volume_limit_pct']"
    assert keywords["fill_policy"] == "aq.NextOpen()"
    assert keywords["risk_config"] == "profile['risk_config']"
    assert keywords["lot_size"] == "profile['lot_size']"
    call_index = next(
        index
        for index, node in enumerate(execute.body)
        if isinstance(node, ast.Assign) and node.value is backtest
    )
    assert (
        ast.unparse(execute.body[call_index + 1])
        == "native_configuration.require_applied()"
    )


def test_reference_cash_controls_differ_only_after_first_decision():
    from tests.portable.test_isolation import affordable_reference_data, fixture_data

    adverse = fixture_data("US", "portfolio")
    control = affordable_reference_data("US", "portfolio")
    first_decision = adverse["sessions"][20]["session_date"]
    assert control["sessions"] == adverse["sessions"]
    assert control["listings"] == adverse["listings"]
    assert control["corporate_actions"] == adverse["corporate_actions"]
    for positive, negative in zip(control["bars"], adverse["bars"]):
        assert {k: v for k, v in positive.items() if k != "open"} == {
            k: v for k, v in negative.items() if k != "open"
        }
        if positive["session_date"] <= first_decision:
            assert positive == negative
        else:
            assert positive["low"] < positive["open"] < positive["close"]
            assert negative["open"] > negative["close"]
    assert affordable_reference_data("CN", "event") == fixture_data("CN", "event")


def test_no_touch_reservation_control_avoids_unrelated_native_fee_rejection():
    from tests.portable.test_isolation import fixture_data

    data = fixture_data("CN", "event", count=9)
    profile = reviewed_profile("CN", 100000.0)
    limit, quantity = 1000.0, 200.0
    assert all(bar["high"] < limit for bar in data["bars"])
    hypothetical_fees = (
        quantity * limit * (profile["commission_rate"] + profile["stamp_tax_rate"])
    )
    conservative_cash = 100000.0 - 300 * max(bar["high"] for bar in data["bars"])
    assert hypothetical_fees < conservative_cash
    assert 200 * 1000000.0 * 0.0015 > 100000.0
