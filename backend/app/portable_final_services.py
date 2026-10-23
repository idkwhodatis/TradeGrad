"""Distinct portable final admission and fenced completion, never optimization.

All mutations are atomic caller-owned transactions. Admission itself is retained
as conservative exposure even when cancelled, failed, or retried before results.
"""

from __future__ import annotations

import hashlib
import re
from uuid import uuid4

from sqlalchemy import func, select

from backend.app.experiment_schemas import FinalSelectionCreate, SplitDefinition
from backend.app.experiment_services import (
    TRACKING_COVERAGE,
    get_exposure_snapshot,
    lock_split,
    owned_resource,
    record_exposure,
    split_definition,
    validate_selection_source,
    validate_split_definition,
)
from backend.app.models import (
    Artifact,
    Candidate,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    FinalSelection,
    OutboxEvent,
    ResearchCheckpoint,
    ResearchRun,
    RunEvent,
    SplitVersion,
    StrategyVersion,
    utcnow,
)
from backend.app.portable_final_schemas import PortableFinalEvaluationPreflight
from backend.app.research_services import (
    bounded_json,
    execution_config,
    payload_hash,
)
from backend.app.services import (
    TERMINAL,
    DomainError,
    advisory_lock,
    append_event,
    fenced_attempt,
    owned_run,
)
from backend.app.storage import redact
from packages.tradegrad_adapter.config import DateWindow, VersionRef
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)


def final_config(run):
    try:
        config = PortableFinalEvaluationConfig.model_validate(run.config)
        if run.request_sha256 != config.sha256:
            raise ValueError("Final RunConfig hash differs")
        return config
    except (ValueError, TypeError):
        raise DomainError(
            "invalid_final_config", "Run is not an exact frozen final evaluation", 409
        ) from None


def _make_config(session, owner_id, body):
    selection = owned_resource(session, FinalSelection, body.selection_id, owner_id)
    split = owned_resource(session, SplitVersion, selection.split_version_id, owner_id)
    source = owned_run(session, selection.source_run_id, owner_id)
    config = execution_config(source)
    if config.schema_version != "tradegrad.portable.run.v1":
        raise DomainError("invalid_portable_config", "Selection is not portable", 409)
    strategy = owned_resource(
        session, StrategyVersion, selection.strategy_version_id, owner_id
    )
    from packages.strategy_sdk import hash_json

    definition = split_definition(split)
    if definition.windows.holdout is None:
        raise DomainError("holdout_required", "Selection has no final holdout", 422)
    final = PortableFinalEvaluationConfig(
        selection_id=selection.id,
        split_version_id=split.id,
        split_sha256=split.content_sha256,
        selected_strategy=VersionRef(
            id=str(selection.strategy_version_id), sha256=selection.source_code_sha256
        ),
        selected_manifest=VersionRef(
            id=f"{strategy.id}/manifest", sha256=hash_json(strategy.manifest)
        ),
        selected_parameters=VersionRef(
            id=f"{strategy.id}/parameters", sha256=hash_json(strategy.parameters)
        ),
        source_config=config,
        source_config_sha256=selection.source_config_sha256,
        holdout=definition.windows.holdout,
        environment_digest=config.environment_digest,
        max_runtime_seconds=body.max_runtime_seconds,
    )
    return final, selection, split, source


