"""Prepared real-PostgreSQL management and admission tests; collect-only locally.

Uses fresh migrated schemas and generated disposable identities. No provider,
SDK, Docker, candidate or worker execution is involved.
"""

import copy
import secrets
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import ResearchRun, User
from backend.app.services import DomainError
from backend.app.workspace_policy import enforce_model_policy
from packages.tradegrad_adapter.model_profiles import OperatorModelProfile
from tests.integration.test_research_lifecycle import (
    research_payload as research_payload,  # noqa: PLC0414 - shared fixture
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def admin_headers(owner, migrated_engine, owner_headers):
    with Session(migrated_engine) as session:
        session.get(User, owner.user.id).is_superuser = True
        session.commit()
    return owner_headers


def test_nonadmin_access_denial_and_self_update_cannot_escalate(
    api, other, other_headers
):
    assert api.get("/api/v1/settings/users", headers=other_headers).status_code == 403
    assert (
        api.post(
            "/api/v1/settings/profile",
            headers=other_headers,
            json={"full_name": "safe", "is_superuser": True},
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/api/v1/settings/users/{other.user.id}/quota",
            headers=other_headers,
            json={"max_active_runs": 1},
        ).status_code
        == 403
    )
    saved = api.post(
        "/api/v1/settings/profile", headers=other_headers, json={"full_name": "safe"}
    )
    assert saved.status_code == 200 and not saved.json()["is_superuser"]
    assert (
        api.get("/api/v1/users/me", headers=other_headers).json()["full_name"] == "safe"
    )


def test_create_update_deactivate_and_no_password_response(
    api, admin_headers, other, other_headers
):
    email = f"created-{uuid4().hex}@example.com"
    password = secrets.token_urlsafe(32)
    body = {"email": email, "password": password, "full_name": "Disposable"}
    response = api.post("/api/v1/settings/users", headers=admin_headers, json=body)
    assert response.status_code == 201
    assert password not in response.text and "hashed_password" not in response.text
    assert (
        api.post("/api/v1/settings/users", headers=admin_headers, json=body).status_code
        == 409
    )
    changed = api.post(
        f"/api/v1/settings/users/{other.user.id}",
        headers=admin_headers,
        json={"full_name": "Disabled", "is_active": False, "is_superuser": False},
    )
    assert changed.status_code == 200
    assert api.get("/api/v1/users/me", headers=other_headers).status_code == 401


def test_last_active_admin_is_protected_in_concurrent_demotions(
    api, admin_headers, other, other_headers, migrated_engine, owner
):
    with Session(migrated_engine) as session:
        session.get(User, other.user.id).is_superuser = True
        session.commit()
    barrier = Barrier(2)

    def demote(pair):
        account, headers = pair
        barrier.wait(timeout=10)
        return api.post(
            f"/api/v1/settings/users/{account.user.id}",
            headers=headers,
            json={"full_name": "Still here", "is_active": True, "is_superuser": False},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(demote, [(owner, admin_headers), (other, other_headers)])
        )
    assert sorted(statuses) == [200, 409]
    with Session(migrated_engine) as session:
        assert (
            len(
                session.scalars(
                    select(User).where(
                        User.is_active.is_(True), User.is_superuser.is_(True)
                    )
                ).all()
            )
            == 1
        )


def test_defaults_persist_without_rewriting_historical_config(
    api, admin_headers, submit_run, migrated_engine
):
    admitted = submit_run(key="settings-history")
    assert admitted.status_code == 202
    run_id = UUID(admitted.json()["id"])
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, run_id)
        original = (copy.deepcopy(run.config), run.request_sha256)
    defaults = api.get("/api/v1/settings/defaults", headers=admin_headers).json()
    defaults.update(initial_cash=123456, max_iterations=3)
    assert (
        api.post(
            "/api/v1/settings/defaults", headers=admin_headers, json=defaults
        ).status_code
        == 200
    )
    assert (
        api.get("/api/v1/settings/defaults", headers=admin_headers).json() == defaults
    )
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, run_id)
        assert (run.config, run.request_sha256) == original


def test_persisted_quota_serializes_native_and_seed_admissions(
    api,
    admin_headers,
    owner,
    owner_headers,
    run_payload,
    research_payload,
    migrated_engine,
):
    saved = api.post(
        f"/api/v1/settings/users/{owner.user.id}/quota",
        headers=admin_headers,
        json={"max_active_runs": 1},
    )
    assert saved.status_code == 200
    barrier = Barrier(2)

    def submit(item):
        path, body = item
        barrier.wait(timeout=10)
        return api.post(
            "/api/v1" + path,
            headers={**owner_headers, "Idempotency-Key": uuid4().hex},
            json=body,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                submit, [("/runs", run_payload), ("/research-runs", research_payload)]
            )
        )
    assert sorted(r.status_code for r in responses) == [202, 429]
    assert (
        next(r for r in responses if r.status_code == 429).json()["code"] == "run_quota"
    )
    # Both final modes use the same owner-wide pool as native and portable runs.
    from backend.app.final_evaluation_services import _quota as native_final_quota
    from backend.app.portable_final_services import _quota as portable_final_quota

    with Session(migrated_engine) as session:
        settings = api.app.state.settings
        assert native_final_quota(session, owner.user.id, settings) == 0
        assert portable_final_quota(session, owner.user.id, settings) == 0


