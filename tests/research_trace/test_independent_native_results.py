"""Actual pinned AKQuant observations. Only maintained SHA-locked sources execute.

Business binding hashes below are synthetic fixture metadata, not PG admission.
"""

import base64
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.tradegrad_adapter.independent_results import (
    RESULT_POLICY,
    make_envelope,
    validate_envelope,
    validate_result_bundle,
)
from packages.tradegrad_adapter.results import (
    canonical_json_bytes,
    sha256_bytes,
)
from packages.tradegrad_adapter.results import (
    validate_result_bundle as validate_legacy,
)
from runtimes.evaluator.runner import decode_result_protocol

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
import hashlib,json,sys
from pathlib import Path
import pandas as pd
import akquant as aq
from packages.research_trace.samples import TIMING_TAPE,CROSS_SECTION_TAPE
from packages.tradegrad_adapter.runtime_observation import NativeAuditObserver,capture_rng_state,restore_rng_state,make_runtime_observation
from packages.research_trace.recorder import Recorder,recording
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.native import NativeCapture
from packages.tradegrad_adapter.independent_results import observation_performance,export_native_result
from src.evaluate import Date2Symbols
name,path,expected,scenario,enabled,restore_json=sys.argv[1:]
source=Path(path)
assert source.name in ('research_time.py','research_cross.py') and not source.is_symlink()
assert hashlib.sha256(source.read_bytes()).hexdigest()==expected
n=1 if scenario=='one_session' else 8
tape=TIMING_TAPE if name=='daily_timing' else CROSS_SECTION_TAPE
params={'min_average_dollar_volume':1e9} if scenario in ('one_session','no_trade') else {'exit_buffer':1.0} if scenario=='open_only' else {}
if scenario=='failure':params={'fast_window':3,'slow_window':3}
data={}
for item in tape.instruments:
 close=list(item.closes[:n])
 data[item.instrument_id]=pd.DataFrame({'date':pd.to_datetime(tape.dates[:n]).tz_localize(tape.market_timezone)+pd.Timedelta(hours=16),'open':close,'high':[p+.1 for p in close],'low':[p-.1 for p in close],'close':close,'volume':item.volumes[:n],'symbol':item.instrument_id})
context={'target_symbol':'target_symbol','tradegrad_parameters':params} if name=='daily_timing' else {'all_symbols':sorted(data),'date2symbols':Date2Symbols(dict(tape.universe))}
audit=NativeAuditObserver()
kwargs={'initial_cash':tape.initial_cash,'commission_rate':1.0 if scenario=='rejected' else 0,'stamp_tax_rate':0,'slippage':0,'history_depth':252,'lot_size':1,'timezone':tape.market_timezone,'show_progress':False}
if scenario=='partial':kwargs['volume_limit_pct']=.1
if scenario=='rejected':kwargs['risk_config']={'restricted_list':['target_symbol']}
recorder=Recorder(code_sha256=expected,config=CaptureConfig(mode="research"))
records=[]
observer=NativeCapture(recorder,records.append)
recorder._on_record=observer.append
restore=json.loads(restore_json)
if restore is not None:restore_rng_state(restore,include_numpy=True)
rng=capture_rng_state(include_numpy=True)
try:
 with recording(recorder):
  result=aq.run_backtest(data=data,strategy_source=str(source),context=context,**kwargs,**({'on_event':audit,'stream_mode':'audit'} if enabled=='yes' else {}))
  observer.finalize(result,market_timezone=tape.market_timezone)
except Exception as exc:
 if scenario!='failure':raise
 print('I5_RESULT:'+json.dumps({'failed':type(exc).__name__,'audit':audit.finish()}));sys.exit(0)
performance=observation_performance(result,market='US',profile='corrected_explicit_config')
bundle=export_native_result(result,performance,identity={'strategy_sha256':expected,'run_config_sha256':'a'*64,'input_manifest_sha256':'b'*64},window={'start':'2024-01-02','end':'2024-01-12','interval':'half_open','warmup':{'sessions':0,'history_policy':'source_cold_start'}},engine_kwargs=kwargs,currency='USD')
def table(frame):
 return json.loads(frame.drop(columns=[c for c in frame.columns if c in ('id','order_id','trade_id','execution_id')]).reset_index(drop=True).to_json(orient='split'))
