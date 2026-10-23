"""Final evaluation has separate submission, lifecycle and evidence routes."""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.experiment_services import record_exposure
from backend.app.final_result_admission import require_final_result_admission
from backend.app.final_summary_access import (
    final_evidence_response,
    final_public_response,
    public_projection,
)
from backend.app.models import (
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
)
from backend.app.portable_final_schemas import (
    PortableFinalEvaluationCreate,
    PortableFinalEvaluationDetail,
    PortableFinalEvaluationEvidence,
    PortableFinalEvaluationPreflight,
    PortableFinalEvaluationPublic,
)
from backend.app.portable_final_services import (
    owned_final_run,
    preflight_final_evaluation,
    retry_final_evaluation,
    submit_final_evaluation,
)
from backend.app.portable_final_views import (
    PortableFinalResultQuery,
    final_result_view,
    load_owned_final_result,
)
from backend.app.result_exploration import price_replay, result_return_analysis
from backend.app.result_exploration_schemas import PriceReplay, ResultReturnAnalysis
from backend.app.schemas import AttemptPublic, Page
from backend.app.services import DomainError, request_cancel
from packages.tradegrad_adapter.config import DateWindow

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.post(
    "/portable-final-evaluations/preflight",
    response_model=PortableFinalEvaluationPreflight,
    operation_id="portable_preflight_final_evaluation",
)
def preflight(
    body: PortableFinalEvaluationCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
):
    return preflight_final_evaluation(
        session, user.id, body, request.app.state.settings
    )


