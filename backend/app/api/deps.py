"""Dependency pattern adapted from FastAPI full-stack template; see NOTICE.md."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from backend.app.core.security import decode_access_token
from backend.app.models import User
from backend.app.services import DomainError

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl="/api/v1/login/access-token", auto_error=False
)


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_current_user(
    request: Request,
    session: SessionDep,
    token: Annotated[str | None, Depends(reusable_oauth2)],
) -> User:
    if token is None:
        raise DomainError("unauthorized", "Authentication required", 401)
    try:
        user_id = decode_access_token(token, request.app.state.settings)
    except (InvalidTokenError, ValueError, TypeError):
        raise DomainError(
            "unauthorized", "Invalid or expired access token", 401
        ) from None
    user = session.get(User, user_id)
    if not user or not user.is_active:
        raise DomainError("unauthorized", "Invalid or inactive user", 401)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
