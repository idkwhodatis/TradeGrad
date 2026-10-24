"""Descriptions are never an execution language or synthetic execution evidence."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import String, delete, exists, func, literal, or_, select, union_all
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from backend.app.models import (
    Artifact,
    Candidate,
    CandidateBookmark,
    Evaluation,
    FinalSelection,
    IndependentEvaluation,
    ModelCall,
    ResearchRun,
    StrategySpecRevision,
    StrategyVersion,
)
from backend.app.result_access import record_result_access
from backend.app.services import DomainError
from backend.app.strategy_services import submission_id
from backend.app.workbench_schemas import (
    CandidateBookmark as BookmarkPublic,
)
from backend.app.workbench_schemas import (
    LineageContext,
    LineageNode,
    LineageReference,
    ResearchTemplate,
    WorkbenchEvaluation,
    WorkbenchLineage,
    WorkbenchSourceAssociation,
    WorkbenchSpec,
)
from packages.research_trace.contracts import StrategySpec, digest
from packages.research_trace.specification import (
    describe_source,
    revise_prose,
    source_sha256,
    spec_sha256,
    validate_source_spec,
)
from packages.research_trace.templates import (
    available_templates,
    native_contract,
    template_source,
    template_spec,
)
from packages.tradegrad_adapter.checkpoint import validate_candidate_context
from packages.tradegrad_adapter.native_contract import NativeStrategyContract

SPEC_NAMESPACE = UUID("71d23b42-7257-4e60-a717-c138120d1b97")


@dataclass(frozen=True)
class SourceResource:
    kind: str
    item: StrategyVersion | Candidate
    strategy: StrategyVersion
    run: ResearchRun | None
    code_sha256: str
    parameters: dict | None

    @property
    def id(self):
        return self.item.id


def owned_source(session, kind, identifier, owner_id, *, lock=False):
    model = StrategyVersion if kind == "strategy_version" else Candidate
    statement = select(model).where(model.id == identifier)
    if model is StrategyVersion:
        statement = statement.where(StrategyVersion.owner_id == owner_id)
    else:
        statement = statement.join(
            ResearchRun, ResearchRun.id == Candidate.run_id
        ).where(ResearchRun.owner_id == owner_id)
    if lock:
        statement = statement.with_for_update(of=model)
    item = session.scalar(statement)
    if item is None:
        raise DomainError("not_found", "Source resource not found", 404)
    run = None if model is StrategyVersion else session.get(ResearchRun, item.run_id)
    strategy = (
        item
        if run is None
        else session.scalar(
            select(StrategyVersion).where(
                StrategyVersion.id == run.strategy_version_id,
                StrategyVersion.owner_id == owner_id,
            )
        )
    )
    if strategy is None:
        raise DomainError("not_found", "Source strategy not found", 404)
    sha = item.content_sha256 if run is None else item.code_sha256
    try:
        if source_sha256(item.code) != sha:
            raise ValueError("Source hash mismatch")
        parameters = None
        if run is None and strategy.mode == "native_akquant" and strategy.manifest:
            contract = NativeStrategyContract.model_validate(strategy.manifest)
            parameters = contract.parameter_values()
            if strategy.parameters != parameters:
                raise ValueError("Source parameter declaration differs")
        elif run is not None and strategy.mode == "native_akquant":
            config = run.config.get("source_config", run.config)
            if config.get("strategy_contract") is not None:
                parameters = NativeStrategyContract.model_validate(
                    config["strategy_contract"]
                ).parameter_values()
    except (ValueError, TypeError, UnicodeError) as error:
        raise DomainError(
            "source_integrity",
            "Source bytes or immutable parameter binding is invalid",
            409,
        ) from error
    return SourceResource(kind, item, strategy, run, sha, parameters)


def _clause(resource):
    column = (
        StrategySpecRevision.strategy_version_id
        if resource.kind == "strategy_version"
        else StrategySpecRevision.candidate_id
    )
    return column == resource.id


def _spec_public(row, resource):
    try:
        spec = StrategySpec.model_validate(row.spec)
        if (
            row.code_sha256 != resource.code_sha256
            or row.revision != spec.revision
            or row.spec_sha256 != spec_sha256(spec)
        ):
            raise ValueError("Description hash or revision mismatch")
        validate_source_spec(spec, resource.item.code)
        if spec.template_id:
            if resource.strategy.mode != "native_akquant":
                raise ValueError("Controlled native template has an incompatible mode")
            original = template_spec(
                spec.template_id,
                source=resource.item.code,
                parameters=resource.parameters,
            )
            if any(
                a.parameters != b.parameters
                or a.units != b.units
                or a.inputs != b.inputs
                or a.outputs != b.outputs
                or a.category != b.category
                for a, b in zip(spec.rules, original.rules)
            ):
                raise ValueError(
                    "Description parameters or structure differ from source contract"
                )
    except (ValueError, TypeError, KeyError) as error:
        raise DomainError(
            "spec_integrity",
            "Stored description does not match its immutable source/revision/hash",
            409,
        ) from error
    return WorkbenchSpec(
        resource_kind=resource.kind,
        resource_id=resource.id,
        strategy_version_id=resource.strategy.id,
        candidate_id=resource.id if resource.kind == "candidate" else None,
        code_sha256=row.code_sha256,
        spec_sha256=row.spec_sha256,
        revision=row.revision,
        created_at=row.created_at,
        origin=row.origin,
        change_note=row.change_note,
        spec=spec,
    )


def _ensure_initial(session, resource, owner_id):
    row = session.scalar(
        select(StrategySpecRevision)
        .where(_clause(resource))
        .order_by(StrategySpecRevision.revision.desc())
        .limit(1)
    )
    if row is None:
        try:
            spec = describe_source(
                resource.item.code,
                resource.code_sha256,
                parameters=resource.parameters,
                allow_controlled=resource.strategy.mode == "native_akquant",
            )
        except (ValueError, TypeError) as error:
            raise DomainError(
                "spec_unavailable",
                "Cannot describe this immutable source/parameter contract",
                409,
            ) from error
        row = StrategySpecRevision(
            id=uuid5(SPEC_NAMESPACE, f"initial:{resource.kind}:{resource.id}"),
            owner_id=owner_id,
            strategy_version_id=resource.id
            if resource.kind == "strategy_version"
            else None,
            candidate_id=resource.id if resource.kind == "candidate" else None,
            code_sha256=resource.code_sha256,
            revision=1,
            spec_sha256=spec_sha256(spec),
            spec=spec.model_dump(mode="json"),
            origin="static",
            change_note=None,
            request_sha256=None,
        )
        session.add(row)
        session.flush()
    return row


def read_spec(
    session, kind, identifier, owner_id, *, revision=None, expected_spec_sha256=None
):
    if (revision is None) != (expected_spec_sha256 is None):
        raise DomainError(
            "invalid_spec_pin",
            "Historical description requires both revision and spec_sha256",
            422,
        )
    resource = owned_source(session, kind, identifier, owner_id, lock=True)
    current = _ensure_initial(session, resource, owner_id)
    row = (
        current
        if revision is None
        else session.scalar(
            select(StrategySpecRevision).where(
                _clause(resource), StrategySpecRevision.revision == revision
            )
        )
    )
    if row is None:
        raise DomainError("not_found", "Description revision not found", 404)
    if expected_spec_sha256 is not None and row.spec_sha256 != expected_spec_sha256:
        raise DomainError(
            "spec_pin_mismatch",
            "Description revision does not have the requested immutable hash",
            409,
        )
    return _spec_public(row, resource)


def list_specs(session, kind, identifier, owner_id, *, offset, limit):
    resource = owned_source(session, kind, identifier, owner_id, lock=True)
    _ensure_initial(session, resource, owner_id)
    rows = session.scalars(
        select(StrategySpecRevision)
        .where(_clause(resource))
        .order_by(StrategySpecRevision.revision.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "data": [_spec_public(row, resource) for row in rows],
        "count": session.scalar(
            select(func.count())
            .select_from(StrategySpecRevision)
            .where(_clause(resource))
        ),
    }


def create_spec_revision(session, kind, identifier, owner_id, body, key):
    resource = owned_source(session, kind, identifier, owner_id, lock=True)
    request_hash = digest(
        {
            "resource_kind": kind,
            "resource_id": str(identifier),
            "body": body.model_dump(mode="json"),
        }
    )
    row_id = uuid5(SPEC_NAMESPACE, str(submission_id(owner_id, key)))
    existing = session.get(StrategySpecRevision, row_id)
    if existing is not None:
        if existing.owner_id != owner_id or existing.request_sha256 != request_hash:
            raise DomainError(
                "idempotency_conflict",
                "This key was used for a different description revision",
                409,
            )
        return _spec_public(existing, resource), False
    current = _ensure_initial(session, resource, owner_id)
    public = _spec_public(current, resource)
    if (
        body.code_sha256 != resource.code_sha256
        or body.base_revision != current.revision
        or body.base_spec_sha256 != current.spec_sha256
    ):
        raise DomainError(
            "spec_revision_conflict",
            "The source or latest description changed; review the current version before revising",
            409,
        )
    try:
        spec = revise_prose(
            public.spec,
            [edit.model_dump() for edit in body.rules],
            source=resource.item.code,
        )
    except (ValueError, TypeError) as error:
        raise DomainError("invalid_spec_revision", str(error), 422) from None
    values = {
        "id": row_id,
        "owner_id": owner_id,
        "strategy_version_id": identifier if kind == "strategy_version" else None,
        "candidate_id": identifier if kind == "candidate" else None,
        "code_sha256": resource.code_sha256,
        "revision": spec.revision,
        "spec_sha256": spec_sha256(spec),
        "spec": spec.model_dump(mode="json"),
        "origin": "human",
        "change_note": body.change_note,
        "request_sha256": request_hash,
    }
    created = (
        session.scalar(
            insert(StrategySpecRevision)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[StrategySpecRevision.id])
            .returning(StrategySpecRevision.id)
        )
        is not None
    )
    row = session.get(StrategySpecRevision, row_id)
    if row is None or row.owner_id != owner_id or row.request_sha256 != request_hash:
        raise DomainError(
            "idempotency_conflict",
            "This key was used for a different description revision",
            409,
        )
    return _spec_public(row, resource), created


def set_bookmark(session, identifier, owner_id, bookmarked):
    resource = owned_source(session, "candidate", identifier, owner_id)
    if bookmarked:
        session.execute(
            insert(CandidateBookmark)
            .values(
                owner_id=owner_id,
                candidate_id=identifier,
                code_sha256=resource.code_sha256,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CandidateBookmark.owner_id,
                    CandidateBookmark.candidate_id,
                ]
            )
        )
    else:
        session.execute(
            delete(CandidateBookmark).where(
                CandidateBookmark.owner_id == owner_id,
                CandidateBookmark.candidate_id == identifier,
            )
        )
    return BookmarkPublic(
        candidate_id=identifier, code_sha256=resource.code_sha256, bookmarked=bookmarked
    )


def template_catalog():
    result = []
    for identifier in available_templates():
        code = template_source(identifier)
        result.append(
            ResearchTemplate(
                template_id=identifier,
                name="Daily timing"
                if identifier == "daily_timing"
                else "Cross-sectional selection",
                task_suffix="time" if identifier == "daily_timing" else "cross",
                code=code,
                code_sha256=source_sha256(code),
                manifest=native_contract(identifier),
                spec=template_spec(identifier),
            )
        )
    return {"data": result, "count": len(result)}


def _candidate_link_clause(candidate):
    """Raw FK or exact same-run/key/hash; never a hash-only candidate guess."""
    return (Evaluation.code_sha256 == candidate.code_sha256) & or_(
        Evaluation.candidate_id == candidate.id,
        Evaluation.candidate_id.is_(None)
        & (Evaluation.run_id == candidate.run_id)
        & (Evaluation.candidate_key == candidate.candidate_key),
    )


def _strategy_link_clause(strategy):
    """The same source predicate serves direct deep links and paginated lists."""
    recorded_seed = exists(
        select(Candidate.id)
        .where(
            Candidate.run_id == Evaluation.run_id,
            Candidate.kind == "seed",
            _candidate_link_clause(Candidate),
        )
        .correlate_except(Candidate)
    )
    # Initial registration can be interrupted before its first Candidate row.
    # Only the pipeline's reserved initial keys qualify, with no contradictory
    # persisted row. No candidate UUID is manufactured for this case.
    initial_seed = (
        Evaluation.candidate_id.is_(None)
        & Evaluation.candidate_key.in_(("candidate-0000", "seed"))
        & ~exists(
            select(Candidate.id)
            .where(
                Candidate.run_id == Evaluation.run_id,
                Candidate.candidate_key == Evaluation.candidate_key,
            )
            .correlate_except(Candidate)
        )
    )
    saved_seed = ResearchRun.final_selection_id.is_(None) & (
        recorded_seed | initial_seed
    )
    selected_final = exists(
        select(FinalSelection.id)
        .where(
            FinalSelection.id == ResearchRun.final_selection_id,
            FinalSelection.owner_id == ResearchRun.owner_id,
            FinalSelection.strategy_version_id == strategy.id,
            FinalSelection.source_candidate_id == Evaluation.candidate_id,
            FinalSelection.source_code_sha256 == Evaluation.code_sha256,
        )
        .correlate_except(FinalSelection)
    )
    independent = exists(
        select(IndependentEvaluation.id)
        .where(
            IndependentEvaluation.run_id == ResearchRun.id,
            IndependentEvaluation.owner_id == ResearchRun.owner_id,
            ResearchRun.config["selected_strategy"]["id"].astext
            == strategy.id.cast(String),
            Evaluation.candidate_id.is_(None),
            Evaluation.candidate_key
            == literal("independent:") + IndependentEvaluation.id.cast(String),
        )
        .correlate_except(IndependentEvaluation)
    )
    return (
        (ResearchRun.strategy_version_id == strategy.id)
        & (Evaluation.code_sha256 == strategy.content_sha256)
        & (saved_seed | selected_final | independent)
    )


def _evaluation_clause(resource):
    if resource.kind == "candidate":
        return exists(
            select(Candidate.id).where(
                Candidate.id == resource.id,
                _candidate_link_clause(Candidate),
            )
        )
    return exists(
        select(StrategyVersion.id).where(
            StrategyVersion.id == resource.id,
            _strategy_link_clause(StrategyVersion),
        )
    )


def _source_association(session, evaluation, run, owner_id):
    candidate_run = aliased(ResearchRun)
    candidate = session.scalar(
        select(Candidate)
        .select_from(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .join(Candidate, _candidate_link_clause(Candidate))
        .join(candidate_run, candidate_run.id == Candidate.run_id)
        .where(
            Evaluation.id == evaluation.id,
            ResearchRun.owner_id == owner_id,
            candidate_run.owner_id == owner_id,
        )
    )
    strategy = session.scalar(
        select(StrategyVersion)
        .select_from(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .join(StrategyVersion, _strategy_link_clause(StrategyVersion))
        .where(
            Evaluation.id == evaluation.id,
            ResearchRun.owner_id == owner_id,
            StrategyVersion.owner_id == owner_id,
        )
    )
    reasons = []
    if candidate is None:
        reasons.append(
            "No owned candidate matches the registered FK or exact same-run candidate key and code hash."
        )
    if strategy is None:
        reasons.append(
            "This leaf is not associated with the saved seed source or its exact frozen final selection; matching code bytes alone do not establish that relationship."
        )
    return WorkbenchSourceAssociation(
        candidate_id=candidate.id if candidate else None,
        candidate_basis=(
            "registered_fk"
            if evaluation.candidate_id is not None
            else "same_run_key_hash"
        )
        if candidate
        else "unavailable",
        strategy_version_id=strategy.id if strategy else None,
        strategy_basis=(
            "independent_selected"
            if run.config.get("execution_mode") == "independent_evaluation"
            else "selected_final"
            if run.final_selection_id is not None
            else "saved_seed"
        )
        if strategy
        else "unavailable",
        missing_reasons=reasons,
    )


def _registered_hash(session, evaluation, name):
    reference = (evaluation.payload or {}).get(name)
    if not isinstance(reference, dict):
        return None
    try:
        identifier = UUID(reference["artifact_id"])
    except (ValueError, TypeError, KeyError):
        return None
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == identifier,
            Artifact.run_id == evaluation.run_id,
            Artifact.attempt_id == evaluation.attempt_id,
            Artifact.sha256 == reference.get("sha256"),
            Artifact.size_bytes == reference.get("size_bytes"),
        )
    )
    return artifact.sha256 if artifact else None


def _evaluation_public(session, evaluation, owner_id):
    run = session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == evaluation.run_id, ResearchRun.owner_id == owner_id
        )
    )
    if run is None:
        raise DomainError("not_found", "Evaluation run not found", 404)
    result = _registered_hash(
        session,
        evaluation,
        "portable_result"
        if run.config.get("schema_version", "").startswith("tradegrad.portable.")
        else "result_bundle",
    )
    trace = _registered_hash(session, evaluation, "trace_manifest")
    missing = []
    if result is None:
        missing.append(
            "No matching immutable result artifact is registered for this leaf evaluation."
        )
    if trace is None:
        missing.append(
            "No matching trace manifest is registered; internal observations are not inferred."
        )
    config = run.config.get("source_config", run.config)
    try:
        universe_id = UUID(config["universe"]["id"])
    except (KeyError, ValueError, TypeError):
        universe_id = None
    # Failure text may contain observed outcome details. Preserve the existing
    # exposure-before-commit boundary instead of exposing an unrecorded shortcut.
    if evaluation.error:
        record_result_access(
            session,
            run,
            owner_id,
            evaluation=evaluation,
            source="workbench-evaluation-status",
        )
    schema_version = run.config.get("schema_version")
    run_kind = {
        "tradegrad.independent-evaluation.v1": "native_independent",
        "tradegrad.portable.independent-evaluation.v1": "portable_independent",
        "tradegrad.run.v1": "native_research",
        "tradegrad.final-evaluation.v1": "native_final",
        "tradegrad.portable.run.v1": "portable_research",
        "tradegrad.portable.final-evaluation.v1": "portable_final",
    }.get(schema_version, "legacy")
    return WorkbenchEvaluation(
        source_association=_source_association(session, evaluation, run, owner_id),
        run_kind=run_kind,
        run_schema_version=schema_version,
        final_test_id=run.id
        if run_kind in {"native_final", "portable_final"}
        else None,
        evaluation_id=evaluation.id,
        run_id=run.id,
        strategy_version_id=run.strategy_version_id,
        candidate_id=evaluation.candidate_id,
        execution_attempt_id=evaluation.attempt_id,
        code_sha256=evaluation.code_sha256,
        status=evaluation.status,
        split=evaluation.split,
        window=evaluation.window,
        dataset_version_id=run.dataset_version_id,
        universe_version_id=universe_id,
        result_manifest_sha256=result,
        trace_manifest_sha256=trace,
        evidence_status="registered" if result else "unavailable",
        missing_reasons=missing,
        error=evaluation.error,
        created_at=evaluation.created_at,
    )


def source_evaluations(session, kind, identifier, owner_id, *, offset, limit):
    resource = owned_source(session, kind, identifier, owner_id)
    clause = _evaluation_clause(resource) & (ResearchRun.owner_id == owner_id)
    query = (
        select(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(clause)
    )
    rows = session.scalars(
        query.order_by(Evaluation.created_at, Evaluation.id).offset(offset).limit(limit)
    ).all()
    count = session.scalar(
        select(func.count())
        .select_from(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(clause)
    )
    return {
        "data": [_evaluation_public(session, row, owner_id) for row in rows],
        "count": count,
    }


def _children_query(resource, owner_id):
    if resource.kind == "candidate":
        return (
            select(
                Candidate.id.label("id"),
                literal("candidate").label("resource_kind"),
                Candidate.created_at.label("created_at"),
            )
            .join(ResearchRun, ResearchRun.id == Candidate.run_id)
            .where(
                Candidate.parent_id == resource.id,
                Candidate.run_id == resource.run.id,
                ResearchRun.owner_id == owner_id,
            )
        )
    versions = select(
        StrategyVersion.id.label("id"),
        literal("strategy_version").label("resource_kind"),
        StrategyVersion.created_at.label("created_at"),
    ).where(
        StrategyVersion.parent_id == resource.id, StrategyVersion.owner_id == owner_id
    )
    seeds = (
        select(
            Candidate.id.label("id"),
            literal("candidate").label("resource_kind"),
            Candidate.created_at.label("created_at"),
        )
        .join(ResearchRun, ResearchRun.id == Candidate.run_id)
        .where(
            ResearchRun.strategy_version_id == resource.id,
            ResearchRun.owner_id == owner_id,
            Candidate.parent_id.is_(None),
        )
    )
    return union_all(versions, seeds)


def _node(session, resource, owner_id):
    evaluations = source_evaluations(
        session, resource.kind, resource.id, owner_id, offset=0, limit=20
    )
    bookmark = (
        session.get(CandidateBookmark, (owner_id, resource.id))
        if resource.kind == "candidate"
        else None
    )
    if bookmark is not None and bookmark.code_sha256 != resource.code_sha256:
        raise DomainError("bookmark_integrity", "Bookmark source binding differs", 409)
    item = resource.item
    selected_clause = (
        FinalSelection.source_candidate_id == resource.id
        if resource.kind == "candidate"
        else FinalSelection.strategy_version_id == resource.id
    )
    selection_ids = list(
        session.scalars(
            select(FinalSelection.id)
            .where(selected_clause, FinalSelection.owner_id == owner_id)
            .order_by(FinalSelection.created_at, FinalSelection.id)
            .limit(20)
        ).all()
    )
    selection_count = session.scalar(
        select(func.count())
        .select_from(FinalSelection)
        .where(selected_clause, FinalSelection.owner_id == owner_id)
    )
    recorded_best = None
    best_missing = (
        "No completed-run best-candidate selection is recorded for this resource."
    )
    if (
        resource.kind == "candidate"
        and isinstance(resource.run.result, dict)
        and "best_candidate_id" in resource.run.result
    ):
        best_id = resource.run.result["best_candidate_id"]
        if best_id is None:
            recorded_best = False
        else:
            try:
                best = session.scalar(
                    select(Candidate).where(
                        Candidate.id == UUID(best_id),
                        Candidate.run_id == resource.run.id,
                    )
                )
            except (ValueError, TypeError):
                best = None
            if best is None:
                raise DomainError(
                    "lineage_integrity",
                    "Recorded best candidate does not belong to its run",
                    409,
                )
            recorded_best = best.id == resource.id
            record_result_access(
                session, resource.run, owner_id, source="workbench-recorded-best"
            )
            best_missing = None
    return LineageNode(
        resource_kind=resource.kind,
        id=resource.id,
        strategy_version_id=resource.strategy.id,
        code_sha256=resource.code_sha256,
        name=item.candidate_key if resource.kind == "candidate" else item.name,
        kind=item.kind
        if resource.kind == "candidate"
        else "derived"
        if item.parent_id
        else "strategy",
        mode=resource.strategy.mode,
        created_at=item.created_at,
        run_id=resource.run.id if resource.run else None,
        candidate_key=item.candidate_key if resource.kind == "candidate" else None,
        parent_id=item.parent_id,
        bookmarked=bookmark is not None,
        is_recorded_best=recorded_best,
        selection_missing_reason=best_missing,
        final_selection_ids=selection_ids,
        final_selection_count=selection_count,
        final_selections_truncated=selection_count > len(selection_ids),
        aggregate_status="not_loaded",
        aggregate_missing_reason="This source/lineage response does not synthesize aggregate returns. Inspect a registered leaf Evaluation or the original run summary.",
        evaluation_count=evaluations["count"],
        evaluations=evaluations["data"],
        evaluations_truncated=evaluations["count"] > len(evaluations["data"]),
        child_count=session.scalar(
            select(func.count()).select_from(
                _children_query(resource, owner_id).subquery()
            )
        ),
    )


def _context(session, resource, owner_id):
    if resource.kind != "candidate" or resource.item.lineage_context is None:
        return [], LineageContext(
            status="not_collected",
            missing_reasons=[
                "Per-version algorithm references and generation suggestions were not persisted; no relationship is inferred from the code parent."
            ],
        )
    value = resource.item.lineage_context
    try:
        keys = value.get("reference_candidate_keys", [])
        rows = session.scalars(
            select(Candidate).where(
                Candidate.run_id == resource.run.id, Candidate.candidate_key.in_(keys)
            )
        ).all()
        validate_candidate_context(
            value,
            [row.candidate_key for row in rows],
            candidate_kind=resource.item.kind,
        )
    except (ValueError, TypeError, AttributeError) as error:
        raise DomainError(
            "lineage_integrity",
            "Persisted candidate context has invalid or missing references",
            409,
        ) from error
    calls = session.scalars(
        select(ModelCall.id).where(
            ModelCall.run_id == resource.run.id,
            ModelCall.logical_call_id.in_(value["model_call_ids"]),
        )
    ).all()
    missing = []
    if len(calls) != len(value["model_call_ids"]):
        missing.append("Some recorded logical model calls have no persisted call row.")
    if (
        value["feedback"] is not None
        or value["reference_candidate_keys"]
        or value["model_call_ids"]
    ):
        record_result_access(
            session, resource.run, owner_id, source="workbench-generation-context"
        )
    references = [
        LineageReference(
            candidate_id=row.id,
            candidate_key=row.candidate_key,
            code_sha256=row.code_sha256,
        )
        for row in sorted(rows, key=lambda row: keys.index(row.candidate_key))
    ]
    return references, LineageContext(
        status="available",
        feedback=value["feedback"],
        revision_mode=value["revision_mode"],
        generation_status=value["generation_status"],
        feedback_sha256=value["feedback_sha256"],
        feedback_truncated=value["feedback_truncated"],
        model_call_ids=list(calls),
        missing_reasons=missing,
    )


def source_lineage(session, kind, identifier, owner_id, *, offset, limit):
    resource = owned_source(session, kind, identifier, owner_id)
    parent = None
    missing = []
    if resource.item.parent_id:
        parent_resource = owned_source(session, kind, resource.item.parent_id, owner_id)
        if kind == "candidate" and parent_resource.run.id != resource.run.id:
            raise DomainError(
                "lineage_integrity",
                "Candidate parent is not in the same immutable run",
                409,
            )
        parent = _node(session, parent_resource, owner_id)
    child_query = _children_query(resource, owner_id).subquery()
    children = session.execute(
        select(child_query)
        .order_by(child_query.c.created_at, child_query.c.id)
        .offset(offset)
        .limit(limit)
    ).all()
    references, context = _context(session, resource, owner_id)
    focus = _node(session, resource, owner_id)
    if focus.evaluation_count == 0:
        missing.append(
            "No leaf Evaluation is registered for this exact code resource; absence is not a successful or zero-return backtest."
        )
    return WorkbenchLineage(
        focus=focus,
        parent=parent,
        seed_strategy=_node(
            session,
            owned_source(session, "strategy_version", resource.strategy.id, owner_id),
            owner_id,
        )
        if kind == "candidate"
        else None,
        children=[
            _node(
                session,
                owned_source(session, row.resource_kind, row.id, owner_id),
                owner_id,
            )
            for row in children
        ],
        child_count=focus.child_count,
        offset=offset,
        limit=limit,
        reference_candidates=references,
        context=context,
        missing_reasons=missing,
    )
