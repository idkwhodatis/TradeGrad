"""In-process HTTP middleware checks, with no database or network server."""

import secrets
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

from backend.app.core.config import Settings
from backend.app.main import create_app


@pytest.mark.parametrize(
    "route_headers,expected",
    [
        ([], "no-store"),
        (["no-store"], "no-store"),
        (["private, no-store"], "private, no-store"),
        (["private"], "private, no-store"),
        (["  PrIvAtE , max-age=3600"], "private, no-store"),
        (["public, max-age=3600, s-maxage=86400"], "no-store"),
        (["private, public, max-age=3600"], "private, no-store"),
        (["public, max-age=3600", "private"], "private, no-store"),
        (['private="Set-Cookie", max-age=3600'], "private, no-store"),
        (["x-private=true, max-age=3600"], "no-store"),
    ],
)
def test_middleware_enforces_no_store_and_preserves_private(route_headers, expected):
    engine = MagicMock()
    app = create_app(
        Settings(
            database_url="postgresql://localhost/not_used",
            secret_key=secrets.token_urlsafe(48),
        ),
        engine=engine,
    )

    @app.get("/cache-policy-test")
    async def cache_policy_test():
        response = Response("synthetic response")
        for value in route_headers:
            response.headers.append("Cache-Control", value)
        return response

    with TestClient(app) as client:
        response = client.get("/cache-policy-test")

    assert response.status_code == 200
    assert response.headers.get_list("cache-control") == [expected]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert str(UUID(response.headers["x-correlation-id"]))
    assert engine.mock_calls == []
