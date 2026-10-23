"""Owner-scoped portable API, explicit separate schemas and result validator."""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.models import (
    Candidate,
    Evaluation,
    ExecutionAttempt,
    ModelCall,
    ResearchCheckpoint,
    ResearchRun,
    StrategyVersion,
)
from backend.app.portable_schemas import (
    PortableDefaultRequest,
    PortableEvaluationDetail,
    PortableEvaluationPublic,
    PortablePreflightPublic,
    PortableResultQuery,
    PortableRunCreate,
    PortableRunDetail,
    PortableRunPublic,
    PortableStrategyCreate,
    PortableStrategyDetail,
    PortableStrategyPublic,
    PortableTemplatePublic,
)
from backend.app.portable_services import (
    create_portable_strategy,
    owned_portable_run,
    pause_portable_run,
    portable_defaults,
    resume_portable_run,
    submit_portable_run,
)
from backend.app.portable_views import (
    load_owned_portable_result,
    portable_result_view,
    portable_usage,
    preflight_portable,
)
from backend.app.research_schemas import (
    CandidateDetail,
    CandidatePublic,
    CheckpointDetail,
    CheckpointPublic,
    ModelCallDetail,
    ModelCallPublic,
)
from backend.app.research_view_schemas import ResearchUsagePublic
from backend.app.result_access import (
    contains_result_data,
    record_control_response_access,
    record_result_access,
    record_verified_result_access,
)
from backend.app.schemas import AttemptPublic, Page
from backend.app.services import DomainError, request_cancel
from packages.tradegrad_adapter.portable_prompts import PROMPT_VERSION, template_source

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.get(
    "/portable-templates",
    response_model=list[PortableTemplatePublic],
    operation_id="list_portable_templates",
)
def templates(user: CurrentUser):
    return [
        PortableTemplatePublic(
            strategy_kind=kind,
            template_version=PROMPT_VERSION,
            source=(source := template_source(kind)),
            code_sha256=hashlib.sha256(source.encode()).hexdigest(),
        )
        for kind in ("portfolio", "event")
    ]


@router.post(
    "/portable-strategies",
    response_model=PortableStrategyDetail,
    status_code=201,
    operation_id="create_portable_strategy",
)
def create_strategy(
    body: PortableStrategyCreate, user: CurrentUser, session: SessionDep
):
    item = create_portable_strategy(session, user.id, body)
    session.commit()
    return item


@router.get(
    "/portable-strategies",
    response_model=Page[PortableStrategyPublic],
    operation_id="list_portable_strategies",
)
def strategies(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    clause = (StrategyVersion.owner_id == user.id) & (
        StrategyVersion.mode == "portable"
    )
    return {
        "data": session.scalars(
            select(StrategyVersion)
            .where(clause)
            .order_by(StrategyVersion.created_at.desc(), StrategyVersion.id)
            .offset(offset)
            .limit(limit)
        ).all(),
        "count": session.scalar(
            select(func.count()).select_from(StrategyVersion).where(clause)
        ),
    }


@router.get(
    "/portable-strategies/{strategy_id}",
    response_model=PortableStrategyDetail,
    operation_id="read_portable_strategy",
)
def strategy(strategy_id: UUID, user: CurrentUser, session: SessionDep):
    item = session.scalar(
        select(StrategyVersion).where(
            StrategyVersion.id == strategy_id,
            StrategyVersion.owner_id == user.id,
            StrategyVersion.mode == "portable",
        )
    )
    if item is None:
        raise DomainError("not_found", "Portable strategy not found", 404)
    return item


@router.post(
    "/portable-runs/default-config",
    response_model=PortableRunCreate,
    operation_id="default_portable_config",
)
def defaults(body: PortableDefaultRequest, user: CurrentUser, session: SessionDep):
    return portable_defaults(session, user.id, body)


@router.post(
    "/portable-runs/preflight",
    response_model=PortablePreflightPublic,
    operation_id="preflight_portable_run",
)
def preflight(
    body: PortableRunCreate, user: CurrentUser, session: SessionDep, request: Request
):
    return preflight_portable(session, user.id, body, request.app.state.settings)


@router.post(
    "/portable-runs",
    response_model=PortableRunPublic,
    status_code=202,
    operation_id="create_portable_run",
)
def create_run(
    body: PortableRunCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
    experiment_id: UUID | None = None,
    split_version_id: UUID | None = None,
):
    run = submit_portable_run(
        session,
        user.id,
        body,
        idempotency_key,
        request.app.state.settings,
        experiment_id=experiment_id,
        split_version_id=split_version_id,
    )
    record_control_response_access(session, run, user.id)
    session.commit()
    return run


@router.get(
    "/portable-runs",
    response_model=Page[PortableRunPublic],
    operation_id="list_portable_runs",
)
def runs(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    clause = (ResearchRun.owner_id == user.id) & (
        ResearchRun.config["schema_version"].astext == "tradegrad.portable.run.v1"
    )
    response = {
        "data": session.scalars(
            select(ResearchRun)
            .where(clause)
            .order_by(ResearchRun.created_at.desc(), ResearchRun.id)
            .offset(offset)
            .limit(limit)
        ).all(),
        "count": session.scalar(
            select(func.count()).select_from(ResearchRun).where(clause)
        ),
    }

    for item in response["data"]:
        if contains_result_data(item.result):
            record_result_access(session, item, user.id, source="run-summary")
    session.commit()
    return response


@router.get(
    "/portable-runs/{run_id}",
    response_model=PortableRunDetail,
    operation_id="read_portable_run",
)
def run(run_id: UUID, user: CurrentUser, session: SessionDep):
    item = owned_portable_run(session, run_id, user.id)
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run_id)
        .order_by(ExecutionAttempt.generation)
    ).all()
    if contains_result_data(item.result):
        record_result_access(session, item, user.id, source="run-summary")
    session.commit()
    return PortableRunDetail(
        **PortableRunPublic.model_validate(item).model_dump(),
        attempts=[AttemptPublic.model_validate(a) for a in attempts],
    )


