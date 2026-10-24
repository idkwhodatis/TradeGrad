"""Status-only comparison jobs; outcome reads re-admit both immutable sources."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import func, select

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.comparison_schemas import (
    ComparisonCreate,
    ComparisonDetail,
    ComparisonPublic,
    ComparisonResult,
    ComparisonSectionPage,
)
from backend.app.comparison_services import (
    comparison_result_view,
    comparison_section_view,
    load_owned_comparison_result,
    owned_comparison,
    release_comparison_access,
    retry_comparison,
    submit_comparison,
)
from backend.app.comparison_views import comparison_public, safe_error
from backend.app.models import Comparison, ExecutionAttempt
from backend.app.schemas import AttemptPublic, Page
from backend.app.services import DomainError, request_cancel

router = APIRouter(tags=["comparisons"])
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]
ManifestPin = Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]
ComparisonSection = Literal[
    "events",
    "candidates",
    "features",
    "predicates",
    "targets",
    "orders",
    "fills",
    "positions",
    "account",
    "turnover",
    "holding_duration",
]


@router.post(
    "/comparisons",
    response_model=ComparisonPublic,
    status_code=202,
    operation_id="create_comparison",
)
def create(
    body: ComparisonCreate,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item, run = submit_comparison(
        session, user.id, body, idempotency_key, request.app.state.settings
    )
    response = comparison_public(item, run)
    session.commit()
    return response


@router.get(
    "/comparisons",
    response_model=Page[ComparisonPublic],
    operation_id="list_comparisons",
)
def listing(
    user: CurrentUser,
    session: SessionDep,
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(50, ge=1, le=100),
):
    query = select(Comparison).where(Comparison.owner_id == user.id)
    items = session.scalars(
        query.order_by(Comparison.created_at.desc(), Comparison.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "data": [
            comparison_public(*owned_comparison(session, item.id, user.id))
            for item in items
        ],
        "count": session.scalar(select(func.count()).select_from(query.subquery())),
    }


@router.get(
    "/comparisons/{comparison_id}",
    response_model=ComparisonDetail,
    operation_id="read_comparison",
)
def detail(comparison_id: UUID, user: CurrentUser, session: SessionDep):
    item, run = owned_comparison(session, comparison_id, user.id)
    attempts = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.run_id == run.id)
        .order_by(ExecutionAttempt.generation)
    ).all()
    return ComparisonDetail(
        **comparison_public(item, run).model_dump(),
        attempts=[
            AttemptPublic.model_validate(attempt).model_copy(
                update={"error": safe_error(attempt.error)}
            )
            for attempt in attempts
        ],
    )


@router.post(
    "/comparisons/{comparison_id}/cancel",
    response_model=ComparisonPublic,
    status_code=202,
    operation_id="cancel_comparison",
)
def cancel(comparison_id: UUID, user: CurrentUser, session: SessionDep):
    item, run = owned_comparison(session, comparison_id, user.id)
    run = request_cancel(session, run.id, user.id)
    response = comparison_public(item, run)
    session.commit()
    return response


@router.post(
    "/comparisons/{comparison_id}/retry",
    response_model=ComparisonPublic,
    status_code=202,
    operation_id="retry_comparison",
)
def retry(
    comparison_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    idempotency_key: IdempotencyKey,
):
    item, run = retry_comparison(
        session,
        comparison_id,
        user.id,
        request.app.state.settings,
        idempotency_key=idempotency_key,
    )
    response = comparison_public(item, run)
    session.commit()
    return response


@router.post(
    "/comparisons/{comparison_id}/pause", operation_id="pause_comparison_unsupported"
)
def pause(comparison_id: UUID, user: CurrentUser, session: SessionDep):
    owned_comparison(session, comparison_id, user.id)
    raise DomainError(
        "pause_unsupported",
        "Comparison cannot pause; cancel, confirm query stop, and retry instead",
    )


@router.get(
    "/comparisons/{comparison_id}/result",
    response_model=ComparisonResult,
    operation_id="read_comparison_result",
)
def result(
    comparison_id: UUID,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    manifest_sha256: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
):
    loaded = load_owned_comparison_result(
        session,
        comparison_id,
        user.id,
        request.app.state.settings,
        manifest_sha256=manifest_sha256,
    )
    response = comparison_result_view(loaded)
    release_comparison_access(session, loaded, user.id)
    session.commit()
    return response


@router.get(
    "/comparisons/{comparison_id}/sections/{section}",
    response_model=ComparisonSectionPage,
    operation_id="read_comparison_section",
)
def section(
    comparison_id: UUID,
    section: ComparisonSection,
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    manifest_sha256: ManifestPin,
    event_pair_id: str | None = Query(None, min_length=1, max_length=200),
    instrument_id: str | None = Query(None, min_length=1, max_length=200),
    offset: int = Query(0, ge=0, le=100000),
    limit: int = Query(100, ge=1, le=500),
):
    loaded = load_owned_comparison_result(
        session,
        comparison_id,
        user.id,
        request.app.state.settings,
        manifest_sha256=manifest_sha256,
    )
    response = comparison_section_view(
        loaded,
        section,
        event_pair_id=event_pair_id,
        instrument_id=instrument_id,
        offset=offset,
        limit=limit,
    )
    release_comparison_access(session, loaded, user.id)
    session.commit()
    return response
