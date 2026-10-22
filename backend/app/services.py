"""Business state transitions. Callers own commit/rollback; every mutation locks its Run.

Run generation + active attempt + random lease token + lease expiry fence writes.
A timeout is never evidence that a sandbox has stopped.
"""

import hashlib
import json
import math
from collections.abc import Callable
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.models import (
    Artifact,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    StrategyVersion,
    utcnow,
)
from backend.app.schemas import RunCreate

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "paused"}


class DomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, details=None):
        super().__init__(message)
        self.code, self.message, self.status_code, self.details = (
            code,
            message,
            status_code,
            details,
        )


class FenceLost(DomainError):
    def __init__(self):
        super().__init__(
            "attempt_fenced",
            "Attempt lease or execution generation is no longer current",
        )


def advisory_lock(session: Session, scope: str):
    key = int.from_bytes(
        hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True
    )
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def owned_run(
    session: Session, run_id: UUID, owner_id: UUID, lock=False
) -> ResearchRun:
    query = select(ResearchRun).where(
        ResearchRun.id == run_id, ResearchRun.owner_id == owner_id
    )
    if lock:
        query = query.with_for_update()
    run = session.scalar(query)
    if run is None:
        raise DomainError("not_found", "Run not found", 404)
    return run


def append_event(
    session: Session,
    run: ResearchRun,
    kind: str,
    payload: dict,
    attempt_id: UUID | None = None,
):
    """Caller MUST hold the Run row lock, or be creating it in this transaction."""
    run.next_event_seq += 1
    run.updated_at = utcnow()
    session.add(
        RunEvent(
            run_id=run.id,
            attempt_id=attempt_id,
            seq=run.next_event_seq,
            type=kind,
            payload=payload,
        )
    )


def submit_run(
    session: Session,
    owner_id: UUID,
    body: RunCreate,
    idempotency_key: str,
    settings: Settings,
) -> ResearchRun:
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1 to 128 printable ASCII characters without spaces",
            422,
        )
    config = body.model_dump(mode="json")
    digest = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    # One lock per owner also serializes quota accounting across different keys.
    advisory_lock(session, f"submit:{owner_id}")
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if previous.request_sha256 != digest:
            raise DomainError(
                "idempotency_conflict",
                "This Idempotency-Key was already used with a different request",
            )
        return previous
    dataset = session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == body.dataset_version_id,
            DatasetVersion.owner_id == owner_id,
        )
    )
    strategy = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == body.strategy_version_id,
            StrategyVersion.owner_id == owner_id,
        )
    )
    if dataset is None or strategy is None:
        raise DomainError("not_found", "Dataset or strategy version not found", 404)
    market = body.task[:2].upper()
    if dataset.market != market or strategy.task != body.task:
        raise DomainError(
            "reference_mismatch",
            "Task, dataset market and strategy task must agree",
            422,
        )
    if body.start_date < dataset.start_date or body.end_date > dataset.end_date:
        raise DomainError(
            "coverage_error",
            "Requested [start_date, end_date) lies outside the immutable dataset coverage",
            422,
            {"start_date": str(dataset.start_date), "end_date": str(dataset.end_date)},
        )
    compatibility = dataset.manifest.get("evaluation_compatibility", {})
    if compatibility.get("compatible") is False:
        raise DomainError(
            "incompatible_data_units",
            "Dataset cannot be evaluated until its units and semantic flags are resolved in a new snapshot",
            422,
            compatibility.get("reasons", []),
        )
    listings = dataset.manifest.get("listings", [])
    if listings and body.task.endswith("_time") and len(listings) != 1:
        raise DomainError(
            "single_listing_required",
            "Time-series evaluation requires a snapshot with exactly one listing",
            422,
        )
    if listings and dataset.manifest.get("quality", {}).get("unknown_flags"):
        raise DomainError(
            "unknown_data_semantics",
            "Uploaded data has unresolved semantic flags; resolve them in a new upload before evaluation",
            422,
        )
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= settings.max_active_runs_per_user:
        raise DomainError("run_quota", "Active run limit reached", 429)
    config.update(
        market=market,
        dataset_sha256=dataset.content_sha256,
        strategy_sha256=strategy.content_sha256,
        execution_mode="seed_evaluation",
        is_synthetic=dataset.is_synthetic,
    )
    run = ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key=idempotency_key,
        request_sha256=digest,
        config=config,
        dataset_version_id=dataset.id,
        strategy_version_id=strategy.id,
        status="queued",
        generation=1,
        next_event_seq=0,
    )
    session.add(run)
    session.flush()
    session.add(OutboxEvent(run_id=run.id, generation=1))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "execution_mode": "seed_evaluation",
            "is_synthetic": dataset.is_synthetic,
        },
    )
    session.flush()
    return run


