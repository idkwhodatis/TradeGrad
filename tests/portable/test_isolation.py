"""Opt-in actual sibling-container acceptance gates, NEVER a host fallback.

No Docker discovery is attempted unless TRADEGRAD_REQUIRE_PORTABLE_DOCKER=1.
CI must build both images using the same PORTABLE_ENVIRONMENT_DIGEST first.
These tests are intentionally pending locally when Docker is unavailable.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from textwrap import dedent, indent
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from packages.strategy_sdk import (
    DataRequirements,
    EngineProfile,
    PriceAssumptions,
    StateCodec,
    StateField,
    StateSchema,
    StrategyManifest,
    UniverseAssumptions,
    canonical_json,
    hash_json,
)
from runtimes.evaluator.portable_runner import (
    PortableDockerEvaluator,
    safe_policy_manifest,
)
from runtimes.evaluator.runner import IsolationUnavailable
from runtimes.portable_engine.runtime import profile_sha256
from tests.portable.diagnostics import assert_status, safe_diagnostic

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.getenv("TRADEGRAD_REQUIRE_PORTABLE_DOCKER") != "1",
    reason="Actual portable Docker/AKQuant gates explicitly deferred; no daemon probe or host policy execution",
)


def fixture_data(market, kind, *, count=24):
    """Known regular March3–April3 2025 sessions; no provider/network calls."""
    tz = ZoneInfo("Asia/Shanghai" if market == "CN" else "America/New_York")
    first = date(2025, 3, 3)
    days = [
        first + timedelta(days=i)
        for i in range(32)
        if (first + timedelta(days=i)).weekday() < 5
    ][:count]
    lot, currency, calendar, mic = (
        (100, "CNY", "XSHG", "XSHG") if market == "CN" else (1, "USD", "XNYS", "XNAS")
    )
    listings = []
    for n in range(2 if kind == "portfolio" else 1):
        identity = str(UUID(int=810 + n))
        listings.append(
            {
                "listing_id": identity,
                "instrument_id": identity,
                "symbol": f"TEST{n}",
                "mic": mic,
                "currency": currency,
                "calendar": calendar,
                "timezone": str(tz),
                "effective_from": "2020-01-01",
                "effective_to": None,
                "effective_to_known_at": None,
                "known_at": "2020-01-01T00:00:00Z",
                "lot_size": lot,
            }
        )
    sessions, bars = [], []
    for index, day in enumerate(days):
        following = day + timedelta(days=1)
        while following.weekday() > 4:
            following += timedelta(days=1)
        if market == "CN" and following == date(2025, 4, 4):
            following = date(2025, 4, 7)  # Qingming exchange closure
        close_time = datetime.combine(day, time(15 if market == "CN" else 16), tz)
        open_time = datetime.combine(day, time(9, 30), tz)
        next_time = datetime.combine(following, time(9, 30), tz)
        timestamp = close_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
        sessions.append(
            {
                "session_date": day.isoformat(),
                "open_time": open_time.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
                "close_time": timestamp,
                "next_open_time": next_time.astimezone(UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        for n, listing in enumerate(listings):
            close = (
                100.0 + index * 1.5 + n * 10
                if kind == "portfolio"
                else 100.0 + (index % 6) * 2
            )
            opened = close * 1.03
            bars.append(
                {
                    "listing_id": listing["listing_id"],
                    "session_date": day.isoformat(),
                    "completed_at": timestamp,
                    "available_at": timestamp,
                    "open": opened,
                    "high": opened + 5.0,
                    "low": close - 5.0,
                    "close": close,
                    "volume": 1_000_000.0,
                    "volume_unit": "shares",
                    "complete": True,
                }
            )
    return {
        "listings": listings,
        "bars": bars,
        "sessions": sessions,
        "corporate_actions": [],
    }


def stage(
    tmp_path,
    market="US",
    kind="event",
    *,
    data=None,
    source=None,
    suffix="",
    limit=False,
    warmup=None,
):
    data = data or fixture_data(market, kind)
    filename = "portfolio_momentum.py" if kind == "portfolio" else "event_trend.py"
    source = (
        source
        if source is not None
        else (ROOT / "packages/strategy_sdk/templates" / filename).read_text()
    )
    if limit:
        source = source.replace(
            'order_type="market",',
            'order_type="limit", limit_price=bars[-1].close * (1.2 if side == "buy" else 0.8),',
        )
    source += suffix
    parameters = (
        {"top_n": 1}
        if kind == "portfolio"
        else {"quantity": 100 if market == "CN" else 1}
    )
    fields = [StateField(name="last_session", kind="string")]
    if kind == "portfolio":
        fields.append(StateField(name="decisions", kind="integer"))
    schema = StateSchema(
        name="momentum" if kind == "portfolio" else "trend",
        version=1,
        fields=tuple(fields),
    )
    state = {"last_session": ""}
    if kind == "portfolio":
        state["decisions"] = 0
    lookback = 21 if kind == "portfolio" else 2
    warmup = (20 if kind == "portfolio" else 2) if warmup is None else warmup
    digest = os.environ.get("PORTABLE_ENVIRONMENT_DIGEST", "")
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        "Set the actual build-input PORTABLE_ENVIRONMENT_DIGEST for both prebuilt images"
    )
    manifest = StrategyManifest(
        strategy_kind=kind,
        strategy_id=str(UUID(int=820 if kind == "portfolio" else 821)),
        code_sha256=hashlib.sha256(source.encode()).hexdigest(),
        parameters_sha256=hash_json(parameters),
        market=market,
        currency="CNY" if market == "CN" else "USD",
        calendar="XSHG" if market == "CN" else "XNYS",
        timezone="Asia/Shanghai" if market == "CN" else "America/New_York",
        data=DataRequirements(
            lookback_bars=lookback,
            warmup_bars=lookback,
            max_listings=len(data["listings"]),
        ),
        universe=UniverseAssumptions(
            semantics="user_fixed",
            snapshot_sha256=hash_json(data["listings"]),
            survivorship_bias="possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256=hash_json([])),
        state_schema=schema,
        engine_profile=EngineProfile(
            profile_sha256=profile_sha256(market, 100000.0), environment_sha256=digest
        ),
    )
    market_bytes = canonical_json(data)
    request = {
        "schema_version": "portable-engine-request.v1",
        "manifest": manifest.model_dump(mode="json"),
        "window": {
            "start": data["sessions"][warmup]["session_date"],
            "end": (
                date.fromisoformat(data["sessions"][-1]["session_date"])
                + timedelta(days=1)
            ).isoformat(),
        },
        "initial_cash": 100000.0,
        "initial_state": StateCodec(schema).encode(state).model_dump(mode="json"),
        "input_identity": {
            "run_config_sha256": "f" * 64,
            "market_data_sha256": hashlib.sha256(market_bytes).hexdigest(),
        },
    }
    engine, policy = tmp_path / "engine", tmp_path / "policy"
    engine.mkdir(parents=True)
    policy.mkdir(parents=True)
    (engine / "request.json").write_bytes(canonical_json(request))
    (engine / "market.json").write_bytes(market_bytes)
    (policy / "policy.py").write_text(source)
    (policy / "manifest.json").write_bytes(
        canonical_json(safe_policy_manifest(manifest))
    )
    (policy / "parameters.json").write_bytes(canonical_json(parameters))
    return engine, policy, request


@pytest.fixture
def evaluator(tmp_path):
    runner = PortableDockerEvaluator(
        state_dir=tmp_path / "runner-state", timeout=60, decision_timeout=15
    )
    assert runner.available(), (
        "Actual paired portable images and Docker isolation are required, never substitute a host process"
    )
    return runner


def run(evaluator, tmp_path, market="US", kind="event", **kwargs):
    engine, policy, request = stage(tmp_path, market, kind, **kwargs)
    identity = str(uuid4())
    result = evaluator.run(
        identity,
        engine,
        policy,
        tmp_path / "output",
        run_id=str(uuid4()),
        attempt_id=str(uuid4()),
        expected_config_sha256="f" * 64,
    )
    assert all(
        evaluator._inspect_role(identity, role) is None for role in ("engine", "policy")
    ), safe_diagnostic(result)
    return result, request


@pytest.mark.parametrize("market", ["CN", "US"])
@pytest.mark.parametrize("kind", ["event", "portfolio"])
def test_actual_reference_policies_warmup_and_next_open(
    evaluator, tmp_path, market, kind
):
    result, request = run(evaluator, tmp_path / "case", market, kind)
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    assert_native_controller_inventory(bundle)
    assert (
        result["runtime_certified"] is False
    )  # one CI gate is not a broad certification
    assert bundle["fills"], "Reference fixture must actually exercise trusted fills"
    assert all(
        t["outcome"] == "warmup_state_only"
        for t in bundle["decision_trace"]
        if t["warmup"]
    )
    assert bundle["account_series"][0]["session_date"] >= request["window"]["start"]
    logical = {row["logical_order_id"]: row for row in bundle["logical_orders"]}
    for fill in bundle["fills"]:
        order = logical[fill["logical_order_id"]]
        assert fill["session_date"] > order["decision_session"]
        assert fill["native_at"] != fill["economic_reference_time"]
    assert (tmp_path / "case/output/portable-result.json").is_file()


@pytest.mark.parametrize("market", ["CN", "US"])
def test_actual_limit_matching_and_pending_orders(evaluator, tmp_path, market):
    result, _ = run(evaluator, tmp_path / "case", market, limit=True)
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    assert_native_controller_inventory(bundle)
    assert bundle["fills"], "Open-marketable limits must exercise real native fills"
    assert any(order["order_type"] == "limit" for order in bundle["logical_orders"])
    logical = {row["logical_order_id"]: row for row in bundle["logical_orders"]}
    for fill in bundle["fills"]:
        limit = logical[fill["logical_order_id"]]["limit_price"]
        assert (
            fill["price"] <= limit if fill["side"] == "buy" else fill["price"] >= limit
        )


@pytest.mark.parametrize("kind", ["portfolio", "event"])
def test_actual_missingness_cannot_shrink_portfolio_universe(evaluator, tmp_path, kind):
    data = fixture_data("US", "portfolio")
    data["bars"].pop(-1)
    result, _ = run(evaluator, tmp_path / "case", kind=kind, data=data)
    assert_status(result, "failed")
    assert result["bundle"] is None


@pytest.mark.parametrize("kind", ["portfolio", "event"])
def test_actual_symbol_order_and_future_suffix_invariance(evaluator, tmp_path, kind):
    first, _ = run(
        evaluator, tmp_path / "first", kind=kind, data=fixture_data("US", "portfolio")
    )
    assert first["status"] == "succeeded", first
    data = fixture_data("US", "portfolio")
    cutoff = data["sessions"][-2]["session_date"]
    data["bars"].reverse()
    data["listings"].reverse()
    for bar in data["bars"]:
        if bar["session_date"] > cutoff:
            for key in ("open", "high", "low", "close"):
                bar[key] *= 2
            bar["volume"] *= 2
    second, _ = run(evaluator, tmp_path / "second", kind=kind, data=data)
    assert second["status"] == "succeeded", second
    before = lambda result: [
        row
        for row in result["bundle"]["decision_trace"]
        if row["session_date"] <= cutoff
    ]
    assert before(first) == before(second)


PROBE = """
import importlib.util, os, pathlib, socket
assert set(os.listdir('/input')) == {'policy.py', 'parameters.json', 'manifest.json'}
assert not pathlib.Path('/input/market.json').exists()
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/opt/tradegrad/runtimes/portable_engine').exists()
assert importlib.util.find_spec('akquant') is None
assert not any(k in os.environ for k in ('DATABASE_URL','OPENAI_API_KEY','AWS_SECRET_ACCESS_KEY'))
try:
    pathlib.Path('/opt/tradegrad/probe').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
