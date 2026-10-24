"""Real controller observations plus opt-in pinned AKQuant parity.

Pure tests use existing inert broker fixtures, never candidate code. The optional
subprocess executes only the maintained test policy with actual AKQuant. It is
not Docker/policy-isolation, broker, provider, PostgreSQL or browser evidence.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.research_trace.contracts import CaptureConfig, DateRange
from packages.research_trace.recorder import Recorder
from packages.strategy_sdk import DataRequirements, canonical_json
from runtimes.portable_engine.runtime import Controller, load_inputs

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "portable_controller_fixture", ROOT / "tests/portable/test_engine.py"
)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


def recorder(**changes):
    return Recorder(
        code_sha256="a" * 64, config=CaptureConfig(mode="research", **changes)
    )


def stage(record, stage_id):
    return next(item for item in record.stages if item.stage_id == stage_id)


def run_controller(capture=None, *, kind="portfolio", count=2, prewarm=False):
    inputs = fixture.inputs(kind=kind, listing_count=count, prewarm=prewarm)
    observations = []

    def exchange(observation):
        observations.append(observation)
        if kind == "event":
            intents = () if observation.warmup else (fixture.order(observation),)
            return fixture.event_reply(observation, intents)
        reply = fixture.portfolio_reply(
            observation,
            "rebalance",
            (fixture.Target(listing_id=fixture.L1, value=10),),
        )
        reply["decision"]["scores"] = [{"listing_id": fixture.L1, "value": 7.5}]
        return reply

    controller = Controller(inputs, exchange, research_recorder=capture)
    broker = fixture.FakeBroker(inputs.sessions[0], legs=2)
    for session in inputs.sessions:
        broker.session = session
        if session.session_date.isoformat() < inputs.window["start"]:
            controller.on_session(session)
        else:
            controller.on_session(session, broker, fixture.FakeAQ)
    return controller, broker, observations


def test_capture_config_is_optional_strict_and_listing_abi_remains_100():
    request, data = fixture.fixture()
    assert (
        load_inputs(
            canonical_json(request),
            canonical_json(data),
            calendar_verifier=lambda *_: None,
        ).capture_config
        is None
    )
    request["research_capture"] = {"mode": "research", "instrument_ids": [fixture.L2]}
    loaded = load_inputs(
        canonical_json(request), canonical_json(data), calendar_verifier=lambda *_: None
    )
    assert loaded.capture_config.mode == "research"
    assert loaded.capture_config.instrument_ids == [fixture.L2]
    for invalid in ({"mode": "invented"}, {"surprise": True}, None):
        request["research_capture"] = invalid
        with pytest.raises(ValueError):
            load_inputs(
                canonical_json(request),
                canonical_json(data),
                calendar_verifier=lambda *_: None,
            )
    with pytest.raises(ValueError):
        DataRequirements(lookback_bars=1, warmup_bars=1, max_listings=101)


def test_snapshot_is_copied_before_exchange_and_state_mutation():
    capture = recorder()
    inputs = fixture.inputs()
    broker = fixture.FakeBroker(
        inputs.sessions[0], cash=1234.0, equity=1334.0, positions={fixture.L1: 10.0}
    )

    def exchange(observation):
        # Deliberate fake-broker mutation proves capture preceded the exchange.
        broker.cash, broker.equity = 1111.0, 1111.0
        broker.positions.clear()
        return fixture.portfolio_reply(observation)

    controller = Controller(inputs, exchange, research_recorder=capture)
    controller.on_session(inputs.sessions[0], broker, fixture.FakeAQ)
    before = capture.records[0].pre_state
    assert before.cash == 1234.0 and before.equity == 1334.0
    assert before.positions[0]["quantity"] == 10.0
    assert before.open_orders == [] and before.coverage == "complete"
    assert json.loads(before.strategy_state["payload_json"]) == {"counter": 0}
    assert json.loads(controller.state.payload_json) == {"counter": 1}
    controller.state = fixture.StateCodec(fixture.SCHEMA).encode({"counter": 999})
    assert json.loads(before.strategy_state["payload_json"]) == {"counter": 0}


def test_scores_and_reply_are_received_values_never_inferred_predicates_or_ranks():
    capture = recorder()
    controller, _, observations = run_controller(capture)
    first = capture.records[0]
    assert first.scope == "portfolio" and first.instrument_id is None
    assert stage(first, "portable.policy_internals").coverage == "unsupported"
    reply = stage(first, "portable.policy_reply")
    candidates = {row.instrument_id: row for row in reply.candidates}
    assert set(candidates) == {fixture.L1, fixture.L2}
    assert candidates[fixture.L1].score == 7.5
    assert candidates[fixture.L2].score is None
    assert candidates[fixture.L2].score_validity == "not_collected"
    assert all(row.rank is None and row.selected is None for row in candidates.values())
    assert all(row.eligibility == "unknown" for row in candidates.values())
    assert all(
        not item.predicates for record in capture.records for item in record.stages
    )
    assert reply.raw_targets == [
        {"listing_id": fixture.L1, "value": 10.0, "basis": "target_quantity"}
    ]
    received = json.loads(
        next(f.value for f in reply.features if f.feature_id == "portable.reply.json")
    )
    assert received["decision"] == controller.trace[0]["decision"]
    assert (
        received["next_state"]["payload_sha256"]
        == controller.trace[0]["state_after_sha256"]
    )
    assert first.pre_state.strategy_state == observations[0].state.model_dump(
        mode="json"
    )


def test_open_order_snapshot_has_actual_child_ids_and_survives_later_mutation():
    capture = recorder()
    controller, _, _ = run_controller(capture)
    pending_snapshot = capture.records[1].pre_state.open_orders
    logical = controller.logical_orders[0]
    assert pending_snapshot[0]["order_id"] == logical["logical_order_id"]
    assert pending_snapshot[0]["native_order_ids"] == ["native:1:0", "native:1:1"]
    logical["native_order_ids"].clear()
    logical["remaining_quantity"] = 0
    assert pending_snapshot[0]["remaining_quantity"] == 10.0
    assert pending_snapshot[0]["native_order_ids"] == ["native:1:0", "native:1:1"]


def test_warmup_has_real_state_but_does_not_invent_native_account():
    capture = recorder()
    controller, _, _ = run_controller(capture, prewarm=True)
    before = capture.records[0].pre_state
    assert before.cash is None and before.equity is None
    assert before.positions is None and before.open_orders is None
    assert (
        before.coverage == "partial"
        and "before native broker startup" in before.missing_reason
    )
    assert json.loads(before.strategy_state["payload_json"]) == {"counter": 0}
    assert controller.trace[0]["outcome"] == "warmup_state_only"
    assert capture.records[1].pre_state.coverage == "complete"


def test_native_links_are_separate_later_facts_with_actual_fill_ids_and_times():
    capture = recorder()
    controller, broker, _ = run_controller(capture)
    first_before = capture.records[0].model_dump(mode="json")
    fills = []
    for index, native in enumerate(broker.orders.values()):
        native.status = "Filled"
        native.filled_quantity = native.quantity
        native.updated_at = fixture.to_ns(controller.inputs.sessions[1].close_time)
        fills.append(
            SimpleNamespace(
                id=f"actual-fill:{index}",
                order_id=native.id,
                symbol=native.symbol,
                side=native.side,
                quantity=native.quantity,
                price=10.0,
                commission=0.0,
                timestamp=native.updated_at,
            )
        )
    controller.current_session = controller.inputs.sessions[1]
    controller._refresh(broker)
    controller.current_session = controller.inputs.sessions[-1]
    result = fixture.export_result(
        fixture.fake_result(controller, broker, fills), controller
    )
    controller.research.finalize(controller, result)
    assert capture.records[0].model_dump(mode="json") == first_before
    later = capture.records[-1]
    assert later.callback_phase == "portable.execution_result"
    assert later.data_event_id == capture.records[0].data_event_id
    assert later.event_seq == capture.records[0].event_seq
    assert (
        later.pre_state is None
        and "not a decision callback" in later.pre_state_missing_reason
    )
    assert later.decision_time == controller.inputs.sessions[-1].close_time
    link = stage(later, "portable.native_execution").order_links[0]
    assert link.logical_order_id == controller.logical_orders[0]["logical_order_id"]
    assert link.child_order_ids == ["native:1:0", "native:1:1"]
    assert link.fill_ids == ["actual-fill:0", "actual-fill:1"]
    assert link.fill_times == [controller.inputs.sessions[1].close_time] * 2
    assert link.submitted_at == controller.inputs.sessions[0].close_time
    assert link.status == "filled"
    initial_link = stage(capture.records[0], "portable.controller_plan").order_links[0]
    assert initial_link.fill_ids == [] and initial_link.submitted_at is None
    assert initial_link.status == "unknown"


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("Rejected", "native_cash_insufficient", "rejected"),
        ("Rejected", "", "unknown"),
        ("Cancelled", "", "cancelled"),
        ("Accepted", "", "unfilled"),
    ],
)
def test_native_terminal_status_and_rejection_reason_are_never_fabricated(
    status, reason, expected
):
    capture = recorder()
    controller, broker, _ = run_controller(capture)
    for native in broker.orders.values():
        native.status, native.reject_reason = status, reason
    controller._refresh(broker)
    bundle = fixture.export_result(fixture.fake_result(controller, broker), controller)
    controller.research.finalize(controller, bundle)
    observed = stage(capture.records[-1], "portable.native_execution")
    link = observed.order_links[0]
    assert link.status == expected and link.fill_ids == []
    assert (
        (reason in link.rejection_reason) if reason else link.rejection_reason is None
    )
    if expected == "unknown":
        assert observed.coverage == "partial" and observed.missing_reason


def test_event_policy_records_exact_order_and_cancel_intents():
    capture = recorder()
    inputs = fixture.inputs(kind="event")
    controller = None

    def exchange(observation):
        if observation.open_orders:
            intents = (
                fixture.CancelIntent(
                    signal_id=f"cancel:{observation.session_date}",
                    order_id=observation.open_orders[0].order_id,
                    listing_id=fixture.L1,
                    effective_at=observation.next_open_time,
                ),
            )
        else:
            intents = (fixture.order(observation),)
        return fixture.event_reply(observation, intents)

    controller = Controller(inputs, exchange, research_recorder=capture)
    broker = fixture.FakeBroker(inputs.sessions[0])
    for session in inputs.sessions[:2]:
        broker.session = session
        controller.on_session(session, broker, fixture.FakeAQ)
    for index, record in enumerate(capture.records):
        emitted = stage(record, "portable.order_intents").constrained_plan
        assert emitted == controller.trace[index]["decision"]["intents"]
    assert broker.cancelled == ["native:1:0"]


def test_partial_fills_keep_actual_ids_and_mixed_rejection_stays_unknown():
    capture = recorder()
    controller, broker, _ = run_controller(capture)
    first, second = list(broker.orders.values())
    first.status = "Filled"
    first.filled_quantity = first.quantity
    first.updated_at = fixture.to_ns(controller.inputs.sessions[-1].close_time)
    second.status = "Rejected"
    second.reject_reason = "actual_native_second_leg_rejection"
    fill = SimpleNamespace(
        id="actual-partial-fill",
        order_id=first.id,
        symbol=first.symbol,
        side=first.side,
        quantity=first.quantity,
        price=10.0,
        commission=0.0,
        timestamp=first.updated_at,
    )
    controller._refresh(broker)
    result = fixture.export_result(
        fixture.fake_result(controller, broker, [fill]), controller
    )
    controller.research.finalize(controller, result)
    observed = stage(capture.records[-1], "portable.native_execution")
    assert observed.order_links[0].status == "partially_filled"
    assert observed.order_links[0].fill_ids == ["actual-partial-fill"]
    assert observed.order_links[0].child_order_ids == [first.id, second.id]
    receipts = json.loads(observed.features[0].value)
    assert receipts[0]["orders"][1]["reject_reason"] == second.reject_reason

    capture = recorder()
    controller, broker, _ = run_controller(capture)
    first, _ = list(broker.orders.values())
    first.status, first.reject_reason = "Rejected", "actual_native_first_leg_rejection"
    controller._refresh(broker)
    result = fixture.export_result(fixture.fake_result(controller, broker), controller)
    controller.research.finalize(controller, result)
    observed = stage(capture.records[-1], "portable.native_execution")
    assert observed.order_links[0].status == "unknown"
    assert observed.coverage == "partial" and observed.missing_reason


def test_observation_window_does_not_move_later_execution_facts_into_a_decision():
    capture = recorder(
        observation_window=DateRange(start="2025-01-02", end="2025-01-03")
    )
    controller, broker, _ = run_controller(capture)
    baseline, expected_broker, _ = run_controller()
    assert controller.trace == baseline.trace
    assert broker.submissions == expected_broker.submissions
    assert len(capture.records) == 1
    controller.research.finalize(
        controller,
        fixture.export_result(fixture.fake_result(controller, broker), controller),
    )
    assert len(capture.records) == 1
    assert capture.summary()["coverage"] == "partial"
    assert any(
        "observation window" in reason
        for reason in capture.summary()["missing_reasons"]
    )
    link = stage(capture.records[0], "portable.controller_plan").order_links[0]
    assert link.status == "unknown" and link.fill_ids == []


def test_filters_budgets_streaming_and_failed_sink_do_not_change_execution():
    baseline, broker, obs = run_controller()
    variants = [
        recorder(instrument_ids=[fixture.L2], stage_ids=["portable.policy_reply"]),
        recorder(max_uncompressed_bytes=1024),
        Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="off")),
    ]
    emitted = []
    variants.append(
        Recorder(
            code_sha256="a" * 64,
            config=CaptureConfig(mode="research"),
            on_record=emitted.append,
        )
    )

    def broken_sink(_):
        raise RuntimeError("synthetic sink failure")

    variants.append(
        Recorder(
            code_sha256="a" * 64,
            config=CaptureConfig(mode="research"),
            on_record=broken_sink,
        )
    )
    for capture in variants:
        controller, actual_broker, observations = run_controller(capture)
        assert controller.trace == baseline.trace
        assert controller.logical_orders == baseline.logical_orders
        assert actual_broker.submissions == broker.submissions
        assert [canonical_json(o) for o in observations] == [
            canonical_json(o) for o in obs
        ]
    assert len(emitted) == 3
    assert variants[2].summary()["decisions_seen"] == 3
    assert variants[2].summary()["coverage"] == "not_collected"
    assert variants[-1].summary()["decisions_saved"] == 0
    narrowed = stage(variants[0].records[0], "portable.policy_reply")
    assert [row.instrument_id for row in narrowed.candidates] == [fixture.L2]
    assert narrowed.coverage == "partial"


def test_source_event_pairing_ignores_strategy_state_and_future_market_rows():
    captures = []
    for initial_counter, future_price in [(0, 10), (999, 100)]:
        request, market = fixture.fixture(listing_count=2)
        request["initial_state"] = (
            fixture.StateCodec(fixture.SCHEMA)
            .encode({"counter": initial_counter})
            .model_dump(mode="json")
        )
        for bar in market["bars"]:
            if bar["session_date"] > "2025-01-02":
                bar.update(
                    open=future_price,
                    close=future_price,
                    high=future_price + 2,
                    low=future_price - 2,
                )
        inputs = fixture.changed_input(request, market)
        capture = recorder()
        controller = Controller(
            inputs, fixture.portfolio_reply, research_recorder=capture
        )
        controller.on_session(
            inputs.sessions[0], fixture.FakeBroker(inputs.sessions[0]), fixture.FakeAQ
        )
        captures.append(capture.records[0])
    assert captures[0].pairing_key() == captures[1].pairing_key()
    assert captures[0].pre_state.strategy_state != captures[1].pre_state.strategy_state


ENGINE_SCRIPT = r"""
import json, sys
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder
from runtimes.portable_engine.runtime import execute
from packages.strategy_sdk import Target
from tests.portable import engine_fixtures as f
mode, scope=sys.argv[1:]
r=Recorder(code_sha256='a'*64, config=CaptureConfig(mode=mode, instrument_ids=[f.L2] if scope=='narrow' else []))
inputs=f.inputs(listing_count=2)
observations=[]
def exchange(obs):
    observations.append(len(obs.listings))
    reply=f.portfolio_reply(obs, 'rebalance' if obs.session_date == inputs.sessions[0].session_date else 'hold', (Target(listing_id=f.L1,value=10),))
    reply['decision']['scores']=[{'listing_id':f.L1,'value':7.5}]
    return reply
