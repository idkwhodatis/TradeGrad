from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from packages.tradegrad_adapter.checkpoint import (
    CheckpointError,
    CheckpointStore,
)
from packages.tradegrad_adapter.model_calls import FrozenModel, ModelResponse
from packages.tradegrad_adapter.optimizer import (
    immutable_input_digest,
    run_optimization,
)

CODE = """from akquant import Strategy
class DemoStrategy(Strategy):
    ### EDIT START
    value = 0
    ### EDIT END
"""
WINDOWS = [
    {"split": "train", "start": "2021-01-01", "end": "2022-01-01"},
    {"split": "validation", "start": "2022-01-01", "end": "2023-01-01"},
]


def configuration(iterations=3):
    return {
        "optimizer": {
            "max_iterations": iterations,
            "sample_size": 3,
            "sample_method": "power_law",
            "large_revision_prob": 1.0,
            "memory_window": 2,
            "seed": 17,
            "worst_ratio": 0.3,
        },
        "budgets": {
            "max_model_calls": 100,
            "max_tokens": 2_000_000,
            "max_runtime_seconds": 120,
            "max_repairs": 1,
        },
        "model": {
            "provider": "fixture",
            "model": "frozen-response-fixture",
            "temperature": 0,
        },
    }


class FixedModel(FrozenModel):
    """Same deterministic outputs for a logical call, independent of process age."""

    def __init__(self):
        super().__init__({})

    def complete(self, request):
        self.requests.append(request)
        if ":generate:" in request.call_id or request.call_id.startswith("repair:"):
            number = request.logical_index + 1
            text = (
                "```python\n" + CODE.replace("value = 0", f"value = {number}") + "\n```"
            )
        else:
            text = "Prefer a stable diversified revision; preserve risk controls."
        return ModelResponse(text, 100, 50)


def evaluate(code, candidate_id, window):
    value = int(code.split("value = ")[1].splitlines()[0])
    return {
        "combined_score": value + 1,
        "evaluation_id": candidate_id + ":" + window["split"],
        "stderr": "warning: synthetic fixture",
        "result_bundle": {"available": True},
    }


def run(root, *, config=None, **kwargs):
    return run_optimization(
        config=config or configuration(),
        initial_code=CODE,
        windows=WINDOWS,
        evaluate=kwargs.pop("evaluate", evaluate),
        root=root,
        model=kwargs.pop("model", FixedModel()),
        **kwargs,
    )


def chain(result):
    return [
        (
            c["candidate_id"],
            c["parent_id"],
            c["code_sha256"],
            c["evaluation"],
            c["valid"],
        )
        for c in result.candidates
    ]


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary",
    [
        "before_code_write",
        "after_code_write",
        "before_model_request",
        "after_model_response",
        "before_evaluation",
        "after_evaluation",
        "before_manifest_publish",
        "after_manifest_publish",
        "before_checkpoint_pointer",
        "after_checkpoint_pointer",
    ],
)
def test_continuous_equals_crash_resume(tmp_path, boundary):
    expected = run(tmp_path / "continuous")
    pointer = []
    counts = {}

    def fault(label):
        counts[label] = counts.get(label, 0) + 1
        # Crash after the initial checkpoint, exercising generated candidates.
        target = (
            2
            if label
            in {
                "before_code_write",
                "after_code_write",
                "before_evaluation",
                "after_evaluation",
            }
            else 3
            if "manifest" in label or "pointer" in label
            else 1
        )
        if label == boundary and counts[label] == target:
            raise Crash(label)

    model = FixedModel()
    with pytest.raises(Crash):
        run(
            tmp_path / "interrupted",
            model=model,
            fault=fault,
            checkpoint_committed=pointer.append,
        )
    assert pointer
    before_requests = len(model.requests)
    resumed_model = FixedModel()
    actual = run(tmp_path / "interrupted", model=resumed_model, resume=pointer[-1])
    assert chain(actual) == chain(expected)
    assert actual.best_candidate_id == expected.best_candidate_id
    assert [r["call_id"] for r in actual.model_calls] == [
        r["call_id"] for r in expected.model_calls
    ]
    # Every fully persisted completed response is replayed, not sent again.
    assert before_requests + len(resumed_model.requests) == len(expected.model_calls)