sock = socket.socket()
sock.settimeout(.2)
assert sock.connect_ex(('1.1.1.1', 443)) != 0
sock.close()
"""


def test_actual_policy_cannot_read_engine_ledger_dataset_secrets_or_network(
    evaluator, tmp_path
):
    result, _ = run(evaluator, tmp_path / "case", suffix=PROBE)
    assert_status(result, "succeeded")


@pytest.mark.parametrize("attack", ["forgery", "flood", "forks"])
def test_actual_hostile_policy_cannot_publish_and_both_siblings_die(
    evaluator, tmp_path, attack
):
    if attack == "forgery":
        suffix = "\nimport os\nfrom runtimes.portable_policy.protocol import frame\nos.write(1, frame({'kind':'result','result_trust':'isolated_policy_trusted_akquant_engine'}))\n"
    elif attack == "flood":
        suffix = "\nimport os\nos.write(1,b'x'*1048576)\n"
    else:
        suffix = "\nimport os,signal,time\npid=os.fork()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nwhile True: time.sleep(.1)\n"
        evaluator.timeout = 5
        evaluator.decision_timeout = 2
    result, _ = run(evaluator, tmp_path / "case", suffix=suffix)
    assert_status(result, "failed")
    assert result["bundle"] is None
    assert not list((tmp_path / "case/output").iterdir())


def test_actual_partial_create_unknown_response_stops_both(
    evaluator, tmp_path, monkeypatch
):
    real = evaluator._command

    def lose_engine_create_reply(*args, **kwargs):
        result = real(*args, **kwargs)
        if args[0] == "create" and any(
            part == "io.tradegrad.role=engine" for part in args
        ):
            raise IsolationUnavailable(
                "Injected lost create response after daemon execution"
            )
        return result

    monkeypatch.setattr(evaluator, "_command", lose_engine_create_reply)
    result, _ = run(evaluator, tmp_path / "case")
    assert_status(result, "failed")
    assert result["bundle"] is None


@pytest.mark.parametrize("cutoff_count", [12, 18])
def test_actual_prefix_replay_keeps_decisions_and_pre_cutoff_ledger(
    evaluator, tmp_path, cutoff_count
):
    complete, _ = run(evaluator, tmp_path / "complete")
    prefix_data = fixture_data("US", "event", count=cutoff_count)
    prefix, _ = run(evaluator, tmp_path / "prefix", data=prefix_data)
    assert complete["status"] == prefix["status"] == "succeeded", (complete, prefix)
    cutoff = prefix_data["sessions"][-1]["session_date"]
    assert prefix["bundle"]["decision_trace"] == [
        row
        for row in complete["bundle"]["decision_trace"]
        if row["session_date"] <= cutoff
    ]

    def normalized(result):
        return [
            {
                key: value
                for key, value in row.items()
                if key not in {"fill_id", "order_id"}
            }
            for row in result["bundle"]["fills"]
            if row["session_date"] <= cutoff
        ]

    assert normalized(prefix) == normalized(complete)


@pytest.mark.parametrize("market", ["CN", "US"])
def test_actual_no_touch_gtc_stays_unresolved_without_terminal_liquidation(
    evaluator, tmp_path, market
):
    source = (
        (ROOT / "packages/strategy_sdk/templates/event_trend.py")
        .read_text()
        .replace('order_type="market",', 'order_type="limit", limit_price=0.01,')
    )
    result, _ = run(evaluator, tmp_path / "case", market, source=source)
    assert_status(result, "succeeded")
    assert result["bundle"]["logical_orders"]
    assert result["bundle"]["fills"] == []
    assert any(not order["terminal"] for order in result["bundle"]["logical_orders"])
    assert result["bundle"]["engine"]["forced_final_liquidation"] is False


def test_actual_zero_volume_never_fabricates_fills(evaluator, tmp_path):
    data = fixture_data("US", "event")
    for bar in data["bars"]:
        bar["volume"] = 0.0
    result, _ = run(evaluator, tmp_path / "case", data=data)
    assert_status(result, "succeeded")
    assert result["bundle"]["fills"] == []


CANCEL_PENDING = """
from packages.strategy_sdk import CancelIntent
_original_event = Policy.on_event
def _cancel_pending(self, observation):
    if observation.warmup or not observation.open_orders:
        return _original_event(self, observation)
    intents = tuple(CancelIntent(signal_id=stable_signal_id(self.strategy_id, observation, 'cancel:' + order.order_id),
        order_id=order.order_id, listing_id=order.listing_id, effective_at=observation.next_open_time,
        expires_at=None) for order in observation.open_orders)
    return EventReply(decision=EventDecision(intents=intents), next_state=self.codec.encode({'last_session': observation.session_date.isoformat()}))