@router.post(
    "/portable-runs/{run_id}/pause",
    response_model=PortableRunPublic,
    status_code=202,
    operation_id="pause_portable_run",
)
def pause(run_id: UUID, user: CurrentUser, session: SessionDep):
    item = pause_portable_run(session, run_id, user.id)
    record_control_response_access(session, item, user.id)
    session.commit()
    return item


@router.post(
    "/portable-runs/{run_id}/cancel",
    response_model=PortableRunPublic,
    status_code=202,
    operation_id="cancel_portable_run",
)
def cancel(run_id: UUID, user: CurrentUser, session: SessionDep):
    owned_portable_run(session, run_id, user.id)
    item = request_cancel(session, run_id, user.id)
    record_control_response_access(session, item, user.id)
    session.commit()
    return item


@router.post(
    "/portable-runs/{run_id}/resume",
    response_model=PortableRunPublic,
    status_code=202,
    operation_id="resume_portable_run",
)
def resume(run_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    item = resume_portable_run(session, run_id, user.id, request.app.state.settings)
    record_control_response_access(session, item, user.id)
    session.commit()
    return item


def _page(session, model, run_id, owner_id, offset, limit):
    owned_portable_run(session, run_id, owner_id)
    clause = model.run_id == run_id
    return {
        "data": session.scalars(
            select(model)
            .where(clause)
            .order_by(
                model.sequence if model is ResearchCheckpoint else model.created_at,
                model.id,
            )
            .offset(offset)
            .limit(limit)
        ).all(),
        "count": session.scalar(select(func.count()).select_from(model).where(clause)),
    }


def _owned(session, model, identifier, owner_id):
    row = session.scalar(
        select(model)
        .join(ResearchRun, ResearchRun.id == model.run_id)
        .where(
            model.id == identifier,
            ResearchRun.owner_id == owner_id,
            ResearchRun.config["schema_version"].astext == "tradegrad.portable.run.v1",
        )
    )
    if row is None:
        raise DomainError("not_found", "Portable resource not found", 404)
    if (model is Evaluation and contains_result_data(row.payload)) or model in {
        ResearchCheckpoint,
        ModelCall,
    }:
        run = owned_portable_run(session, row.run_id, owner_id)
        record_result_access(
            session,
            run,
            owner_id,
            evaluation=row if model is Evaluation else None,
            source=f"{model.__tablename__}:{row.id}",
        )
        session.commit()
    return row


@router.get(
    "/portable-runs/{run_id}/candidates",
    response_model=Page[CandidatePublic],
    operation_id="list_portable_candidates",
)
def candidates(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, Candidate, run_id, user.id, offset, limit)


@router.get(
    "/portable-candidates/{candidate_id}",
    response_model=CandidateDetail,
    operation_id="read_portable_candidate",
)
def candidate(candidate_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, Candidate, candidate_id, user.id)


@router.get(
    "/portable-runs/{run_id}/evaluations",
    response_model=Page[PortableEvaluationPublic],
    operation_id="list_portable_evaluations",
)
def evaluations(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, Evaluation, run_id, user.id, offset, limit)


@router.get(
    "/portable-evaluations/{evaluation_id}",
    response_model=PortableEvaluationDetail,
    operation_id="read_portable_evaluation",
)
def evaluation(evaluation_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, Evaluation, evaluation_id, user.id)


@router.get(
    "/portable-runs/{run_id}/model-calls",
    response_model=Page[ModelCallPublic],
    operation_id="list_portable_model_calls",
)
def model_calls(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, ModelCall, run_id, user.id, offset, limit)


@router.get(
    "/portable-model-calls/{model_call_id}",
    response_model=ModelCallDetail,
    operation_id="read_portable_model_call",
)
def model_call(model_call_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, ModelCall, model_call_id, user.id)


@router.get(
    "/portable-runs/{run_id}/checkpoints",
    response_model=Page[CheckpointPublic],
    operation_id="list_portable_checkpoints",
)
def checkpoints(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, ResearchCheckpoint, run_id, user.id, offset, limit)


@router.get(
    "/portable-checkpoints/{checkpoint_id}",
    response_model=CheckpointDetail,
    operation_id="read_portable_checkpoint",
)
def checkpoint(checkpoint_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, ResearchCheckpoint, checkpoint_id, user.id)


@router.get(
    "/portable-runs/{run_id}/usage",
    response_model=ResearchUsagePublic,
    operation_id="read_portable_usage",
)
def usage(run_id: UUID, user: CurrentUser, session: SessionDep):
    return portable_usage(session, run_id, user.id)


@router.get(
    "/portable-evaluations/{evaluation_id}/results",
    response_model=PortableResultQuery,
    operation_id="read_portable_results",
)
def results(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    section: Literal[
        "account_series",
        "positions",
        "orders",
        "fills",
        "closed_trades",
        "native_metrics",
        "decision_trace",
        "logical_orders",
    ] = "account_series",
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    result = load_owned_portable_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    response = portable_result_view(result, section, offset, limit)
    record_verified_result_access(session, result, user.id)
    session.commit()
    return response


@router.get(
    "/portable-evaluations/{evaluation_id}/results/download",
    response_class=Response,
    operation_id="download_portable_results",
    responses={
        200: {
            "content": {
                "application/json": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result = load_owned_portable_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    record_verified_result_access(session, result, user.id, purpose="result_download")
    session.commit()
    return Response(
        result.content,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="portable-evaluation-{evaluation_id}.json"',
        },
    )
