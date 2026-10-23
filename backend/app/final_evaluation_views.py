"""Read-only, owned holdout results without reopening optimizer provenance."""

from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import select

from backend.app.final_result_admission import require_final_result_admission
from backend.app.models import Artifact, Evaluation, ExecutionAttempt, ResearchRun
from backend.app.research_services import bounded_json
from backend.app.research_view_schemas import (
    ResultQueryPublic,
    ViewModel,
)
from backend.app.research_views import (
    MAX_WIRE_BYTES,
    SECTIONS,
    VerifiedResult,
    _invalid,
    _validate_native_persisted_result,
    query_result_table,
)
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig


class FinalResultWindow(ViewModel):
    split: Literal["holdout"]
    start: date
    end: date


class FinalResultQuery(ResultQueryPublic):
    schema_version: Literal["tradegrad.final-result-query.v1"] = (
        "tradegrad.final-result-query.v1"
    )
    split: Literal["holdout"]
    window: FinalResultWindow
    config: FinalEvaluationConfig
    selection_id: UUID
    exposure_snapshot: dict
    selection_feedback: Literal[False] = False


def validate_final_terminal(evaluation, run, attempt=None):
    """A completed evaluation is not a committed logical final success."""
    if (
        run.status != "succeeded"
        or not isinstance(run.result, dict)
        or run.result.get("evaluation_id") != str(evaluation.id)
        or evaluation.run_id != run.id
        or evaluation.generation != run.generation
        or run.active_attempt_id is None
        or evaluation.attempt_id != run.active_attempt_id
        or evaluation.status != "succeeded"
        or evaluation.finished_at is None
        or not evaluation.sandbox_stopped
    ):
        raise DomainError(
            "result_unavailable", "Final result is not durably committed", 409
        )
    if attempt is not None and (
        attempt.id != run.active_attempt_id
        or attempt.run_id != run.id
        or attempt.generation != run.generation
        or attempt.status != "succeeded"
        or attempt.finished_at is None
        or not attempt.sandbox_stopped
        or attempt.exit_code != 0
    ):
        raise DomainError(
            "result_unavailable", "Final attempt has not durably succeeded", 409
        )


def validate_final_persisted_result(evaluation, run, dataset, artifact, content):
    """Pure final-config/bytes seam; DB ownership/frozen binding is checked by loader."""
    validate_final_terminal(evaluation, run)
    try:
        config = FinalEvaluationConfig.model_validate(run.config)
        window = {
            "split": "holdout",
            "start": str(config.holdout.start),
            "end": str(config.holdout.end),
        }
        if (
            run.final_selection_id != config.selection_id
            or run.split_version_id != config.split_version_id
        ):
            raise ValueError("Final job selection/split differs from immutable config")
        return _validate_native_persisted_result(
            evaluation,
            run,
            dataset,
            artifact,
            content,
            config=config.source_config,
            config_sha256=config.sha256,
            strategy_version_id=UUID(config.selected_strategy.id),
            allowed_windows=(window,),
            selected_code_sha256=config.selected_strategy.sha256,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        raise _invalid() from None


def load_owned_final_result(session, evaluation_id, owner_id, settings):
    # Perform owner+schema authorization before lookup of artifact or files.
    pair = session.execute(
        select(Evaluation, ResearchRun)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(
            Evaluation.id == evaluation_id,
            ResearchRun.owner_id == owner_id,
            ResearchRun.config["schema_version"].astext
            == "tradegrad.final-evaluation.v1",
        )
    ).one_or_none()
    if pair is None:
        raise DomainError("not_found", "Final evaluation not found", 404)
    evaluation, run = pair
    from backend.app.final_evaluation_services import (
        _check_evaluation_binding,
        validate_final_binding,
    )

    selection, _source, dataset, _strategy, _split = validate_final_binding(
        session, run
    )
    attempt = session.get(ExecutionAttempt, evaluation.attempt_id)
    if attempt is None:
        raise DomainError("result_unavailable", "Final attempt is missing", 409)
    validate_final_terminal(evaluation, run, attempt)
    _check_evaluation_binding(
        run,
        FinalEvaluationConfig.model_validate(run.config),
        evaluation,
        attempt,
        selection,
    )
    require_final_result_admission(session, run, evaluation, owner_id, dataset=dataset)
    try:
        artifact_id = UUID(evaluation.payload["result_bundle"]["artifact_id"])
    except (KeyError, ValueError, TypeError):
        raise _invalid() from None
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == artifact_id,
            Artifact.run_id == run.id,
            Artifact.attempt_id == evaluation.attempt_id,
        )
    )
    if artifact is None:
        raise _invalid()
    content = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        min(settings.max_artifact_bytes, MAX_WIRE_BYTES),
    )
    bundle = validate_final_persisted_result(
        evaluation, run, dataset, artifact, content
    )
    return VerifiedResult(evaluation, run, dataset, artifact, bundle, content)