Policy.on_event = _cancel_pending
"""


def test_actual_explicit_cancel_clears_no_touch_limit(evaluator, tmp_path):
    source = (
        (ROOT / "packages/strategy_sdk/templates/event_trend.py")
        .read_text()
        .replace('order_type="market",', 'order_type="limit", limit_price=0.01,')
    )
    result, _ = run(evaluator, tmp_path / "case", source=source, suffix=CANCEL_PENDING)
    assert_status(result, "succeeded")
    assert result["bundle"]["fills"] == []
    assert any(order["status"] == "cancelled" for order in result["bundle"]["orders"])


@pytest.mark.parametrize("market,quantity", [("CN", 1), ("US", 1_000_000)])
def test_actual_invalid_lot_or_cash_request_rejected_without_resizing(
    evaluator, tmp_path, market, quantity
):
    suffix = f"\n_original_init=Policy.__init__\ndef _init(self,strategy_id,parameters):\n    _original_init(self,strategy_id,parameters)\n    self.quantity={quantity}\nPolicy.__init__=_init\n"
    result, _ = run(evaluator, tmp_path / "case", market, suffix=suffix)
    assert_status(result, "failed")
    assert result["bundle"] is None


def test_actual_cn_partial_fills_and_t_plus_one(evaluator, tmp_path):
    data = fixture_data("CN", "event")
    for bar in data["bars"]:
        bar["volume"] = 400.0
    suffix = "\n_original_init=Policy.__init__\ndef _init(self,strategy_id,parameters):\n    _original_init(self,strategy_id,parameters)\n    self.quantity=500\nPolicy.__init__=_init\n"
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    assert_status(result, "succeeded")
    fills = result["bundle"]["fills"]
    logical = {
        row["logical_order_id"]: row for row in result["bundle"]["logical_orders"]
    }
    assert any(
        fill["quantity"] < logical[fill["logical_order_id"]]["quantity"]
        for fill in fills
    )
    for fill in fills:
        assert fill["quantity"] % 100 == 0
        if fill["side"] == "sell":
            preceding_buys = [
                buy
                for buy in fills
                if buy["side"] == "buy"
                and buy["native_timestamp_ns"] < fill["native_timestamp_ns"]
            ]
            assert preceding_buys
            assert all(
                buy["session_date"] < fill["session_date"] for buy in preceding_buys
            )


@pytest.mark.parametrize(
    "kind,sell_order_type",
    [("portfolio", "market"), ("event", "market"), ("event", "limit")],
)
def test_actual_cn_immediate_reversal_requires_earliest_next_open_sale(
    evaluator, tmp_path, kind, sell_order_type
):
    """Blocking acceptance gate, not xfail: no T+2 or rejection passes as T+1.

    The explicit controller-T+1 profile uses native T+0 admission, but can sell
    only shares already held at the preceding complete daily close. It must
    admit this earliest eligible NextOpen sale without native settlement delay.
    The fixture is source text only on host; Policy runs exclusively inside the
    paired restricted policy container. Local collection does not prove this.
    """
    data = fixture_data("CN", kind)
    first_active = 20 if kind == "portfolio" else 2
    buy_decision, buy_fill, sell_fill = data["sessions"][
        first_active : first_active + 3
    ]
    buy_date, reverse_date = buy_decision["session_date"], buy_fill["session_date"]
    if kind == "portfolio":
        suffix = f"""
