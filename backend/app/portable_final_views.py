"""Owned portable holdout evidence; never reopen source optimization for feedback."""

import hashlib
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select

from backend.app.benchmark_services import comparison_view
from backend.app.final_evaluation_views import validate_final_terminal
from backend.app.final_result_admission import require_final_result_admission
from backend.app.models import (
    Artifact,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
)
from backend.app.portable_final_services import (
    _check_evaluation_binding,
    final_config,
    validate_final_binding,
)
from backend.app.portable_schemas import PortableResultQuery
from backend.app.portable_services import portable_score, verify_portable_bundle_binding
from backend.app.portable_views import portable_data_evidence
from backend.app.research_services import bounded_json, payload_hash
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.portable_final_config import (
    PortableFinalEvaluationConfig,
)


class PortableFinalResultQuery(PortableResultQuery):
    schema_version: Literal["tradegrad.portable.final-result-query.v1"] = (
        "tradegrad.portable.final-result-query.v1"
    )
    result_schema_version: Literal["tradegrad.portable-result.v1"] = (
        "tradegrad.portable-result.v1"
    )
    config: PortableFinalEvaluationConfig
    selection_id: UUID
    exposure_snapshot: dict
    selection_feedback: Literal[False] = False


@dataclass(frozen=True)
class VerifiedPortableFinal:
    evaluation: Evaluation
    run: ResearchRun
    dataset: DatasetVersion
    artifact: Artifact
    bundle: dict
    content: bytes


def validate_portable_final_result(evaluation, run, dataset, artifact, content):
    """Pure persisted-byte check; owner and selected-source binding belong to loader."""
    try:
        config = final_config(run)
        window = {"split": "holdout", **config.holdout.model_dump(mode="json")}
        ref = evaluation.payload["portable_result"]
        if (
            run.status != "succeeded"
            or (run.result or {}).get("evaluation_id") != str(evaluation.id)
            or evaluation.payload.get("status") != "succeeded"
            or evaluation.payload.get("exit_code") != 0
            or evaluation.status != "succeeded"
            or not evaluation.sandbox_stopped
            or evaluation.finished_at is None
            or evaluation.run_id != run.id
            or evaluation.generation != run.generation
            or evaluation.attempt_id != run.active_attempt_id
            or evaluation.window != window
            or evaluation.split != "holdout"
            or evaluation.window_sha256 != payload_hash(window)
            or evaluation.code_sha256 != config.selected_strategy.sha256
            or run.final_selection_id != config.selection_id
            or run.split_version_id != config.split_version_id
            or str(run.strategy_version_id) != config.selected_strategy.id
            or str(dataset.id) != config.source_config.dataset.id
            or dataset.owner_id != run.owner_id
            or dataset.content_sha256 != config.source_config.dataset.sha256
            or artifact.id != UUID(ref["artifact_id"])
            or artifact.run_id != run.id
            or artifact.attempt_id != evaluation.attempt_id
            or artifact.name != f"evaluation-{evaluation.id}-portable-result.json"
            or artifact.media_type != "application/json"
            or artifact.sha256 != ref["sha256"]
            or artifact.size_bytes != ref["size_bytes"]
            or not 0 < artifact.size_bytes <= 8 * 1_048_576
            or len(content) != artifact.size_bytes
            or hashlib.sha256(content).hexdigest() != artifact.sha256
            or evaluation.image_digest != run.research_image_digest
            or evaluation.payload.get("image_digest") != run.research_image_digest
            or evaluation.payload.get("policy_image_digest")
            != run.portable_policy_image_digest
            or evaluation.payload_sha256 != payload_hash(evaluation.payload)
        ):
            raise ValueError("Portable final artifact binding differs")
        bundle = verify_portable_bundle_binding(content, config, evaluation)
        if (
            evaluation.payload.get("metrics") != bundle["summary"]
            or evaluation.payload.get("combined_score") != portable_score(bundle)
            or evaluation.payload.get("result_trust") != bundle["result_trust"]
            or evaluation.payload.get("ledger_origin") != "trusted_engine"
            or evaluation.payload.get("runtime_certified") is not False
        ):
            raise ValueError("Portable final summary differs from engine bytes")
        return bundle
    except (KeyError, ValueError, TypeError, AttributeError, DomainError):
        raise DomainError(
            "portable_final_result_invalid",
            "Portable final evidence failed integrity verification",
            409,
        ) from None