def test_zero_import_quota_blocks_data_admission_and_can_inherit_again(
    api, admin_headers, owner, owner_headers
):
    path = f"/api/v1/settings/users/{owner.user.id}/quota"
    assert (
        api.post(
            path, headers=admin_headers, json={"max_active_imports": 0}
        ).status_code
        == 200
    )
    body = {
        "name": "Disposable",
        "provider": "yfinance",
        "market": "US",
        "symbols": ["AAPL"],
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "price_mode": "provider_native",
    }
    denied = api.post(
        "/api/v1/data/imports",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert denied.status_code == 429 and denied.json()["code"] == "import_quota"
    assert (
        api.post(path, headers=admin_headers, json={"max_active_imports": None}).json()[
            "effective_active_imports"
        ]
        > 0
    )


def test_reference_restriction_never_grants_owner_or_changes_profile_hash(
    api,
    settings,
    owner,
    other,
    admin_headers,
    owner_headers,
    research_payload,
    migrated_engine,
):
    profile = OperatorModelProfile(
        id="trusted",
        label="Trusted model",
        model="model-a",
        base_url="https://models.example.com/v1",
        secret_env="UNSET_DISPOSABLE_MODEL_ENV",
        owner_ids=[owner.user.id],
    )
    settings.model_profiles_enabled, settings.model_profiles = True, (profile,)
    path = f"/api/v1/settings/users/{owner.user.id}/secret-references/trusted"
    disabled = api.post(path, headers=admin_headers, json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()[0]["enabled"] is False
    assert (
        profile.base_url not in disabled.text
        and profile.secret_env not in disabled.text
    )
    assert [
        p["id"]
        for p in api.get("/api/v1/research-capabilities", headers=owner_headers).json()[
            "model_profiles"
        ]
    ] == ["fixture"]
    body = copy.deepcopy(research_payload)
    body["model"] = profile.public_config().model_dump(mode="json")
    denied = api.post(
        "/api/v1/research-runs",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert (
        denied.status_code == 422
        and denied.json()["code"] == "model_profile_unavailable"
    )
    with Session(migrated_engine) as session, pytest.raises(DomainError):
        enforce_model_policy(session, owner.user.id, profile.public_config())
    assert (
        api.post(
            f"/api/v1/settings/users/{other.user.id}/secret-references/trusted",
            headers=admin_headers,
            json={"enabled": True},
        ).status_code
        == 404
    )
    assert (
        api.post(
            path, headers=admin_headers, json={"enabled": True, "secret_env": "NEW_ENV"}
        ).status_code
        == 422
    )
    assert (
        api.post(path, headers=admin_headers, json={"enabled": True}).status_code == 200
    )
    profiles = api.get("/api/v1/research-capabilities", headers=owner_headers).json()[
        "model_profiles"
    ]
    assert (
        next(p for p in profiles if p["id"] == "trusted")["config"]["profile_sha256"]
        == profile.sha256
    )

    admitted = api.post(
        "/api/v1/research-runs",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert admitted.status_code == 202
    run_id = admitted.json()["id"]
    assert (
        api.post(
            f"/api/v1/research-runs/{run_id}/cancel", headers=owner_headers
        ).status_code
        == 202
    )
    assert (
        api.post(path, headers=admin_headers, json={"enabled": False}).status_code
        == 200
    )
    resumed = api.post(f"/api/v1/research-runs/{run_id}/resume", headers=owner_headers)
    assert resumed.status_code == 422
    assert resumed.json()["code"] == "model_profile_unavailable"


def test_reduced_quota_blocks_seed_resume_and_data_retry(
    api, admin_headers, owner, owner_headers, submit_run
):
    admitted = submit_run(key="quota-retry-seed")
    assert admitted.status_code == 202
    run_id = admitted.json()["id"]
    assert (
        api.post(f"/api/v1/runs/{run_id}/cancel", headers=owner_headers).status_code
        == 202
    )
    body = {
        "name": "Disposable retry",
        "provider": "yfinance",
        "market": "US",
        "symbols": ["AAPL"],
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "price_mode": "provider_native",
    }
    imported = api.post(
        "/api/v1/data/imports",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert imported.status_code == 202
    import_id = imported.json()["id"]
    assert (
        api.post(
            f"/api/v1/data/imports/{import_id}/cancel", headers=owner_headers
        ).status_code
        == 202
    )
    assert (
        api.post(
            f"/api/v1/settings/users/{owner.user.id}/quota",
            headers=admin_headers,
            json={"max_active_runs": 0, "max_active_imports": 0},
        ).status_code
        == 200
    )
    assert (
        api.post(f"/api/v1/runs/{run_id}/resume", headers=owner_headers).status_code
        == 429
    )
    assert (
        api.post(
            f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers
        ).status_code
        == 429
    )


def test_workspace_schema_round_trip_never_rewrites_immutable_runs(
    migrated_engine, submit_run
):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    admitted = submit_run(key="settings-upgrade-history")
    assert admitted.status_code == 202
    run_id = UUID(admitted.json()["id"])
    with Session(migrated_engine) as session:
        before = session.execute(
            text("SELECT config::text, request_sha256 FROM research_runs WHERE id=:id"),
            {"id": run_id},
        ).one()
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "backend/alembic.ini"))
    config.set_main_option("script_location", str(root / "backend/alembic"))
    with migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "004_m5")
        command.upgrade(config, "head")
        after = connection.execute(
            text("SELECT config::text, request_sha256 FROM research_runs WHERE id=:id"),
            {"id": run_id},
        ).one()
        assert tuple(before) == tuple(after)


def test_status_never_invents_backup_receipt(api, owner_headers):
    result = api.get("/api/v1/settings/status", headers=owner_headers)
    assert result.status_code == 200
    assert result.json()["schema_revision"] == "006_provider_connections"
    assert result.json()["backup_status"] == "restricted"
    assert result.json()["backup_evidence"] == []
    assert "administrators" in result.json()["backup_note"]