def _earliest_exit(self, observation):
    state = self.codec.decode(observation.state)
    current = observation.session_date.isoformat()
    state = {{"last_session": current, "decisions": state["decisions"] + 1}}
    action, targets = "hold", ()
    if observation.warmup:
        action = "no_decision"
    elif current == {buy_date!r}:
        action = "rebalance"
        targets = (Target(listing_id=observation.listings[0].listing_id, value=100.0),)
    elif current == {reverse_date!r}:
        action = "liquidate"
    return PortfolioReply(decision=TargetPortfolio(
        signal_id=stable_signal_id(self.strategy_id, observation, "earliest-exit"),
        effective_at=observation.next_open_time, expires_at=None,
        action=action, basis="target_quantity", targets=targets,
        omitted_positions="liquidate" if action == "liquidate" else "preserve"
    ), next_state=self.codec.encode(state))
Policy.decide = _earliest_exit
"""
    else:
        suffix = f"""
def _earliest_exit(self, observation):
    current = observation.session_date.isoformat()
    intents = ()
    if not observation.warmup and current in ({buy_date!r}, {reverse_date!r}):
        side = "buy" if current == {buy_date!r} else "sell"
        order_type = {sell_order_type!r} if side == "sell" else "market"
        intents = (OrderIntent(
            signal_id=stable_signal_id(self.strategy_id, observation, "earliest-exit"),
            listing_id=observation.listings[0].listing_id, side=side, quantity=100.0,
            order_type=order_type, limit_price=1.0 if order_type == "limit" else None,
            time_in_force="GTC", reduce_only=side == "sell",
            position_effect="close" if side == "sell" else "open",
            effective_at=observation.next_open_time, expires_at=None
        ),)
    return EventReply(decision=EventDecision(intents=intents),
        next_state=self.codec.encode({{"last_session": current}}))