bundle=execute(inputs, exchange, research_recorder=r)
# Native UUIDs vary across genuine runs. Normalize identifiers only, preserving
# every economic row/value, row order, timestamps and logical order identity.
ids={row['order_id']: 'native-order:'+str(i) for i,row in enumerate(bundle['orders'])}
ids.update({row['fill_id']: 'native-fill:'+str(i) for i,row in enumerate(bundle['fills'])})
def normalize(v):
    if isinstance(v,str): return ids.get(v,v)
    if isinstance(v,list): return [normalize(x) for x in v]
    if isinstance(v,dict): return {k:normalize(x) for k,x in v.items()}
    return v
print('PORTABLE_NATIVE_CAPTURE:'+json.dumps({'result':normalize(bundle), 'records':[row.model_dump(mode='json') for row in r.records], 'summary':r.summary(), 'universe_sizes':observations},allow_nan=False,sort_keys=True))
"""


def _native(mode, scope="all"):
    requested = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    required = os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1"
    local = os.environ.get("TRADEGRAD_LOCAL_ENGINE_BRIDGE") == "1"
    if not (requested or required or local):
        pytest.skip("Actual portable controller/AKQuant subprocess check is opt-in")
    if requested:
        engine = Path(requested)
    elif importlib.util.find_spec("akquant") is not None:
        engine = Path(sys.executable)
    elif local:
        engine = ROOT.parent / "tradegrad-webui-v1/.venv/bin/python"
    else:
        pytest.fail(
            "Required native tests need AKQuant or TRADEGRAD_NATIVE_TEST_PYTHON"
        )
    assert engine.is_file(), "Explicitly selected pinned native engine is absent"
    completed = subprocess.run(
        [str(engine), "-B", "-c", ENGINE_SCRIPT, mode, scope],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TRADEGRAD_PORTABLE_ENGINE_CONTAINER": "1",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-8192:].decode(errors="replace")
    lines = [
        line
        for line in completed.stdout.decode().splitlines()
        if line.startswith("PORTABLE_NATIVE_CAPTURE:")
    ]
    assert len(lines) == 1
    return json.loads(lines[0].split(":", 1)[1])


def test_actual_akquant_trace_modes_preserve_all_economics_and_capture_real_pre_state():
    baseline = _native("off")
    for mode, scope in [
        ("summary", "all"),
        ("research", "all"),
        ("research", "narrow"),
    ]:
        run = _native(mode, scope)
        assert run["result"] == baseline["result"]
        assert run["universe_sizes"] == [2, 2, 2]
        records = run["records"]
        callbacks = [
            row
            for row in records
            if row["callback_phase"] == "portable.complete_daily_cross_section"
        ]
        assert callbacks[0]["pre_state"]["cash"] == 10000.0
        assert callbacks[0]["pre_state"]["positions"] == []
        assert callbacks[1]["pre_state"]["cash"] < 10000.0
        assert callbacks[1]["pre_state"]["positions"][0]["quantity"] == 10.0
        assert callbacks[1]["pre_state"]["open_orders"] == []
        final = next(
            row
            for row in records
            if row["callback_phase"] == "portable.execution_result"
        )
        link = final["stages"][0]["order_links"][0]
        assert link["status"] == "filled" and len(link["fill_ids"]) == 1
        assert len(link["child_order_ids"]) == 1
        assert datetime.fromisoformat(link["fill_times"][0]) > datetime.fromisoformat(
            link["submitted_at"]
        )
        assert run["result"]["summary"]["execution_count"] == 1
        assert run["summary"]["decisions_seen"] == 4
