"""Native actual observations and opt-in controlled AKQuant runtime parity."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.native import NativeCapture
from packages.research_trace.recorder import Recorder
from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.research_trace.transport import (
    CaptureReceiver,
    iter_capture_records,
)

ROOT = Path(__file__).resolve().parents[2]


def order(
    key,
    status="Filled",
    filled=1,
    reason="",
    created=1_704_379_200_000_000_000,
    updated=1_704_465_600_000_000_000,
):
    return SimpleNamespace(
        id=key,
        status=status,
        filled_quantity=filled,
        quantity=2,
        reject_reason=reason,
        created_at=created,
        updated_at=updated,
        symbol="B",
    )


def fill(key, child, timestamp=1_704_465_600_000_000_000):
    return SimpleNamespace(id=key, order_id=child, timestamp=timestamp)


def test_native_observed_only_distinguishes_same_timestamp_orders_and_later_multiple_fills():
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observer = NativeCapture(recorder, lambda value: None)
    raw = SimpleNamespace(
        orders=[
            order("one", "PartiallyFilled"),
            order("two", "Rejected", 0, "InsufficientCash"),
        ],
        executions=[fill("f1", "one"), fill("f2", "one", 1_704_552_000_000_000_000)],
    )
    observer.finalize(SimpleNamespace(_raw=raw), market_timezone="UTC")
    assert len(recorder.records) == 2
    assert recorder.records[0].pairing_key() != recorder.records[1].pairing_key()
    first, second = (r.stages[0].order_links[0] for r in recorder.records)
    assert first.fill_ids == ["f1", "f2"] and first.status == "partially_filled"
    assert first.fill_times[0] > first.submitted_at
    assert second.status == "rejected" and second.rejection_reason == "InsufficientCash"
    assert all(
        r.pre_state is None and r.pre_state_missing_reason for r in recorder.records
    )
    assert all(
        not r.stages[0].predicates and not r.stages[0].features
        for r in recorder.records
    )
    assert recorder.summary()["coverage"] == "partial"


def test_explicit_logical_to_multiple_children_is_not_joined_by_timestamp():
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    output = []
    observer = NativeCapture(recorder, output.append)
    recorder._on_record = observer.append
    recorder.begin_decision(
        data_event_id="complete-portfolio-event",
        event_seq=7,
        scope="portfolio",
        callback_phase="on_session",
        decision_time="2024-01-04T12:00:00Z",
        information_cutoff="2024-01-04T12:00:00Z",
        market_timezone="UTC",
    )
    recorder.emit(
        "execution",
        {
            "order_links": [
                {
                    "logical_order_id": "intent-B",
                    "child_order_ids": ["one", "two"],
                    "fill_ids": [],
                    "instrument_id": "B",
                    "submitted_at": "2024-01-04T12:00:00Z",
                    "fill_times": [],
                    "status": "intended",
                }
            ]
        },
    )
    recorder.end_decision()
    original_origins = observer.order_origins.copy()
    output.clear()
    observer.finalize(
        SimpleNamespace(
            _raw=SimpleNamespace(
                orders=[order("one"), order("two")],
                executions=[fill("a", "one"), fill("b", "two")],
            )
        ),
        market_timezone="UTC",
    )
    assert len(output) == 1
    assert (
        output[0].data_event_id == "complete-portfolio-event"
        and output[0].event_seq == 7
    )
    assert (
        output[0].callback_phase == "native.execution_result:on_session"
        and output[0].scope == "portfolio"
    )
    link = output[0].stages[0].order_links[0]
    assert link.logical_order_id == "intent-B"
    assert link.child_order_ids == ["one", "two"] and link.fill_ids == ["a", "b"]
    assert observer.order_origins == original_origins
    assert not observer._emitting_result
    assert not any(
        "Conflicting" in reason for reason in recorder.summary()["missing_reasons"]
    )


def test_conflicting_order_origin_is_never_resurrected_by_later_emissions():
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observer = NativeCapture(recorder, lambda value: None)
    recorder._on_record = observer.append
    for index in range(4):
        recorder.begin_decision(
            data_event_id=f"event:{index}",
            event_seq=index,
            scope="portfolio",
            # A strategy cannot bypass conflict detection by claiming the
            # collector's reserved completion phase outside finalize().
            callback_phase="native.execution_result:on_session"
            if index == 1
            else "on_session",
            decision_time="2024-01-04T12:00:00Z",
            information_cutoff="2024-01-04T12:00:00Z",
            market_timezone="UTC",
        )
        recorder.emit(
            "execution",
            {
                "order_links": [
                    {
                        "logical_order_id": f"intent:{index}",
                        "child_order_ids": ["same-child"],
                        "fill_ids": [],
                        "instrument_id": "B",
                        "submitted_at": "2024-01-04T12:00:00Z",
                        "fill_times": [],
                        "status": "intended",
                    }
                ]
            },
        )
        recorder.end_decision()
    assert observer.order_origins["same-child"] is None
    output = []
    recorder._on_record = output.append
    observer.finalize(
        SimpleNamespace(
            _raw=SimpleNamespace(orders=[order("same-child")], executions=[])
        ),
        market_timezone="UTC",
    )
    assert (
        output[0].stages[0].order_links[0].logical_order_id
        == "observed-native:same-child"
    )
    assert output[0].data_event_id.startswith("native_order:")


@pytest.mark.parametrize("prior", [False, True])
def test_completion_guard_restores_prior_state_if_record_completion_raises(
    monkeypatch, prior
):
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observer = NativeCapture(recorder, lambda record: None)
    recorder._on_record = observer.append
    observer._emitting_result = prior

    def fail():
        raise RuntimeError("Completion sink unavailable")

    monkeypatch.setattr(recorder, "end_decision", fail)
    observer.finalize(
        SimpleNamespace(_raw=SimpleNamespace(orders=[order("one")], executions=[])),
        market_timezone="UTC",
    )
    assert observer._emitting_result is prior
    monkeypatch.undo()
    assert any(
        "could not be fully collected" in reason
        for reason in recorder.summary()["missing_reasons"]
    )


SCRIPT = r"""
import hashlib, json, sys
from pathlib import Path
import pandas as pd
import akquant as aq
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder, recording
from packages.research_trace.native import NativeCapture
from packages.research_trace.transport import CaptureWriter
from packages.research_trace.samples import TIMING_TAPE, CROSS_SECTION_TAPE
from src.evaluate import Date2Symbols
name, source_path, expected_sha, mode, scope = sys.argv[1:]
path = Path(source_path)
assert path.name in {'research_time.py', 'research_cross.py'}
assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha
assert not path.is_symlink()
tape = TIMING_TAPE if name == 'daily_timing' else CROSS_SECTION_TAPE
data = {}
for item in tape.instruments:
    closes = list(item.closes)
    data[item.instrument_id] = pd.DataFrame({'date': pd.to_datetime(tape.dates).tz_localize(tape.market_timezone) + pd.Timedelta(hours=16),
      'open': closes, 'high': [x+.1 for x in closes], 'low': [x-.1 for x in closes], 'close': closes,
      'volume': item.volumes, 'symbol': item.instrument_id})
