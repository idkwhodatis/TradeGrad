"""Offline API contract checks with explicit fake persistence, not PostgreSQL."""

import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from backend.app.api.deps import get_current_user, get_session
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.services import DomainError, owned_native_run


@pytest.fixture
def fake_api():
    session = MagicMock()
    user = SimpleNamespace(id=uuid4())
    app = create_app(
        Settings(
            database_url="postgresql://localhost/not_used",
            secret_key=secrets.token_urlsafe(48),
        )
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client, session, user


@pytest.mark.parametrize(
    "version",
    [
        "tradegrad.portable.run.v1",
        "tradegrad.final-evaluation.v1",
        "tradegrad.portable.final-evaluation.v1",
        "future.unknown.v1",
    ],
)
@pytest.mark.parametrize(
    "suffix,method", [("", "get"), ("/cancel", "post"), ("/resume", "post")]
)
def test_native_routes_do_not_serialize_or_mutate_other_run_types(
    fake_api, version, suffix, method
):
    client, session, user = fake_api
    identifier = uuid4()
    session.scalar.return_value = SimpleNamespace(
        id=identifier, owner_id=user.id, config={"schema_version": version}
    )
    response = getattr(client, method)(f"/api/v1/runs/{identifier}{suffix}")
    assert response.status_code == 404
    session.commit.assert_not_called()
    session.flush.assert_not_called()


@pytest.mark.parametrize("version", [None, "tradegrad.run.v1"])
def test_native_guard_preserves_both_existing_config_formats(version):
    session = MagicMock()
    item = SimpleNamespace(
        config={} if version is None else {"schema_version": version}
    )
    session.scalar.return_value = item
    assert owned_native_run(session, uuid4(), uuid4()) is item
    session.scalar.return_value = None
    with pytest.raises(DomainError) as error:
        owned_native_run(session, uuid4(), uuid4())
    assert error.value.status_code == 404


@pytest.mark.parametrize(
    "resource,expected",
    [("runs", "tradegrad.run.v1"), ("strategy-versions", "native_akquant")],
)
def test_native_lists_filter_before_pagination_and_count(fake_api, resource, expected):
    client, session, user = fake_api
    session.scalars.return_value.all.return_value = []
    session.scalar.return_value = 0
    response = client.get(f"/api/v1/{resource}")
    assert response.status_code == 200
    assert response.json() == {"data": [], "count": 0}
    for mock in (session.scalars, session.scalar):
        statement = mock.call_args.args[0]
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert expected in sql
        assert str(user.id) in sql


def test_native_strategy_detail_applies_mode_filter(fake_api):
    client, session, _ = fake_api
    session.scalar.return_value = None
    assert client.get(f"/api/v1/strategy-versions/{uuid4()}").status_code == 404
    sql = str(
        session.scalar.call_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "native_akquant" in sql


def test_readiness_requires_current_settings_schema(fake_api):
    client, session, _ = fake_api
    assert client.get("/api/v1/health/ready").status_code == 200
    statement = str(session.execute.call_args.args[0])
    assert "version_num = '006_provider_connections'" in statement


def test_readiness_fails_closed_on_stale_schema(fake_api):
    from sqlalchemy.exc import NoResultFound

    client, session, _ = fake_api
    session.execute.return_value.scalar_one.side_effect = NoResultFound()
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["database"] is False


@pytest.mark.parametrize("owned", [True, False])
def test_configured_research_detail_distinguishes_owned_incompatible_from_absent(
    fake_api, owned
):
    client, session, user = fake_api
    identifier = uuid4()
    session.scalar.return_value = (
        SimpleNamespace(
            id=identifier,
            owner_id=user.id,
            config={"schema_version": "tradegrad.portable.run.v1"},
        )
        if owned
        else None
    )
    response = client.get(f"/api/v1/research-runs/{identifier}")
    assert response.status_code == (409 if owned else 404)
    assert response.json()["code"] == (
        "invalid_research_config" if owned else "not_found"
    )
    session.commit.assert_not_called()
    session.flush.assert_not_called()


def test_portable_fixture_universe_payload_uses_public_interval_contract():
    from datetime import UTC, datetime

    from pydantic import ValidationError

    from backend.app.data_schemas import UniverseCreate

    member = {"listing_id": str(uuid4()), "valid_from": None, "valid_to": None}
    body = {
        "name": "Bounded synthetic fixture",
        "state": "user_fixed",
        "as_of": datetime.now(UTC).isoformat(),
        "members": [member],
        "evidence": [],
    }
    parsed = UniverseCreate.model_validate(body)
    assert parsed.members[0].listing_id == member["listing_id"]
    body["members"] = [
        {"listing_id": member["listing_id"], "member_from": None, "member_to": None}
    ]
    with pytest.raises(ValidationError):
        UniverseCreate.model_validate(body)