def final_result_view(
    result,
    *,
    exposure_snapshot,
    sections=None,
    start=None,
    end=None,
    listing_id=None,
    max_points=1000,
    offset=0,
    limit=100,
):
    config = FinalEvaluationConfig.model_validate(result.run.config)
    sections = sections or ["equity", "drawdown"]
    if (
        len(sections) > len(SECTIONS)
        or len(set(sections)) != len(sections)
        or any(section not in SECTIONS for section in sections)
        or (start is not None and end is not None and start >= end)
    ):
        raise DomainError("invalid_result_query", "Invalid final result query", 422)
    bounded_json(exposure_snapshot, 256_000)
    symbol = None
    if listing_id is not None:
        symbol = result.dataset.manifest.get("evaluator_symbol_mapping", {}).get(
            str(listing_id)
        )
        if not isinstance(symbol, str) or not symbol:
            raise DomainError("invalid_result_listing", "Listing not in snapshot", 422)
        if config.source_config.task.endswith("_time"):
            symbol = "target_symbol"
    bundle, item = result.bundle, result.evaluation
    semantics = result.dataset.manifest.get("price_semantics")
    return FinalResultQuery(
        evaluation_id=item.id,
        run_id=item.run_id,
        candidate_key=item.candidate_key,
        code_sha256=item.code_sha256,
        config_sha256=config.sha256,
        artifact_sha256=result.artifact.sha256,
        split="holdout",
        window=item.window,
        config=config,
        selection_id=config.selection_id,
        exposure_snapshot=exposure_snapshot,
        currency=bundle["engine"]["currency"],
        price_definition={
            "execution_policy": bundle["engine"]["resolved_execution_policy"],
            "dataset_price_semantics": semantics,
            "definition_status": "declared"
            if isinstance(semantics, dict)
            else "dataset_semantics_unavailable",
        },
        result_trust=bundle["result_trust"],
        is_synthetic=result.dataset.is_synthetic,
        metrics=bundle["metrics"],
        tables={
            section: query_result_table(
                bundle[section],
                section,
                start=start,
                end=end,
                symbol=symbol,
                max_points=max_points,
                offset=offset,
                limit=limit,
                timezone=bundle["engine"]["resolved_config"]["timezone"],
            )
            for section in sections
        },
        capabilities=bundle["capabilities"],
        fees=bundle["fees"],
        benchmark=bundle["benchmark"],
        warnings=[
            "This is a separate final evaluation of an already frozen candidate; it is not optimizer feedback.",
            "Prior platform-recorded exposure and incomplete external history do not establish an independently unseen holdout.",
            "Native strategy and engine share a process; the ledger is not independently trusted against strategy modification.",
            "Displayed metrics describe the full persisted holdout window, not filtered chart slices.",
        ],
        listing_id=listing_id,
        start=start,
        end=end,
    )
