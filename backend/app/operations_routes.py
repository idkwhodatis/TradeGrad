"""Owner samples by default; global operations require an active administrator."""

from typing import Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.deps import CurrentUser, SessionDep
from backend.app.operations_schemas import OperationsPublic
from backend.app.operations_views import read_operations
from backend.app.services import DomainError

router = APIRouter()


@router.get(
    "/operations", response_model=OperationsPublic, operation_id="read_operations"
)
def operations(
    user: CurrentUser,
    session: SessionDep,
    request: Request,
    scope: Literal["owner", "deployment"] = "owner",
    days: int = Query(7, ge=1, le=31),
):
    if scope == "deployment" and not user.is_superuser:
        raise DomainError("forbidden", "Active administrator permission required", 403)
    try:
        return read_operations(
            session, user, request.app.state.settings, scope=scope, days=days
        )
    except SQLAlchemyError:
        session.rollback()
        raise DomainError(
            "operations_unavailable",
            "Operational observations are unavailable or exceeded the bounded query deadline. Retry later.",
            503,
        ) from None
