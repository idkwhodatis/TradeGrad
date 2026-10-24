"""Strict independent result loaders, not a relaxation of historical eligibility."""

from __future__ import annotations

import hashlib
from contextvars import ContextVar
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from backend.app.independent_schemas import (
    ControlledArmPublic,
    ControlledExperimentCreate,
    ControlledExperimentPublic,
    DiagnosticReplayCreate,
    IndependentEvaluationPublic,
    IndependentResultPin,
)
from backend.app.independent_services import (
    _check_evaluation,
    _integrity,
    independent_config,
    owned_independent,
    require_independent_admission,
    resolve_source,
    validate_independent_inputs,
)
from backend.app.models import (
    Artifact,
    ControlledArm,
    ControlledExperiment,
    DatasetVersion,
    Evaluation,
    ExecutionAttempt,
    Experiment,
    IndependentEvaluation,
    ResearchRun,
    SplitVersion,
    StrategyVersion,
)
from backend.app.research_services import payload_hash
from backend.app.research_views import (
    MAX_WIRE_BYTES,
    VerifiedResult,
    _validate_native_persisted_result,
)
from backend.app.result_access import record_verified_result_access
from backend.app.services import DomainError
from backend.app.storage import read_verified
from packages.research_trace.contracts import digest
from packages.tradegrad_adapter.independent_config import NATIVE_SCHEMA, SCHEMAS
from packages.tradegrad_adapter.results import canonical_json_bytes

_depth = ContextVar("independent_source_depth", default=0)


def _artifact(session, run, evaluation, payload):
    key = (
        "result_bundle"
        if run.config.get("schema_version") == NATIVE_SCHEMA
        else "portable_result"
    )
    try:
        reference = payload[key]
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.id == UUID(reference["artifact_id"]),
                Artifact.run_id == run.id,
                Artifact.attempt_id == evaluation.attempt_id,
            )
        )
        name = (
            "result-bundle.json" if key == "result_bundle" else "portable-result.json"
        )
        if (
            artifact is None
            or artifact.name != f"evaluation-{evaluation.id}-{name}"
            or artifact.sha256 != reference["sha256"]
            or artifact.size_bytes != reference["size_bytes"]
            or artifact.media_type != "application/json"
            or not 0 < artifact.size_bytes <= MAX_WIRE_BYTES
        ):
            raise ValueError("Result registration differs")
        return artifact
    except (ValueError, KeyError, TypeError):
        raise _integrity() from None


def validate_independent_payload(session, run, evaluation, payload, settings):
    """Completion verifies persisted bytes before granting physical leaf success."""
    from backend.app.models import utcnow

    config = independent_config(run)
    artifact = _artifact(session, run, evaluation, payload)
    dataset = session.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == run.dataset_version_id,
            DatasetVersion.owner_id == run.owner_id,
        )
    )
    content = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        min(settings.max_artifact_bytes, MAX_WIRE_BYTES),
    )
    if (
        dataset is None
        or dataset.content_sha256 != config.source_config.dataset.sha256
        or str(dataset.id) != config.source_config.dataset.id
        or len(content) != artifact.size_bytes
        or hashlib.sha256(content).hexdigest() != artifact.sha256
    ):
        raise _integrity()
    if (
        payload.get("status") != "succeeded"
        or payload.get("exit_code") != 0
        or payload.get("image_digest") != run.research_image_digest
    ):
        raise _integrity()
    if config.schema_version == NATIVE_SCHEMA:
        projected = SimpleNamespace(
            **{
                column.name: getattr(evaluation, column.name)
                for column in Evaluation.__table__.columns
            }
        )
        projected.payload, projected.payload_sha256 = payload, payload_hash(payload)
        projected.status, projected.sandbox_stopped, projected.finished_at = (
            "succeeded",
            True,
            utcnow(),
        )
        bundle = _validate_native_persisted_result(
            projected,
            run,
            dataset,
            artifact,
            content,
            config=config.effective_source(),
            config_sha256=config.sha256,
            strategy_version_id=UUID(config.selected_strategy.id),
            allowed_windows=(config.window,),
            selected_code_sha256=config.selected_strategy.sha256,
            independent_config=config,
        )
    else:
        from backend.app.portable_services import verify_portable_bundle_binding

        bundle = verify_portable_bundle_binding(content, config, evaluation)
        if (
            canonical_json_bytes(bundle) != content
            or payload.get("metrics") != bundle["summary"]
            or payload.get("combined_score") is not None
            or payload.get("policy_image_digest") != run.portable_policy_image_digest
            or payload.get("result_trust") != bundle["result_trust"]
            or payload.get("ledger_origin") != "trusted_engine"
            or payload.get("runtime_certified") is not False
        ):
            raise _integrity()
    from backend.app.benchmark_services import verify_persisted_benchmark

    verify_persisted_benchmark(
        session, run.owner_id, config, dataset.manifest, bundle, settings
    )
    verified = VerifiedResult(evaluation, run, dataset, artifact, bundle, content)
    from backend.app.independent_services import load_owned_runtime_observation

    projection = SimpleNamespace(
        **{
            column.name: getattr(evaluation, column.name)
            for column in Evaluation.__table__.columns
        }
    )
    projection.payload = payload
    runtime_result = VerifiedResult(projection, run, dataset, artifact, bundle, content)
    observation, _ = load_owned_runtime_observation(session, runtime_result, settings)
    if observation is None:
        raise DomainError(
            "runtime_observation_required",
            "New independent execution requires its actual runtime initialization evidence",
            409,
        )
    from backend.app.diagnostic_parity_views import validate_parity_payload

    validate_parity_payload(session, verified, payload, settings)
    return verified