context = {'all_symbols': sorted(data), 'date2symbols': Date2Symbols(dict(tape.universe))} if name == 'cross_sectional' else {'target_symbol': 'target_symbol'}
frames = []
config = CaptureConfig(mode=mode, instrument_ids=['B'] if scope == 'narrow' else [])
writer = CaptureWriter(config, frames.append)
recorder = Recorder(code_sha256=expected_sha, config=config)
observer = NativeCapture(recorder, writer.append)
recorder._on_record = observer.append
with recording(recorder):
    result = aq.run_backtest(data=data, strategy_source=str(path), context=context,
        initial_cash=tape.initial_cash, commission_rate=0, stamp_tax_rate=0, slippage=0,
        history_depth=252, lot_size=1, timezone=tape.market_timezone, show_progress=False,
        **({"volume_limit_pct":0.1} if scope == "partial" else {}))
    observer.finalize(result, market_timezone=tape.market_timezone)
writer.finish(recorder.summary())
def table(frame):
    return json.loads(frame.drop(columns=[c for c in frame.columns if c in ('id','order_id','trade_id','execution_id')]).reset_index(drop=True).to_json(orient='split'))
economics = {'orders':table(result.orders_df), 'fills':table(result.executions_df), 'positions':table(result.positions_df),
             'trades':table(result.trades_df), 'equity':json.loads(result.equity_curve.to_json()), 'metrics':json.loads(result.metrics_df.to_json())}
