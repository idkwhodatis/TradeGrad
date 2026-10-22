"""Mandatory integration gates: these execute installed AKQuant, never a fake engine."""
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from script.validate_baseline import run
from src.loss import run_time_window_backtest
from src.market import backtest_kwargs
from src.run_one import build_loss
from src.tasks import TASKS

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("real-akquant")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AKQUANT_DATA_CACHE", str(root / "cache"))
        first = run(root / "cold-attempt")
        second = run(root / "warm-attempt")
    for task in TASKS:
        assert first["results"][task].get("metrics") == second["results"][task].get("metrics")
        assert first["results"][task].get("combined_score") == second["results"][task].get("combined_score")
    return first


@pytest.mark.parametrize("task", ["cn_cross", "cn_time", "us_cross", "us_time"])
def test_four_original_tasks_execute_real_engine(baseline, task):
    assert baseline["versions"]["akquant"] == "0.3.27"
    assert baseline["llm_calls"] == 0
    actual = baseline["results"][task]
    assert "error" not in actual, actual
    expected = json.loads((Path(__file__).parent / "expected.json").read_text())[task]
    assert actual["market"] == expected["market"]
    assert actual["profile"] == "corrected_explicit_config"
    assert actual["combined_score"] == pytest.approx(expected["combined_score"], abs=0.01)
    assert actual["metrics"] == pytest.approx(expected["metrics"], abs=0.01)
    assert actual["metrics"]["trade_per_day"] > 0


@pytest.mark.parametrize("task", sorted(TASKS))
def test_training_entry_passes_explicit_market(task, monkeypatch):
    monkeypatch.setenv("AKQUANT_MARKET", "CN" if task.startswith("us") else "US")
    loss = build_loss(TASKS[task], None, 0.3)
    assert loss.market == TASKS[task]["market"]
    assert loss.max_workers == 1


def test_market_profiles_do_not_leak():
    cn, us = backtest_kwargs("CN"), backtest_kwargs("US")
    assert cn["t_plus_one"] is True and us["t_plus_one"] is False
    assert cn["stamp_tax_rate"] == 0.001 and us["stamp_tax_rate"] == 0
    assert cn["lot_size"] == 100 and us["lot_size"] == 1
    assert cn["timezone"] == "Asia/Shanghai"
    assert us["timezone"] == "America/New_York"
    assert us["risk_config"]["enable_short_sell"] is True
    assert "risk_config" not in cn
    assert "timezone" not in backtest_kwargs("US", "source_compat")
    us["risk_config"]["enable_short_sell"] = False
    assert backtest_kwargs("US")["risk_config"]["enable_short_sell"] is True


def test_simultaneous_cn_us_evaluations_have_independent_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("AKQUANT_MARKET", "WRONG")
    def evaluate(market):
        task=TASKS[f"{market.lower()}_time"]
        return run_time_window_backtest(task["initial"], tmp_path / market, task["evaluate"], str(FIXTURE / market / "INDEX.csv"), "target_symbol", "2024-01-02", "2024-04-26", market)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(evaluate, ("CN", "US")))
    assert [result.get("market") for result in results] == ["CN", "US"]
    assert all("error" not in result for result in results)
    for market in ("CN", "US"):
        assert json.loads((tmp_path / market / "metrics.json").read_text())["market"] == market
        assert json.loads((tmp_path / market / "job.json").read_text())["status"] == "succeeded"


def test_evaluator_cli_requires_market(tmp_path):
    result=subprocess.run([sys.executable, str(ROOT / "src/evaluate_time.py")], capture_output=True, text=True)
    assert result.returncode != 0
    assert "--market" in result.stderr


def test_frozen_inputs_and_original_seeds():
    manifest=json.loads((FIXTURE / "manifest.json").read_text())
    assert manifest["source_commit"] == "baf73ee56353de8996731139f6721b62f4b26da7"
    for filename, digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURE / filename).read_bytes()).hexdigest() == digest


def test_standalone_scoring_propagates_failed_evaluation(tmp_path, monkeypatch):
    from src import eval_runs
    class FailedReward:
        def __init__(self, *args, **kwargs):
            assert kwargs["market"] == "US"
            assert kwargs["freq_floor"] == 0
        def forward(self, strategy):
            return {"error": "deliberate evaluation failure"}
    monkeypatch.setattr(eval_runs, "TimeStrategyReward", FailedReward)
    with pytest.raises(RuntimeError, match="deliberate evaluation failure"):
        eval_runs.evaluate_one("us_time", TASKS["us_time"]["initial"], tmp_path / "attempt")