Policy.on_event = _earliest_exit
"""
    result, _ = run(evaluator, tmp_path / "case", "CN", kind, data=data, suffix=suffix)
    assert result["status"] == "succeeded", (
        (
            "Controller CN T+1 NextOpen acceptance failed: earliest eligible sale "
            "must execute, never be rejected, clipped, or silently delayed to T+2"
        ),
        result,
    )
    bundle = result["bundle"]
    assert_native_controller_inventory(bundle)
    buys = [fill for fill in bundle["fills"] if fill["side"] == "buy"]
    sells = [fill for fill in bundle["fills"] if fill["side"] == "sell"]
    assert len(buys) == len(sells) == 1
    assert buys[0]["quantity"] == sells[0]["quantity"] == 100.0
    assert buys[0]["session_date"] == buy_fill["session_date"]
    assert buys[0]["economic_reference_time"] == buy_fill["open_time"]
    assert sells[0]["decision_session"] == buy_fill["session_date"]
    assert sells[0]["session_date"] == sell_fill["session_date"]
    assert sells[0]["economic_reference_time"] == sell_fill["open_time"]
    assert sells[0]["session_date"] > buys[0]["session_date"]
    assert int(sells[0]["native_timestamp_ns"]) > int(buys[0]["native_timestamp_ns"])
    assert bundle["runtime_certified"] is False


CONTROLLER_EVENT_HELPERS = """
from packages.strategy_sdk import CancelIntent
def _order(self, observation, label, side, quantity, limit=None, effective_at=None):
    return OrderIntent(
        signal_id=stable_signal_id(self.strategy_id, observation, label),
        listing_id=observation.listings[0].listing_id,
        side=side, quantity=float(quantity),
        order_type="market" if limit is None else "limit", limit_price=limit,
        time_in_force="GTC", reduce_only=side == "sell",
        position_effect="close" if side == "sell" else "open",
        effective_at=observation.next_open_time if effective_at is None else effective_at,
        expires_at=None,
    )
def _cancel(self, observation, order):
    return CancelIntent(
        signal_id=stable_signal_id(self.strategy_id, observation, "cancel:" + order.order_id),
        listing_id=order.listing_id, order_id=order.order_id,
        effective_at=observation.next_open_time, expires_at=None,
    )
def _reply(self, observation, intents=()):
    return EventReply(decision=EventDecision(intents=tuple(intents)),
        next_state=self.codec.encode({"last_session": observation.session_date.isoformat()}))
def _inventory(observation, held, available, reserved):
    key = observation.listings[0].listing_id
    positions = {p.listing_id: p for p in observation.account.positions}
    position = positions.get(key)
    assert (0 if position is None else position.quantity) == held
    assert (0 if position is None else position.available_quantity) == available
    assert position is None or position.available_quantity_semantics == "next_open_unreserved_holdings"
    assert sum(o.remaining_quantity for o in observation.open_orders
        if o.listing_id == key and o.side == "sell") == reserved
    assert available == held - reserved