print('RUNTIME_RESULT:' + json.dumps({'frames':frames, 'economics':economics}, sort_keys=True, allow_nan=False))
"""


def _native(name, mode, scope="all"):
    requested = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    required = os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1"
    if not requested and not required:
        pytest.skip("Actual controlled native runtime checks are opt-in")
    if not requested:
        pytest.fail("Required runtime tests need TRADEGRAD_NATIVE_TEST_PYTHON")
    path = (
        ROOT
        / "seed_strategies"
        / ("research_time.py" if name == "daily_timing" else "research_cross.py")
    )
    assert (
        hashlib.sha256(path.read_bytes()).hexdigest() == CONTROLLED_SOURCE_SHA256[name]
    )
    result = subprocess.run(
        [
            requested,
            "-c",
            SCRIPT,
            name,
            str(path),
            CONTROLLED_SOURCE_SHA256[name],
            mode,
            scope,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(
        next(
            line.removeprefix("RUNTIME_RESULT:")
            for line in result.stdout.splitlines()
            if line.startswith("RUNTIME_RESULT:")
        )
    )


@pytest.mark.parametrize("name", ["daily_timing", "cross_sectional"])
def test_actual_native_runtime_capture_stream_economic_parity_and_explicit_next_day_links(
    name, tmp_path
):
    off = _native(name, "off")
    research = _native(name, "research")
    summary_run = _native(name, "summary")
    narrow = _native(name, "research", "narrow")
    assert (
        off["economics"]
        == research["economics"]
        == summary_run["economics"]
        == narrow["economics"]
    )
    receiver = CaptureReceiver(
        tmp_path, CaptureConfig(mode="research"), source="strategy_emitted"
    )
    for value in research["frames"]:
        receiver.accept(value)
    envelope = receiver.result()
    records = list(
        iter_capture_records(
            envelope["research_capture_path"],
            envelope["research_capture_sha256"],
            envelope["research_capture_size_bytes"],
            CaptureConfig(mode="research"),
        )
    )
    original = [
        r for r in records if not r.callback_phase.startswith("native.execution_result")
    ]
    executions = [
        r for r in records if r.callback_phase.startswith("native.execution_result")
    ]
    assert original and executions
    assert all(
        r.pre_state is not None and r.pre_state.cash is not None for r in original
    )
    assert all(r.pre_state is None for r in executions)
    intents = {
        link.logical_order_id: link
        for r in original
        for s in r.stages
        for link in s.order_links
    }
    observed = [link for r in executions for s in r.stages for link in s.order_links]
    assert all(link.logical_order_id in intents for link in observed)
    assert any(
        link.fill_ids and link.fill_times[0] > link.submitted_at for link in observed
    )
    assert all(not link.fill_ids for link in intents.values())
    assert off["frames"][-1]["summary"]["coverage"] == "not_collected"
    assert summary_run["frames"][-1]["summary"]["coverage"] == "partial"


@pytest.mark.parametrize("missing", [None, ""])
def test_native_missing_rejection_reason_preserves_actual_child_without_fabrication(
    missing,
):
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    capture = NativeCapture(recorder, lambda value: None)
    capture.finalize(
        SimpleNamespace(
            _raw=SimpleNamespace(
                orders=[order("rejected", "Rejected", 0, missing)], executions=[]
            )
        ),
        market_timezone="UTC",
    )
    assert len(recorder.records) == 1
    link = recorder.records[0].stages[0].order_links[0]
    assert link.child_order_ids == ["rejected"]
    assert link.status == "unknown" and link.rejection_reason is None
    assert any(
        "rejection reason" in reason for reason in recorder.summary()["missing_reasons"]
    )


def test_actual_native_multiple_or_partial_fill_shape_remains_explicit(tmp_path):
    off = _native("cross_sectional", "off", "partial")
    observed = _native("cross_sectional", "research", "partial")
    assert off["economics"] == observed["economics"]
    receiver = CaptureReceiver(
        tmp_path, CaptureConfig(mode="research"), source="strategy_emitted"
    )
    for frame in observed["frames"]:
        receiver.accept(frame)
    envelope = receiver.result()
    records = list(
        iter_capture_records(
            envelope["research_capture_path"],
            envelope["research_capture_sha256"],
            envelope["research_capture_size_bytes"],
            CaptureConfig(mode="research"),
        )
    )
    links = [
        link
        for record in records
        if record.callback_phase.startswith("native.execution_result")
        for stage in record.stages
        for link in stage.order_links
    ]
    assert any(
        len(link.fill_ids) > 1 or link.status == "partially_filled" for link in links
    )
    assert all(len(link.fill_ids) == len(link.fill_times) for link in links)
    assert all(time >= link.submitted_at for link in links for time in link.fill_times)


def test_same_timestamp_distinct_callback_phases_do_not_collapse_execution_origins():
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observer = NativeCapture(recorder, lambda record: None)
    recorder._on_record = observer.append
    for index, phase in enumerate(("on_bar", "on_signal")):
        recorder.begin_decision(
            data_event_id="same-data-event",
            event_seq=1,
            scope="portfolio",
            callback_phase=phase,
            decision_time="2024-01-04T12:00:00Z",
            information_cutoff="2024-01-04T12:00:00Z",
            market_timezone="UTC",
        )
        recorder.emit(
            "execution",
            {
                "order_links": [
                    {
                        "logical_order_id": f"logical:{index}",
                        "child_order_ids": [f"child:{index}"],
                        "fill_ids": [],
                        "fill_times": [],
                        "submitted_at": "2024-01-04T12:00:00Z",
                        "instrument_id": "B",
                        "status": "intended",
                    }
                ]
            },
        )
        recorder.end_decision()
    output = []
    recorder._on_record = output.append
    observer.finalize(
        SimpleNamespace(
            _raw=SimpleNamespace(
                orders=[order("child:0"), order("child:1")], executions=[]
            )
        ),
        market_timezone="UTC",
    )
    assert len(output) == 2
    assert {row.callback_phase for row in output} == {
        "native.execution_result:on_bar",
        "native.execution_result:on_signal",
    }
    assert len({row.pairing_key() for row in output}) == 2
    assert all(len(row.stages[0].order_links) == 1 for row in output)


@pytest.mark.parametrize("updated", [None, 0])
def test_native_unset_update_time_never_predates_actual_submission(updated):
    recorder = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    NativeCapture(recorder, lambda record: None).finalize(
        SimpleNamespace(
            _raw=SimpleNamespace(
                orders=[order("pending", "Accepted", 0, updated=updated)], executions=[]
            )
        ),
        market_timezone="UTC",
    )
    assert len(recorder.records) == 1
    row = recorder.records[0]
    assert row.decision_time == row.stages[0].order_links[0].submitted_at
    assert row.decision_time.year == 2024
