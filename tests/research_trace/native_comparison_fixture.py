"""Real maintained AKQuant runs for comparison tests, never arbitrary source.

The child verifies the maintained source pin before applying the one fixed,
reviewable comment-only mutation. It retains actual engine identifiers and
uses the production ResultBundle exporter. Business bindings remain synthetic
test identities, not evidence of PostgreSQL/HTTP/container admission.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.research_trace.templates import template_source

ROOT = Path(__file__).resolve().parents[2]
COMMENT_ONLY = "\n# Comparison fixture: descriptive comment, no executable change.\n"

SCRIPT = r"""
import hashlib, json, sys, tempfile
from pathlib import Path
import pandas as pd
import akquant as aq
from packages.research_trace.contracts import CaptureConfig
from packages.research_trace.recorder import Recorder, recording
from packages.research_trace.native import NativeCapture
from packages.research_trace.transport import CaptureWriter
from packages.research_trace.samples import TIMING_TAPE, CROSS_SECTION_TAPE
from packages.tradegrad_adapter.results import export_native_result
from src.evaluate import Date2Symbols, reward_function

name, source_path, expected_sha, parameters_json, capture_json, comment = sys.argv[1:]
path = Path(source_path)
assert path.name in {'research_time.py', 'research_cross.py'}
assert not path.is_symlink()
original = path.read_bytes()
assert hashlib.sha256(original).hexdigest() == expected_sha
assert comment in ('', '\n# Comparison fixture: descriptive comment, no executable change.\n')
source = original.decode() + comment
code_hash = hashlib.sha256(source.encode()).hexdigest()
tape = TIMING_TAPE if name == 'daily_timing' else CROSS_SECTION_TAPE
data = {}
for item in tape.instruments:
    closes = list(item.closes)
    data[item.instrument_id] = pd.DataFrame({
        'date': pd.to_datetime(tape.dates).tz_localize(tape.market_timezone) + pd.Timedelta(hours=16),
        'open': closes, 'high': [x + .1 for x in closes], 'low': [x - .1 for x in closes],
        'close': closes, 'volume': item.volumes, 'symbol': item.instrument_id,
    })
context = ({'all_symbols': sorted(data), 'date2symbols': Date2Symbols(dict(tape.universe))}
           if name == 'cross_sectional' else {'target_symbol': 'target_symbol'})
context['tradegrad_parameters'] = json.loads(parameters_json)
frames = []
capture = CaptureConfig.model_validate(json.loads(capture_json))
writer = CaptureWriter(capture, frames.append)
recorder = Recorder(code_sha256=code_hash, config=capture)
observer = NativeCapture(recorder, writer.append)
recorder._on_record = observer.append
engine_kwargs = dict(initial_cash=tape.initial_cash, commission_rate=0,
                    stamp_tax_rate=0, slippage=0, history_depth=252,
                    lot_size=1, timezone=tape.market_timezone)
with tempfile.TemporaryDirectory(prefix='tradegrad-maintained-comparison-') as temporary:
    executed = Path(temporary) / path.name
    executed.write_text(source)
    with recording(recorder):
        result = aq.run_backtest(data=data, strategy_source=str(executed), context=context,
                                 show_progress=False, **engine_kwargs)
        observer.finalize(result, market_timezone=tape.market_timezone)
writer.finish(recorder.summary())
native = result.metrics_df['value']
metrics = {
    'sharpe_ratio': float(native['sharpe_ratio']),
    'annualized_return': float(native['annualized_return']) * 100,
    'max_drawdown': float(native['max_drawdown_pct']),
    'trade_per_day': float(native['closed_trade_count']) / (native['duration'].days / 365 * 243),
    'win_rate': float(native['win_rate']),
    'return_per_trade': float(native['avg_return_pct']),
}
metrics = {key: round(value, 2) for key, value in metrics.items()}
performance = {'combined_score': round(reward_function(metrics, market='US'), 2),
               'metrics': metrics, 'market': 'US', 'profile': 'corrected_explicit_config'}
bundle = export_native_result(
    result, performance, identity={'strategy_sha256': code_hash},
    window={'start': tape.dates[0], 'end': '2024-01-12', 'interval': 'half_open'},
    engine_kwargs=engine_kwargs, currency='USD',
)
print('COMPARISON_NATIVE_RESULT:' + json.dumps(
    {'source': source, 'code_sha256': code_hash, 'frames': frames, 'bundle': bundle},
    sort_keys=True, allow_nan=False,
))
"""


def run_native(
    name="cross_sectional", *, parameters=None, capture=None, comment_only=False
):
    requested = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    required = os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1"
    if not requested and not required:
        pytest.skip("Actual comparison engine runs are opt-in")
    if not requested:
        pytest.fail("Required comparison checks need TRADEGRAD_NATIVE_TEST_PYTHON")
    source = (
        ROOT
        / "seed_strategies"
        / ("research_time.py" if name == "daily_timing" else "research_cross.py")
    )
    assert source.read_text() == template_source(name)
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == CONTROLLED_SOURCE_SHA256[name]
    )
    completed = subprocess.run(
        [
            requested,
            "-B",
            "-c",
            SCRIPT,
            name,
            str(source),
            CONTROLLED_SOURCE_SHA256[name],
            json.dumps(parameters or {}),
            json.dumps(capture or {"mode": "research"}),
            COMMENT_ONLY if comment_only else "",
        ],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TRADEGRAD_DATA_WORKERS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        },
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-8192:].decode(errors="replace")
    rows = [
        line
        for line in completed.stdout.decode().splitlines()
        if line.startswith("COMPARISON_NATIVE_RESULT:")
    ]
    assert len(rows) == 1
    return json.loads(rows[0].partition(":")[2])