"""


def controller_event_suffix(body):
    """Generate inert source only; never import or execute the fixture on host."""
    return (
        CONTROLLER_EVENT_HELPERS
        + "\ndef _controller_event(self, observation):\n"
        + indent(dedent(body).strip(), "    ")
        + "\nPolicy.on_event = _controller_event\n"
    )


def assert_native_controller_inventory(bundle):
    """Prove sales use prior-close shares, independent of native fill ordering.

    This is an assertion over exported native executions, not a substitute
    ledger or an engine/policy simulation. Same-session buys cannot fund sales.
    """
    assert bundle["ledger_origin"] == "trusted_engine"
    profile = bundle["engine"]["profile"]
    assert profile["profile_version"] == (
        "akquant.portable.cash-daily.controller-t1.v3"
    )
    assert profile["t_plus_one"] is False
    assert profile["engine_settlement_mode"] == "T+0"
    assert profile["application_settlement_enforcement"] == (
        "trusted_controller_daily_next_open_inventory.v1"
    )
    assert profile["application_t_plus_one"] is (bundle["manifest"]["market"] == "CN")
    assert profile["sellability_observation"] == "next_open_unreserved_holdings"
    assert (
        profile["limit_execution"] == "reject_non_open_or_limit_breaching_native_fill"
    )
    assert bundle["manifest"]["engine_profile"]["profile_sha256"] == hash_json(profile)
    assert all(order["time_in_force"] == "GTC" for order in bundle["orders"])
    assert all(order["status"] != "rejected" for order in bundle["orders"])
    held = {}
    for day in sorted({fill["session_date"] for fill in bundle["fills"]}):
        current = [fill for fill in bundle["fills"] if fill["session_date"] == day]
        for key in {fill["listing_id"] for fill in current}:
            buys = sum(
                fill["quantity"]
                for fill in current
                if fill["listing_id"] == key and fill["side"] == "buy"
            )
            sells = sum(
                fill["quantity"]
                for fill in current
                if fill["listing_id"] == key and fill["side"] == "sell"
            )
            assert sells <= held.get(key, 0.0), (
                "Native T+0 must not permit sales funded by same-session buys",
                day,
                key,
            )
            held[key] = held.get(key, 0.0) + buys - sells
        assert all(fill["decision_session"] < day for fill in current)
    assert all(position["short_quantity"] == 0 for position in bundle["positions"])
    return held


def assert_controller_rejected(result, output):
    assert_status(result, "failed")
    assert result["bundle"] is None
    assert result["metrics"] is None
    assert not list(output.iterdir()), "Rejected intents must not publish a result"


@pytest.mark.parametrize("same_session", [False, True])
def test_actual_cn_controller_refuses_same_session_effective_time(
    evaluator, tmp_path, same_session
):
    """The valid control must exit next open; changing only timing must fail."""
    data = fixture_data("CN", "event", count=8)
    buy_day, held_day, exit_day = [s["session_date"] for s in data["sessions"][2:5]]
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        if current == {buy_day!r}:
            return _reply(self, observation, (_order(self, observation, "seed", "buy", 100),))
        if current == {held_day!r}:
            _inventory(observation, 100, 100, 0)
            when = observation.decision_time if {same_session!r} else observation.next_open_time
            return _reply(self, observation, (_order(self, observation, "exit", "sell", 100, effective_at=when),))
        return _reply(self, observation)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if same_session:
        assert_controller_rejected(result, tmp_path / "case/output")
    else:
        assert_status(result, "succeeded")
        bundle = result["bundle"]
        assert_native_controller_inventory(bundle)
        assert [
            (f["side"], f["quantity"], f["session_date"]) for f in bundle["fills"]
        ] == [("buy", 100.0, held_day), ("sell", 100.0, exit_day)]


@pytest.mark.parametrize("sell_first", [False, True])
@pytest.mark.parametrize("sell_quantity", [100, 200])
def test_actual_cn_simultaneous_buy_sell_uses_only_preexisting_holdings(
    evaluator, tmp_path, sell_first, sell_quantity
):
    """A concurrent buy neither supplies nor expands either ordering's sell budget."""
    data = fixture_data("CN", "event", count=8)
    buy_day, held_day, simultaneous_day = [
        s["session_date"] for s in data["sessions"][2:5]
    ]
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        if current == {buy_day!r}:
            return _reply(self, observation, (_order(self, observation, "seed", "buy", 100),))
        if current == {held_day!r}:
            _inventory(observation, 100, 100, 0)
            buy = _order(self, observation, "add", "buy", 100)
            sell = _order(self, observation, "sell", "sell", {sell_quantity})
            return _reply(self, observation, (sell, buy) if {sell_first!r} else (buy, sell))
        if current == {simultaneous_day!r}:
            _inventory(observation, 100, 100, 0)
            assert not observation.open_orders
        return _reply(self, observation)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if sell_quantity > 100:
        assert_controller_rejected(result, tmp_path / "case/output")
    else:
        assert_status(result, "succeeded")
        bundle = result["bundle"]
        assert set(assert_native_controller_inventory(bundle).values()) == {100.0}
        assert len(bundle["fills"]) == 3
        assert sorted(
            (f["side"], f["quantity"])
            for f in bundle["fills"]
            if f["session_date"] == simultaneous_day
        ) == [("buy", 100.0), ("sell", 100.0)]


@pytest.mark.parametrize("pending_buy", [False, True])
@pytest.mark.parametrize("attempt_sale", [False, True])
def test_actual_cn_buy_intents_and_pending_buys_never_credit_sell_inventory(
    evaluator, tmp_path, pending_buy, attempt_sale
):
    """Both same-reply and older no-touch buys start with zero saleable shares."""
    data = fixture_data("CN", "event", count=8)
    buy_day, following_day = [s["session_date"] for s in data["sessions"][2:4]]
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        intents = []
        if current == {buy_day!r}:
            _inventory(observation, 0, 0, 0)
            intents.append(_order(self, observation, "buy", "buy", 100, limit=0.01 if {pending_buy!r} else None))
            if {attempt_sale!r} and not {pending_buy!r}:
                intents.append(_order(self, observation, "unfunded-sale", "sell", 100))
        if current == {following_day!r} and {pending_buy!r}:
            _inventory(observation, 0, 0, 0)
            assert len(observation.open_orders) == 1
            assert observation.open_orders[0].side == "buy"
            assert observation.open_orders[0].remaining_quantity == 100
            if {attempt_sale!r}:
                intents.append(_order(self, observation, "pending-buy-sale", "sell", 100))
        return _reply(self, observation, intents)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if attempt_sale:
        assert_controller_rejected(result, tmp_path / "case/output")
    else:
        assert_status(result, "succeeded")
        bundle = result["bundle"]
        assert_native_controller_inventory(bundle)
        assert len(bundle["logical_orders"]) == 1
        if pending_buy:
            assert bundle["fills"] == []
            assert bundle["logical_orders"][0]["terminal"] is False
        else:
            assert [(f["side"], f["quantity"]) for f in bundle["fills"]] == [
                ("buy", 100.0)
            ]