def load_owned_final_result(session, evaluation_id, owner_id, settings):
    pair = session.execute(
        select(Evaluation, ResearchRun)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(
            Evaluation.id == evaluation_id,
            ResearchRun.owner_id == owner_id,
            ResearchRun.config["schema_version"].astext
            == "tradegrad.portable.final-evaluation.v1",
        )
    ).one_or_none()
    if pair is None:
        raise DomainError("not_found", "Portable final evaluation not found", 404)
    evaluation, run = pair
    selection, _, dataset, _, _ = validate_final_binding(session, run)
    attempt = session.get(ExecutionAttempt, evaluation.attempt_id)
    if (
        attempt is None
        or attempt.id != run.active_attempt_id
        or attempt.run_id != run.id
        or attempt.generation != run.generation
        or attempt.exit_code != 0
        or attempt.status != "succeeded"
        or not attempt.sandbox_stopped
        or attempt.finished_at is None
    ):
        raise DomainError(
            "result_unavailable",
            "Portable final attempt has not durably succeeded",
            409,
        )
    validate_final_terminal(evaluation, run, attempt)
    _check_evaluation_binding(run, final_config(run), evaluation, attempt, selection)
    require_final_result_admission(session, run, evaluation, owner_id, dataset=dataset)
    try:
        artifact_id = UUID(evaluation.payload["portable_result"]["artifact_id"])
    except (TypeError, KeyError, ValueError):
        raise DomainError(
            "result_unavailable", "Portable final result is unavailable", 409
        ) from None
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.run_id == run.id,
            Artifact.attempt_id == evaluation.attempt_id,
        )
    )
    if artifact is None:
        raise DomainError(
            "portable_final_result_invalid", "Portable final artifact is missing", 409
        )
    content = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        min(settings.max_artifact_bytes, 8 * 1_048_576),
    )
    bundle = validate_portable_final_result(evaluation, run, dataset, artifact, content)
    from backend.app.benchmark_services import verify_persisted_benchmark

    verify_persisted_benchmark(
        session, owner_id, final_config(run), dataset.manifest, bundle, settings
    )
    return VerifiedPortableFinal(evaluation, run, dataset, artifact, bundle, content)


def final_result_view(
    result, *, exposure_snapshot, section="account_series", offset=0, limit=100
):
    allowed = {
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    }
    if (
        section not in allowed
        or type(offset) is not int
        or not 0 <= offset <= 100000
        or type(limit) is not int
        or not 1 <= limit <= 500
    ):
        raise DomainError(
            "invalid_result_query", "Portable final query exceeds bounds", 422
        )
    bounded_json(exposure_snapshot, 256_000)
    config, bundle = final_config(result.run), result.bundle
    return PortableFinalResultQuery(
        run_id=result.run.id,
        image_digest=result.run.research_image_digest,
        policy_image_digest=result.run.portable_policy_image_digest,
        environment_digest=config.environment_digest,
        is_synthetic=result.dataset.is_synthetic,
        data_evidence=portable_data_evidence(result.dataset),
        currency=bundle["manifest"]["currency"],
        evaluation_id=result.evaluation.id,
        result_trust=bundle["result_trust"],
        ledger_origin=bundle["ledger_origin"],
        artifact_sha256=result.artifact.sha256,
        identity=bundle["identity"],
        window=bundle["window"],
        summary=bundle["summary"],
        benchmark=comparison_view(bundle.get("benchmark"), max_points=500),
        combined_score=portable_score(bundle),
        section=section,
        rows=bundle[section][offset : offset + limit],
        count=len(bundle[section]),
        offset=offset,
        limit=limit,
        config=config,
        selection_id=config.selection_id,
        exposure_snapshot=exposure_snapshot,
    )