def test_pause_resume_preserves_rng_and_archive_order(tmp_path):
    expected = run(tmp_path / "continuous")
    checks = 0

    def pause():
        nonlocal checks
        checks += 1
        return "pause" if checks == 13 else None

    paused = run(tmp_path / "paused", control=pause)
    assert paused.status == "paused"
    actual = run(tmp_path / "new-attempt", resume=paused.checkpoint)
    assert chain(actual) == chain(expected)
    assert actual.best_candidate_id == expected.best_candidate_id


def test_unknown_model_response_is_not_recharged(tmp_path):
    pointers = []
    model = FixedModel()

    def fault(label):
        if label == "before_model_response":
            raise Crash()

    with pytest.raises(Crash):
        run(tmp_path, model=model, checkpoint_committed=pointers.append, fault=fault)
    restored = FixedModel()
    actual = run(tmp_path, model=restored, resume=pointers[-1])
    assert len(model.requests) == 1
    assert not restored.requests
    assert actual.stop_reason == "model_call_outcome_unknown"
    assert actual.model_calls[0]["status"] == "started"
    assert actual.model_calls[0]["charged_tokens"] > 0


def test_failed_candidate_and_repair_are_immutable(tmp_path):
    def callback(code, candidate_id, window):
        if candidate_id == "candidate-0000":
            return {"error": "synthetic failure"}
        return evaluate(code, candidate_id, window)

    result = run(tmp_path, config=configuration(0), evaluate=callback)
    assert result.status == "succeeded"
    initial, repair = result.candidates
    assert initial["evaluation"]["error"] == "synthetic failure"
    assert repair["kind"] == "repair" and repair["parent_id"] == initial["candidate_id"]
    assert (
        tmp_path / "candidates" / initial["candidate_id"] / "strategy.py"
    ).read_text() == CODE
    assert repair["code"] != CODE
    assert initial["evaluation"]["windows"][0]["error"] == "synthetic failure"
    assert repair["evaluation"]["windows"][0]["result_bundle"]["available"]


def test_no_usable_strategy_and_budgets(tmp_path):
    cfg = configuration(0)
    cfg["budgets"]["max_repairs"] = 0
    failed = run(
        tmp_path / "invalid", config=cfg, evaluate=lambda *_: {"error": "not runnable"}
    )
    assert failed.status == "failed" and failed.stop_reason == "all_candidates_invalid"
    assert failed.best_candidate_id is None
    cfg = configuration()
    cfg["budgets"]["max_model_calls"] = 1
    limited = run(tmp_path / "calls", config=cfg)
    assert limited.stop_reason == "max_model_calls"
    assert len(limited.model_calls) == 1
    cfg["budgets"]["max_tokens"] = 1
    model = FixedModel()
    limited = run(tmp_path / "tokens", config=cfg, model=model)
    assert limited.stop_reason == "max_tokens" and not model.requests
    cfg["budgets"]["max_runtime_seconds"] = 0
    limited = run(tmp_path / "runtime", config=cfg)
    assert limited.stop_reason == "max_runtime_seconds"


def test_checkpoint_tamper_wrong_inputs_and_orphans(tmp_path):
    result = run(tmp_path)
    ref = result.checkpoint
    expected_digest = immutable_input_digest(configuration(), CODE, WINDOWS)
    state = CheckpointStore.load(ref, expected_input_digest=expected_digest)
    assert state["next_stage"] == "finished"
    (tmp_path / "checkpoints" / "999999999").mkdir()
    assert CheckpointStore.load(ref, expected_input_digest=expected_digest) == state
    with pytest.raises(CheckpointError, match="incompatible"):
        CheckpointStore.load(ref, expected_input_digest="0" * 64)
    manifest = Path(ref.manifest_path)
    (manifest.parent / "memory.json").write_text("{}")
    with pytest.raises(CheckpointError, match="mismatch"):
        CheckpointStore.load(ref, expected_input_digest=expected_digest)


def test_validation_does_not_change_reward_and_holdout_is_refused(tmp_path):
    result = run(
        tmp_path / "validation",
        config=configuration(0),
        evaluate=lambda code, cid, window: {
            "combined_score": 2.345 if window["split"] == "train" else 9999
        },
    )
    assert result.candidates[0]["evaluation"]["combined_score"] == 2.35
    assert result.candidates[0]["evaluation"]["validation_score"] == 9999
    with pytest.raises(ValueError, match="holdout"):
        run_optimization(
            config=configuration(),
            initial_code=CODE,
            windows=[{"split": "holdout"}],
            root=tmp_path / "holdout",
            model=FixedModel(),
            evaluate=evaluate,
        )