@pytest.mark.parametrize("replacement_quantity", [100, 200])
@pytest.mark.parametrize("cancel_pending", [False, True])
def test_actual_cn_no_touch_gtc_sell_reservation_survives_cancel_reply(
    evaluator, tmp_path, replacement_quantity, cancel_pending
):
    """300 held - 200 pending = 100 available, including a same-reply cancel."""
    data = fixture_data("CN", "event", count=9)
    buy_day, reserve_day, replace_day, release_day, exit_day = [
        s["session_date"] for s in data["sessions"][2:7]
    ]
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        if current == {buy_day!r}:
            return _reply(self, observation, (_order(self, observation, "seed", "buy", 300),))
        if current == {reserve_day!r}:
            _inventory(observation, 300, 300, 0)
            return _reply(self, observation, (_order(self, observation, "no-touch", "sell", 200, limit=1000000.0),))
        if current == {replace_day!r}:
            _inventory(observation, 300, 100, 200)
            assert len(observation.open_orders) == 1
            pending = observation.open_orders[0]
            assert pending.order_type == "limit" and pending.limit_price == 1000000.0
            intents = [_cancel(self, observation, pending)] if {cancel_pending!r} else []
            intents.append(_order(self, observation, "replacement", "sell", {replacement_quantity}))
            return _reply(self, observation, intents)
        if current == {release_day!r}:
            _inventory(observation, 200, 200 if {cancel_pending!r} else 0, 0 if {cancel_pending!r} else 200)
            if {cancel_pending!r}:
                assert not observation.open_orders
                assert any(event.status == "cancelled" for event in observation.order_events)
                return _reply(self, observation, (_order(self, observation, "released", "sell", 200),))
        return _reply(self, observation)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if replacement_quantity > 100:
        assert_controller_rejected(result, tmp_path / "case/output")
        return
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    final = assert_native_controller_inventory(bundle)
    no_touch = next(
        o for o in bundle["logical_orders"] if o["limit_price"] == 1000000.0
    )
    assert no_touch["filled_quantity"] == 0
    assert no_touch["terminal"] is cancel_pending
    assert all(
        f["logical_order_id"] != no_touch["logical_order_id"] for f in bundle["fills"]
    )
    expected_sales = [(100.0, release_day)] + (
        [(200.0, exit_day)] if cancel_pending else []
    )
    assert [
        (f["quantity"], f["session_date"])
        for f in bundle["fills"]
        if f["side"] == "sell"
    ] == expected_sales
    assert set(final.values()) == ({0.0} if cancel_pending else {200.0})
    assert bundle["engine"]["forced_final_liquidation"] is False


@pytest.mark.parametrize("oversell_after_partial", [False, True])
def test_actual_cn_partial_sell_reduces_holdings_and_reservation_equally(
    evaluator, tmp_path, oversell_after_partial
):
    data = fixture_data("CN", "event", count=10)
    buy_day, reserve_day, first_partial, second_partial, release_day, exit_day = [
        s["session_date"] for s in data["sessions"][2:8]
    ]
    for bar in data["bars"]:
        if bar["session_date"] in {first_partial, second_partial}:
            bar["volume"] = 400.0  # reviewed 25% limit = one 100-share CN lot
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        if current == {buy_day!r}:
            return _reply(self, observation, (_order(self, observation, "seed", "buy", 500),))
        if current == {reserve_day!r}:
            _inventory(observation, 500, 500, 0)
            return _reply(self, observation, (_order(self, observation, "partial", "sell", 500, limit=1.0),))
        if current == {first_partial!r}:
            _inventory(observation, 400, 0, 400)
            assert any(event.status == "partial_fill" and event.filled_quantity == 100 for event in observation.order_events)
            if {oversell_after_partial!r}:
                return _reply(self, observation, (_order(self, observation, "oversell", "sell", 100),))
        if current == {second_partial!r}:
            _inventory(observation, 300, 0, 300)
            assert any(event.status == "partial_fill" and event.filled_quantity == 200 for event in observation.order_events)
            return _reply(self, observation, (_cancel(self, observation, observation.open_orders[0]),))
        if current == {release_day!r}:
            _inventory(observation, 300, 300, 0)
            assert not observation.open_orders
            assert any(event.status == "cancelled" and event.filled_quantity == 200 for event in observation.order_events)
            return _reply(self, observation, (_order(self, observation, "released", "sell", 300),))
        return _reply(self, observation)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if oversell_after_partial:
        assert_controller_rejected(result, tmp_path / "case/output")
        return
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    assert set(assert_native_controller_inventory(bundle).values()) == {0.0}
    sells = [f for f in bundle["fills"] if f["side"] == "sell"]
    assert [(f["quantity"], f["session_date"]) for f in sells] == [
        (100.0, first_partial),
        (100.0, second_partial),
        (300.0, exit_day),
    ]
    partial = next(
        o
        for o in bundle["logical_orders"]
        if o["side"] == "sell" and o["quantity"] == 500
    )
    assert partial["terminal"] is True
    assert partial["filled_quantity"] == 200.0
    assert any(
        o["logical_order_id"] == partial["logical_order_id"]
        and o["status"] == "cancelled"
        for o in bundle["orders"]
    )


