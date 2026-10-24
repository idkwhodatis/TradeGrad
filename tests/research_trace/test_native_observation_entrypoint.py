"""Real evaluator entrypoint and diagnostic seam on immutable maintained source."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.tradegrad_adapter.independent_results import validate_envelope
from packages.tradegrad_adapter.results import validate_envelope as legacy_envelope
from packages.tradegrad_adapter.runtime_observation import (
    decode_runtime_protocol,
    digest,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
import hashlib,json,sys,tempfile
from pathlib import Path
import pandas as pd
from packages.research_trace.samples import TIMING_TAPE
from runtimes.evaluator.entrypoint import main
path,sha,independent,restore_json,zero=sys.argv[1:]
source=Path(path)
assert source.name=='research_time.py' and not source.is_symlink()
assert hashlib.sha256(source.read_bytes()).hexdigest()==sha
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);inputs=root/'input';outputs=root/'output';(inputs/'data').mkdir(parents=True)
 (inputs/'strategy.py').write_bytes(source.read_bytes())
 tape=TIMING_TAPE;item=tape.instruments[0];close=list(item.closes)
 frame=pd.DataFrame({'date':pd.to_datetime(tape.dates),'open':close,'high':[p+.1 for p in close],'low':[p-.1 for p in close],'close':close,'volume':item.volumes})
 frame.to_csv(inputs/'data'/'INDEX.csv',index=False)
 config={'engine_overrides':{'initial_cash':10000,'commission_rate':0,'stamp_tax_rate':0,'slippage':{'type':'percent','value':0},'history_depth':252,'lot_size':1,'timezone':tape.market_timezone},'window':{'start':'2024-01-02','end':'2024-01-12','role':'train'},'warmup':{'sessions':0,'history_policy':'source_cold_start'},'benchmark':None,'run_config_sha256':'a'*64,'research_capture':{'mode':'research'}}
 if independent=='yes':config['result_policy']='independent_observation.v1'
 if zero=='yes':config['strategy_contract']={'lookback_bars':3,'warmup_bars':0,'requires_shorting':False,'parameters':[{'name':'min_average_dollar_volume','kind':'number','value':1e9,'minimum':0.0,'maximum':1e12}]}
 request={'task':'us_time','market':'US','profile':'corrected_explicit_config','start_date':'2024-01-02','end_date':'2024-01-12','evaluation_config':config}
 restored=json.loads(restore_json)
 if restored is not None:request['runtime_observation']={'schema_version':'tradegrad.runtime-observation-request.v1','rng_restore':restored}
 (inputs/'request.json').write_text(json.dumps(request))
 try:main(inputs,outputs)
 except Exception as exc:
  print('EXPECTED_FAILURE:'+type(exc).__name__+':'+str(exc));sys.exit(2)
"""


def execute(*, independent=True, restore=None, zero=False):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Native test interpreter required")
        pytest.skip(
            "Actual entrypoint execution requires pinned native test interpreter"
        )
    source = ROOT / "seed_strategies/research_time.py"
    sha = CONTROLLED_SOURCE_SHA256["daily_timing"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == sha
    process = subprocess.run(
        [
            python,
            "-c",
            SCRIPT,
            str(source),
            sha,
            "yes" if independent else "no",
            json.dumps(restore),
            "yes" if zero else "no",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    return process


def decoded(process):
    assert process.returncode == 0, (
        process.stderr.decode() + process.stdout.decode()[-2000:]
    )
    line = next(
        line
        for line in process.stdout.splitlines()
        if line.startswith(b"TRADEGRAD_RESULT_")
    )
    data = base64.b64decode(line.split(b":", 1)[1], validate=True)
    envelope = (
        validate_envelope
        if line.startswith(b"TRADEGRAD_RESULT_V3:")
        else legacy_envelope
    )(data)
    observation = decode_runtime_protocol(
        process.stdout,
        expected_identity=envelope["bundle"]["identity"],
        expected_initialization={
            "policy": "reconstruct_from_original_start.v1",
            "window": envelope["bundle"]["window"],
        },
    )
    return envelope, observation


def normalized(bundle):
    result = {}
    for section in (
        "equity",
        "drawdown",
        "cash",
        "margin",
        "positions",
        "orders",
        "fills",
        "closed_trades",
    ):
        table = bundle[section]
        indices = [
            i
            for i, c in enumerate(table["columns"])
            if c["name"] not in ("id", "order_id", "trade_id", "execution_id")
        ]
        result[section] = [[row[i] for i in indices] for row in table["rows"]]
    return result


def test_actual_future_ordinary_source_and_independent_replay_restore_same_seam():
    source, original = decoded(execute(independent=False))
    replay, observed = decoded(execute(restore=original["rng"]["state"]))
    assert source["bundle"]["schema_version"] == "tradegrad.result-bundle.v1"
    assert replay["bundle"]["schema_version"] == "tradegrad.result-bundle.v2"
    assert original["rng"]["restored_from_sha256"] is None
    assert observed["rng"]["restored_from_sha256"] == original["rng"]["state_sha256"]
    assert observed["rng"]["state"] == original["rng"]["state"]
    assert normalized(source["bundle"]) == normalized(replay["bundle"])
    assert original["audit"]["completed"] and observed["audit"]["completed"]
    assert source["metrics"]["combined_score"] is not None
    assert replay["metrics"]["combined_score"] is None
    assert (
        source["bundle"]["identity"]["input_manifest_sha256"]
        != replay["bundle"]["identity"]["input_manifest_sha256"]
    )


def test_actual_zero_trade_entrypoint_succeeds_only_independent_policy():
    value, observation = decoded(execute(zero=True))
    assert not value["bundle"]["fills"]["rows"]
    assert value["metrics"]["metrics"]["trade_per_day"] == 0
    assert observation["audit"]["completed"]
    legacy = execute(independent=False, zero=True)
    assert legacy.returncode == 2 and b"No trades executed" in legacy.stdout
    assert b"TRADEGRAD_RESULT_" not in legacy.stdout


def test_actual_numpy_and_stdlib_state_round_trip_at_native_seam():
    _, source = decoded(execute())
    rng = source["rng"]["state"]
    assert rng["numpy_legacy"]["algorithm"] == "MT19937"
    assert len(rng["numpy_legacy"]["keys"]) == 624
    _, replay = decoded(execute(restore=rng))
    assert replay["rng"]["state_sha256"] == digest(rng)


def test_native_rng_snapshot_restores_cached_gaussian_and_numpy_future_draws():
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Native test interpreter required")
        pytest.skip("Pinned NumPy interpreter is opt-in")
    program = """
import random
import numpy as np
from packages.tradegrad_adapter.runtime_observation import capture_rng_state, restore_rng_state
random.gauss(0,1)
np.random.normal()
before_py=random.getstate()
before_np=np.random.get_state()
state=capture_rng_state(include_numpy=True)
assert before_py==random.getstate()
assert (before_np[1]==np.random.get_state()[1]).all()
expected=([random.gauss(0,1),random.random()],np.random.normal(size=5).tolist(),np.random.random(5).tolist())
restore_rng_state(state,include_numpy=True)
assert ([random.gauss(0,1),random.random()],np.random.normal(size=5).tolist(),np.random.random(5).tolist())==expected
"""
    process = subprocess.run(
        [python, "-c", program],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