def validate_final_binding(session, run):
    """Read-time immutable bindings; deliberately independent of today's deployment."""
    config = final_config(run)
    selection = owned_resource(
        session, FinalSelection, config.selection_id, run.owner_id
    )
    split = owned_resource(session, SplitVersion, config.split_version_id, run.owner_id)
    source = owned_run(session, selection.source_run_id, run.owner_id)
    source_config = execution_config(source)
    if source_config.schema_version != "tradegrad.portable.run.v1":
        raise DomainError("invalid_portable_config", "Selection is not portable", 409)
    definition = split_definition(split)
    dataset, _ = validate_split_definition(session, run.owner_id, definition)
    strategy = owned_resource(
        session, StrategyVersion, selection.strategy_version_id, run.owner_id
    )
    candidate = session.scalar(
        select(Candidate).where(
            Candidate.id == selection.source_candidate_id, Candidate.run_id == source.id
        )
    )
    checkpoint = session.scalar(
        select(ResearchCheckpoint).where(
            ResearchCheckpoint.id == selection.source_checkpoint_id,
            ResearchCheckpoint.run_id == source.id,
        )
    )
    summary = (
        (checkpoint.summary if checkpoint else {})
        .get("candidates", {})
        .get(candidate.candidate_key if candidate else "", {})
    )
    if (
        run.final_selection_id != selection.id
        or run.split_version_id != split.id
        or run.experiment_id != split.experiment_id
        or selection.split_version_id != split.id
        or selection.experiment_id != split.experiment_id
        or source.split_version_id != split.id
        or source.experiment_id != split.experiment_id
        or source.status != "succeeded"
        or run.dataset_version_id != dataset.id
        or run.strategy_version_id != strategy.id
        or config.split_sha256 != split.content_sha256
        or config.selected_strategy.id != str(strategy.id)
        or source_config != config.source_config
        or source_config.sha256 != selection.source_config_sha256
        or source.request_sha256 != source_config.sha256
        or SplitDefinition(
            dataset=source_config.dataset,
            universe=source_config.universe,
            windows=source_config.windows,
        )
        != definition
        or config.selected_strategy.sha256 != selection.source_code_sha256
        or strategy.content_sha256 != selection.source_code_sha256
        or hashlib.sha256(strategy.code.encode()).hexdigest()
        != selection.source_code_sha256
        or strategy.mode != "portable"
        or strategy.task != source_config.task
        or candidate is None
        or candidate.code != strategy.code
        or candidate.code_sha256 != selection.source_code_sha256
        or checkpoint is None
        or not checkpoint.complete
        or checkpoint.manifest_sha256 != selection.source_checkpoint_sha256
        or checkpoint.config_sha256 != source_config.sha256
        or summary.get("id") != str(candidate.id)
        or not summary.get("valid")
        or not summary.get("complete")
        or run.latest_checkpoint_id is not None
        or run.resume_checkpoint_id is not None
        or not run.research_image_digest
        or run.research_image_digest != source.research_image_digest
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", run.research_image_digest)
        or run.portable_policy_image_digest != source.portable_policy_image_digest
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", run.portable_policy_image_digest or ""
        )
    ):
        raise DomainError(
            "final_binding_mismatch",
            "Frozen selection, exact code, source config or final run bindings differ",
        )
    from backend.app.experiment_services import portable_selected_metadata
    from workers.research.portable_final_staging import validate_portable_final_binding

    seed = owned_resource(
        session, StrategyVersion, source.strategy_version_id, run.owner_id
    )
    expected_manifest, expected_parameters = portable_selected_metadata(
        source_config, seed, candidate
    )
    try:
        validate_portable_final_binding(config, strategy.manifest, strategy.parameters)
        if (
            strategy.manifest != expected_manifest
            or strategy.parameters != expected_parameters
        ):
            raise ValueError(
                "Frozen selected policy differs from source policy contract"
            )
    except (ValueError, TypeError, KeyError):
        raise DomainError(
            "final_binding_mismatch",
            "Frozen portable manifest, parameters or state differ",
        ) from None
    return selection, source_config, dataset, strategy, split


def validate_final_inputs(session, run, settings):
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )

    result = validate_final_binding(session, run)
    selection, config, _, _, _ = result
    if config.environment_digest != portable_environment_digest():
        raise DomainError(
            "environment_mismatch",
            "Final evaluation requires the pinned source environment",
            422,
        )
    validate_selection_source(
        session,
        run.owner_id,
        FinalSelectionCreate(
            split_version_id=selection.split_version_id,
            source_run_id=selection.source_run_id,
            source_candidate_id=selection.source_candidate_id,
            source_checkpoint_id=selection.source_checkpoint_id,
        ),
        settings,
    )
    from backend.app.storage import read_verified
    from workers.research.portable_final_staging import (
        stage_portable_final_dataset_window,
    )

    dataset = result[2]
    hashes = dataset.manifest.get("files", {}) | dataset.manifest.get(
        "artifact_files", {}
    )

    def read_file(name):
        if name not in hashes:
            raise DomainError(
                "invalid_dataset_file",
                "Dataset file is absent from immutable manifest",
                422,
            )
        return read_verified(
            settings.storage_root,
            f"{dataset.storage_key}/{name}",
            hashes[name],
            settings.max_artifact_bytes,
        )

    try:
        stage_portable_final_dataset_window(
            final_config(run), dataset.manifest, read_file
        )
    except DomainError:
        raise
    except (ValueError, KeyError, TypeError) as error:
        raise DomainError(
            "final_preflight_failed", redact(str(error))[:2000], 422
        ) from None
    return result