def load_owned_independent_result(session, evaluation_id, owner_id, settings):
    depth = _depth.get()
    if depth >= 16:
        raise DomainError(
            "independent_source_depth",
            "Source evidence chain exceeds bounded read admission; no provenance was flattened or discarded",
            409,
        )
    token = _depth.set(depth + 1)
    from backend.app.independent_services import _source_cache

    cache_token = _source_cache.set({}) if _source_cache.get() is None else None
    try:
        pair = session.execute(
            select(Evaluation, ResearchRun)
            .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
            .where(
                Evaluation.id == evaluation_id,
                ResearchRun.owner_id == owner_id,
                ResearchRun.config["schema_version"].astext.in_(SCHEMAS),
            )
        ).one_or_none()
        if pair is None:
            raise DomainError("not_found", "Independent result not found", 404)
        evaluation, run = pair
        validate_independent_inputs(session, run, settings, execution=False)
        config = independent_config(run)
        attempt = session.get(ExecutionAttempt, evaluation.attempt_id)
        from backend.app.final_evaluation_views import validate_final_terminal

        validate_final_terminal(evaluation, run, attempt)
        if attempt is None:
            raise _integrity()
        _check_evaluation(run, config, evaluation, attempt)
        require_independent_admission(session, run, evaluation)
        if evaluation.payload_sha256 != payload_hash(evaluation.payload):
            raise _integrity()
        return validate_independent_payload(
            session, run, evaluation, evaluation.payload, settings
        )
    finally:
        _depth.reset(token)
        if cache_token is not None:
            _source_cache.reset(cache_token)


