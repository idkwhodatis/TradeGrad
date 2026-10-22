"""Adapted from FastAPI full-stack template (MIT); see backend/NOTICE.md."""

import secrets
from datetime import timedelta
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from backend.app.core.config import Settings
from backend.app.models import utcnow

password_hash = PasswordHash.recommended()
# Keep absent-user verification work comparable without a reusable dummy input.
DUMMY_HASH = password_hash.hash(secrets.token_urlsafe(32))
ALGORITHM = "HS256"


def get_password_hash(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain 12 to 1024 characters")
    return password_hash.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if len(plain) > 1024:
        return False
    try:
        return password_hash.verify(plain, hashed)
    except Exception:  # noqa: BLE001 - malformed stored password hashes must never authenticate
        return False


def create_access_token(user_id: UUID, settings: Settings) -> str:
    now = utcnow()
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": now,
            "exp": now + timedelta(minutes=settings.access_token_minutes),
            "iss": "tradegrad",
            "aud": "tradegrad-api",
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str, settings: Settings) -> UUID:
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[ALGORITHM],
        issuer="tradegrad",
        audience="tradegrad-api",
        options={"require": ["sub", "exp", "iat", "iss", "aud"]},
    )
    return UUID(payload["sub"])
