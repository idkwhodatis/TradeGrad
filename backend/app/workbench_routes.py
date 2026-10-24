"""Version-bound I2 description and expandable provenance endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.models import Evaluation, ResearchRun
from backend.app.schemas import Page
from backend.app.services import DomainError
from backend.app.workbench_schemas import (
    CandidateBookmark,
    CandidateBookmarkSet,
    ResearchTemplate,
    SpecRevisionCreate,
    WorkbenchEvaluation,
    WorkbenchLineage,
    WorkbenchSpec,
)
from backend.app.workbench_services import (
    _evaluation_public,
    create_spec_revision,
    list_specs,
    read_spec,
    set_bookmark,
    source_evaluations,
    source_lineage,
    template_catalog,
)

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
Offset = Annotated[int, Query(ge=0, le=100000)]
Limit = Annotated[int, Query(ge=1, le=100)]
Revision = Annotated[int | None, Query(ge=1, le=1000000)]
SpecHash = Annotated[str | None, Query(pattern="^[a-f0-9]{64}$")]


@router.get(
    "/research-templates",
    response_model=Page[ResearchTemplate],
    operation_id="list_research_templates",
)
def templates(user: CurrentUser):
    return template_catalog()


@router.get(
    "/strategy-versions/{strategy_id}/spec",
    response_model=WorkbenchSpec,
    operation_id="read_strategy_spec",
)
def strategy_spec(
    strategy_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    revision: Revision = None,
    spec_sha256: SpecHash = None,
):
    result = read_spec(
        session,
        "strategy_version",
        strategy_id,
        user.id,
        revision=revision,
        expected_spec_sha256=spec_sha256,
    )
    session.commit()
    return result


@router.get(
    "/candidates/{candidate_id}/spec",
    response_model=WorkbenchSpec,
    operation_id="read_candidate_spec",
)
def candidate_spec(
    candidate_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    revision: Revision = None,
    spec_sha256: SpecHash = None,
):
    result = read_spec(
        session,
        "candidate",
        candidate_id,
        user.id,
        revision=revision,
        expected_spec_sha256=spec_sha256,
    )
    session.commit()
    return result


@router.get(
    "/strategy-versions/{strategy_id}/spec/revisions",
    response_model=Page[WorkbenchSpec],
    operation_id="list_strategy_spec_revisions",
)
def strategy_revisions(
    strategy_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = list_specs(
        session, "strategy_version", strategy_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.get(
    "/candidates/{candidate_id}/spec/revisions",
    response_model=Page[WorkbenchSpec],
    operation_id="list_candidate_spec_revisions",
)
def candidate_revisions(
    candidate_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = list_specs(
        session, "candidate", candidate_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.post(
    "/strategy-versions/{strategy_id}/spec/revisions",
    response_model=WorkbenchSpec,
    status_code=201,
    responses={200: {"model": WorkbenchSpec, "description": "Exact idempotent replay"}},
    operation_id="create_strategy_spec_revision",
)
def revise_strategy_spec(
    strategy_id: UUID,
    body: SpecRevisionCreate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    result, created = create_spec_revision(
        session, "strategy_version", strategy_id, user.id, body, idempotency_key
    )
    session.commit()
    response.status_code = 201 if created else 200
    return result


@router.post(
    "/candidates/{candidate_id}/spec/revisions",
    response_model=WorkbenchSpec,
    status_code=201,
    responses={200: {"model": WorkbenchSpec, "description": "Exact idempotent replay"}},
    operation_id="create_candidate_spec_revision",
)
def revise_candidate_spec(
    candidate_id: UUID,
    body: SpecRevisionCreate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    result, created = create_spec_revision(
        session, "candidate", candidate_id, user.id, body, idempotency_key
    )
    session.commit()
    response.status_code = 201 if created else 200
    return result


@router.get(
    "/strategy-versions/{strategy_id}/lineage",
    response_model=WorkbenchLineage,
    operation_id="read_strategy_lineage",
)
def strategy_lineage(
    strategy_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = source_lineage(
        session, "strategy_version", strategy_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.get(
    "/candidates/{candidate_id}/lineage",
    response_model=WorkbenchLineage,
    operation_id="read_candidate_lineage",
)
def candidate_lineage(
    candidate_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = source_lineage(
        session, "candidate", candidate_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.get(
    "/strategy-versions/{strategy_id}/evaluations",
    response_model=Page[WorkbenchEvaluation],
    operation_id="list_strategy_evaluations",
)
def strategy_evaluations(
    strategy_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = source_evaluations(
        session, "strategy_version", strategy_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.get(
    "/candidates/{candidate_id}/evaluations",
    response_model=Page[WorkbenchEvaluation],
    operation_id="list_candidate_evaluations",
)
def candidate_evaluations(
    candidate_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    offset: Offset = 0,
    limit: Limit = 50,
):
    result = source_evaluations(
        session, "candidate", candidate_id, user.id, offset=offset, limit=limit
    )
    session.commit()
    return result


@router.get(
    "/evaluations/{evaluation_id}/workbench",
    response_model=WorkbenchEvaluation,
    operation_id="read_workbench_evaluation",
)
def exact_evaluation(evaluation_id: UUID, user: CurrentUser, session: SessionDep):
    evaluation = session.scalar(
        select(Evaluation)
        .join(ResearchRun, ResearchRun.id == Evaluation.run_id)
        .where(Evaluation.id == evaluation_id, ResearchRun.owner_id == user.id)
    )
    if evaluation is None:
        raise DomainError("not_found", "Evaluation not found", 404)
    result = _evaluation_public(session, evaluation, user.id)
    session.commit()
    return result


@router.post(
    "/candidates/{candidate_id}/bookmark",
    response_model=CandidateBookmark,
    operation_id="set_candidate_bookmark",
)
def bookmark(
    candidate_id: UUID,
    body: CandidateBookmarkSet,
    user: CurrentUser,
    session: SessionDep,
):
    result = set_bookmark(session, candidate_id, user.id, body.bookmarked)
    session.commit()
    return result
