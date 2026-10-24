"""Actual native admission of the complete frozen browser comparison fixture.

This is not HTTP, PostgreSQL or container acceptance. The pytest-free native
child runs the maintained source through the real evaluator entrypoint, with
the same CSV, split windows, warmup, market profile and variant as Playwright.
No short manual tape, synthetic result or independent-observation reward bypass
can certify the optimization fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "frontend/tests/fixtures"
COMPARISON = json.loads((FIXTURES / "mechanism-comparison.json").read_text())
CSV_METADATA = json.loads((FIXTURES / "research-cn.json").read_text())

SCRIPT = r"""
import base64, contextlib, csv, hashlib, io, json, sys, tempfile
from pathlib import Path
import pandas as pd
from packages.research_trace.contracts import DecisionRecord
from packages.research_trace.templates import native_contract
from packages.research_trace.transport import (
    CaptureReceiver, TRACE_PREFIX, iter_capture_records,
)
from packages.tradegrad_adapter.config import DateWindow, MarketConfig, canonical_bytes
from packages.tradegrad_adapter.results import validate_envelope
from packages.tradegrad_adapter.windows import stage_csv_window
from runtimes.evaluator.entrypoint import main

source_path, source_sha, fixture_path, fixture_sha, contract_json, split, variant = sys.argv[1:]
source, fixture = Path(source_path), Path(fixture_path)
assert source.name == 'research_time.py' and not source.is_symlink()
assert fixture.name == 'research-cn.csv' and not fixture.is_symlink()
assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
assert hashlib.sha256(fixture.read_bytes()).hexdigest() == fixture_sha
contract = json.loads(contract_json)
window = contract['windows'][split]
parameters = {} if variant == 'baseline' else {
    'max_weight': 0.15 if variant == 'inadmissible_old_variant'
    else contract['allocation_and_fees']['max_weight'],
}
engine = MarketConfig.for_market('CN', 'corrected_explicit_config').engine_kwargs()
if variant != 'baseline':
    engine['commission_rate'] = contract['allocation_and_fees']['commission_rate']

# Reproduce write_bundle's evaluator CSV for these explicitly mapped upload
# fields. Upload normalization casts every numeric Bar field to float; dates
# remain session labels. The complete 76-row source is checked before staging.
fields = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount']
frame = pd.read_csv(fixture, usecols=fields)
normalized = io.StringIO(newline='')
writer = csv.DictWriter(normalized, fieldnames=fields)
writer.writeheader()
for row in frame.to_dict('records'):
    writer.writerow({key: row[key] if key == 'date' else float(row[key]) for key in fields})
staged = stage_csv_window(
    normalized.getvalue().encode(), listing_id='frozen-browser-fixture',
    window=DateWindow.model_validate(window),
    warmup_sessions=contract['warmup_sessions'], history_policy='require',
)
config = {
    'engine_overrides': engine,
    'window': {**window, 'role': split},
    'warmup': {'sessions': contract['warmup_sessions'], 'history_policy': 'require'},
    'benchmark': None,
    # A local test binding, not a claim of a persisted API run identity.
    'run_config_sha256': hashlib.sha256(canonical_bytes(contract)).hexdigest(),
    'research_capture': {'mode': 'research'},
    'strategy_contract': native_contract('daily_timing', parameters=parameters).model_dump(mode='json'),
}
assert 'result_policy' not in config
request = {
    'task': 'cn_time', 'market': 'CN', 'profile': 'corrected_explicit_config',
    'start_date': window['start'], 'end_date': window['end'], 'evaluation_config': config,
}
report = {
    'source_sha256': source_sha, 'fixture_sha256': fixture_sha,
    'fixture_rows': len(frame), 'staged_sha256': hashlib.sha256(staged.content).hexdigest(),
    'warmup_sessions': staged.report.staged_warmup_sessions,
    'evaluation_rows': staged.report.evaluation_rows,
}
with tempfile.TemporaryDirectory(prefix='native-browser-comparison-') as directory:
    root = Path(directory).resolve(); inputs = root / 'input'; output = root / 'output'
    (inputs / 'data').mkdir(parents=True)
    (inputs / 'strategy.py').write_bytes(source.read_bytes())
    (inputs / 'data/INDEX.csv').write_bytes(staged.content)
    (inputs / 'request.json').write_bytes(canonical_bytes(request))
    stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout):
            main(inputs, output)
    except ValueError as error:
        if variant != 'inadmissible_old_variant':
            raise
        assert str(error) == 'No trades executed, invalid strategy.'
        assert 'TRADEGRAD_RESULT_' not in stdout.getvalue()
        report.update(status='rejected', error=str(error))
    else:
        lines = stdout.getvalue().splitlines()
        results = [line for line in lines if line.startswith('TRADEGRAD_RESULT_')]
        assert len(results) == 1 and results[0].startswith('TRADEGRAD_RESULT_V2:')
        envelope = validate_envelope(base64.b64decode(results[0].split(':', 1)[1], validate=True))
        bundle = envelope['bundle']
        receiver = CaptureReceiver(root, config['research_capture'], source='strategy_emitted')
        for line in lines:
            if line.startswith(TRACE_PREFIX):
                receiver.accept(json.loads(line[len(TRACE_PREFIX):]))
        captured = receiver.result()
        records = [DecisionRecord.model_validate(record) for record in iter_capture_records(
            captured['research_capture_path'], captured['research_capture_sha256'],
            captured['research_capture_size_bytes'], config['research_capture'],
        )]
        decisions = {
            record.data_event_id: {
                'pre_state': record.pre_state.model_dump(mode='json', include={'cash', 'equity', 'positions'}),
                'targets': [stage.constrained_plan for stage in record.stages if stage.constrained_plan is not None],
            }
            for record in records if record.pre_state is not None
        }
        report.update(
            status='succeeded', metrics=envelope['metrics'],
            engine=bundle['engine']['requested_kwargs'],
            closed_trades=len(bundle['closed_trades']['rows']),
            fills=len(bundle['fills']['rows']), decisions=decisions,
            capture_mode=captured['research_capture']['capture']['mode'],
        )
