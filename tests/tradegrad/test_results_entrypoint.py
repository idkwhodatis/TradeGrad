"""Trusted unchanged-seed entrypoint checks, not a host candidate execution path."""

import base64
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from packages.tradegrad_adapter.results import RESULT_PREFIX_V2, validate_envelope
from runtimes.evaluator.entrypoint import _evaluation_config, main
from src.market import backtest_kwargs

ROOT = Path(__file__).parents[2]


def config():
    overrides = backtest_kwargs("US")
    overrides.pop("show_progress")
    return {
        "engine_overrides": overrides,
        "window": {"start": "2024-02-02", "end": "2024-04-27", "role": "train"},
        "warmup": {"sessions": 20, "history_policy": "require"},
        "benchmark": None,
        "run_config_sha256": "a" * 64,
    }


@pytest.mark.parametrize("version", [1, 2])
def test_actual_original_seed_entrypoint_wire(tmp_path, capsys, version):
    inputs, outputs = tmp_path / "input", tmp_path / "output"
    (inputs / "data").mkdir(parents=True)
    shutil.copyfile(ROOT / "seed_strategies/us_time.py", inputs / "strategy.py")
    shutil.copyfile(
        ROOT / "tests/baseline/fixtures/US/INDEX.csv", inputs / "data/INDEX.csv"
    )
    request = {"task": "us_time", "start_date": "2024-02-02", "end_date": "2024-04-27"}
    if version == 2:
        request["evaluation_config"] = config()
    (inputs / "request.json").write_text(json.dumps(request))
    main(inputs, outputs)
    records = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("TRADEGRAD_RESULT_")
    ]
    assert len(records) == 1
    if version == 1:
        assert records[0].startswith("TRADEGRAD_RESULT_V1:")
        decoded = base64.b64decode(records[0].split(":", 1)[1], validate=True)
        assert decoded == (outputs / "metrics.json").read_bytes()
    else:
        assert records[0].startswith(RESULT_PREFIX_V2)
        envelope = validate_envelope(
            base64.b64decode(records[0][len(RESULT_PREFIX_V2) :], validate=True)
        )
        assert envelope["metrics"] == json.loads(
            (outputs / "metrics.json").read_bytes()
        )
        bundle = envelope["bundle"]
        assert bundle["window"]["warmup"]["sessions"] == 20
        assert bundle["identity"]["run_config_sha256"] == "a" * 64
        assert {item["path"] for item in bundle["identity"]["inputs"]} == {
            "strategy.py",
            "request.json",
            "data/INDEX.csv",
        }
        assert bundle["legacy_performance"]["reward_parameters"]["freq_floor"] == 0.0187


@pytest.mark.parametrize(
    "change",
    [
        {"benchmark": {"symbol": "SPY"}},
        {"run_config_sha256": "bad"},
        {"warmup": {"sessions": 20, "history_policy": "source_cold_start"}},
        {"window": {"start": "2024-02-01", "end": "2024-04-27", "role": "train"}},
        {"engine_overrides": {"magic": True}},
        {"unrecognized": True},
    ],
)
def test_configuration_is_never_silently_ignored(change):
    value = config()
    value.update(change)
    with pytest.raises(ValueError):
        _evaluation_config(value, date(2024, 2, 2), date(2024, 4, 27))
