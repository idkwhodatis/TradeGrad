"""Compare adapter choices directly with the source TradeTGD, not a reimplementation."""

from __future__ import annotations

import json
import random
import re

import numpy as np
import pytest
from test_optimizer_recovery import CODE, WINDOWS, configuration, evaluate

from packages.tradegrad_adapter.checkpoint import CheckpointStore, canonical_json
from packages.tradegrad_adapter.model_calls import FrozenModel, ModelResponse
from packages.tradegrad_adapter.optimizer import (
    _aggregate,
    _normalized_outcome,
    run_optimization,
)
from src.optimizer import TradeTGD
from src.prompt import OPTIMIZER_SYSTEM_PROMPT, OPTIMIZER_SYSTEM_PROMPT_SMALL_REVISION
from src.strategy import Strategy


class ScriptedQueries:
    def __init__(self):
        self.calls = []

    def query(self, user_prompt, system_prompt=None):
        self.calls.append((user_prompt, system_prompt))
        if system_prompt in {
            OPTIMIZER_SYSTEM_PROMPT,
            OPTIMIZER_SYSTEM_PROMPT_SMALL_REVISION,
        }:
            parent_code = re.search(
                r"```python\n(.*?)\n```", user_prompt, re.DOTALL
            ).group(1)
            old_value = re.search(r"value = (\d+)", parent_code).group(0)
            replacement = f"value = {len(self.calls)}"
            if system_prompt == OPTIMIZER_SYSTEM_PROMPT_SMALL_REVISION:
                return f"<<<<<<< SEARCH\n{old_value}\n=======\n{replacement}\n>>>>>>> REPLACE"
            return "```python\n" + parent_code.replace(old_value, replacement) + "\n```"
        return f"Fixed summary or gradient number {len(self.calls)}."


class ScriptedProvider(FrozenModel):
    def __init__(self):
        super().__init__({})
        self.queries = ScriptedQueries()

    def complete(self, request):
        return ModelResponse(
            self.queries.query(request.user_prompt, request.system_prompt), 20, 20
        )


@pytest.mark.parametrize("sample_method", ["power_law", "uniform", "top_k"])
@pytest.mark.parametrize("large_probability", [0.0, 0.55, 1.0])
def test_exact_source_sampling_memory_revision_and_reward(
    tmp_path, monkeypatch, sample_method, large_probability
):
    monkeypatch.setenv("TRADEGRAD_LLM_WORKERS", "1")
    cfg = configuration(6)
    cfg["optimizer"].update(
        sample_method=sample_method, large_revision_prob=large_probability
    )
    provider = ScriptedProvider()
    actual = run_optimization(
        config=cfg,
        initial_code=CODE,
        windows=WINDOWS,
        evaluate=evaluate,
        root=tmp_path / "adapter",
        model=provider,
    )
    settings = cfg["optimizer"]
    random.seed(settings["seed"])
    np.random.seed(settings["seed"])
    query = ScriptedQueries()
    source = TradeTGD(
        query,
        sample_size=settings["sample_size"],
        sample_method="topk" if sample_method == "top_k" else sample_method,
        large_revision_prob=large_probability,
        memory_window=settings["memory_window"],
    )

    def strategy(number, code):
        identifier = f"candidate-{number:04d}"
        perf = _aggregate(
            [
                _normalized_outcome(evaluate(code, identifier, window), window)
                for window in WINDOWS
            ],
            settings["worst_ratio"],
        )
        return Strategy(
            identifier,
            code,
            tmp_path / "source" / identifier,
            performance=json.loads(canonical_json(perf)),
        )

    current = strategy(0, CODE)
    chain = [(current.id, None, current.code)]
    for step in range(settings["max_iterations"]):
        grad = source.backward(current, step)
        code = source.step(grad)
        current = strategy(step + 1, code)
        chain.append((current.id, source.pending_parent_id, current.code))
    source.update_database(current)
    assert [
        (c["candidate_id"], c["parent_id"], c["code"]) for c in actual.candidates
    ] == chain
    assert [
        (r["user_prompt"], r["system_prompt"]) for r in actual.model_calls
    ] == query.calls
    saved = CheckpointStore.load(
        actual.checkpoint, expected_input_digest=actual.checkpoint.input_digest
    )
    assert saved["optimizer"]["archive"] == [s.id for s in source.database]
    assert saved["optimizer"]["memory_ids"] == [s.id for s in source.memory_storage]
    assert saved["optimizer"]["memory_summary"] == source.memory_summary
    assert actual.best_candidate_id == source.best().id
