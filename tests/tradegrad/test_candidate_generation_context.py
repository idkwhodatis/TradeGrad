"""Actual optimizer provenance is captured, not guessed from a code parent."""

from __future__ import annotations

import json

import pytest
from test_optimizer_recovery import configuration, run

from packages.tradegrad_adapter.checkpoint import (
    CheckpointError,
    CheckpointStore,
    digest,
    state_artifacts,
    validate_candidate_context,
    validate_state,
)
from packages.tradegrad_adapter.optimizer import _lineage_context


def test_generated_context_is_immutable_and_links_actual_calls(tmp_path):
    result = run(tmp_path)
    assert result.candidates[0]["lineage_context"] == _lineage_context(
        generation_status="seed"
    )
    earlier = set()
    calls = {row["call_id"]: row for row in result.model_calls}
    for candidate in result.candidates:
        context = candidate["lineage_context"]
        validate_candidate_context(context, earlier, candidate_kind=candidate["kind"])
        if candidate["kind"] == "generated":
            assert context["reference_candidate_keys"]
            assert context["revision_mode"] == "large"
            assert context["generation_status"] == "generated"
            assert (
                context["feedback"]
                == "Prefer a stable diversified revision; preserve risk controls."
            )
            assert context["feedback_sha256"] == digest(context["feedback"].encode())
            assert context["model_call_ids"]
            assert all(key in calls for key in context["model_call_ids"])
            assert any(":backward:" in key for key in context["model_call_ids"])
            assert any(":generate:" in key for key in context["model_call_ids"])
        earlier.add(candidate["candidate_id"])
    state = CheckpointStore.load(
        result.checkpoint, expected_input_digest=result.checkpoint.input_digest
    )
    assert [row["lineage_context"] for row in state["candidates"]] == [
        row["lineage_context"] for row in result.candidates
    ]
    lineage = json.loads(state_artifacts(state)["lineage.json"])
    assert [row["lineage_context"] for row in lineage] == [
        row["lineage_context"] for row in result.candidates
    ]


def test_legacy_context_absence_preserves_checkpoint_projection(tmp_path):
    result = run(tmp_path, config=configuration(0))
    state = CheckpointStore.load(
        result.checkpoint, expected_input_digest=result.checkpoint.input_digest
    )
    for candidate in state["candidates"]:
        candidate.pop("lineage_context")
    validate_state(state, expected_input_digest=result.checkpoint.input_digest)
    assert all(
        set(row) == {"candidate_id", "parent_id", "kind", "iteration", "repair_index"}
        for row in json.loads(state_artifacts(state)["lineage.json"])
    )


def test_feedback_preview_is_explicit_and_unicode_safe():
    feedback = "研究" * 40000
    context = _lineage_context(
        generation_status="generated", feedback=feedback, revision_mode="small"
    )
    assert context["feedback_truncated"] is True
    assert context["feedback_sha256"] == digest(feedback.encode())
    assert len(context["feedback"].encode()) <= 64 * 1024
    assert feedback.startswith(context["feedback"])
    validate_candidate_context(context, set(), candidate_kind="generated")


@pytest.mark.parametrize(
    "change",
    [
        {"reference_candidate_keys": ["not-an-earlier-candidate"]},
        {"reference_candidate_keys": ["a", "a"]},
        {"feedback_sha256": "f" * 64},
        {"generation_status": "repair"},
        {"feedback_truncated": 1},
        {"model_call_ids": ["unsafe/path"]},
        {"extra": True},
    ],
)
def test_invalid_context_is_rejected(change):
    context = _lineage_context(
        generation_status="generated",
        references=["a"],
        feedback="observed",
        revision_mode="small",
    )
    context.update(change)
    with pytest.raises(CheckpointError):
        validate_candidate_context(context, {"a"}, candidate_kind="generated")


def test_repair_has_actual_error_but_no_invented_algorithm_references(tmp_path):
    from test_optimizer_recovery import evaluate

    result = run(
        tmp_path,
        config=configuration(0),
        evaluate=lambda code, candidate, window: (
            {"error": "observed engine error"}
            if candidate == "candidate-0000"
            else evaluate(code, candidate, window)
        ),
    )
    context = result.candidates[1]["lineage_context"]
    assert context["generation_status"] == "repair"
    assert context["feedback"] == "observed engine error"
    assert context["reference_candidate_keys"] == []
    assert context["revision_mode"] is None
    assert context["model_call_ids"] == ["repair:candidate-0000:0000"]


def test_unchanged_fallback_keeps_failure_status_and_true_base(tmp_path, monkeypatch):
    from src.optimizer import TradeTGD

    def failing_step(self, _feedback):
        raise ValueError("synthetic generation failure")

    monkeypatch.setattr(TradeTGD, "step", failing_step)
    result = run(tmp_path, config=configuration(1))
    seed, child = result.candidates
    assert child["parent_id"] == seed["candidate_id"]
    assert child["code"] == seed["code"]
    assert child["lineage_context"]["generation_status"] == "unchanged_fallback"
    assert child["lineage_context"]["reference_candidate_keys"] == [
        seed["candidate_id"]
    ]
    assert not any(
        ":generate:" in key for key in child["lineage_context"]["model_call_ids"]
    )


def test_large_memory_context_remains_within_existing_run_budget():
    calls = [f"iteration:0001:backward:{index:04d}" for index in range(1002)]
    context = _lineage_context(generation_status="generated", model_call_ids=calls)
    assert context["model_call_ids"] == calls


def test_checkpoint_context_cannot_invent_a_model_call(tmp_path):
    result = run(tmp_path, config=configuration(1))
    state = CheckpointStore.load(
        result.checkpoint, expected_input_digest=result.checkpoint.input_digest
    )
    state["candidates"][1]["lineage_context"]["model_call_ids"].append(
        "iteration:0000:generate:9999"
    )
    with pytest.raises(CheckpointError, match="unrecorded model call"):
        validate_state(state, expected_input_digest=result.checkpoint.input_digest)