def _provisional_run(owner_id, final, selection, split, source):
    return ResearchRun(
        id=uuid4(),
        owner_id=owner_id,
        idempotency_key="preflight-only",
        request_sha256=final.sha256,
        config=final.model_dump(mode="json"),
        dataset_version_id=source.dataset_version_id,
        strategy_version_id=selection.strategy_version_id,
        experiment_id=split.experiment_id,
        split_version_id=split.id,
        final_selection_id=selection.id,
        research_image_digest=source.research_image_digest,
        portable_policy_image_digest=source.portable_policy_image_digest,
        latest_checkpoint_id=None,
        resume_checkpoint_id=None,
        status="queued",
        generation=1,
        next_event_seq=0,
    )


def _quota(session, owner_id, settings):
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(ResearchRun.owner_id == owner_id, ResearchRun.status.not_in(TERMINAL))
    )
    return max(0, settings.max_active_runs_per_user - active)


def preflight_final_evaluation(session, owner_id, body, settings):
    final, selection, split, source = _make_config(session, owner_id, body)
    provisional = _provisional_run(owner_id, final, selection, split, source)
    validate_final_inputs(session, provisional, settings)
    snapshot = get_exposure_snapshot(session, owner_id, split)
    remaining = _quota(session, owner_id, settings)
    return PortableFinalEvaluationPreflight(
        eligible=remaining > 0,
        selection_id=selection.id,
        config=final,
        exposure_snapshot=snapshot,
        quota_remaining=remaining,
        warnings=[
            TRACKING_COVERAGE,
            "Admission is permanently recorded as possible exposure, including cancellation and failure.",
            "Portable policy and engine execute in separate sibling containers. Runtime and external framework equivalence are not certified; prior tracked exposure does not establish an unseen holdout.",
        ],
    )


def _idempotency_key(value):
    if (
        not value
        or len(value) > 128
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise DomainError(
            "invalid_idempotency_key",
            "Idempotency-Key must be 1 to 128 printable ASCII characters without spaces",
            422,
        )


def record_final_admission(session, run, selection, split, dataset):
    config = final_config(run)
    snapshot = get_exposure_snapshot(session, run.owner_id, split)
    record_exposure(
        session,
        owner_id=run.owner_id,
        dataset=dataset,
        window=DateWindow(start=dataset.start_date, end=config.holdout.end),
        purpose="final_evaluation_admission",
        source_key=f"final:{run.id}:{run.generation}",
        run=run,
        provenance={
            "generation": run.generation,
            "selection_id": str(selection.id),
            "final_config_sha256": config.sha256,
            "admission_before_dispatch": True,
            "requested_window": config.holdout.model_dump(mode="json"),
            "coverage": "conservative_dataset_prefix",
            "prior_exposure_snapshot": snapshot,
            "tracking_coverage": TRACKING_COVERAGE,
        },
    )
    return snapshot


def submit_final_evaluation(session, owner_id, body, idempotency_key, settings):
    _idempotency_key(idempotency_key)
    advisory_lock(session, f"submit:{owner_id}")
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.idempotency_key == idempotency_key,
        )
    )
    if previous:
        if (
            previous.config.get("schema_version")
            != "tradegrad.portable.final-evaluation.v1"
        ):
            raise DomainError(
                "idempotency_conflict", "This key already belongs to another request"
            )
        previous_config = final_config(previous)
        if (
            previous_config.selection_id != body.selection_id
            or previous_config.max_runtime_seconds != body.max_runtime_seconds
        ):
            raise DomainError(
                "idempotency_conflict",
                "This key already belongs to another final request",
            )
        return previous
    config, selection, split, source = _make_config(session, owner_id, body)
    lock_split(session, owner_id, split)
    run = _provisional_run(owner_id, config, selection, split, source)
    run.idempotency_key = idempotency_key
    _, _, dataset, _, _ = validate_final_inputs(session, run, settings)
    if _quota(session, owner_id, settings) < 1:
        raise DomainError("run_quota", "Active run limit reached", 429)
    session.add(run)
    session.flush()
    snapshot = record_final_admission(session, run, selection, split, dataset)
    # Dispatch is impossible before the exposure row is part of this transaction.
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "final_evaluation_admitted",
        {
            "status": "queued",
            "selection_id": str(selection.id),
            "config_sha256": config.sha256,
            "exposure_snapshot": snapshot,
        },
    )
    session.flush()
    return run


def owned_final_run(session, run_id, owner_id, *, lock=False):
    run = owned_run(session, run_id, owner_id, lock=lock)
    final_config(run)
    return run


