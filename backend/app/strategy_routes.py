"""Immutable native strategy authoring; evaluation remains an isolated Run action."""

from typing import Annotated

from fastapi import APIRouter, Header, Response

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.schemas import StrategyDetail
from backend.app.strategy_schemas import StrategyCreate
from backend.app.strategy_services import create_strategy_version

router = APIRouter()
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
]


@router.post(
    "/strategy-versions",
    response_model=StrategyDetail,
    status_code=201,
    responses={
        200: {"model": StrategyDetail, "description": "Exact idempotent replay"}
    },
    operation_id="create_strategy_version",
)
def create_strategy(
    body: StrategyCreate,
    user: CurrentUser,
    session: SessionDep,
    response: Response,
    idempotency_key: IdempotencyKey,
):
    item, created = create_strategy_version(session, user.id, body, idempotency_key)
    session.commit()
    response.status_code = 201 if created else 200
    return item
