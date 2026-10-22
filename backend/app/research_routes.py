"""Owner-scoped M3 research control and immutable provenance queries."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
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
):
    run = submit_research_run(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
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
    return {
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
    session.commit()
    return run


def _page(session, model, run_id, owner_id, offset, limit):
    owned_research_run(session, run_id, owner_id)
    clause = model.run_id == run_id
    return {
        "data": session.scalars(
            select(model)
            .where(clause)
            .order_by(model.created_at, model.id)
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
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return _page(session, Candidate, run_id, user.id, offset, limit)


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


@router.get("/research-capabilities", operation_id="research_capabilities")
def research_capabilities(user: CurrentUser):
    from packages.tradegrad_adapter.environment import environment_digest

    return {
        "environment_digest": environment_digest(),
        "schema_version": "tradegrad.run.v1",
        "prompt_versions": ["source_prompts.v1"],
        "model_providers": ["fixture"],
        "models": ["deterministic-fixture-v1"],
        "execution_modes": ["optimization"],
        "seed_only_research": {"optimizer": {"max_iterations": 0}},
        "live_model_calls": False,
        "holdout_execution": False,
        "checkpoint_resume": True,
    }