def test_process_ambient_random_state_is_unchanged(tmp_path):
    import numpy as np

    random.seed(123)
    np.random.seed(456)
    expected_python, expected_np = random.getstate(), np.random.get_state()
    run(tmp_path)
    assert random.getstate() == expected_python
    actual = np.random.get_state()
    assert (
        actual[0] == expected_np[0]
        and list(actual[1]) == list(expected_np[1])
        and actual[2:] == expected_np[2:]
    )


def test_model_secrets_sanitized_and_unknown_usage_is_reserved(tmp_path):
    class SecretModel(FixedModel):
        redactions = ("unit-test-private-value",)

        def complete(self, request):
            self.requests.append(request)
            return ModelResponse("Bearer token-to-redact unit-test-private-value")

    cfg = configuration(1)
    cfg["budgets"]["max_model_calls"] = 1
    result = run(tmp_path, config=cfg, model=SecretModel())
    record = result.model_calls[0]
    assert "unit-test-private-value" not in json.dumps(record)
    assert "token-to-redact" not in json.dumps(record)
    assert record["usage_source"] == "conservative_reservation"
    assert record["charged_tokens"] > 0 and record["price"] is None


def test_failed_requested_validation_is_not_usable_best(tmp_path):
    config = configuration(0)
    config["budgets"]["max_repairs"] = 0
    result = run(
        tmp_path,
        config=config,
        evaluate=lambda code, cid, window: (
            {"combined_score": 2}
            if window["split"] == "train"
            else {"error": "validation execution failed"}
        ),
    )
    assert result.status == "failed" and result.stop_reason == "all_candidates_invalid"
    assert result.best_candidate_id is None
    candidate = result.candidates[0]
    assert not candidate["valid"]
    assert candidate["evaluation"]["combined_score"] == 2
    assert candidate["evaluation"]["validation_error"] == "validation execution failed"


def test_malformed_generation_preserves_base_without_adopting_response(tmp_path):
    class Malformed(FixedModel):
        def complete(self, request):
            self.requests.append(request)
            return ModelResponse("not a code block", 1, 1)

    result = run(tmp_path, config=configuration(1), model=Malformed())
    assert result.status == "succeeded"
    assert len(result.candidates) == 2
    assert result.candidates[1]["code"] == CODE
    assert result.candidates[1]["parent_id"] == result.candidates[0]["candidate_id"]
    calls = [row for row in result.model_calls if ":generate:" in row["call_id"]]
    assert len(calls) == 5
    assert all(not row["adopted"] for row in calls)


def test_raw_fixture_usage_not_mislabeled_as_provider_tokens(tmp_path):
    cfg = configuration(1)
    cfg["budgets"]["max_model_calls"] = 1
    result = run(tmp_path, config=cfg, model=FrozenModel(["short fixture feedback"]))
    record = result.model_calls[0]
    assert record["usage_source"] == "synthetic_fixture"
    assert record["input_tokens"] == record["output_tokens"] == 0
    assert record["request_bytes"] > 0 and record["response_bytes"] > 0
    assert record["price"] is None


def test_recorded_model_failure_cannot_claim_run_success(tmp_path):
    class Failed(FixedModel):
        def complete(self, request):
            raise RuntimeError("fixture provider unavailable")

    result = run(tmp_path, model=Failed())
    assert result.status == "failed" and result.stop_reason == "model_call_failed"
    assert result.best_candidate_id == "candidate-0000"
    assert result.model_calls[0]["charged_tokens"] > 0
    assert result.model_calls[0]["error_class"] == "RuntimeError"


