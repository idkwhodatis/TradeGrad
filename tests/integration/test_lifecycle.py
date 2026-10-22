"""Real row-lock, lease, cancellation and execution-generation regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres
SUCCESS = {"status": "succeeded", "exit_code": 0, "metrics": {"total_return": 0.1}}
FAILURE = {"status": "failed", "exit_code": 1, "error": "deterministic test failure"}


def test_concurrent_delivery_claims_exactly_one_attempt(
    migrated_engine, settings, queued_run
):
    from backend.app.models import ExecutionAttempt, ResearchRun
    from backend.app.services import claim_attempt

    barrier = Barrier(6)
    run_id = UUID(queued_run["id"])

    def claim(index):
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            attempt = claim_attempt(session, run_id, 1, f"worker-{index}", settings)
            session.commit()
            return None if attempt is None else attempt.id

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(claim, range(6)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 1
        run = session.get(ResearchRun, run_id)
        assert run.active_attempt_id == winners[0]
        assert run.status == "preparing"


def test_stale_generation_delivery_cannot_claim(migrated_engine, settings, queued_run):
    from backend.app.models import ExecutionAttempt
    from backend.app.services import claim_attempt

    with Session(migrated_engine) as session:
        assert (
            claim_attempt(session, UUID(queued_run["id"]), 0, "old-worker", settings)
            is None
        )
        assert (
            claim_attempt(session, UUID(queued_run["id"]), 2, "future-worker", settings)
            is None
        )
        session.commit()
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_cancel_before_claim_is_terminal_and_idempotent(
    api, owner_headers, queued_run, migrated_engine, settings
):
    from backend.app.models import RunEvent
    from backend.app.services import claim_attempt

    path = f"/api/v1/runs/{queued_run['id']}/cancel"
    first = api.post(path, headers=owner_headers)
    second = api.post(path, headers=owner_headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    with Session(migrated_engine) as session:
        assert (
            claim_attempt(session, UUID(queued_run["id"]), 1, "late-delivery", settings)
            is None
        )
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 2


def test_running_cancel_wins_over_late_success(
    migrated_engine, settings, owner, claimed_attempt
):
    from backend.app.models import ResearchRun
    from backend.app.services import (
        finish_attempt,
        heartbeat,
        mark_running,
        request_cancel,
    )

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        assert mark_running(
            session, attempt.id, attempt.generation, attempt.lease_token
        )
        session.commit()
    with Session(migrated_engine) as session:
        run = request_cancel(session, attempt.run_id, owner.user.id)
        assert run.status == "cancel_requested"
        session.commit()
    with Session(migrated_engine) as session:
        assert heartbeat(
            session, attempt.id, attempt.generation, attempt.lease_token, settings
        )
        assert not mark_running(
            session, attempt.id, attempt.generation, attempt.lease_token
        )
        run = finish_attempt(
            session, attempt.id, attempt.generation, attempt.lease_token, SUCCESS, True
        )
        assert run.status == "cancelled"
        assert run.result is None
        session.commit()
    with Session(migrated_engine) as session:
        assert session.get(ResearchRun, attempt.run_id).status == "cancelled"


def test_unconfirmed_sandbox_stop_cannot_finish_or_resume(
    migrated_engine, owner, claimed_attempt
):
    from backend.app.models import ExecutionAttempt, ResearchRun
    from backend.app.services import DomainError, finish_attempt, resume_run

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        run = finish_attempt(
            session, attempt.id, attempt.generation, attempt.lease_token, SUCCESS, False
        )
        assert run.status not in {"succeeded", "failed", "cancelled", "interrupted"}
        session.commit()
    with Session(migrated_engine) as session:
        persisted = session.get(ExecutionAttempt, attempt.id)
        assert not persisted.sandbox_stopped
        assert persisted.finished_at is None
        assert session.get(ResearchRun, attempt.run_id).result is None
        with pytest.raises(DomainError):
            resume_run(session, attempt.run_id, owner.user.id)


def test_resume_creates_new_generation_and_fences_old_callbacks(
    migrated_engine, settings, owner, claimed_attempt
):
    from backend.app.models import ExecutionAttempt, OutboxEvent, ResearchRun
    from backend.app.services import (
        FenceLost,
        claim_attempt,
        finish_attempt,
        heartbeat,
        resume_run,
    )

    old = claimed_attempt
    with Session(migrated_engine) as session:
        finish_attempt(session, old.id, old.generation, old.lease_token, FAILURE, True)
        session.commit()
    with Session(migrated_engine) as session:
        run = resume_run(session, old.run_id, owner.user.id)
        assert run.generation == 2
        assert run.status == "queued"
        session.commit()
    with Session(migrated_engine, expire_on_commit=False) as session:
        current = claim_attempt(session, old.run_id, 2, "replacement-worker", settings)
        assert current is not None
        session.commit()
    assert current.id != old.id
    assert current.lease_token != old.lease_token
    assert current.sandbox_id != old.sandbox_id
    for operation in ("heartbeat", "finish"):
        with Session(migrated_engine) as session, pytest.raises(FenceLost):
            if operation == "heartbeat":
                heartbeat(session, old.id, old.generation, old.lease_token, settings)
            else:
                finish_attempt(
                    session, old.id, old.generation, old.lease_token, SUCCESS, True
                )
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, old.run_id)
        assert run.active_attempt_id == current.id
        assert run.status == "preparing"
        assert run.result is None
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 2
        generations = session.scalars(
            select(OutboxEvent.generation).order_by(OutboxEvent.generation)
        ).all()
        assert generations == [1, 2]


def test_concurrent_resume_does_not_create_duplicate_generations(
    migrated_engine, owner, claimed_attempt
):
    from backend.app.models import OutboxEvent, ResearchRun
    from backend.app.services import DomainError, finish_attempt, resume_run

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        finish_attempt(session, attempt.id, 1, attempt.lease_token, FAILURE, True)
        session.commit()
    barrier = Barrier(2)

    def resume(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            try:
                run = resume_run(session, attempt.run_id, owner.user.id)
                session.commit()
                return run.generation
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(resume, range(2)))
    assert sorted(results, key=str) == [2, "invalid_transition"]
    with Session(migrated_engine) as session:
        assert session.get(ResearchRun, attempt.run_id).generation == 2
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2


def test_expired_lease_cannot_be_renewed_or_publish_success(
    migrated_engine, settings, claimed_attempt
):
    from backend.app.models import ExecutionAttempt, utcnow
    from backend.app.services import FenceLost, finish_attempt, heartbeat

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        session.get(ExecutionAttempt, attempt.id).lease_expires_at = (
            utcnow() - timedelta(seconds=1)
        )
        session.commit()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        heartbeat(session, attempt.id, 1, attempt.lease_token, settings)
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        finish_attempt(session, attempt.id, 1, attempt.lease_token, SUCCESS, True)


@pytest.mark.parametrize("wrong", ["token", "generation"])
def test_wrong_fencing_credentials_cannot_heartbeat(
    migrated_engine, settings, claimed_attempt, wrong
):
    from backend.app.services import FenceLost, heartbeat

    attempt = claimed_attempt
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        heartbeat(
            session,
            attempt.id,
            attempt.generation + (wrong == "generation"),
            uuid4() if wrong == "token" else attempt.lease_token,
            settings,
        )


def test_recovery_requires_confirmed_stop_before_new_generation(
    migrated_engine, settings, owner, claimed_attempt
):
    from backend.app.models import ExecutionAttempt, ResearchRun, utcnow
    from backend.app.services import (
        DomainError,
        FenceLost,
        heartbeat,
        reconcile_attempt,
        resume_run,
    )

    old = claimed_attempt
    with Session(migrated_engine) as session:
        session.get(ExecutionAttempt, old.id).lease_expires_at = utcnow() - timedelta(
            seconds=1
        )
        session.commit()
    called = []

    def cannot_stop(identifier):
        called.append(identifier)
        return False

    with Session(migrated_engine) as session:
        assert not reconcile_attempt(session, old.id, cannot_stop)
        session.commit()
    assert called == [str(old.id)]
    with Session(migrated_engine) as session:
        assert not session.get(ExecutionAttempt, old.id).sandbox_stopped
        assert session.get(ResearchRun, old.run_id).generation == 1
        with pytest.raises(DomainError):
            resume_run(session, old.run_id, owner.user.id)
    with Session(migrated_engine) as session:
        assert reconcile_attempt(session, old.id, lambda _: True)
        session.commit()
    with Session(migrated_engine) as session:
        persisted = session.get(ExecutionAttempt, old.id)
        assert persisted.sandbox_stopped
        assert persisted.lease_token != old.lease_token
        assert persisted.status == "interrupted"
        assert resume_run(session, old.run_id, owner.user.id).generation == 2
        session.commit()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        heartbeat(session, old.id, 1, old.lease_token, settings)


def test_live_lease_reconciliation_does_not_stop_worker(
    migrated_engine, claimed_attempt
):
    from backend.app.services import reconcile_attempt

    def forbidden_stop(_):
        pytest.fail("A live lease must not be stopped by expired-lease reconciliation")

    with Session(migrated_engine) as session:
        assert not reconcile_attempt(session, claimed_attempt.id, forbidden_stop)
        session.commit()


@pytest.mark.parametrize(
    "metrics",
    [
        None,
        {},
        {"metrics": {}},
        {"metrics": {"score": True}},
        {"metrics": {"score": "0.1"}},
        {"metrics": {"score": None}},
        {"metrics": {"score": []}},
        {"metrics": {"score": float("nan")}},
        {"metrics": {"score": float("inf")}},
        {"metrics": {"score": 10**1000}},
        {"metrics": {"score": 0.1}, "combined_score": float("nan")},
        {"metrics": {"score": 0.1}, "combined_score": True},
        {"metrics": {"score": 0.1}, "combined_score": "not-a-number"},
    ],
    ids=[
        "missing",
        "empty",
        "empty-nested",
        "boolean",
        "string",
        "null",
        "array",
        "nan",
        "infinity",
        "overflowing-integer",
        "nan-combined-score",
        "boolean-combined-score",
        "string-combined-score",
    ],
)
def test_malformed_success_metrics_become_durable_failed_attempt(
    migrated_engine, claimed_attempt, metrics
):
    from backend.app.models import ExecutionAttempt, ResearchRun, RunEvent
    from backend.app.services import finish_attempt

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        result = finish_attempt(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            {"status": "succeeded", "exit_code": 0, "metrics": metrics},
            True,
        )
        assert result.status == "failed"
        assert result.result is None
        session.commit()
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, attempt.run_id)
        persisted = session.get(ExecutionAttempt, attempt.id)
        assert run.status == persisted.status == "failed"
        assert run.error
        assert persisted.sandbox_stopped
        assert persisted.finished_at is not None
        event_types = session.scalars(
            select(RunEvent.type).order_by(RunEvent.seq)
        ).all()
        assert "evaluation_completed" not in event_types
        assert event_types[-1] == "run_finished"


def test_valid_metric_envelope_is_flattened_and_marked_with_trust_boundary(
    migrated_engine, claimed_attempt
):
    from backend.app.models import ResearchRun
    from backend.app.services import finish_attempt

    attempt = claimed_attempt
    with Session(migrated_engine) as session:
        finish_attempt(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            {
                "status": "succeeded",
                "exit_code": 0,
                "metrics": {"metrics": {"return": 0.1}, "combined_score": 0.4},
            },
            True,
        )
        session.commit()
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, attempt.run_id)
        assert run.status == "succeeded"
        assert run.result["metrics"] == {"return": 0.1}
        assert run.result["combined_score"] == 0.4
        assert run.result["result_trust"] == "native_strategy_and_engine_share_process"