def _check_evaluation_binding(run, config, evaluation, attempt, selection):
    expected = {"split": "holdout", **config.holdout.model_dump(mode="json")}
    if (
        evaluation.run_id != run.id
        or evaluation.attempt_id != attempt.id
        or evaluation.generation != run.generation
        or evaluation.split != "holdout"
        or evaluation.window != expected
        or evaluation.window_sha256 != payload_hash(expected)
        or evaluation.code_sha256 != config.selected_strategy.sha256
        or evaluation.image_digest != run.research_image_digest
        or evaluation.candidate_id != selection.source_candidate_id
        or evaluation.candidate_key != f"final:{config.selection_id}"
        or evaluation.sandbox_id != f"tradegrad-portable-{evaluation.id}"
    ):
        raise DomainError(
            "final_evaluation_mismatch",
            "Evaluation is not the exact selected holdout execution",
        )


def begin_final_evaluation(session, attempt_id, generation, lease_token):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    selection, _, _, _, _ = validate_final_binding(session, run)
    config = final_config(run)
    if run.status == "cancel_requested":
        raise DomainError(
            "cancel_requested", "Cancellation prevents launching another sandbox"
        )
    if not session.scalar(
        select(DataUsageExposure.id).where(
            DataUsageExposure.owner_id == run.owner_id,
            DataUsageExposure.source_key == f"final:{run.id}:{generation}",
        )
    ):
        raise DomainError(
            "admission_required", "Final data admission was not durably recorded"
        )
    previous = session.scalar(
        select(Evaluation).where(Evaluation.attempt_id == attempt.id)
    )
    if previous:
        _check_evaluation_binding(run, config, previous, attempt, selection)
        return previous
    identifier = uuid4()
    window = {"split": "holdout", **config.holdout.model_dump(mode="json")}
    evaluation = Evaluation(
        id=identifier,
        run_id=run.id,
        attempt_id=attempt.id,
        generation=generation,
        candidate_id=selection.source_candidate_id,
        candidate_key=f"final:{selection.id}",
        code_sha256=config.selected_strategy.sha256,
        split="holdout",
        window=window,
        window_sha256=payload_hash(window),
        image_digest=run.research_image_digest,
        sandbox_id=f"tradegrad-portable-{identifier}",
        sandbox_stopped=False,
        status="registered",
    )
    session.add(evaluation)
    session.flush()
    return evaluation