def test_importing_contracts_does_not_load_research_sdks():
    import subprocess
    import sys

    script = """
import sys
from packages.tradegrad_adapter import optimizer, checkpoint, model_calls
assert 'numpy' not in sys.modules
assert 'openai' not in sys.modules
assert 'src.optimizer' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", script], check=True)


@pytest.mark.parametrize("boundary", ["after_model_response", "before_model_response"])
def test_three_attempt_recovery_binds_journal_before_fresh_call(tmp_path, boundary):
    checks = 0

    def pause_after_backward():
        nonlocal checks
        checks += 1
        return "pause" if checks == 6 else None

    first = run(tmp_path / "attempt-a", control=pause_after_backward)
    assert first.status == "paused"
    assert len(first.model_calls) == 1
    second_pointers = []
    second_model = FixedModel()

    def fault(label):
        if label == boundary:
            raise Crash()

    with pytest.raises(Crash):
        run(
            tmp_path / "attempt-b",
            resume=first.checkpoint,
            model=second_model,
            checkpoint_committed=second_pointers.append,
            fault=fault,
        )
    assert second_pointers, (
        "new journal root must be published before making fresh calls"
    )
    assert "/attempt-b/" in second_pointers[-1].manifest_path
    third_model = FixedModel()
    actual = run(tmp_path / "attempt-c", resume=second_pointers[-1], model=third_model)
    attempted_id = second_model.requests[0].call_id
    assert attempted_id not in {request.call_id for request in third_model.requests}
    if boundary == "after_model_response":
        assert chain(actual) == chain(run(tmp_path / "continuous"))
    else:
        assert actual.stop_reason == "model_call_outcome_unknown"
        assert actual.status == "failed"


def test_resume_pointer_failure_aborts_before_new_model_call(tmp_path):
    checks = 0

    def pause():
        nonlocal checks
        checks += 1
        return "pause" if checks == 6 else None

    paused = run(tmp_path / "attempt-a", control=pause)
    model = FixedModel()

    def reject_pointer(_reference):
        raise RuntimeError("stale worker fence")

    with pytest.raises(RuntimeError, match="fence"):
        run(
            tmp_path / "attempt-b",
            resume=paused.checkpoint,
            model=model,
            checkpoint_committed=reject_pointer,
        )
    assert model.requests == []


def test_explicit_nonoptimization_mode_is_rejected(tmp_path):
    config = configuration()
    config["execution_mode"] = "seed_evaluation"
    with pytest.raises(ValueError, match="execution_mode"):
        run(tmp_path, config=config)


@pytest.mark.parametrize("mutation", ["rng", "stage", "archive", "code_hash"])
def test_invalid_checkpoint_state_is_rejected_before_pointer(tmp_path, mutation):
    result = run(tmp_path / "original", config=configuration(0))
    state = CheckpointStore.load(
        result.checkpoint, expected_input_digest=result.checkpoint.input_digest
    )
    if mutation == "rng":
        state["random_state"]["numpy"][1] = [1]
    elif mutation == "stage":
        state["next_stage"] = "import_generated_code"
    elif mutation == "archive":
        state["optimizer"]["archive"] = ["unknown-candidate"]
    else:
        state["candidates"][0]["code_sha256"] = "0" * 64
    pointers = []
    with pytest.raises(CheckpointError):
        CheckpointStore(tmp_path / "invalid", result.checkpoint.input_digest).commit(
            state, committed=pointers.append
        )
    assert not pointers


def test_timeout_is_uncertain_and_never_automatically_recharged(tmp_path):
    class TimedOut(FixedModel):
        def complete(self, request):
            self.requests.append(request)
            raise TimeoutError("fixture connection ended before response")

    model = TimedOut()
    result = run(tmp_path / "first", model=model)
    assert (
        result.status == "failed" and result.stop_reason == "model_call_outcome_unknown"
    )
    assert result.model_calls[0]["status"] == "started"
    assert result.model_calls[0]["error_class"] == "TimeoutError"
    assert result.model_calls[0]["charged_tokens"] > 0
    assert len(model.requests) == 1
    next_model = FixedModel()
    again = run(tmp_path / "second", resume=result.checkpoint, model=next_model)
    assert again.stop_reason == "model_call_outcome_unknown" and not next_model.requests


def test_completed_overbudget_response_stops_same_way_after_crash(tmp_path):
    class OverBudget(FixedModel):
        def complete(self, request):
            self.requests.append(request)
            return ModelResponse("too many recorded fixture tokens", 2_000_000, 1)

    pointers = []

    def crash_after_response(label):
        if label == "after_model_response":
            raise Crash()

    with pytest.raises(Crash):
        run(
            tmp_path / "first",
            model=OverBudget(),
            fault=crash_after_response,
            checkpoint_committed=pointers.append,
        )
    restored_model = OverBudget()
    result = run(tmp_path / "second", resume=pointers[-1], model=restored_model)
    assert result.stop_reason == "max_tokens" and not restored_model.requests
    assert result.model_calls[0]["adopted"] is False


def test_remaining_runtime_is_reported_before_evaluation(tmp_path):
    observed = []
    remaining = None

    def event(kind, payload):
        nonlocal remaining
        if kind == "budget_updated":
            remaining = payload.get("remaining_runtime_seconds", remaining)

    def evaluator(*args):
        observed.append(remaining)
        return evaluate(*args)

    run(tmp_path, event=event, evaluate=evaluator)
    assert observed and all(
        value is not None and 0 < value <= 120 for value in observed
    )
    assert observed == sorted(observed, reverse=True)


@pytest.mark.parametrize(
    "outcome",
    [
        {"combined_score": 50, "status": "interrupted"},
        {"combined_score": 50, "status": "succeeded", "exit_code": 1},
        {"combined_score": 50, "status": "succeeded", "timed_out": True},
        {"combined_score": 50, "status": "registered"},
    ],
)
def test_failed_process_outcome_cannot_be_usable(tmp_path, outcome):
    config = configuration(0)
    config["budgets"]["max_repairs"] = 0
    result = run(tmp_path, config=config, evaluate=lambda *_: outcome)
    assert result.status == "failed" and result.best_candidate_id is None
    assert result.stop_reason == "all_candidates_invalid"


def test_seed_content_hash_is_enforced_when_configured(tmp_path):
    config = configuration()
    config["seed_strategy"] = {"id": "fixture-seed", "sha256": "0" * 64}
    with pytest.raises(ValueError, match="seed strategy bytes"):
        run(tmp_path, config=config)


@pytest.mark.parametrize(
    "failed_status,expected_requests", [("started", 0), ("completed", 1)]
)
def test_model_journal_failure_never_retries_or_publishes_partial_state(
    tmp_path, monkeypatch, failed_status, expected_requests
):
    from packages.tradegrad_adapter import model_calls
    from packages.tradegrad_adapter.model_calls import ModelCallBoundaryFailure

    original = model_calls.atomic_json
    failed = False

    def fail_once(path, value):
        nonlocal failed
        if not failed and value["record"]["status"] == failed_status:
            failed = True
            raise OSError("fixture journal fsync failure")
        return original(path, value)

    monkeypatch.setattr(model_calls, "atomic_json", fail_once)
    model = FixedModel()
    pointers = []
    with pytest.raises(ModelCallBoundaryFailure, match="persistence"):
        run(
            tmp_path / "first",
            config=configuration(1),
            model=model,
            checkpoint_committed=pointers.append,
        )
    assert len(model.requests) == expected_requests
    assert pointers
    last = CheckpointStore.load(
        pointers[-1], expected_input_digest=pointers[-1].input_digest
    )
    assert last["next_stage"] == "backward" and last["stop_reason"] is None
    assert not last["model_calls"], (
        "failed operation must not advance authoritative checkpoint"
    )
    journal = list((tmp_path / "first" / "model_calls").glob("*.json"))
    if failed_status == "completed":
        assert len(journal) == 1
        assert json.loads(journal[0].read_text())["record"]["status"] == "started"
        resumed_model = FixedModel()
        resumed = run(
            tmp_path / "second",
            config=configuration(1),
            model=resumed_model,
            resume=pointers[-1],
        )
        assert resumed.stop_reason == "model_call_outcome_unknown"
        assert not resumed_model.requests
    else:
        assert not journal


def test_post_response_event_failure_cannot_repeat_paid_request(tmp_path):
    from packages.tradegrad_adapter.model_calls import ModelCallBoundaryFailure

    model = FixedModel()
    pointers = []

    def event(kind, _payload):
        if kind == "model_call_completed":
            raise RuntimeError("fixture event DB unavailable")

    with pytest.raises(ModelCallBoundaryFailure, match="publication"):
        run(
            tmp_path / "first",
            config=configuration(1),
            model=model,
            checkpoint_committed=pointers.append,
            event=event,
        )
    assert len(model.requests) == 1
    last = CheckpointStore.load(
        pointers[-1], expected_input_digest=pointers[-1].input_digest
    )
    assert last["next_stage"] == "backward" and not last["model_calls"]
    resumed_model = FixedModel()
    resumed = run(
        tmp_path / "second",
        config=configuration(1),
        model=resumed_model,
        resume=pointers[-1],
    )
    assert resumed.status == "succeeded"
    assert model.requests[0].call_id not in {
        request.call_id for request in resumed_model.requests
    }
    assert chain(resumed) == chain(
        run(tmp_path / "continuous", config=configuration(1))
    )


def test_post_response_metadata_failure_stops_with_started_journal(tmp_path):
    from packages.tradegrad_adapter.model_calls import ModelCallBoundaryFailure

    class InvalidMetadata(FixedModel):
        def complete(self, request):
            self.requests.append(request)
            return ModelResponse("received response", 1, 1, price={"amount": object()})

    model = InvalidMetadata()
    pointers = []
    with pytest.raises(ModelCallBoundaryFailure):
        run(
            tmp_path / "first",
            config=configuration(1),
            model=model,
            checkpoint_committed=pointers.append,
        )
    assert len(model.requests) == 1
    journal = list((tmp_path / "first" / "model_calls").glob("*.json"))
    assert len(journal) == 1
    assert json.loads(journal[0].read_text())["record"]["status"] == "started"
    restored_model = FixedModel()
    result = run(
        tmp_path / "second",
        config=configuration(1),
        model=restored_model,
        resume=pointers[-1],
    )
    assert (
        result.stop_reason == "model_call_outcome_unknown"
        and not restored_model.requests
    )


def test_completed_journal_directory_sync_failure_replays_verified_response(
    tmp_path, monkeypatch
):
    from packages.tradegrad_adapter import model_calls
    from packages.tradegrad_adapter.model_calls import ModelCallBoundaryFailure

    original = model_calls.atomic_json
    failed = False

    def fail_after_write(path, value):
        nonlocal failed
        original(path, value)
        if not failed and value["record"]["status"] == "completed":
            failed = True
            raise OSError("fixture directory fsync failed after replacement")

    monkeypatch.setattr(model_calls, "atomic_json", fail_after_write)
    model = FixedModel()
    pointers = []
    with pytest.raises(ModelCallBoundaryFailure, match="persistence"):
        run(
            tmp_path / "first",
            config=configuration(1),
            model=model,
            checkpoint_committed=pointers.append,
        )
    assert len(model.requests) == 1
    # The caller must stop even if replacement happened before fsync reported an
    # error. Only a subsequent explicit checkpoint+journal verification can reuse it.
    last = CheckpointStore.load(
        pointers[-1], expected_input_digest=pointers[-1].input_digest
    )
    assert last["next_stage"] == "backward" and not last["model_calls"]
    resumed_model = FixedModel()
    resumed = run(
        tmp_path / "second",
        config=configuration(1),
        model=resumed_model,
        resume=pointers[-1],
    )
    assert resumed.status == "succeeded"
    assert model.requests[0].call_id not in {
        request.call_id for request in resumed_model.requests
    }


@pytest.mark.parametrize(
    "stop_reason",
    ["max_iterations", "max_model_calls", "max_tokens", "max_runtime_seconds"],
)
def test_finished_checkpoint_migrates_without_restarting_work(tmp_path, stop_reason):
    config = configuration(1)
    if stop_reason != "max_iterations":
        config["budgets"][stop_reason] = (
            0 if stop_reason == "max_runtime_seconds" else 1
        )
    first = run(tmp_path / "attempt-a", config=config)
    assert first.stop_reason == stop_reason
    first_state = CheckpointStore.load(
        first.checkpoint, expected_input_digest=first.checkpoint.input_digest
    )
    assert first_state["next_stage"] == "finished"
    second_model = FixedModel()
    pointers = []

    def forbidden_work(*_args):
        pytest.fail("finished resume must not perform evaluation or control work")

    second = run(
        tmp_path / "attempt-b",
        config=config,
        resume=first.checkpoint,
        model=second_model,
        evaluate=forbidden_work,
        control=forbidden_work,
        checkpoint_committed=pointers.append,
    )
    assert len(pointers) == 1 and pointers[0] == second.checkpoint
    assert "/attempt-b/" in second.checkpoint.manifest_path
    assert second.checkpoint.manifest_path != first.checkpoint.manifest_path
    assert not second_model.requests
    assert second.stop_reason == first.stop_reason and second.status == first.status
    assert second.best_candidate_id == first.best_candidate_id
    assert chain(second) == chain(first) and second.model_calls == first.model_calls
    second_state = CheckpointStore.load(
        second.checkpoint, expected_input_digest=second.checkpoint.input_digest
    )
    assert second_state["next_stage"] == "finished"
    for key in first_state.keys() - {"elapsed_seconds"}:
        assert second_state[key] == first_state[key], key


def test_finished_resume_requires_successful_new_attempt_pointer(tmp_path):
    from packages.tradegrad_adapter.model_calls import ModelCallBoundaryFailure

    first = run(tmp_path / "attempt-a", config=configuration(0))
    model = FixedModel()

    def rejected(_reference):
        raise RuntimeError("fixture stale fence")

    with pytest.raises(RuntimeError, match="stale fence") as error:
        run(
            tmp_path / "attempt-b",
            config=configuration(0),
            resume=first.checkpoint,
            model=model,
            checkpoint_committed=rejected,
        )
    assert not isinstance(error.value, ModelCallBoundaryFailure)
    assert not model.requests
