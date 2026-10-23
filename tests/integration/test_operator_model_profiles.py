"""Real PostgreSQL API admission, without any model/network/worker execution.

Collected locally; execution requires the existing real migrated PostgreSQL test
fixture. No SQLite, model double, provider credential, or SDK construction can
stand in for these application transactions. Hosted wire/worker acceptance lives
separately in tests/system/test_operator_model_pipeline.py.
"""

import copy
import json
from uuid import uuid4

import pytest
from test_research_lifecycle import research_payload as research_payload  # noqa: PLC0414 - re-export a shared pytest fixture

pytestmark = pytest.mark.postgres


@pytest.fixture
def operator_settings(settings, owner, other, monkeypatch):
    from backend.app.core.config import Settings

    profiles = []
    for name, account in (("owner-model", owner), ("other-model", other)):
        secret_env = "TRADEGRAD_UNSET_" + uuid4().hex.upper()
        monkeypatch.delenv(secret_env, raising=False)
        profiles.append(
            {
                "id": name,
                "label": name,
                "provider": "openai-compatible",
                "model": "operator-pinned-model",
                "base_url": "https://operator-models.example.com/v1",
                "secret_env": secret_env,
                "owner_ids": [str(account.user.id)],
                "temperature": 0,
                "max_output_tokens": 4096,
                "request_timeout_seconds": 4,
            }
        )
    return Settings.model_validate(
        {
            **settings.model_dump(),
            "model_profiles_enabled": True,
            "model_profiles": profiles,
        }
    )


@pytest.fixture
def api(migrated_engine, operator_settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    with TestClient(
        create_app(settings=operator_settings, engine=migrated_engine)
    ) as client:
        yield client


def assert_public(response, settings):
    # Never allow pytest to print an entire response/settings object on failure.
    raw = response.content
    forbidden = [
        value.encode()
        for profile in settings.model_profiles
        for value in (profile.base_url, profile.secret_env)
    ]
    if any(value in raw for value in forbidden):
        pytest.fail(
            "API disclosed an operator endpoint or credential reference", pytrace=False
        )


def selected_payload(research_payload, settings, index=0):
    payload = copy.deepcopy(research_payload)
    payload["model"] = (
        settings.model_profiles[index].public_config().model_dump(mode="json")
    )
    return payload


def submit(api, headers, payload):
    return api.post(
        "/api/v1/research-runs",
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json=payload,
    )


def test_capabilities_owner_filter_default_and_no_api_credentials(
    api, owner_headers, other_headers, operator_settings
):
    for headers, expected in (
        (owner_headers, "owner-model"),
        (other_headers, "other-model"),
    ):
        response = api.get("/api/v1/research-capabilities", headers=headers)
        assert response.status_code == 200
        assert_public(response, operator_settings)
        profiles = response.json()["model_profiles"]
        assert [row["id"] for row in profiles] == ["fixture", expected]
        assert profiles[0]["config"]["provider"] == "fixture"
        assert profiles[1]["config"]["credential_ref"] is None
        assert profiles[1]["config"]["profile_id"] == expected
        assert len(profiles[1]["config"]["profile_sha256"]) == 64
        assert "owner_ids" not in json.dumps(profiles)
    response = api.get("/openapi.json")
    assert response.status_code == 200
    assert_public(response, operator_settings)


def test_enabled_operator_profile_submits_without_sdk_or_secret_in_api(
    api, owner_headers, research_payload, operator_settings, migrated_engine
):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from backend.app.models import OutboxEvent, ResearchRun

    payload = selected_payload(research_payload, operator_settings)
    response = submit(api, owner_headers, payload)
    assert response.status_code == 202
    assert_public(response, operator_settings)
    assert response.json()["config"]["model"] == payload["model"]
    run_id = response.json()["id"]
    response = api.get(f"/api/v1/research-runs/{run_id}", headers=owner_headers)
    assert response.status_code == 200
    assert_public(response, operator_settings)
    assert response.json()["config"] == payload
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_sha256", "0" * 64),
        ("model", "different-model"),
        ("temperature", 0.5),
        ("max_output_tokens", 123),
        ("profile_id", "missing-model"),
    ],
)
def test_profile_identity_parameter_tampering_is_transactionally_rejected(
    api,
    owner_headers,
    research_payload,
    operator_settings,
    migrated_engine,
    field,
    value,
):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from backend.app.models import OutboxEvent, ResearchRun

    payload = selected_payload(research_payload, operator_settings)
    payload["model"][field] = value
    response = submit(api, owner_headers, payload)
    assert response.status_code == 422
    assert_public(response, operator_settings)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_foreign_and_unknown_profile_are_indistinguishable(
    api, owner_headers, research_payload, operator_settings
):
    foreign = selected_payload(research_payload, operator_settings, index=1)
    unknown = copy.deepcopy(foreign)
    unknown["model"]["profile_id"] = "unknown-profile"
    responses = [submit(api, owner_headers, body) for body in (foreign, unknown)]
    for response in responses:
        assert response.status_code == 422
        assert_public(response, operator_settings)
    bodies = [response.json() for response in responses]
    for body in bodies:
        body.pop("correlation_id")
    assert bodies[0] == bodies[1]


def test_operator_disable_restores_fixture_only_and_rejects_old_selection(
    api, owner_headers, research_payload, operator_settings
):
    from backend.app.core.config import Settings

    disabled = Settings.model_validate(
        {**operator_settings.model_dump(), "model_profiles_enabled": False}
    )
    api.app.state.settings = disabled
    response = api.get("/api/v1/research-capabilities", headers=owner_headers)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["model_profiles"]] == ["fixture"]
    response = submit(
        api, owner_headers, selected_payload(research_payload, operator_settings)
    )
    assert response.status_code == 422
    assert_public(response, operator_settings)
    response = submit(api, owner_headers, research_payload)
    assert response.status_code == 202
    assert response.json()["config"]["model"]["provider"] == "fixture"