def independent_public(session, item, run, owner_id, settings, *, admit_result=False):
    config = independent_config(run)
    evaluation = (
        session.scalar(
            select(Evaluation).where(
                Evaluation.run_id == run.id,
                Evaluation.attempt_id == run.active_attempt_id,
            )
        )
        if run.active_attempt_id
        else None
    )
    pin = None
    if admit_result and run.status == "succeeded" and evaluation is not None:
        result = load_owned_independent_result(
            session, evaluation.id, owner_id, settings
        )
        from backend.app.trace_services import load_owned_trace

        trace = load_owned_trace(session, evaluation.id, owner_id, settings)
        record_verified_result_access(session, result, owner_id)
        pin = IndependentResultPin(
            execution_config_sha256=result.bundle["identity"][
                "evaluation_config_sha256"
                if config.schema_version == NATIVE_SCHEMA
                else "run_config_sha256"
            ],
            evaluation_id=evaluation.id,
            execution_attempt_id=evaluation.attempt_id,
            result_manifest_sha256=result.artifact.sha256,
            trace_manifest_sha256=trace.context.trace_manifest_sha256,
            source_id=run.strategy_version_id,
            code_sha256=evaluation.code_sha256,
        )
    return IndependentEvaluationPublic(
        id=run.id,
        independent_id=item.id,
        status=run.status,
        generation=run.generation,
        purpose=item.purpose,
        request=DiagnosticReplayCreate.model_validate(item.request),
        request_sha256=item.request_sha256,
        config_sha256=config.sha256,
        config=config,
        source=config.source,
        experiment_id=run.experiment_id,
        split_version_id=run.split_version_id,
        active_attempt_id=run.active_attempt_id,
        next_event_seq=run.next_event_seq,
        evaluation_id=evaluation.id if evaluation else None,
        result_ready=pin is not None,
        result_pin=pin,
        error="independent_execution_failed" if run.error else None,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def owned_controlled(session, identifier, owner_id):
    item = session.scalar(
        select(ControlledExperiment).where(
            ControlledExperiment.id == identifier,
            ControlledExperiment.owner_id == owner_id,
        )
    )
    if item is None:
        raise DomainError("not_found", "Controlled experiment not found", 404)
    if item.request_sha256 != digest(item.request) or item.protocol_sha256 != digest(
        item.protocol
    ):
        raise _integrity()
    return item


def controlled_public(session, item, owner_id, settings):
    body = ControlledExperimentCreate.model_validate(item.request)
    source = resolve_source(session, owner_id, body.source, settings)
    if digest(source.protocol) != item.protocol_sha256:
        raise _integrity()
    experiment = session.get(Experiment, item.experiment_id)
    split = session.get(SplitVersion, item.split_version_id)
    if (
        experiment is None
        or split is None
        or experiment.owner_id != owner_id
        or split.owner_id != owner_id
        or split.experiment_id != experiment.id
        or experiment.parent_experiment_id != source.result.run.experiment_id
    ):
        raise _integrity()
    arms = session.scalars(
        select(ControlledArm)
        .where(ControlledArm.controlled_experiment_id == item.id)
        .order_by(ControlledArm.ordinal)
    ).all()
    if len(arms) != len(body.arms) or [arm.ordinal for arm in arms] != list(
        range(len(body.arms))
    ):
        raise _integrity()
    values = []
    for arm in arms:
        execution = session.get(IndependentEvaluation, arm.independent_id)
        if execution is None:
            raise _integrity()
        execution, run = owned_independent(session, execution.run_id, owner_id)
        if (
            run.experiment_id != item.experiment_id
            or run.split_version_id != item.split_version_id
            or execution.purpose != "controlled"
            or execution.protocol_sha256 != item.protocol_sha256
        ):
            raise _integrity()
        public = independent_public(
            session, execution, run, owner_id, settings, admit_result=True
        )
        strategy = session.get(StrategyVersion, run.strategy_version_id)
        values.append(
            ControlledArmPublic(
                ordinal=arm.ordinal,
                name=arm.name,
                planned_change=arm.planned_change,
                strategy_version_id=strategy.id,
                code_sha256=strategy.content_sha256,
                parameters=strategy.parameters,
                run_id=run.id,
                status=run.status,
                evaluation_id=public.evaluation_id,
                result_pin=public.result_pin,
                changed_fields=arm.changed_fields,
                interpretation=arm.interpretation,
                error=public.error,
            )
        )
    from backend.app.experiment_services import get_exposure_snapshot

    return ControlledExperimentPublic(
        id=item.id,
        experiment_id=item.experiment_id,
        split_version_id=item.split_version_id,
        parent_experiment_id=experiment.parent_experiment_id,
        root_experiment_id=experiment.root_experiment_id or experiment.id,
        request=body,
        source=body.source,
        request_sha256=item.request_sha256,
        name=body.name,
        hypothesis=body.hypothesis,
        planned_change=body.planned_change,
        protocol=item.protocol,
        protocol_sha256=item.protocol_sha256,
        arms=values,
        exposure_snapshot=get_exposure_snapshot(session, owner_id, split),
        created_at=item.created_at,
    )
