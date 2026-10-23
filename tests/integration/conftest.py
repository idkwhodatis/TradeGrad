"""PostgreSQL-only test infrastructure for the M1 application boundary.

Every test gets a fresh schema and real Alembic migrations.  A missing test URL
is an explicit skip; an invalid/unreachable configured server is a failure.
There is deliberately no SQLite, mocked-session, or metadata.create_all fallback.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]


@dataclass(repr=False)
class SyntheticAccount:
    """Generated fixture credentials must not enter pytest argument diagnostics."""

    user: object
    password: str
    dataset: object
    strategy: object

    def __repr__(self) -> str:
        return "SyntheticAccount(<private fixture>)"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "postgres: requires a real PostgreSQL TEST_DATABASE_URL"
    )


@pytest.fixture(scope="session")
def postgres_url() -> URL:
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "TEST_DATABASE_URL is unset; real PostgreSQL integration was not run"
        )
    try:
        url = make_url(value)
    except (ArgumentError, ValueError):
        pytest.fail("TEST_DATABASE_URL is not a valid PostgreSQL URL", pytrace=False)
    if url.get_backend_name() != "postgresql":
        pytest.fail(
            "TEST_DATABASE_URL must use PostgreSQL; SQLite is not integration evidence",
            pytrace=False,
        )
    return url.set(drivername="postgresql+psycopg")


@pytest.fixture
def postgres_engine(postgres_url: URL) -> Iterator[Engine]:
    """Isolate parallel tests without touching any pre-existing schema."""
    schema = f"tradegrad_test_{uuid4().hex}"
    admin = create_engine(
        postgres_url,
        poolclass=NullPool,
        connect_args={"connect_timeout": 5},
    )
    engine = None
    created = False
    try:
        # Both the identifier and its prefix are generated here, never user input.
        with admin.begin() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
            assert "PostgreSQL" in version
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        engine = create_engine(
            postgres_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 5,
                "options": f"-csearch_path={schema} -cstatement_timeout=10000",
            },
        )
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT current_schema()")).scalar_one()
                == schema
            )
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def migrated_engine(postgres_engine: Engine) -> Engine:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    with postgres_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return postgres_engine


@pytest.fixture
def settings(migrated_engine: Engine, tmp_path: Path):
    from backend.app.core.atomic_storage import provision_directory
    from backend.app.core.config import Settings

    provision_directory(tmp_path / "storage", durable_parent=tmp_path)
    return Settings(
        database_url=migrated_engine.url.render_as_string(hide_password=False),
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path / "storage",
        lease_seconds=10,
        heartbeat_seconds=1,
        sse_poll_seconds=0.1,
    )


@pytest.fixture
def api(migrated_engine: Engine, settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    with TestClient(create_app(settings=settings, engine=migrated_engine)) as client:
        yield client


@pytest.fixture
def make_account(migrated_engine: Engine):
    """Provision actual persisted users; authentication still goes through HTTP."""
    from datetime import date
    from hashlib import sha256

    from sqlalchemy.orm import Session

    from backend.app.core.security import get_password_hash
    from backend.app.models import DatasetVersion, StrategyVersion, User

    def factory(label: str):
        password = secrets.token_urlsafe(32)
        user = User(
            email=f"{label}-{uuid4().hex}@example.com",
            hashed_password=get_password_hash(password),
            full_name=label,
        )
        with Session(migrated_engine, expire_on_commit=False) as session:
            session.add(user)
            session.flush()
            dataset = DatasetVersion(
                owner_id=user.id,
                name=f"{label} synthetic CN fixture",
                market="CN",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 4, 1),
                source="synthetic_fixture",
                is_synthetic=True,
                content_sha256=sha256(f"fixture-{label}".encode()).hexdigest(),
                manifest={"synthetic": True, "schema_version": 1},
                storage_key=f"datasets/{uuid4()}",
            )
            code = "# Integration fixture; never executed as an evaluator candidate.\n"
            strategy = StrategyVersion(
                owner_id=user.id,
                name=f"{label} synthetic seed",
                task="cn_time",
                mode="native_akquant",
                content_sha256=sha256(code.encode()).hexdigest(),
                code=code,
            )
            session.add_all([dataset, strategy])
            session.commit()
        return SyntheticAccount(
            user=user,
            password=password,
            dataset=dataset,
            strategy=strategy,
        )

    return factory


@pytest.fixture
def owner(make_account):
    return make_account("owner")


@pytest.fixture
def other(make_account):
    return make_account("other")


@pytest.fixture
def authorize(api):
    def login(account):
        response = api.post(
            "/api/v1/login/access-token",
            data={"username": account.user.email, "password": account.password},
        )
        if response.status_code != 200:
            pytest.fail("Generated test account could not authenticate", pytrace=False)
        token = response.json()
        if token.get("token_type") != "bearer":
            pytest.fail(
                "Authentication response has an invalid token type", pytrace=False
            )
        return {"Authorization": f"Bearer {token['access_token']}"}

    return login


@pytest.fixture
def owner_headers(authorize, owner):
    return authorize(owner)


@pytest.fixture
def other_headers(authorize, other):
    return authorize(other)


@pytest.fixture
def run_payload(owner):
    return {
        "task": "cn_time",
        "dataset_version_id": str(owner.dataset.id),
        "strategy_version_id": str(owner.strategy.id),
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "profile": "corrected_explicit_config",
    }


@pytest.fixture
def submit_run(api, owner_headers, run_payload):
    def submit(
        *,
        key: str | None = None,
        payload: dict | None = None,
        headers: dict | None = None,
    ):
        return api.post(
            "/api/v1/runs",
            headers={
                **(headers or owner_headers),
                "Idempotency-Key": key or uuid4().hex,
            },
            json=payload or run_payload,
        )

    return submit


@pytest.fixture
def queued_run(submit_run):
    response = submit_run()
    assert response.status_code in (201, 202), response.text
    return response.json()


@pytest.fixture
def claimed_attempt(migrated_engine: Engine, settings, queued_run):
    from uuid import UUID

    from sqlalchemy.orm import Session

    from backend.app.services import claim_attempt

    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session,
            UUID(queued_run["id"]),
            queued_run["generation"],
            "test-worker",
            settings,
        )
        assert attempt is not None
        session.commit()
        return attempt
