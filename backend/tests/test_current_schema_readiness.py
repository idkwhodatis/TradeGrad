"""Offline readiness contracts, with explicitly fake SQL responses."""

import secrets
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from backend.app.api.deps import get_session
from backend.app.core.config import Settings
from backend.app.core.db import REQUIRED_SCHEMA_REVISION
from backend.app.main import create_app


def test_executable_schema_requirement_is_exactly_the_only_alembic_head():
    root = Path(__file__).resolve().parents[2]
    script = ScriptDirectory.from_config(Config(str(root / "backend/alembic.ini")))
    assert script.get_heads() == [REQUIRED_SCHEMA_REVISION]


@pytest.mark.parametrize(
    "revisions,ready",
    [
        ([REQUIRED_SCHEMA_REVISION], True),
        ([], False),
        (["006_provider_connections"], False),
        (["009_independent_evaluations"], False),
        (["011_future_schema"], False),
        ([REQUIRED_SCHEMA_REVISION, "unrecognized_branch"], False),
        ([REQUIRED_SCHEMA_REVISION, REQUIRED_SCHEMA_REVISION], False),
        (None, False),
    ],
)
def test_readiness_rejects_missing_old_future_branched_and_failed_schema(
    revisions, ready
):
    session = MagicMock()
    if revisions is None:
        session.execute.side_effect = SQLAlchemyError("synthetic unavailable DB")
    else:
        session.execute.return_value.scalars.return_value.all.return_value = revisions
    app = create_app(
        Settings(
            database_url="postgresql://localhost/not_used",
            secret_key=secrets.token_urlsafe(48),
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == (200 if ready else 503)
    assert response.json()["database"] is ready
    assert "synthetic unavailable" not in response.text