@pytest.mark.parametrize("kind", ["portfolio", "event"])
def test_actual_cn_multi_symbol_barrier_sees_complete_native_close_ledger(
    evaluator, tmp_path, kind
):
    """Both modes must see both fills and every close mark before deciding once."""
    data = fixture_data("CN", "portfolio")
    first_active = 20 if kind == "portfolio" else 2
    buy_day, held_day, exit_day = [
        s["session_date"] for s in data["sessions"][first_active : first_active + 3]
    ]
    data["listings"].reverse()
    data["bars"].reverse()
    suffix = f"""
def _complete_close(self, observation):
    current = observation.session_date.isoformat()
    state = self.codec.decode(observation.state)
    assert state["last_session"] != current, "decision repeated within one daily barrier"
    assert len(observation.listings) == 2
    current_bars = {{bar.listing_id: bar for bar in observation.history
        if bar.session_date == observation.session_date}}
    assert set(current_bars) == {{listing.listing_id for listing in observation.listings}}
    marked = sum(position.quantity * current_bars[position.listing_id].close
        for position in observation.account.positions)
    assert abs(observation.account.cash + marked - observation.account.equity) < 0.000001
    if current == {held_day!r}:
        positions = {{position.listing_id: position for position in observation.account.positions}}
        assert set(positions) == set(current_bars)
        assert all(position.quantity == position.available_quantity == 100 for position in positions.values())
        assert not observation.open_orders
        assert {{event.listing_id for event in observation.order_events
            if event.status == "filled" and event.filled_quantity == 100}} == set(current_bars)
    if current == {exit_day!r}:
        assert not observation.account.positions
        assert not observation.open_orders
    return current, state
"""
    if kind == "portfolio":
        suffix += f"""
def _complete_portfolio(self, observation):
    current, state = _complete_close(self, observation)
    action, targets = "hold", ()
    if observation.warmup:
        action = "no_decision"
    elif current == {buy_day!r}:
        action = "rebalance"
        targets = tuple(Target(listing_id=listing.listing_id, value=100.0)
            for listing in observation.listings)
    elif current == {held_day!r}:
        action = "liquidate"
    return PortfolioReply(decision=TargetPortfolio(
        signal_id=stable_signal_id(self.strategy_id, observation, "complete-cross-section"),
        effective_at=observation.next_open_time, expires_at=None,
        action=action, basis="target_quantity", targets=targets,
        omitted_positions="liquidate" if action == "liquidate" else "preserve"
    ), next_state=self.codec.encode({{"last_session": current, "decisions": state["decisions"] + 1}}))
Policy.decide = _complete_portfolio
"""
    else:
        suffix += f"""
def _complete_event(self, observation):
    current, state = _complete_close(self, observation)
    intents = ()
    if not observation.warmup and current in ({buy_day!r}, {held_day!r}):
        side = "buy" if current == {buy_day!r} else "sell"
        intents = tuple(OrderIntent(
            signal_id=stable_signal_id(self.strategy_id, observation, "complete:" + listing.listing_id),
            listing_id=listing.listing_id, side=side, quantity=100.0,
            order_type="market", time_in_force="GTC", reduce_only=side == "sell",
            position_effect="close" if side == "sell" else "open",
            effective_at=observation.next_open_time, expires_at=None
        ) for listing in observation.listings)
    return EventReply(decision=EventDecision(intents=intents),
        next_state=self.codec.encode({{"last_session": current}}))
Policy.on_event = _complete_event
"""
    result, _ = run(evaluator, tmp_path / "case", "CN", kind, data=data, suffix=suffix)
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    assert set(assert_native_controller_inventory(bundle).values()) == {0.0}
    assert len(bundle["decision_trace"]) == len(data["sessions"])
    assert [row["session_date"] for row in bundle["decision_trace"]] == [
        session["session_date"] for session in data["sessions"]
    ]
    assert len(bundle["fills"]) == 4
    for listing in data["listings"]:
        assert [
            (f["side"], f["quantity"], f["session_date"])
            for f in bundle["fills"]
            if f["listing_id"] == listing["listing_id"]
        ] == [("buy", 100.0, held_day), ("sell", 100.0, exit_day)]


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(
    "matching", ["marketable_open", "intrabar_only", "slippage_breach"]
)
def test_actual_next_open_limits_reject_intrabar_or_limit_breaching_fills(
    evaluator, tmp_path, side, matching
):
    """Do not relabel the native OHLC touch/slippage matcher as next-open fills.

    Pinned AKQuant 0.3.27 applies slippage after testing the raw limit, so a
    limit exactly at the open also breaches. Both unsupported outcomes must
    discard the entire native run, never rewrite a fill or emit success.
    """
    data = fixture_data("CN", "event", count=8)
    for bar in data["bars"]:
        bar.update(open=100.0, high=110.0, low=90.0, close=100.0)
    buy_day, held_day, exit_day = [s["session_date"] for s in data["sessions"][2:5]]
    limit = {
        "marketable_open": 105.0 if side == "buy" else 95.0,
        "intrabar_only": 95.0 if side == "buy" else 105.0,
        "slippage_breach": 100.0,
    }[matching]
    suffix = controller_event_suffix(f"""
        current = observation.session_date.isoformat()
        if observation.warmup:
            return _reply(self, observation)
        if current == {buy_day!r}:
            limit = {limit!r} if {side!r} == "buy" else None
            return _reply(self, observation, (_order(self, observation, "buy", "buy", 100, limit=limit),))
        if current == {held_day!r} and {side!r} == "sell":
            _inventory(observation, 100, 100, 0)
            return _reply(self, observation, (_order(self, observation, "sell", "sell", 100, limit={limit!r}),))
        return _reply(self, observation)
    """)
    result, _ = run(evaluator, tmp_path / "case", "CN", data=data, suffix=suffix)
    if matching != "marketable_open":
        assert_controller_rejected(result, tmp_path / "case/output")
        return
    assert_status(result, "succeeded")
    bundle = result["bundle"]
    assert_native_controller_inventory(bundle)
    fills = [fill for fill in bundle["fills"] if fill["side"] == side]
    assert len(fills) == 1
    assert fills[0]["quantity"] == 100.0
    assert fills[0]["session_date"] == (held_day if side == "buy" else exit_day)
    assert fills[0]["price"] == pytest.approx(100.05 if side == "buy" else 99.95)
    assert fills[0]["price"] <= limit if side == "buy" else fills[0]["price"] >= limit