def claim_attempt(
    session: Session, run_id: UUID, generation: int, worker_id: str, settings: Settings
) -> ExecutionAttempt | None:
    run = session.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
    )
    if (
        run is None
        or run.generation != generation
        or run.status not in {"queued", "pause_requested"}
    ):
        return None
    if session.scalar(
        select(ExecutionAttempt.id).where(
            ExecutionAttempt.run_id == run_id, ExecutionAttempt.generation == generation
        )
    ):
        return None
    now, attempt_id = utcnow(), uuid4()
    attempt = ExecutionAttempt(
        id=attempt_id,
        run_id=run_id,
        generation=generation,
        worker_id=worker_id,
        resumed_checkpoint_id=run.resume_checkpoint_id,
        lease_token=uuid4(),
        lease_expires_at=now + timedelta(seconds=settings.lease_seconds),
        heartbeat_at=now,
        status="preparing",
        sandbox_id=f"tradegrad-{attempt_id}",
        sandbox_stopped=False,
    )
    session.add(attempt)
    session.flush()
    run.active_attempt_id = attempt_id
    if run.status != "pause_requested":
        run.status = "preparing"
    append_event(
        session,
        run,
        "stage_changed",
        {"status": run.status, "generation": generation},
        attempt_id,
    )
    session.flush()
    return attempt


def fenced_attempt(
    session: Session, attempt_id: UUID, generation: int, lease_token: UUID
):
    # Always take locks in Run -> Attempt order.
    run_id = session.scalar(
        select(ExecutionAttempt.run_id).where(ExecutionAttempt.id == attempt_id)
    )
    if run_id is None:
        raise FenceLost()
    run = session.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
    )
    attempt = session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.id == attempt_id)
        .with_for_update()
    )
    if (
        run.active_attempt_id != attempt_id
        or run.generation != generation
        or attempt.generation != generation
        or attempt.lease_token != lease_token
        or attempt.lease_expires_at <= utcnow()
        or run.status in TERMINAL
        or attempt.finished_at is not None
    ):
        raise FenceLost()
    return run, attempt


def heartbeat(
    session: Session,
    attempt_id: UUID,
    generation: int,
    lease_token: UUID,
    settings: Settings,
) -> bool:
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    attempt.heartbeat_at = utcnow()
    attempt.lease_expires_at = attempt.heartbeat_at + timedelta(
        seconds=settings.lease_seconds
    )
    return run.status == "cancel_requested"


def mark_running(
    session: Session, attempt_id: UUID, generation: int, lease_token: UUID
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    if run.status == "cancel_requested":
        return False
    if run.status != "pause_requested":
        run.status = "running"
    attempt.status = "running"
    append_event(session, run, "stage_changed", {"status": run.status}, attempt_id)
    return True


def finish_attempt(
    session: Session,
    attempt_id: UUID,
    generation: int,
    lease_token: UUID,
    outcome: dict,
    sandbox_stopped: bool,
    artifacts: list[dict] | None = None,
) -> ResearchRun:
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    if run.config.get("schema_version") == "tradegrad.run.v1":
        raise DomainError(
            "research_completion_required",
            "Research attempts require checkpoint-aware completion",
        )
    if not sandbox_stopped:
        # Leave non-terminal and retain generation. Reconciliation must confirm stop.
        attempt.error = "Sandbox exit could not be confirmed; recovery is blocked until stop succeeds"
        run.error = attempt.error
        append_event(
            session, run, "recovery_blocked", {"message": attempt.error}, attempt_id
        )
        return run
    status = outcome.get("status", "failed")
    if run.status == "cancel_requested":
        status = "cancelled"
    if status not in TERMINAL:
        status = "failed"
    envelope = outcome.get("metrics")
    numbers = envelope.get("metrics", envelope) if isinstance(envelope, dict) else None

    def finite_number(value):
        try:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        except OverflowError:
            return False

    if status == "succeeded" and (
        outcome.get("exit_code") != 0
        or not isinstance(numbers, dict)
        or not numbers
        or not all(finite_number(v) for v in numbers.values())
        or (
            envelope.get("combined_score") is not None
            and not finite_number(envelope["combined_score"])
        )
    ):
        status = "failed"
        outcome = {
            **outcome,
            "error": "Evaluator returned no valid finite metrics with a zero exit code",
        }
    if status == "succeeded":
        run.status = "finalizing"
        append_event(
            session, run, "stage_changed", {"status": "finalizing"}, attempt_id
        )
    for artifact in artifacts or []:
        session.add(Artifact(run_id=run.id, attempt_id=attempt.id, **artifact))
    attempt.sandbox_stopped = True
    attempt.status = run.status = status
    attempt.exit_code = outcome.get("exit_code")
    attempt.error = run.error = outcome.get("error")
    attempt.finished_at = utcnow()
    run.result = None
    if status == "succeeded":
        run.result = {
            "metrics": numbers,
            "combined_score": envelope.get("combined_score"),
            "execution_mode": "seed_evaluation",
            "is_synthetic": run.config["is_synthetic"],
            "evaluation_mode": "native_akquant",
            "result_trust": "native_strategy_and_engine_share_process",
            "image_digest": outcome.get("image_digest"),
        }
    if status == "succeeded":
        append_event(
            session,
            run,
            "evaluation_completed",
            {"metrics": outcome["metrics"]},
            attempt_id,
        )
    append_event(
        session, run, "run_finished", {"status": status, "error": run.error}, attempt_id
    )
    session.flush()
    return run


def request_cancel(session: Session, run_id: UUID, owner_id: UUID) -> ResearchRun:
    run = owned_run(session, run_id, owner_id, lock=True)
    if run.status == "paused":
        run.status = "cancelled"
        append_event(
            session, run, "run_finished", {"status": "cancelled"}, run.active_attempt_id
        )
        return run
    if run.status in TERMINAL or run.status == "cancel_requested":
        return run
    run.status = "cancelled" if run.active_attempt_id is None else "cancel_requested"
    append_event(
        session,
        run,
        "run_finished" if run.status == "cancelled" else "stage_changed",
        {"status": run.status},
        run.active_attempt_id,
    )
    return run


def resume_run(
    session: Session, run_id: UUID, owner_id: UUID, settings: Settings | None = None
) -> ResearchRun:
    advisory_lock(session, f"submit:{owner_id}")
    run = owned_run(session, run_id, owner_id, lock=True)
    if run.config.get("schema_version") == "tradegrad.run.v1":
        if settings is None:
            raise DomainError(
                "settings_required",
                "Research resume requires verified storage settings",
            )
        from backend.app.research_services import resume_research_run

        return resume_research_run(session, run_id, owner_id, settings)
    if run.status not in {"failed", "cancelled", "interrupted"}:
        raise DomainError(
            "invalid_transition",
            "Only failed, cancelled or interrupted runs can be resumed",
        )
    if run.active_attempt_id:
        old = session.get(ExecutionAttempt, run.active_attempt_id)
        if old is None or not old.sandbox_stopped:
            raise DomainError(
                "sandbox_not_stopped",
                "Previous sandbox must be confirmed stopped before a new attempt",
            )
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    if active >= (settings.max_active_runs_per_user if settings else 4):
        raise DomainError("run_quota", "Active run limit reached", 429)
    run.generation += 1
    run.active_attempt_id = None
    run.status, run.error, run.result = "queued", None, None
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "stage_changed",
        {
            "status": "queued",
            "generation": run.generation,
            "resume_mode": "reevaluate_seed_from_start",
        },
    )
    return run


