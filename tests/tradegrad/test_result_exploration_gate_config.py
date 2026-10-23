"""Pure gate-config checks, not native execution or system acceptance evidence.

Read the gate as inert AST only: do not import its harness, load the candidate
strategy, start services, or invoke an SDK/engine.
"""

import ast
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.tradegrad_adapter.config import MarketConfig, RunConfig

SOURCE = (
    Path(__file__).resolve().parents[1] / "system/test_result_exploration_pipeline.py"
)


def _gate_config():
    tree = ast.parse(SOURCE.read_text())
    gate = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        == "test_real_recorded_indicators_reconstruct_from_artifact_after_worker_completion"
    )
    assignment = next(
        node
        for node in gate.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "config"
    )
    assert isinstance(assignment.value, ast.Dict)
    fields = {
        ast.literal_eval(key): value
        for key, value in zip(
            assignment.value.keys, assignment.value.values, strict=True
        )
    }
    # Only values returned by the real API and the immutable market profile are
    # substituted. All literal gate settings are validated verbatim, not copied.
    dynamic = {
        "dataset": {"id": "dataset-fixture", "sha256": "a" * 64},
        "seed_strategy": {"id": "strategy-fixture", "sha256": "b" * 64},
        "universe": {"id": "universe-fixture", "sha256": "c" * 64},
        "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
        "environment_digest": "d" * 64,
    }
    assert dynamic.keys() <= fields.keys()
    assert ast.unparse(fields["market_profile"]) == (
        "MarketConfig.for_market('CN').model_dump(mode='json')"
    )
    return {
        key: dynamic[key] if key in dynamic else ast.literal_eval(value)
        for key, value in fields.items()
    }


def test_recorded_indicator_gate_config_has_distinct_optimization_windows():
    config = RunConfig.model_validate(_gate_config())
    assert config.execution_mode == "optimization"
    assert config.task == "cn_time"
    assert config.optimizer.max_iterations == 0
    assert config.warmup.sessions == 20
    assert config.warmup.history_policy == "require"
    assert [name for name, _ in config.windows.items()] == ["train", "validation"]
    train, validation = config.windows.train, config.windows.validation
    assert validation is not None
    assert train.start == date(2024, 2, 1)
    assert train.end == validation.start == date(2024, 3, 15)
    assert validation.end == date(2024, 4, 20)
    assert not train.overlaps(validation)


def test_recorded_indicator_gate_rejects_missing_validation():
    config = _gate_config()
    del config["windows"]["validation"]
    with pytest.raises(ValidationError, match="distinct validation window"):
        RunConfig.model_validate(config)


def test_recorded_indicator_gate_rejects_overlapping_validation():
    config = _gate_config()
    config["windows"]["validation"]["start"] = config["windows"]["train"]["start"]
    with pytest.raises(ValidationError, match="chronological, disjoint"):
        RunConfig.model_validate(config)
