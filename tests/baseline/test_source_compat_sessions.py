"""Freeze actual original-seed legacy economics separately from corrected mode."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.job import evaluator_environment

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = r"""
import hashlib,json,sys,tempfile
from pathlib import Path
from importlib.metadata import version
from src import evaluate,evaluate_time
assert version('akquant')=='0.3.27'
root=Path.cwd();task=sys.argv[1]
assert task in ('cn_time','cn_cross','us_time','us_cross')
evidence=json.loads((root/'docs/evidence/native-session-alignment-20261023.json').read_text())
source=root/'seed_strategies'/f'{task}.py'
assert not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest()==evidence['results'][task]['source_sha256']
fixture=root/'tests/baseline/fixtures';manifest=json.loads((fixture/'manifest.json').read_text())
assert hashlib.sha256((fixture/'manifest.json').read_bytes()).hexdigest()==evidence['fixture_sha256']
for path,sha in manifest['files'].items():assert hashlib.sha256((fixture/path).read_bytes()).hexdigest()==sha
cross=task.endswith('cross');market=task[:2].upper();evaluator=evaluate if cross else evaluate_time
inputs={'date2symbol_dir':str(fixture/f'{market}_list'),'data_dir':str(fixture/market)} if cross else {'data_path':str(fixture/market/'INDEX.csv'),'symbol':'target_symbol'}
def table(df):
 return json.loads(df.drop(columns=[c for c in df.columns if c in ('id','order_id','trade_id','execution_id')]).reset_index(drop=True).to_json(orient='split'))
with tempfile.TemporaryDirectory() as tmp:
 result=evaluator.run_backtest(strategy_path=str(source),result_dir=tmp,start_time=manifest['start'],end_time=manifest['end'],market=market,profile='source_compat',**inputs)
 economics={'orders':table(result.orders_df),'fills':table(result.executions_df),'positions':table(result.positions_df),'trades':table(result.trades_df),'equity':json.loads(result.equity_curve.to_json()),'metrics':json.loads(result.metrics_df.to_json())}
 economic_sha256=hashlib.sha256(json.dumps(economics,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 print('LEGACY_SESSION:'+json.dumps({'economic_sha256':economic_sha256,'performance':json.loads((Path(tmp)/'metrics.json').read_text())}))
"""


@pytest.mark.parametrize("task", ["cn_time", "cn_cross", "us_time", "us_cross"])
def test_original_source_compat_economics_equal_pre_session_fix(task):
    evidence = json.loads(
        (ROOT / "docs/evidence/native-session-alignment-20261023.json").read_text()
    )
    process = subprocess.run(
        [sys.executable, "-c", SCRIPT, task],
        cwd=ROOT,
        env=evaluator_environment(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr + process.stdout[-2000:]
    actual = json.loads(
        next(
            line.removeprefix("LEGACY_SESSION:")
            for line in process.stdout.splitlines()
            if line.startswith("LEGACY_SESSION:")
        )
    )
    assert actual == evidence["results"][task]["before_legacy"]
    assert actual == evidence["results"][task]["after_legacy"]