def reconcile_attempt(
    session: Session, attempt_id: UUID, stop: Callable[[str], bool]
) -> bool:
    run_id = session.scalar(
        select(ExecutionAttempt.run_id).where(ExecutionAttempt.id == attempt_id)
    )
    if run_id is None:
        return False
    run = session.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id).with_for_update()
    )
    attempt = session.scalar(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.id == attempt_id)
        .with_for_update()
    )
    if (
        attempt.finished_at is not None
        or attempt.lease_expires_at > utcnow()
        or run.active_attempt_id != attempt.id
    ):
        return False
    # Holding the Run lock prevents cancellation/resume/old completion racing stop.
    # Stop is bounded by runner timeout. On False, absolutely no retry is released.
    # A research attempt owns many distinct evaluator sandboxes. Registering them
    # before create makes this finite DB inventory authoritative, never a dir scan.
    children = session.scalars(
        select(Evaluation)
        .where(
            Evaluation.attempt_id == attempt.id, Evaluation.sandbox_stopped.is_(False)
        )
        .with_for_update()
    ).all()
    all_stopped = True
    for child in children:
        if stop(str(child.id)):
            child.sandbox_stopped = True
            if child.finished_at is None:
                child.status = "interrupted"
                child.error = (
                    "Worker lease expired; registered evaluation sandbox was stopped"
                )
                child.finished_at = utcnow()
        else:
            all_stopped = False
    # Legacy attempts may still own the attempt-named sandbox.
    parent_stopped = stop(str(attempt.id))
    if not all_stopped or not parent_stopped:
        if run.error != "Recovery blocked: previous sandbox termination is unconfirmed":
            run.error = "Recovery blocked: previous sandbox termination is unconfirmed"
            append_event(
                session, run, "recovery_blocked", {"message": run.error}, attempt.id
            )
        return False
    attempt.sandbox_stopped = True
    attempt.lease_token = uuid4()  # even a delayed previous callback is fenced
    attempt.finished_at = utcnow()
    attempt.status = run.status = (
        "cancelled" if run.status == "cancel_requested" else "interrupted"
    )
    attempt.error = run.error = (
        "Worker lease expired; sandbox was stopped and this attempt is fenced"
    )
    append_event(
        session,
        run,
        "run_finished",
        {"status": run.status, "error": run.error},
        attempt.id,
    )
    return True
