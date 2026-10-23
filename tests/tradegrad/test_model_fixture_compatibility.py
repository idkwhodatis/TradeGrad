"""Sealed M5-era fixture bytes remain immutable across operator-model admission.

Fixtures originate from commit 02e9718d838c5e0d1c169435ab5eda098cf291dd:
native serialized by that commit's RunConfig; portable copied from its checked-in
frontend fixture and canonically encoded. No historic row or hash is rewritten.
"""

import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from packages.tradegrad_adapter.checkpoint import CheckpointStore
from packages.tradegrad_adapter.config import ModelConfig, RunConfig, canonical_bytes
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.model_calls import FrozenModel
from packages.tradegrad_adapter.optimizer import run_optimization
from packages.tradegrad_adapter.portable_config import PortableRunConfig
from packages.tradegrad_adapter.strategy_package import config_projection

ROOT = Path(__file__).parent / "fixtures"
MANIFEST = json.loads((ROOT / "sealed-config-hashes.json").read_bytes())


@pytest.mark.parametrize(
    "name,contract",
    [
        ("sealed-native-config.json", RunConfig),
        ("sealed-portable-config.json", PortableRunConfig),
    ],
)
def test_sealed_fixture_canonical_bytes_and_hash_are_unchanged(name, contract):
    raw = (ROOT / name).read_bytes()
    expected = MANIFEST["sha256"][name]
    assert hashlib.sha256(raw).hexdigest() == expected
    loaded = contract.from_verified_json(raw, expected)
    assert loaded.canonical_bytes() == raw
    assert loaded.sha256 == expected
    assert loaded.model.model_dump(mode="json") == {
        "provider": "fixture",
        "model": "deterministic-fixture-v1",
        "credential_ref": None,
        "temperature": 0.0,
    }
    assert config_projection(loaded.model_dump(mode="json")) == config_projection(
        json.loads(raw)
    )


def test_legacy_fixture_nondefault_new_parameter_cannot_be_silently_dropped():
    with pytest.raises(ValueError):
        ModelConfig(max_output_tokens=128)


def test_sealed_config_remains_exact_in_final_selection_and_package_projection():
    source_bytes = (ROOT / "sealed-native-config.json").read_bytes()
    source = json.loads(source_bytes)
    original_final = {
        "schema_version": "tradegrad.final-evaluation.v1",
        "execution_mode": "final_evaluation",
        "selection_id": str(UUID(int=1)),
        "split_version_id": str(UUID(int=2)),
        "split_sha256": "d" * 64,
        "selected_strategy": source["seed_strategy"],
        "source_config": source,
        "source_config_sha256": MANIFEST["sha256"]["sealed-native-config.json"],
        "holdout": source["windows"]["holdout"],
        "environment_digest": source["environment_digest"],
        "max_runtime_seconds": 120,
    }
    serialized = canonical_bytes(original_final)
    final = FinalEvaluationConfig.model_validate_json(serialized)
    assert final.canonical_bytes() == serialized
    assert final.sha256 == hashlib.sha256(serialized).hexdigest()
    assert final.source_config.canonical_bytes() == source_bytes
    # Execution-only package projection must not alter the original selection hash.
    projection = config_projection(final.model_dump(mode="json"))
    assert projection == config_projection(original_final)
    assert (
        projection["source_config_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    )
    assert "model" not in projection["source_config"]


def test_checkpoint_with_original_fixture_input_bytes_still_replays(tmp_path):
    raw = (ROOT / "sealed-native-config.json").read_bytes()
    old_config = json.loads(raw)
    events = []

    def evaluate(*_args):
        events.append("synthetic evaluation callback, not strategy execution")
        return {"combined_score": 1.0}

    args = {
        "initial_code": MANIFEST["seed"],
        "windows": [{"split": "train", **old_config["windows"]["train"]}],
        "evaluate": evaluate,
        "model": FrozenModel([]),
    }
    paused = run_optimization(
        config=old_config, root=tmp_path / "original", control=lambda: "pause", **args
    )
    assert paused.status == "paused" and not events
    state = CheckpointStore.load(
        paused.checkpoint, expected_input_digest=paused.checkpoint.input_digest
    )
    assert canonical_bytes(state["inputs"]["config"]) == raw
    current = RunConfig.from_verified_json(
        raw, MANIFEST["sha256"]["sealed-native-config.json"]
    )
    restored = run_optimization(
        config=current.model_dump(mode="json"),
        root=tmp_path / "restored",
        resume=paused.checkpoint,
        **args,
    )
    assert restored.status == "succeeded"
    assert len(events) == 1 and restored.model_calls == []
