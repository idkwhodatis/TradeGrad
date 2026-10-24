"""Async comparison admission and publication, never an Evaluation or a model call.

Every evidence response re-admits BOTH original sources through the four trace
loaders. The left run anchors are administrative only and never stand in for
right-side authorization, exposure, frozen-final admission or retry fences.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select

from backend.app.comparison_schemas import (
    ComparisonConfig,
    ComparisonResult,
    ComparisonSource,
)
from backend.app.models import (
    Artifact,
    Comparison,
    ComparisonSubmission,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    StrategySpecRevision,
    UniverseVersion,
    utcnow,
)
from backend.app.result_access import record_verified_result_access
from backend.app.services import (
    TERMINAL,
    DomainError,
    advisory_lock,
    append_event,
    fenced_attempt,
    owned_run,
)
from backend.app.storage import read_verified, safe_path
from backend.app.trace_services import load_owned_trace
from backend.app.workbench_services import (
    _clause,
    _source_association,
    _spec_public,
    owned_source,
)
from backend.app.workspace_policy import quota_limit
from packages.research_trace.binding import make_binding
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    DEFAULT_LIMITS,
    TOLERANCE_VERSION,
    ComparisonError,
    ComparisonInput,
    ComparisonManifest,
    ComparisonPublication,
    EventNamespace,
)
from packages.research_trace.contracts import DateRange, canonical_bytes, digest

SCHEMA = "tradegrad.comparison.v1"
MAX_SNAPSHOT_BYTES = 512 * 1024


def _integrity():
    return DomainError(
        "comparison_integrity",
        "Comparison does not match its immutable source evidence",
        409,
    )


def _key(value):
    from backend.app.final_evaluation_services import _idempotency_key

    _idempotency_key(value)


def reject_comparison_key(session, owner_id, key):
    """Call after the existing owner submission lock in every ordinary Run path."""
    from backend.app.independent_services import reject_control_key

    reject_control_key(session, owner_id, key)
    if (
        session.scalar(
            select(ComparisonSubmission.comparison_id).where(
                ComparisonSubmission.owner_id == owner_id,
                ComparisonSubmission.idempotency_key == key,
            )
        )
        is not None
    ):
        raise DomainError(
            "idempotency_conflict", "This key already belongs to a comparison request"
        )


def comparison_config(run, comparison=None):
    try:
        config = ComparisonConfig.model_validate(run.config)
        if run.request_sha256 != config.sha256 or run.result is not None:
            raise ValueError("Comparison run config or status-only result differs")
        if comparison is not None and (
            comparison.run_id != run.id
            or comparison.owner_id != run.owner_id
            or comparison.id != config.comparison_id
            or comparison.input_sha256 != config.input_sha256
            or comparison.engine_input_sha256 != config.engine_input_sha256
            or comparison.input_sha256 != digest(comparison.input_snapshot)
            or comparison.left_evaluation_id != config.request.left.evaluation_id
            or comparison.right_evaluation_id != config.request.right.evaluation_id
        ):
            raise ValueError("Comparison business binding differs")
        return config
    except (ValueError, TypeError, KeyError):
        raise _integrity() from None


def owned_comparison(session, comparison_id, owner_id, *, lock=False):
    # Run lock always precedes Comparison/Attempt locks.
    item = session.scalar(
        select(Comparison).where(
            Comparison.id == comparison_id,
            Comparison.owner_id == owner_id,
        )
    )
    if item is None:
        raise DomainError("not_found", "Comparison not found", 404)
    run = owned_run(session, item.run_id, owner_id, lock=lock)
    if lock:
        item = session.scalar(
            select(Comparison).where(Comparison.id == item.id).with_for_update()
        )
    comparison_config(run, item)
    return item, run


def _typed_config(run):
    from packages.tradegrad_adapter.config import RunConfig
    from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
    from packages.tradegrad_adapter.independent_config import (
        IndependentEvaluationConfig,
        PortableIndependentEvaluationConfig,
    )
    from packages.tradegrad_adapter.portable_config import PortableRunConfig
    from packages.tradegrad_adapter.portable_final_config import (
        PortableFinalEvaluationConfig,
    )

    model = {
        "tradegrad.independent-evaluation.v1": IndependentEvaluationConfig,
        "tradegrad.portable.independent-evaluation.v1": PortableIndependentEvaluationConfig,
        "tradegrad.run.v1": RunConfig,
        "tradegrad.final-evaluation.v1": FinalEvaluationConfig,
        "tradegrad.portable.run.v1": PortableRunConfig,
        "tradegrad.portable.final-evaluation.v1": PortableFinalEvaluationConfig,
    }.get(run.config.get("schema_version"))
    if model is None:
        raise DomainError(
            "comparison_source_unavailable",
            "No immutable leaf result is available for this historical source",
            409,
        )
    return model.model_validate(run.config)


def common_configuration(result, config, namespace):
    """Full fixed policy, separate from strategy code and path-derived pre-state."""
    source = (
        config.effective_source()
        if getattr(config, "execution_mode", None) == "independent_evaluation"
        else getattr(config, "source_config", config)
    )
    raw = source.model_dump(mode="json")
    portable = config.schema_version.startswith("tradegrad.portable.")
    engine = result.bundle["engine"]
    profile = (
        raw.get("engine_profile", {}) if portable else raw.get("market_profile", {})
    )
    fees = {
        key: profile[key]
        for key in (
            "commission_rate",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "min_commission",
            "slippage",
        )
        if key in profile
    }
    return {
        "data": {
            "version": raw["dataset"],
            "task": source.task,
            "price_semantics": result.dataset.manifest.get("price_semantics"),
            "symbol_mapping": result.dataset.manifest.get("evaluator_symbol_mapping"),
        },
        "universe": {
            "version": raw.get("universe"),
            "frozen_universe_sha256": namespace.frozen_universe_sha256,
            "declaration": result.dataset.manifest.get("universe"),
            "state": result.dataset.manifest.get("universe_status"),
        },
        "window": {
            "evaluation": result.evaluation.window,
            "engine": result.bundle.get("window"),
        },
        "warmup": raw["warmup"],
        "initial_state": {
            "initial_cash": raw.get("initial_cash")
            if portable
            else profile.get("initial_cash"),
            "policy_state": raw.get("initial_state") if portable else None,
            "policy_state_status": "configured"
            if portable
            else "strategy_internal_state_not_recorded",
            "engine_holdings": [],
            "engine_pending_orders": [],
            "basis": "configured_fresh_leaf_engine_not_observed_decision_pre_state",
        },
        "state_policy": {
            "engine_reset": "fresh_engine_per_leaf",
            "state_continuation": "none",
            "policy_reset": "frozen_initial_state"
            if portable
            else "fresh_strategy_instance",
            "checkpoint_reconstruction": "not_used",
        },
        "fees": fees,
        "fill": {
            "resolved_execution_policy": engine.get("resolved_execution_policy"),
            "declared_profile": profile,
        },
        "market": {
            "market": result.bundle.get("identity", {}).get(
                "market", source.task[:2].upper()
            ),
            "profile": raw.get("profile"),
            "currency": engine.get("currency"),
            "calendar": profile.get("calendar"),
            "timezone": profile.get("timezone"),
        },
        "RNG": {
            "strategy_rng": {"status": "not_recorded", "value": None},
            "optimizer_seed": {
                "value": raw.get("optimizer", {}).get("seed"),
                "scope": "candidate_search_only_not_strategy_random_state",
            },
        },
        "engine": {
            "resolved_config": engine.get("resolved_config"),
            "name": engine.get("name"),
            "version": engine.get("version"),
            "dependencies": engine.get("dependencies"),
            "environment_digest": source.environment_digest,
            "research_image_digest": result.run.research_image_digest,
            "portable_policy_image_digest": result.run.portable_policy_image_digest,
            "result_trust": result.bundle.get("result_trust"),
            "ledger_origin": result.bundle.get("ledger_origin"),
        },
    }


@dataclass(frozen=True)
class AdmittedSide:
    trace: object
    input: ComparisonInput
    source: ComparisonSource


def load_comparison_side(session, pin, owner_id, settings):
    trace = load_owned_trace(
        session,
        pin.evaluation_id,
        owner_id,
        settings,
        execution_attempt_id=pin.execution_attempt_id,
        result_manifest_sha256=pin.result_manifest_sha256,
        trace_manifest_sha256=pin.trace_manifest_sha256,
    )
    if trace.context.trace_manifest_sha256 != pin.trace_manifest_sha256:
        raise DomainError(
            "trace_pin_mismatch",
            "Comparison trace pin differs; null means verified absence",
            409,
        )
    result = trace.result
    association = _source_association(session, result.evaluation, result.run, owner_id)
    expected = (
        association.candidate_id
        if pin.source_kind == "candidate"
        else association.strategy_version_id
    )
    if expected != pin.source_id:
        raise DomainError(
            "source_evaluation_mismatch",
            "Selected source is not the exact source of this evaluation",
            409,
        )
    resource = owned_source(session, pin.source_kind, pin.source_id, owner_id)
    if (
        resource.code_sha256 != pin.code_sha256
        or result.evaluation.code_sha256 != pin.code_sha256
    ):
        raise DomainError("source_pin_mismatch", "Comparison source hash differs", 409)
    spec = None
    if pin.spec_revision is not None:
        row = session.scalar(
            select(StrategySpecRevision).where(
                _clause(resource),
                StrategySpecRevision.owner_id == owner_id,
                StrategySpecRevision.revision == pin.spec_revision,
            )
        )
        if row is None:
            raise DomainError("not_found", "Pinned description revision not found", 404)
        if row.spec_sha256 != pin.spec_sha256:
            raise DomainError(
                "spec_pin_mismatch", "Comparison description hash differs", 409
            )
        spec = _spec_public(row, resource).spec
    config = _typed_config(result.run)
    source = getattr(config, "source_config", config)
    if digest(result.dataset.manifest) != result.dataset.content_sha256:
        raise _integrity()
    universe = session.scalar(
        select(UniverseVersion).where(
            UniverseVersion.id == UUID(source.universe.id),
            UniverseVersion.owner_id == owner_id,
            UniverseVersion.content_sha256 == source.universe.sha256,
        )
    )
    if universe is None:
        raise _integrity()
    frozen_refs = {
        name: sha
        for name, sha in result.dataset.manifest.get("files", {}).items()
        if name.startswith("universe/")
    }
    # Namespace includes immutable effective universe file inventory. The bytes
    # are authenticated now, not merely accepted from a caller-provided digest.
    total = 0
    if len(frozen_refs) > 1200:
        raise DomainError(
            "comparison_capacity",
            "Frozen universe exceeds the bounded file inventory",
            422,
        )
    for name, sha in sorted(frozen_refs.items()):
        content = read_verified(
            settings.storage_root,
            f"{result.dataset.storage_key}/{name}",
            sha,
            min(settings.max_artifact_bytes, 16 * 1024**2),
        )
        total += len(content)
        if total > 128 * 1024**2:
            raise DomainError(
                "comparison_capacity",
                "Frozen universe exceeds the bounded input size",
                422,
            )
    namespace = EventNamespace(
        dataset_version_id=result.dataset.id,
        dataset_sha256=result.dataset.content_sha256,
        universe_version_id=universe.id,
        universe_sha256=universe.content_sha256,
        frozen_universe_sha256=digest(frozen_refs) if frozen_refs else None,
        instrument_namespace="portable_listing_id"
        if config.schema_version.startswith("tradegrad.portable.")
        else "native_symbol",
    )
    binding = (
        trace.manifest.binding
        if trace.manifest is not None
        else make_binding(
            run=result.run,
            evaluation=result.evaluation,
            config=config,
            bundle=result.bundle,
            result_sha256=result.artifact.sha256,
            candidate_id=result.evaluation.candidate_id,
        )
    )
    paths = {}
    if trace.manifest is not None:
        names = [item.name for item in trace.manifest.artifacts]
        registrations = (
            session.scalars(
                select(Artifact).where(
                    Artifact.run_id == result.run.id,
                    Artifact.attempt_id == result.evaluation.attempt_id,
                    Artifact.name.in_(names),
                )
            ).all()
            if names
            else []
        )
        paths = {
            item.name: safe_path(settings.storage_root, item.storage_key)
            for item in registrations
        }
        if set(paths) != set(names):
            raise _integrity()
    item = ComparisonInput(
        binding=binding,
        source_kind=pin.source_kind,
        source_id=pin.source_id,
        source=resource.item.code,
        window=DateRange(
            start=result.evaluation.window["start"], end=result.evaluation.window["end"]
        ),
        spec=spec,
        spec_sha256=pin.spec_sha256,
        trace_manifest=trace.manifest,
        trace_manifest_sha256=trace.context.trace_manifest_sha256,
        trace_paths=paths,
        common_config=common_configuration(result, config, namespace),
        economic_bundle=result.bundle,
        namespace=namespace,
    )
    public = ComparisonSource(
        source_strategy_version_id=resource.strategy.id,
        source_kind=pin.source_kind,
        source_id=pin.source_id,
        code_sha256=pin.code_sha256,
        code=resource.item.code,
        spec=spec,
        spec_sha256=pin.spec_sha256,
        spec_missing_reason=None
        if spec is not None
        else "No exact description revision was selected; no description was generated or substituted.",
    )
    return AdmittedSide(trace, item, public)


def load_comparison_inputs(session, owner_id, body, settings):
    advisory_lock(session, f"submit:{owner_id}")
    return (
        load_comparison_side(session, body.left, owner_id, settings),
        load_comparison_side(session, body.right, owner_id, settings),
    )


def input_snapshot(owner_id, body, sides, limits=DEFAULT_LIMITS):
    from packages.research_trace.comparison import comparison_input_hash

    engine_hash = comparison_input_hash(sides[0].input, sides[1].input, limits=limits)
    value = {
        "schema_version": "tradegrad.comparison-input.v1",
        "owner_id": str(owner_id),
        "request": body.model_dump(mode="json"),
        "request_sha256": body.sha256,
        "engine_input_sha256": engine_hash,
        "algorithm_version": ALGORITHM_VERSION,
        "tolerance_version": TOLERANCE_VERSION,
        "limits": limits.model_dump(mode="json"),
        "left_common_config": dict(sides[0].input.common_config),
        "right_common_config": dict(sides[1].input.common_config),
    }
    if len(canonical_bytes(value)) > MAX_SNAPSHOT_BYTES:
        raise DomainError(
            "comparison_capacity",
            "Frozen comparison configuration exceeds its budget",
            422,
        )
    return value


def _record_both(session, sides, owner_id, purpose="result_preview"):
    for side in sides:
        record_verified_result_access(
            session, side.trace.result, owner_id, purpose=purpose
        )


def _quota(session, owner_id, settings):
    active = session.scalar(
        select(func.count())
        .select_from(ResearchRun)
        .where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.status.not_in(TERMINAL),
        )
    )
    if active >= quota_limit(session, owner_id, settings):
        raise DomainError("run_quota", "Active run limit reached", 429)


def submit_comparison(session, owner_id, body, idempotency_key, settings):
    from backend.app.independent_services import reject_control_key

    _key(idempotency_key)
    advisory_lock(session, f"submit:{owner_id}")
    reject_control_key(session, owner_id, idempotency_key)
    alias = session.get(ComparisonSubmission, (owner_id, idempotency_key))
    previous = session.scalar(
        select(ResearchRun).where(
            ResearchRun.owner_id == owner_id,
            ResearchRun.idempotency_key == idempotency_key,
        )
    )
    if alias is not None:
        if alias.request_sha256 != body.sha256:
            raise DomainError(
                "idempotency_conflict",
                "This key already belongs to another comparison request",
            )
        item, run = owned_comparison(session, alias.comparison_id, owner_id)
        if previous is not None and previous.id != run.id:
            raise _integrity()
    elif previous is not None:
        raise DomainError(
            "idempotency_conflict", "This key already belongs to another run request"
        )
    sides = load_comparison_inputs(session, owner_id, body, settings)
    snapshot = input_snapshot(owner_id, body, sides)
    sha = digest(snapshot)
    if alias is not None:
        if item.input_sha256 != sha or item.input_snapshot != snapshot:
            raise _integrity()
        _record_both(session, sides, owner_id)
        return item, run
    item = session.scalar(
        select(Comparison).where(
            Comparison.owner_id == owner_id, Comparison.input_sha256 == sha
        )
    )
    if item is not None:
        item, run = owned_comparison(session, item.id, owner_id)
        if item.input_snapshot != snapshot:
            raise _integrity()
    else:
        _quota(session, owner_id, settings)
        identifier = uuid4()
        config = ComparisonConfig(
            comparison_id=identifier,
            input_sha256=sha,
            engine_input_sha256=snapshot["engine_input_sha256"],
            request=body,
        )
        left = sides[0].trace.result
        run = ResearchRun(
            id=uuid4(),
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            request_sha256=config.sha256,
            config=config.model_dump(mode="json"),
            dataset_version_id=left.dataset.id,
            strategy_version_id=left.run.strategy_version_id,
            status="queued",
            generation=1,
            next_event_seq=0,
            result=None,
        )
        session.add(run)
        session.flush()
        item = Comparison(
            id=identifier,
            owner_id=owner_id,
            run_id=run.id,
            left_evaluation_id=body.left.evaluation_id,
            right_evaluation_id=body.right.evaluation_id,
            input_sha256=sha,
            engine_input_sha256=snapshot["engine_input_sha256"],
            input_snapshot=snapshot,
        )
        session.add(item)
        session.flush()
        session.add(OutboxEvent(run_id=run.id, generation=run.generation))
        append_event(
            session,
            run,
            "comparison_admitted",
            {"status": "queued", "comparison_id": str(item.id)},
        )
    session.add(
        ComparisonSubmission(
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            comparison_id=item.id,
            request_sha256=body.sha256,
        )
    )
    _record_both(session, sides, owner_id)
    session.flush()
    return item, run


def validate_comparison_inputs(session, comparison, run, settings):
    config = comparison_config(run, comparison)
    sides = load_comparison_inputs(session, run.owner_id, config.request, settings)
    if (
        input_snapshot(run.owner_id, config.request, sides, config.limits)
        != comparison.input_snapshot
    ):
        raise _integrity()
    return sides


def retry_comparison(session, comparison_id, owner_id, settings, *, idempotency_key):
    _key(idempotency_key)
    advisory_lock(session, f"submit:{owner_id}")
    item, run = owned_comparison(session, comparison_id, owner_id, lock=True)
    receipt = session.scalar(
        select(RunEvent.id).where(
            RunEvent.run_id == run.id,
            RunEvent.type == "comparison_retry_admitted",
            RunEvent.payload["idempotency_key"].astext == idempotency_key,
        )
    )
    if receipt:
        return item, run
    if (
        run.status not in {"failed", "cancelled", "interrupted"}
        or item.published_attempt_id is not None
    ):
        raise DomainError(
            "invalid_transition",
            "Only unpublished stopped failed, cancelled or interrupted comparisons can retry",
        )
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .with_for_update()
    ).all()
    if any(
        not attempt.sandbox_stopped or attempt.finished_at is None
        for attempt in attempts
    ):
        raise DomainError(
            "comparison_recovery_blocked",
            "Retry requires confirmed termination of every earlier in-process query",
        )
    validate_comparison_inputs(session, item, run, settings)
    _quota(session, owner_id, settings)
    run.generation += 1
    run.active_attempt_id = None
    run.status, run.error, run.result = "queued", None, None
    session.add(OutboxEvent(run_id=run.id, generation=run.generation))
    append_event(
        session,
        run,
        "comparison_retry_admitted",
        {
            "status": "queued",
            "generation": run.generation,
            "comparison_id": str(item.id),
            "idempotency_key": idempotency_key,
        },
    )
    session.flush()
    return item, run


def _validate_manifest(manifest, comparison, run, attempt, sides):
    from packages.research_trace.comparison import comparison_side_pins
    from packages.research_trace.comparison_contracts import FIELD_TOLERANCES

    config = comparison_config(run, comparison)
    if (
        manifest.publication
        != ComparisonPublication(
            comparison_id=comparison.id, run_id=run.id, execution_attempt_id=attempt.id
        )
        or manifest.input_hash != comparison.engine_input_sha256
        or manifest.limits != config.limits
        or manifest.field_tolerances != FIELD_TOLERANCES
    ):
        raise _integrity()
    for pins, side in zip((manifest.left, manifest.right), sides, strict=True):
        if pins != comparison_side_pins(side.input, limits=config.limits):
            raise _integrity()


def finish_comparison_attempt(
    session,
    attempt_id,
    generation,
    lease_token,
    *,
    guard,
    settings,
    status,
    manifest=None,
    artifacts=(),
    error_code=None,
    publication_check=None,
):
    """Only after synchronous query closure; caller retains flock through commit."""
    from backend.app.comparison_views import safe_error

    owner_id = session.scalar(
        select(ResearchRun.owner_id)
        .join(
            ExecutionAttempt,
            ExecutionAttempt.run_id == ResearchRun.id,
        )
        .where(ExecutionAttempt.id == attempt_id)
    )
    if owner_id is None:
        from backend.app.services import FenceLost

        raise FenceLost()
    # Preserve owner -> Run -> Attempt ordering used by submissions/retries.
    advisory_lock(session, f"submit:{owner_id}")
    run, attempt = fenced_attempt(session, attempt_id, generation, lease_token)
    guard.assert_owned(run.id, attempt.id)
    item = session.scalar(
        select(Comparison).where(Comparison.run_id == run.id).with_for_update()
    )
    if item is None:
        raise _integrity()
    comparison_config(run, item)
    if status not in {"succeeded", "failed", "cancelled"}:
        status, error_code = "failed", "comparison_failed"
    if run.status == "cancel_requested":
        status, error_code = "cancelled", "comparison_cancelled"
    if status == "succeeded":
        sides = validate_comparison_inputs(session, item, run, settings)
        if manifest is None or item.published_attempt_id is not None:
            raise _integrity()
        _validate_manifest(manifest, item, run, attempt, sides)
        expected = {ref.name: ref for ref in manifest.artifacts}
        if len(artifacts) != len(expected) + 1 or len(
            {a.name for a in artifacts}
        ) != len(artifacts):
            raise _integrity()
        manifest_artifact = None
        registrations = []
        for artifact in artifacts:
            path = artifact.path
            if (
                not path.is_relative_to(guard.path / "artifacts")
                or path.name != artifact.name
            ):
                raise _integrity()
            key = str(path.relative_to(settings.storage_root.absolute()))
            content = read_verified(
                settings.storage_root, key, artifact.sha256, settings.max_artifact_bytes
            )
            if len(content) != artifact.size_bytes:
                raise _integrity()
            if artifact.section is None:
                if (
                    manifest_artifact is not None
                    or artifact.media_type != "application/json"
                    or content != canonical_bytes(manifest)
                ):
                    raise _integrity()
            else:
                ref = expected.get(artifact.name)
                if ref is None or (
                    ref.sha256,
                    ref.size_bytes,
                    ref.media_type,
                    ref.rows,
                    ref.section,
                ) != (
                    artifact.sha256,
                    artifact.size_bytes,
                    artifact.media_type,
                    artifact.rows,
                    artifact.section,
                ):
                    raise _integrity()
            row = Artifact(
                id=uuid4(),
                run_id=run.id,
                attempt_id=attempt.id,
                name=artifact.name,
                media_type=artifact.media_type,
                storage_key=key,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
            )
            registrations.append(row)
            if artifact.section is None:
                manifest_artifact = row
        if manifest_artifact is None:
            raise _integrity()
        if publication_check is not None:
            publication_check()
        fenced_attempt(session, attempt_id, generation, lease_token)
        session.add_all(registrations)
        session.flush()
        item.published_attempt_id = attempt.id
        item.manifest_artifact_id = manifest_artifact.id
        item.manifest_sha256 = manifest_artifact.sha256
        append_event(
            session,
            run,
            "comparison_artifact_ready",
            {
                "status": "succeeded",
                "comparison_id": str(item.id),
                "manifest_artifact_id": str(manifest_artifact.id),
                "manifest_sha256": manifest_artifact.sha256,
            },
            attempt.id,
        )
    # Failure/cancellation never registers partial blocks or a result manifest.
    attempt.sandbox_stopped = True
    attempt.finished_at = utcnow()
    attempt.status = run.status = status
    attempt.exit_code = 0 if status == "succeeded" else None
    attempt.error = run.error = (
        safe_error(error_code) if status != "succeeded" else None
    )
    run.result = None
    append_event(
        session,
        run,
        "run_finished",
        {
            "status": status,
            "comparison_id": str(item.id),
            "error_code": run.error,
        },
        attempt.id,
    )
    guard.assert_owned(run.id, attempt.id)
    session.flush()
    return item, run


@dataclass(frozen=True)
class OwnedComparisonResult:
    comparison: Comparison
    run: ResearchRun
    manifest: ComparisonManifest
    artifact: Artifact
    content: bytes
    paths: dict
    sides: tuple


def load_owned_comparison_result(
    session, comparison_id, owner_id, settings, *, manifest_sha256=None
):
    advisory_lock(session, f"submit:{owner_id}")
    item, run = owned_comparison(session, comparison_id, owner_id)
    if (
        run.status != "succeeded"
        or item.published_attempt_id is None
        or item.manifest_artifact_id is None
    ):
        raise DomainError(
            "comparison_unavailable",
            "Comparison has no completed immutable result",
            409,
        )
    if manifest_sha256 is not None and manifest_sha256 != item.manifest_sha256:
        raise DomainError(
            "comparison_pin_mismatch",
            "Requested comparison manifest differs; evidence was not retargeted",
            409,
        )
    attempt = session.scalar(
        select(ExecutionAttempt).where(
            ExecutionAttempt.id == item.published_attempt_id,
            ExecutionAttempt.run_id == run.id,
        )
    )
    if (
        attempt is None
        or attempt.id != run.active_attempt_id
        or attempt.generation != run.generation
        or attempt.status != "succeeded"
        or not attempt.sandbox_stopped
        or attempt.finished_at is None
    ):
        raise _integrity()
    sides = validate_comparison_inputs(session, item, run, settings)
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == item.manifest_artifact_id,
            Artifact.run_id == run.id,
            Artifact.attempt_id == attempt.id,
        )
    )
    if (
        artifact is None
        or artifact.sha256 != item.manifest_sha256
        or artifact.media_type != "application/json"
    ):
        raise _integrity()
    config = comparison_config(run, item)
    raw = read_verified(
        settings.storage_root,
        artifact.storage_key,
        artifact.sha256,
        min(settings.max_artifact_bytes, config.limits.max_manifest_bytes),
    )
    try:
        manifest = ComparisonManifest.model_validate_json(raw)
    except (ValueError, TypeError):
        raise _integrity() from None
    if len(raw) != artifact.size_bytes or canonical_bytes(manifest) != raw:
        raise _integrity()
    _validate_manifest(manifest, item, run, attempt, sides)
    names = [ref.name for ref in manifest.artifacts]
    registered = (
        session.scalars(
            select(Artifact).where(
                Artifact.run_id == run.id,
                Artifact.attempt_id == attempt.id,
                Artifact.name.in_(names),
            )
        ).all()
        if names
        else []
    )
    registrations = {entry.name: entry for entry in registered}
    paths = {}
    for ref in manifest.artifacts:
        entry = registrations.get(ref.name)
        if entry is None or (entry.sha256, entry.size_bytes, entry.media_type) != (
            ref.sha256,
            ref.size_bytes,
            ref.media_type,
        ):
            raise _integrity()
        paths[ref.name] = safe_path(settings.storage_root, entry.storage_key)
    return OwnedComparisonResult(item, run, manifest, artifact, raw, paths, sides)


def _bounded_response(response, maximum):
    if len(canonical_bytes(response)) > maximum:
        raise DomainError(
            "comparison_detail_too_large",
            "Comparison response exceeds its explicit byte budget; use bounded sections or authorized artifact download",
            422,
        )
    return response


def comparison_result_view(result):
    return _bounded_response(
        ComparisonResult(
            comparison_id=result.comparison.id,
            input_sha256=result.comparison.input_sha256,
            manifest_artifact_id=result.artifact.id,
            manifest_sha256=result.artifact.sha256,
            manifest=result.manifest,
            left_source=result.sides[0].source,
            right_source=result.sides[1].source,
        ),
        8 * 1024**2,
    )


def comparison_section_view(result, section, **query):
    from backend.app.comparison_schemas import ComparisonSectionPage
    from packages.research_trace.comparison import ComparisonReader

    try:
        page = ComparisonReader(result.manifest, result.paths).page(section, **query)
    except ComparisonError as error:
        raise DomainError(
            error.code,
            error.message,
            422
            if error.code
            in {
                "invalid_comparison_query",
                "comparison_detail_too_large",
                "comparison_query_too_large",
            }
            else 409,
        ) from None
    return _bounded_response(
        ComparisonSectionPage(
            **page.model_dump(),
            comparison_id=result.comparison.id,
            input_sha256=result.comparison.input_sha256,
            manifest_sha256=result.artifact.sha256,
        ),
        4 * 1024**2,
    )


def release_comparison_access(session, result, owner_id, *, purpose="result_preview"):
    _record_both(session, result.sides, owner_id, purpose)


def load_comparison_artifact(session, artifact, owner_id, settings):
    item = session.scalar(
        select(Comparison).where(
            Comparison.run_id == artifact.run_id, Comparison.owner_id == owner_id
        )
    )
    if item is None:
        raise DomainError("not_found", "Comparison artifact not found", 404)
    result = load_owned_comparison_result(session, item.id, owner_id, settings)
    if artifact.attempt_id != result.comparison.published_attempt_id:
        raise _integrity()
    if artifact.id == result.artifact.id:
        content = result.content
    else:
        ref = next(
            (ref for ref in result.manifest.artifacts if ref.name == artifact.name),
            None,
        )
        if ref is None or (ref.sha256, ref.size_bytes, ref.media_type) != (
            artifact.sha256,
            artifact.size_bytes,
            artifact.media_type,
        ):
            raise _integrity()
        content = read_verified(
            settings.storage_root,
            artifact.storage_key,
            artifact.sha256,
            settings.max_artifact_bytes,
        )
        if len(content) != artifact.size_bytes:
            raise _integrity()
    release_comparison_access(session, result, owner_id, purpose="result_download")
    return content
