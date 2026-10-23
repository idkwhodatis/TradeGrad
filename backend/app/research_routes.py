"""Owner-scoped M3 research control and immutable provenance queries."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.list_queries import candidate_query, query_page
from backend.app.models import (
    Candidate,
    Evaluation,
    ExecutionAttempt,
    ModelCall,
    ResearchCheckpoint,
    ResearchRun,
)
from backend.app.research_schemas import (
    CandidateDetail,
    CandidatePublic,
    CheckpointDetail,
    CheckpointPublic,
    EvaluationDetail,
    EvaluationPublic,
    ModelCallDetail,
    ModelCallPublic,
    ResearchRunCreate,
    ResearchRunDetail,
    ResearchRunPublic,
)
from backend.app.research_services import (
    owned_research_run,
    request_pause,
    resume_research_run,
    submit_research_run,
)
from backend.app.research_view_schemas import (
    ResearchCapabilities,
    ResearchPreflightPublic,
    ResearchUsagePublic,
    ResultCompareCreate,
    ResultComparePublic,
    ResultQueryPublic,
    ResultSection,
)
from backend.app.research_views import (
    compare_results,
    load_owned_result,
    preflight_research,
    render_result_report,
    research_capability_metadata,
    research_usage,
    result_view,
)
from backend.app.result_access import (
    contains_result_data,
    record_control_response_access,
    record_result_access,
    record_verified_result_access,
)
from backend.app.result_exploration import price_replay, result_return_analysis
from backend.app.result_exploration_schemas import PriceReplay, ResultReturnAnalysis
from backend.app.schemas import AttemptPublic, Page
from backend.app.services import DomainError, request_cancel

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.post(
    "/research-runs",
    response_model=ResearchRunPublic,
    status_code=202,
    operation_id="create_research_run",
)
def create_research_run(
    body: ResearchRunCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
    experiment_id: UUID | None = None,
    split_version_id: UUID | None = None,
):
    run = submit_research_run(
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
    "/research-runs",
    response_model=Page[ResearchRunPublic],
    operation_id="list_research_runs",
)
def list_research_runs(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    clause = (ResearchRun.owner_id == user.id) & (
        ResearchRun.config["schema_version"].astext == "tradegrad.run.v1"
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
    "/research-runs/{run_id}",
    response_model=ResearchRunDetail,
    operation_id="read_research_run",
)
def read_research_run(run_id: UUID, user: CurrentUser, session: SessionDep):
    run = owned_research_run(session, run_id, user.id)
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .order_by(ExecutionAttempt.generation)
    ).all()
    if contains_result_data(run.result):
        record_result_access(session, run, user.id, source="run-summary")
    session.commit()
    return ResearchRunDetail(
        **ResearchRunPublic.model_validate(run).model_dump(),
        attempts=[AttemptPublic.model_validate(item) for item in attempts],
    )


@router.post(
    "/research-runs/{run_id}/pause",
    response_model=ResearchRunPublic,
    status_code=202,
    operation_id="pause_research_run",
)
@router.post(
    "/runs/{run_id}/pause",
    response_model=ResearchRunPublic,
    status_code=202,
    operation_id="pause_run",
)
def pause_research_run(run_id: UUID, user: CurrentUser, session: SessionDep):
    run = request_pause(session, run_id, user.id)
    record_control_response_access(session, run, user.id)
    session.commit()
    return run


@router.post(
    "/research-runs/{run_id}/cancel",
    response_model=ResearchRunPublic,
    status_code=202,
    operation_id="cancel_research_run",
)
def cancel_research_run(run_id: UUID, user: CurrentUser, session: SessionDep):
    owned_research_run(session, run_id, user.id)
    run = request_cancel(session, run_id, user.id)
    record_control_response_access(session, run, user.id)
    session.commit()
    return run


@router.post(
    "/research-runs/{run_id}/resume",
    response_model=ResearchRunPublic,
    status_code=202,
    operation_id="resume_research_run",
)
def resume_research(
    run_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    run = resume_research_run(session, run_id, user.id, request.app.state.settings)
    record_control_response_access(session, run, user.id)
    session.commit()
    return run


def _page(session, model, run_id, owner_id, offset, limit):
    owned_research_run(session, run_id, owner_id)
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
    item = session.scalar(
        select(model)
        .join(ResearchRun, ResearchRun.id == model.run_id)
        .where(model.id == identifier, ResearchRun.owner_id == owner_id)
    )
    if item is None:
        raise DomainError("not_found", "Research resource not found", 404)
    if (model is Evaluation and contains_result_data(item.payload)) or model in {
        ResearchCheckpoint,
        ModelCall,
    }:
        run = owned_research_run(session, item.run_id, owner_id)
        record_result_access(
            session,
            run,
            owner_id,
            evaluation=item if model is Evaluation else None,
            source=f"{model.__tablename__}:{item.id}",
        )
        session.commit()
    return item


@router.get(
    "/research-runs/{run_id}/candidates",
    response_model=Page[CandidatePublic],
    operation_id="list_research_candidates",
)
def candidates(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
    sort_by: str = Query("created_at", max_length=32),
    sort_direction: str = Query("asc", max_length=4),
    search: str | None = Query(None, max_length=128),
    kind: str | None = Query(None, max_length=16),
    min_iteration: int | None = Query(None, ge=0, le=100000),
):
    owned_research_run(session, run_id, user.id)
    return query_page(
        session,
        candidate_query(
            run_id,
            sort_by=sort_by,
            sort_direction=sort_direction,
            search=search,
            kind=kind,
            min_iteration=min_iteration,
        ),
        offset,
        limit,
    )


@router.get(
    "/candidates/{candidate_id}",
    response_model=CandidateDetail,
    operation_id="read_research_candidate",
)
def candidate(candidate_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, Candidate, candidate_id, user.id)


@router.get(
    "/research-runs/{run_id}/evaluations",
    response_model=Page[EvaluationPublic],
    operation_id="list_research_evaluations",
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
    "/evaluations/{evaluation_id}",
    response_model=EvaluationDetail,
    operation_id="read_research_evaluation",
)
def evaluation(evaluation_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, Evaluation, evaluation_id, user.id)


@router.get(
    "/research-runs/{run_id}/model-calls",
    response_model=Page[ModelCallPublic],
    operation_id="list_research_model_calls",
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
    "/model-calls/{model_call_id}",
    response_model=ModelCallDetail,
    operation_id="read_research_model_call",
)
def model_call(model_call_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, ModelCall, model_call_id, user.id)


@router.get(
    "/research-runs/{run_id}/checkpoints",
    response_model=Page[CheckpointPublic],
    operation_id="list_research_checkpoints",
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
    "/research-checkpoints/{checkpoint_id}",
    response_model=CheckpointDetail,
    operation_id="read_research_checkpoint",
)
def checkpoint(checkpoint_id: UUID, user: CurrentUser, session: SessionDep):
    return _owned(session, ResearchCheckpoint, checkpoint_id, user.id)


@router.get(
    "/research-capabilities",
    response_model=ResearchCapabilities,
    operation_id="research_capabilities",
)
def research_capabilities(user: CurrentUser, request: Request, session: SessionDep):
    from backend.app.workspace_policy import admitted_model_settings, quota_limit

    settings = admitted_model_settings(session, user.id, request.app.state.settings)
    result = research_capability_metadata(settings, user.id)
    result.limits.max_active_runs_per_user = quota_limit(session, user.id, settings)
    result.limits.max_active_imports_per_user = quota_limit(
        session, user.id, settings, "imports"
    )
    return result


@router.post(
    "/research-runs/preflight",
    response_model=ResearchPreflightPublic,
    operation_id="preflight_research_run",
)
def preflight_research_run(
    body: ResearchRunCreate, user: CurrentUser, session: SessionDep, request: Request
):
    return preflight_research(session, user.id, body, request.app.state.settings)


@router.get(
    "/research-evaluations/{evaluation_id}/results",
    response_model=ResultQueryPublic,
    operation_id="read_research_results",
)
def read_research_results(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    sections: Annotated[list[ResultSection] | None, Query(max_length=8)] = None,
    start: date | None = None,
    end: date | None = None,
    listing_id: UUID | None = None,
    max_points: int = Query(1000, ge=4, le=5000),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
    sort_by: str | None = Query(None, max_length=40),
    sort_direction: Literal["asc", "desc"] = "asc",
    side: Literal["buy", "sell"] | None = None,
    order_status: str | None = Query(None, max_length=40),
):
    result = load_owned_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    response = result_view(
        result,
        sections=sections,
        start=start,
        end=end,
        listing_id=listing_id,
        max_points=max_points,
        offset=offset,
        limit=limit,
        sort_by=sort_by,
        sort_direction=sort_direction,
        side=side,
        order_status=order_status,
    )
    record_verified_result_access(session, result, user.id)
    session.commit()
    return response


@router.post(
    "/research-evaluations/compare",
    response_model=ResultComparePublic,
    operation_id="compare_research_results",
)
def compare_research_results(
    body: ResultCompareCreate, user: CurrentUser, session: SessionDep, request: Request
):
    results = [
        load_owned_result(session, identifier, user.id, request.app.state.settings)
        for identifier in body.evaluation_ids
    ]
    response = compare_results(results)
    for result in results:
        record_verified_result_access(session, result, user.id)
    session.commit()
    return response


_DOWNLOAD_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


@router.get(
    "/research-evaluations/{evaluation_id}/results/download",
    response_class=Response,
    operation_id="download_research_results",
    responses={
        200: {
            "content": {
                "application/json": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download_research_results(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result = load_owned_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    record_verified_result_access(session, result, user.id, purpose="result_download")
    session.commit()
    return Response(
        result.content,
        media_type="application/json",
        headers={
            **_DOWNLOAD_HEADERS,
            "Content-Disposition": f'attachment; filename="evaluation-{evaluation_id}.json"',
        },
    )


@router.get(
    "/research-evaluations/{evaluation_id}/report",
    response_class=Response,
    operation_id="download_research_report",
    responses={
        200: {
            "content": {"text/html": {"schema": {"type": "string", "format": "binary"}}}
        }
    },
)
def download_research_report(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result = load_owned_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    content = render_result_report(result)
    record_verified_result_access(session, result, user.id, purpose="result_download")
    session.commit()
    return Response(
        content,
        media_type="text/html",
        headers={
            **_DOWNLOAD_HEADERS,
            "Content-Disposition": f'attachment; filename="evaluation-{evaluation_id}.html"',
        },
    )


@router.get(
    "/research-runs/{run_id}/usage",
    response_model=ResearchUsagePublic,
    operation_id="read_research_usage",
)
def read_research_usage(run_id: UUID, user: CurrentUser, session: SessionDep):
    return research_usage(session, run_id, user.id)


# These read paths use the same authorization, immutable byte/hash validation,
# native trust checks and final-test exposure recording as all result views.


@router.get(
    "/research-evaluations/{evaluation_id}/return-analysis",
    response_model=ResultReturnAnalysis,
    operation_id="read_research_return_analysis",
)
def read_research_return_analysis(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result = load_owned_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    response = result_return_analysis(result)
    record_verified_result_access(session, result, user.id)
    session.commit()
    return response


@router.get(
    "/research-evaluations/{evaluation_id}/price-replay",
    response_model=PriceReplay,
    operation_id="read_research_price_replay",
)
def read_research_price_replay(
    evaluation_id: UUID,
    listing_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    start: date | None = None,
    end: date | None = None,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(250, ge=1, le=500),
):
    result = load_owned_result(
        session, evaluation_id, user.id, request.app.state.settings
    )
    response = price_replay(
        result,
        request.app.state.settings,
        listing_id=listing_id,
        start=start,
        end=end,
        offset=offset,
        limit=limit,
    )
    record_verified_result_access(session, result, user.id)
    session.commit()
    return response