def complete_final_evaluation(
    session,
    attempt_id,
    generation,
    lease_token,
    evaluation_id,
    *,
    outcome,
    sandbox_stopped,
    settings,
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    selection, _, _, _, _ = validate_final_binding(session, run)
    item = session.scalar(
        select(Evaluation)
        .where(
            Evaluation.id == evaluation_id,
            Evaluation.run_id == run.id,
            Evaluation.attempt_id == attempt.id,
        )
        .with_for_update()
    )
    if item is None:
        raise DomainError("not_found", "Registered final evaluation not found", 404)
    _check_evaluation_binding(run, final_config(run), item, attempt, selection)
    from backend.app.portable_services import _complete_portable_evidence

    return _complete_portable_evidence(
        session,
        run,
        attempt,
        final_config(run),
        item,
        outcome=outcome,
        sandbox_stopped=sandbox_stopped,
        settings=settings,
    )


def finish_final_attempt(
    session,
    attempt_id,
    generation,
    lease_token,
    outcome,
    sandbox_stopped,
    artifacts=None,
):
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    selection, _, dataset, _, _ = validate_final_binding(session, run)
    config = final_config(run)
    bounded_json(outcome)
    if not isinstance(outcome, dict):
        raise DomainError(
            "invalid_research_payload", "Final outcome must be an object", 422
        )
    evaluations = session.scalars(
        select(Evaluation).where(Evaluation.attempt_id == attempt.id)
    ).all()
    if not sandbox_stopped or any(not item.sandbox_stopped for item in evaluations):
        run.error = attempt.error = (
            "Recovery blocked: final sandbox termination is unconfirmed"
        )
        append_event(
            session, run, "recovery_blocked", {"message": run.error}, attempt.id
        )
        return run
    evaluation = evaluations[0] if len(evaluations) == 1 else None
    if evaluation:
        _check_evaluation_binding(run, config, evaluation, attempt, selection)
    status = outcome.get("status", "failed")
    if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
        status = "failed"
    if run.status == "cancel_requested":
        status = "cancelled"
    if status == "succeeded" and (
        evaluation is None
        or evaluation.status != "succeeded"
        or evaluation.finished_at is None
        or str(outcome.get("evaluation_id")) != str(evaluation.id)
        or outcome.get("exit_code") != 0
    ):
        status = "failed"
    admission = session.scalar(
        select(DataUsageExposure).where(
            DataUsageExposure.owner_id == run.owner_id,
            DataUsageExposure.source_key == f"final:{run.id}:{generation}",
        )
    )
    if admission is None:
        raise DomainError(
            "admission_required", "Final run has no permanent admission evidence"
        )
    for artifact in artifacts or []:
        session.add(Artifact(run_id=run.id, attempt_id=attempt.id, **artifact))
    payload = (evaluation.payload or {}) if evaluation else {}
    envelope = payload.get("metrics", {})
    numbers = envelope.get("metrics", envelope) if isinstance(envelope, dict) else {}
    score = payload.get(
        "combined_score",
        envelope.get("combined_score") if isinstance(envelope, dict) else None,
    )
    attempt.status = run.status = status
    attempt.sandbox_stopped = True
    attempt.finished_at = utcnow()
    attempt.exit_code = outcome.get("exit_code")
    attempt.error = run.error = (
        redact(str(outcome["error"]))[:4000]
        if outcome.get("error")
        else (
            "Final evaluation evidence is incomplete"
            if status == "failed" and outcome.get("status") == "succeeded"
            else None
        )
    )
    run.result = {
        "execution_mode": "final_evaluation",
        "evaluation_id": str(evaluation.id) if evaluation else None,
        "selection_id": str(selection.id),
        "metrics": numbers if status == "succeeded" else {},
        "combined_score": score if status == "succeeded" else None,
        "is_synthetic": dataset.is_synthetic,
        "evaluation_mode": "portable",
        "result_trust": "isolated_policy_trusted_akquant_engine"
        if status == "succeeded"
        else None,
        "ledger_origin": "trusted_engine" if status == "succeeded" else None,
        "runtime_certified": False,
        "external_framework_equivalence": "not_certified",
        "objective_definition": config.source_config.objective.definition_version,
        "holdout_executed": bool(evaluation and evaluation.status == "succeeded"),
        "image_digest": run.research_image_digest,
        "policy_image_digest": run.portable_policy_image_digest,
        "exposure_snapshot": admission.provenance["prior_exposure_snapshot"],
    }
    append_event(
        session,
        run,
        "run_finished",
        {
            "status": status,
            "execution_mode": "final_evaluation",
            "evaluation_id": str(evaluation.id) if evaluation else None,
            "error": run.error,
        },
        attempt.id,
    )
    session.flush()
    return run


def retry_final_evaluation(session, run_id, owner_id, settings, *, idempotency_key):
    _idempotency_key(idempotency_key)
    advisory_lock(session, f"submit:{owner_id}")
    run = owned_final_run(session, run_id, owner_id)
    split = owned_resource(session, SplitVersion, run.split_version_id, owner_id)
    lock_split(session, owner_id, split)
    run = owned_final_run(session, run_id, owner_id, lock=True)
    receipt = session.scalar(
        select(RunEvent.id).where(
            RunEvent.run_id == run.id,
            RunEvent.type == "final_retry_admitted",
            RunEvent.payload["idempotency_key"].astext == idempotency_key,
        )
    )
    if receipt:
        return run
    if run.status not in {"failed", "cancelled", "interrupted"}:
        raise DomainError(
            "invalid_transition",
            "Only stopped failed, cancelled or interrupted final evaluations can retry",
        )
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .with_for_update()
    ).all()
    pending = session.scalar(
        select(Evaluation.id)
        .where(Evaluation.run_id == run.id, Evaluation.sandbox_stopped.is_(False))
        .limit(1)
    )
    if pending or any(
        not attempt.sandbox_stopped or attempt.finished_at is None
        for attempt in attempts
    ):
        raise DomainError(
            "sandbox_stop_required",
            "Retry requires confirmed termination of every earlier sandbox",
        )
    selection, _, dataset, _, _ = validate_final_inputs(session, run, settings)
    if _quota(session, owner_id, settings) < 1:
        raise DomainError("run_quota", "Active run limit reached", 429)
    run.generation += 1
    run.active_attempt_id = None
    run.status, run.error, run.result = "queued", None, None
    snapshot = record_final_admission(session, run, selection, split, dataset)
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "final_retry_admitted",
        {
            "status": "queued",
            "generation": run.generation,
            "idempotency_key": idempotency_key,
            "retry_mode": "new_physical_final_attempt",
            "exposure_snapshot": snapshot,
        },
    )
    session.flush()
    return run
