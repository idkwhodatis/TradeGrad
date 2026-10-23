"""Pure synthetic holdout read contract, not PostgreSQL or engine acceptance."""

import pytest

from backend.app.final_evaluation_views import (
    final_result_view,
    validate_final_persisted_result,
)
from backend.app.research_views import validate_persisted_result
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import synthetic_final_result


def checked(result):
    return validate_final_persisted_result(
        result.evaluation, result.run, result.dataset, result.artifact, result.content
    )


def test_exact_final_result_is_distinct_from_optimizer_result():
    result = synthetic_final_result()
    assert checked(result) == result.bundle
    with pytest.raises(DomainError, match="compatible immutable research"):
        validate_persisted_result(
            result.evaluation,
            result.run,
            result.dataset,
            result.artifact,
            result.content,
        )


@pytest.mark.parametrize(
    "field",
    ["selection", "split", "strategy", "window", "hash", "artifact", "payload", "stop"],
)
def test_final_binding_or_artifact_tamper_fails_closed(field):
    from uuid import uuid4

    result = synthetic_final_result()
    if field == "selection":
        result.run.final_selection_id = uuid4()
    elif field == "split":
        result.run.split_version_id = uuid4()
    elif field == "strategy":
        result.evaluation.code_sha256 = "a" * 64
    elif field == "window":
        result.evaluation.window = {**result.evaluation.window, "split": "validation"}
    elif field == "hash":
        result.run.request_sha256 = "a" * 64
    elif field == "artifact":
        result.artifact.size_bytes += 1
    elif field == "payload":
        result.evaluation.payload["status"] = "failed"
    else:
        result.evaluation.sandbox_stopped = False
    with pytest.raises(DomainError):
        checked(result)


def test_final_view_keeps_holdout_context_and_exposure_warning():
    result = synthetic_final_result()
    checked(result)
    view = final_result_view(
        result, exposure_snapshot={"tracking_coverage": "unknown"}, max_points=4
    )
    assert view.split == "holdout"
    assert view.config.execution_mode == "final_evaluation"
    assert view.selection_feedback is False
    assert view.tables["equity"].returned_rows <= 4
    assert any("unseen" in warning for warning in view.warnings)
    assert view.result_trust == "native_strategy_and_engine_share_process"


@pytest.mark.parametrize(
    "query",
    [
        {"sections": ["fills", "fills"]},
        {"sections": ["secret"]},
        {"max_points": 1000000},
    ],
)
def test_final_result_query_retains_bounds(query):
    with pytest.raises(DomainError):
        final_result_view(synthetic_final_result(), exposure_snapshot={}, **query)


@pytest.mark.parametrize(
    "status",
    [
        "queued",
        "preparing",
        "running",
        "finalizing",
        "cancel_requested",
        "cancelled",
        "failed",
        "interrupted",
        "paused",
        "pause_requested",
    ],
)
def test_successful_physical_evaluation_is_not_logical_final_success(status):
    result = synthetic_final_result()
    result.run.status = status
    with pytest.raises(DomainError, match="durably committed"):
        checked(result)


@pytest.mark.parametrize(
    "field",
    [
        "retry_generation",
        "no_active_attempt",
        "new_attempt",
        "no_commit",
        "different_commit",
        "unfinished_evaluation",
        "foreign_run",
    ],
)
def test_late_or_uncommitted_final_result_is_not_exportable(field):
    from uuid import uuid4

    result = synthetic_final_result()
    if field == "retry_generation":
        result.run.generation += 1
    elif field == "no_active_attempt":
        result.run.active_attempt_id = None
    elif field == "new_attempt":
        result.run.active_attempt_id = uuid4()
    elif field == "no_commit":
        result.run.result = None
    elif field == "different_commit":
        result.run.result = {"evaluation_id": str(uuid4())}
    elif field == "unfinished_evaluation":
        result.evaluation.finished_at = None
    else:
        result.evaluation.run_id = uuid4()
    with pytest.raises(DomainError):
        checked(result)


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "generation",
        "id",
        "status",
        "finished_at",
        "sandbox_stopped",
        "exit_code",
    ],
)
def test_final_attempt_fence_is_required(field):
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.app.final_evaluation_views import validate_final_terminal

    result = synthetic_final_result()
    attempt = SimpleNamespace(
        id=result.evaluation.attempt_id,
        run_id=result.run.id,
        generation=result.run.generation,
        status="succeeded",
        finished_at=result.evaluation.finished_at,
        sandbox_stopped=True,
        exit_code=0,
    )
    validate_final_terminal(result.evaluation, result.run, attempt)
    setattr(
        attempt,
        field,
        {
            "run_id": uuid4(),
            "generation": 2,
            "id": uuid4(),
            "status": "running",
            "finished_at": None,
            "sandbox_stopped": False,
            "exit_code": 1,
        }[field],
    )
    with pytest.raises(DomainError, match="durably succeeded"):
        validate_final_terminal(result.evaluation, result.run, attempt)


def test_loader_rejects_unfinished_attempt_before_artifact_read(monkeypatch):
    from types import SimpleNamespace

    from backend.app import final_evaluation_services, final_evaluation_views

    result = synthetic_final_result()
    attempt = SimpleNamespace(
        id=result.evaluation.attempt_id,
        run_id=result.run.id,
        generation=result.run.generation,
        status="running",
        finished_at=None,
        sandbox_stopped=False,
        exit_code=None,
    )
    session = SimpleNamespace(
        execute=lambda query: SimpleNamespace(
            one_or_none=lambda: (result.evaluation, result.run)
        ),
        get=lambda model, identifier: attempt,
    )
    monkeypatch.setattr(
        final_evaluation_services,
        "validate_final_binding",
        lambda *args: (
            None,
            None,
            result.dataset,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        final_evaluation_views,
        "read_verified",
        lambda *args: pytest.fail("Unfinished result bytes read"),
    )
    with pytest.raises(DomainError, match="durably succeeded"):
        final_evaluation_views.load_owned_final_result(
            session, result.evaluation.id, result.run.owner_id, None
        )


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "generation",
        "id",
        "exit_code",
        "status",
        "finished_at",
        "sandbox_stopped",
    ],
)
def test_portable_final_attempt_has_same_durable_fence(monkeypatch, field):
    from types import SimpleNamespace
    from uuid import uuid4

    from backend.app import portable_final_views

    result = synthetic_final_result()
    attempt = SimpleNamespace(
        id=result.evaluation.attempt_id,
        run_id=result.run.id,
        generation=result.run.generation,
        status="succeeded",
        finished_at=result.evaluation.finished_at,
        sandbox_stopped=True,
        exit_code=0,
    )
    setattr(
        attempt,
        field,
        {
            "run_id": uuid4(),
            "generation": 2,
            "id": uuid4(),
            "exit_code": 1,
            "status": "running",
            "finished_at": None,
            "sandbox_stopped": False,
        }[field],
    )
    session = SimpleNamespace(
        execute=lambda query: SimpleNamespace(
            one_or_none=lambda: (result.evaluation, result.run)
        ),
        get=lambda model, identifier: attempt,
    )
    monkeypatch.setattr(
        portable_final_views,
        "validate_final_binding",
        lambda *args: (None, None, result.dataset, None, None),
    )
    monkeypatch.setattr(
        portable_final_views,
        "read_verified",
        lambda *args: pytest.fail("Unfinished portable result read"),
    )
    with pytest.raises(DomainError, match="durably succeeded"):
        portable_final_views.load_owned_final_result(
            session, result.evaluation.id, result.run.owner_id, None
        )