@router.post(
    "/portable-final-evaluations",
    response_model=PortableFinalEvaluationPublic,
    status_code=202,
    operation_id="portable_create_final_evaluation",
)
def create(
    body: PortableFinalEvaluationCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    run = submit_final_evaluation(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    response = final_public_response(
        session,
        run,
        user.id,
        request.app.state.settings,
        PortableFinalEvaluationPublic,
        _result_access,
    )
    session.commit()
    return response


@router.get(
    "/portable-final-evaluations",
    response_model=Page[PortableFinalEvaluationPublic],
    operation_id="portable_list_final_evaluations",
)
def listing(
    user: CurrentUser,
    session: SessionDep,
    selection_id: UUID | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    clause = (ResearchRun.owner_id == user.id) & (
        ResearchRun.config["schema_version"].astext
        == "tradegrad.portable.final-evaluation.v1"
    )
    if selection_id:
        clause &= ResearchRun.final_selection_id == selection_id
    return {
        "data": [
            public_projection(PortableFinalEvaluationPublic, run, result=None)
            for run in session.scalars(
                select(ResearchRun)
                .where(clause)
                .order_by(ResearchRun.created_at.desc(), ResearchRun.id)
                .offset(offset)
                .limit(limit)
            ).all()
        ],
        "count": session.scalar(
            select(func.count()).select_from(ResearchRun).where(clause)
        ),
    }


@router.get(
    "/portable-final-evaluations/{run_id}",
    response_model=PortableFinalEvaluationDetail,
    operation_id="portable_read_final_evaluation",
)
def detail(run_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    run = owned_final_run(session, run_id, user.id)
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .order_by(ExecutionAttempt.generation)
    ).all()
    public = final_public_response(
        session,
        run,
        user.id,
        request.app.state.settings,
        PortableFinalEvaluationPublic,
        _result_access,
    )
    response = PortableFinalEvaluationDetail(
        **public.model_dump(),
        attempts=[AttemptPublic.model_validate(item) for item in attempts],
    )
    session.commit()
    return response


@router.post(
    "/portable-final-evaluations/{run_id}/cancel",
    response_model=PortableFinalEvaluationPublic,
    status_code=202,
    operation_id="portable_cancel_final_evaluation",
)
def cancel(run_id: UUID, user: CurrentUser, session: SessionDep, request: Request):
    owned_final_run(session, run_id, user.id)
    run = request_cancel(session, run_id, user.id)
    response = final_public_response(
        session,
        run,
        user.id,
        request.app.state.settings,
        PortableFinalEvaluationPublic,
        _result_access,
    )
    session.commit()
    return response


@router.post(
    "/portable-final-evaluations/{run_id}/retry",
    response_model=PortableFinalEvaluationPublic,
    status_code=202,
    operation_id="portable_retry_final_evaluation",
)
def retry(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    run = retry_final_evaluation(
        session,
        run_id,
        user.id,
        request.app.state.settings,
        idempotency_key=idempotency_key,
    )
    response = final_public_response(
        session,
        run,
        user.id,
        request.app.state.settings,
        PortableFinalEvaluationPublic,
        _result_access,
    )
    session.commit()
    return response


@router.post(
    "/portable-final-evaluations/{run_id}/pause",
    operation_id="portable_pause_final_evaluation_unsupported",
)
def pause(run_id: UUID, user: CurrentUser, session: SessionDep):
    owned_final_run(session, run_id, user.id)
    raise DomainError(
        "pause_unsupported",
        "Final evaluation cannot pause; cancel, confirm sandbox stop and retry instead",
    )


@router.get(
    "/portable-final-evaluations/{run_id}/evaluations",
    response_model=Page[PortableFinalEvaluationEvidence],
    operation_id="portable_list_final_evaluation_evidence",
)
def evidence(
    run_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    run = owned_final_run(session, run_id, user.id)
    clause = Evaluation.run_id == run_id
    response = {
        "data": final_evidence_response(
            session,
            run,
            user.id,
            request.app.state.settings,
            PortableFinalEvaluationEvidence,
            _result_access,
            session.scalars(
                select(Evaluation)
                .where(clause)
                .order_by(Evaluation.created_at, Evaluation.id)
                .offset(offset)
                .limit(limit)
            ).all(),
        ),
        "count": session.scalar(
            select(func.count()).select_from(Evaluation).where(clause)
        ),
    }
    session.commit()
    return response


def _result_access(session, evaluation_id, owner_id, settings, purpose):
    result = load_owned_final_result(session, evaluation_id, owner_id, settings)
    snapshot = require_final_result_admission(
        session, result.run, result.evaluation, owner_id, dataset=result.dataset
    )
    record_exposure(
        session,
        owner_id=owner_id,
        dataset=result.dataset,
        run=result.run,
        window=DateWindow(
            start=result.evaluation.window["start"], end=result.evaluation.window["end"]
        ),
        purpose=purpose,
        source_key=f"final-result:{evaluation_id}:{purpose}",
        provenance={
            "evaluation_id": str(evaluation_id),
            "artifact_sha256": result.artifact.sha256,
        },
    )
    return result, snapshot


@router.get(
    "/portable-final-evaluations/evaluations/{evaluation_id}/result",
    response_model=PortableFinalResultQuery,
    operation_id="portable_query_final_result",
)
def query_final_result(
    evaluation_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    section: str = "account_series",
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
    start: date | None = None,
    end: date | None = None,
    listing_id: UUID | None = None,
    sort_by: str | None = Query(None, max_length=64),
    sort_direction: str = Query("asc", max_length=4),
    side: str | None = Query(None, max_length=8),
):
    result, snapshot = _result_access(
        session, evaluation_id, user.id, request.app.state.settings, "result_preview"
    )
    response = final_result_view(
        result,
        exposure_snapshot=snapshot,
        section=section,
        offset=offset,
        limit=limit,
        start=start,
        end=end,
        listing_id=listing_id,
        sort_by=sort_by,
        sort_direction=sort_direction,
        side=side,
    )
    session.commit()
    return response


@router.get(
    "/portable-final-evaluations/evaluations/{evaluation_id}/result/download",
    response_class=Response,
    operation_id="portable_download_final_result",
    responses={
        200: {
            "content": {
                "application/json": {"schema": {"type": "string", "format": "binary"}}
            }
        }
    },
)
def download_final_result(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result, _ = _result_access(
        session, evaluation_id, user.id, request.app.state.settings, "result_download"
    )
    session.commit()
    return Response(
        result.content,
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": f'attachment; filename="final-evaluation-{evaluation_id}.json"',
        },
    )


@router.get(
    "/portable-final-evaluations/evaluations/{evaluation_id}/return-analysis",
    response_model=ResultReturnAnalysis,
    operation_id="read_portable_final_return_analysis",
)
def read_portable_final_return_analysis(
    evaluation_id: UUID, user: CurrentUser, session: SessionDep, request: Request
):
    result, _ = _result_access(
        session, evaluation_id, user.id, request.app.state.settings, "result_preview"
    )
    response = result_return_analysis(result)
    session.commit()
    return response


@router.get(
    "/portable-final-evaluations/evaluations/{evaluation_id}/price-replay",
    response_model=PriceReplay,
    operation_id="read_portable_final_price_replay",
)
def read_portable_final_price_replay(
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
    result, _ = _result_access(
        session, evaluation_id, user.id, request.app.state.settings, "result_preview"
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
    session.commit()
    return response