economics={'orders':table(result.orders_df),'fills':table(result.executions_df),'positions':table(result.positions_df),'closed_trades':table(result.trades_df),'equity':json.loads(result.equity_curve.to_json()),'cash':json.loads(result.cash_curve.to_json()),'margin':json.loads(result.margin_curve.to_json()),'metrics':json.loads(result.metrics_df.to_json())}
print('I5_RESULT:'+json.dumps({'bundle':bundle,'performance':performance,'audit':audit.finish() if enabled=='yes' else None,'economics':economics,'rng':rng,'observation':make_runtime_observation(runtime_kind='native',identity=bundle['identity'],initialization={'policy':'reconstruct_from_original_start.v1','window':bundle['window']},rng_state=rng,restored_from=restore,audit=audit.finish()) if enabled=='yes' else None,'records':[r.model_dump(mode='json') for r in records]},sort_keys=True,allow_nan=False))
"""


def native(scenario="normal", *, name="daily_timing", observer=True, rng_restore=None):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail(
                "Required actual engine tests need TRADEGRAD_NATIVE_TEST_PYTHON"
            )
        pytest.skip("Actual maintained native engine subprocess is opt-in")
    path = (
        ROOT
        / "seed_strategies"
        / ("research_time.py" if name == "daily_timing" else "research_cross.py")
    )
    expected = CONTROLLED_SOURCE_SHA256[name]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    result = subprocess.run(
        [
            python,
            "-c",
            SCRIPT,
            name,
            str(path),
            expected,
            scenario,
            "yes" if observer else "no",
            json.dumps(rng_restore),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(
        next(
            line[len("I5_RESULT:") :]
            for line in result.stdout.splitlines()
            if line.startswith("I5_RESULT:")
        )
    )


@pytest.fixture(scope="module")
def observed():
    return {
        case: native(case)
        for case in ("one_session", "no_trade", "open_only", "normal", "rejected")
    }


def test_actual_empty_observation_not_optimization_eligibility(observed):
    for case, frequency in (("one_session", None), ("no_trade", 0)):
        value = observed[case]
        bundle = value["bundle"]
        assert (
            not bundle["orders"]["rows"]
            and not bundle["fills"]["rows"]
            and not bundle["closed_trades"]["rows"]
        )
        assert value["performance"]["metrics"]["trade_per_day"] == frequency
        assert value["performance"]["combined_score"] is None
        assert value["performance"]["reward_status"] == "not_applicable"
        assert value["performance"]["metrics"]["win_rate"] is None
        assert value["performance"]["metrics"]["return_per_trade"] is None
        assert value["audit"]["completed"] is True
        validate_result_bundle(bundle)
        validate_envelope(make_envelope(value["performance"], bundle))
        with pytest.raises(ValueError):
            validate_legacy(bundle)


def test_actual_open_position_no_closed_trades_preserves_negative_economics(observed):
    value = observed["open_only"]
    assert len(value["bundle"]["fills"]["rows"]) == 1
    assert not value["bundle"]["closed_trades"]["rows"]
    assert value["performance"]["metrics"]["annualized_return"] < 0
    assert value["performance"]["metrics"]["max_drawdown"] > 0
    assert value["performance"]["metrics"]["win_rate"] is None


@pytest.mark.parametrize("scenario", ["normal", "no_trade", "open_only", "rejected"])
def test_actual_audit_on_off_economics_match(observed, scenario):
    assert (
        observed[scenario]["economics"] == native(scenario, observer=False)["economics"]
    )
    assert observed[scenario]["audit"]["completed"] is True


def test_actual_partial_fills_have_ordered_status_and_explicit_ids():
    on = native("partial", name="cross_sectional")
    off = native("partial", name="cross_sectional", observer=False)
    assert on["economics"] == off["economics"]
    assert on["audit"]["completed"] is True
    events = on["audit"]["events"]
    orders = [e["payload"] for e in events if e["event_type"] == "order"]
    trades = [e["payload"] for e in events if e["event_type"] == "trade"]
    assert any(item["status"] == "PartiallyFilled" for item in orders)
    assert all(item["order_id"] in {o["order_id"] for o in orders} for item in trades)
    assert len({item["trade_id"] for item in trades}) == len(trades)


def test_actual_rejected_orders_are_events_not_missing_intent(observed):
    statuses = [
        e["payload"]["status"]
        for e in observed["rejected"]["audit"]["events"]
        if e["event_type"] == "order"
    ]
    assert "Rejected" in statuses


def test_actual_candidate_failure_stays_failure_not_zero_observation():
    value = native("failure")
    assert value["failed"] and "bundle" not in value
    assert value["audit"]["completed"] is False


def test_only_trusted_policy_can_admit_native_independent_wire(observed, tmp_path):
    value = observed["no_trade"]
    wire = b"TRADEGRAD_RESULT_V3:" + base64.b64encode(
        make_envelope(value["performance"], value["bundle"])
    )
    with pytest.raises(ValueError, match="Child result"):
        decode_result_protocol(wire, tmp_path)
    metrics, bundle = decode_result_protocol(
        wire,
        tmp_path,
        expected_result_policy=RESULT_POLICY,
        expected_config_sha256="a" * 64,
        expected_identity=value["bundle"]["identity"],
    )
    assert metrics == value["performance"] and bundle == value["bundle"]


@pytest.mark.parametrize("field", ["orders", "fills", "equity", "metrics"])
def test_independent_result_hash_tampering_is_rejected(observed, field):
    value = copy.deepcopy(observed["normal"]["bundle"])
    value[field] = {"forged": True}
    with pytest.raises((ValueError, KeyError, TypeError)):
        validate_result_bundle(value)


def test_rehashed_disappearing_fills_and_forged_reward_are_rejected(observed):
    original = observed["normal"]["bundle"]
    for change in ("fills", "reward"):
        value = copy.deepcopy(original)
        if change == "fills":
            value["fills"]["rows"].clear()
        else:
            value["legacy_performance"]["value"]["combined_score"] = 0
        for artifact in value["artifacts"]:
            data = canonical_json_bytes(value[artifact["section"]])
            artifact.update(sha256=sha256_bytes(data), bytes=len(data))
        with pytest.raises(ValueError):
            validate_result_bundle(value)


def test_trusted_window_pin_rejects_otherwise_valid_independent_bundle(
    observed, tmp_path
):
    value = observed["normal"]
    wire = b"TRADEGRAD_RESULT_V3:" + base64.b64encode(
        make_envelope(value["performance"], value["bundle"])
    )
    expected = {
        **value["bundle"]["window"],
        "warmup": {"sessions": 5, "history_policy": "require"},
    }
    with pytest.raises(ValueError, match="trusted reconstruction window"):
        decode_result_protocol(
            wire,
            tmp_path,
            expected_result_policy=RESULT_POLICY,
            expected_identity=value["bundle"]["identity"],
            expected_window=expected,
        )
    assert not list(tmp_path.iterdir())