print('BROWSER_COMPARISON_NATIVE:' + json.dumps(report, allow_nan=False, sort_keys=True))
"""


def execute(split, variant):
    python = os.environ.get("TRADEGRAD_NATIVE_TEST_PYTHON")
    if not python:
        if os.environ.get("TRADEGRAD_NATIVE_TEST_REQUIRED") == "1":
            pytest.fail("Frozen browser admission requires the native interpreter")
        pytest.skip("Actual frozen browser fixture admission is opt-in")
    source = ROOT / "seed_strategies/research_time.py"
    assert (
        hashlib.sha256(source.read_bytes()).hexdigest()
        == CONTROLLED_SOURCE_SHA256["daily_timing"]
    )
    process = subprocess.run(
        [
            python,
            "-B",
            "-c",
            SCRIPT,
            str(source),
            CONTROLLED_SOURCE_SHA256["daily_timing"],
            str(FIXTURES / "research-cn.csv"),
            CSV_METADATA["sha256"],
            json.dumps(COMPARISON),
            split,
            variant,
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
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr[-8192:] + process.stdout[-2000:]
    rows = [
        line.partition(":")[2]
        for line in process.stdout.splitlines()
        if line.startswith("BROWSER_COMPARISON_NATIVE:")
    ]
    assert len(rows) == 1
    return json.loads(rows[0])


@pytest.mark.parametrize("split,evaluation_rows", [("train", 25), ("validation", 24)])
def test_full_browser_comparison_pair_passes_unchanged_native_reward_gate(
    split, evaluation_rows
):
    baseline, changed = (execute(split, side) for side in ("baseline", "changed"))
    for result in (baseline, changed):
        assert result["status"] == "succeeded"
        assert result["fixture_rows"] == CSV_METADATA["rows"] == 76
        assert result["warmup_sessions"] == COMPARISON["warmup_sessions"] == 20
        assert result["evaluation_rows"] == evaluation_rows
        assert result["closed_trades"] > 0 and result["fills"] > 0
        assert result["metrics"]["combined_score"] is not None
        assert result["metrics"]["metrics"]["trade_per_day"] > 0
        assert result["capture_mode"] == "research"
        assert result["engine"]["lot_size"] == 100
        assert result["engine"]["t_plus_one"] is True
    for key in ("source_sha256", "fixture_sha256", "staged_sha256"):
        assert baseline[key] == changed[key]
    assert baseline["engine"]["commission_rate"] == 0.0005
    assert (
        changed["engine"]["commission_rate"]
        == COMPARISON["allocation_and_fees"]["commission_rate"]
        == 0.001
    )
    assert baseline["decisions"].keys() == changed["decisions"].keys()
    assert any(
        baseline["decisions"][key]["pre_state"]
        != changed["decisions"][key]["pre_state"]
        for key in baseline["decisions"]
    )
    assert any(
        baseline["decisions"][key]["targets"] != changed["decisions"][key]["targets"]
        for key in baseline["decisions"]
    )


def test_previous_15_percent_variant_remains_ineligible_for_optimization():
    result = execute("train", "inadmissible_old_variant")
    assert result["status"] == "rejected"
    assert result["error"] == "No trades executed, invalid strategy."
