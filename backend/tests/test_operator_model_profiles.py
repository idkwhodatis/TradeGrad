"""Pure admission/identity checks. No SDK, secret resolution or external services."""

import json
import secrets
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.research_services import validate_model_selection
from backend.app.research_views import research_capability_metadata
from backend.app.services import DomainError
from packages.tradegrad_adapter.config import ModelConfig
from packages.tradegrad_adapter.model_profiles import (
    ModelProfileError,
    OperatorModelProfile,
    available_profiles,
    resolve_profile,
)


def profile(owner, **updates):
    return OperatorModelProfile(
        **{
            "id": "research-a",
            "label": "Reviewed research model",
            "model": "model-a",
            "base_url": "https://models.example.com/v1",
            "secret_env": "OPERATOR_MODEL_KEY",
            "owner_ids": [owner],
            **updates,
        }
    )


def settings(**updates):
    return Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(48),
        **updates,
    )


def test_fixture_only_default_never_reads_model_secret(monkeypatch):
    owner = uuid4()
    candidate = profile(owner)
    monkeypatch.setenv(candidate.secret_env, secrets.token_urlsafe(32))
    config = settings(model_profiles=[candidate])
    assert available_profiles(config, owner) == available_profiles(settings(), owner)
    assert resolve_profile(ModelConfig(), config, owner) is None
    with pytest.raises(ModelProfileError, match="model_profile_unavailable"):
        resolve_profile(candidate.public_config(), config, owner)


def test_owner_allowlist_and_public_capabilities_do_not_resolve_or_expose_secrets(
    monkeypatch,
):
    owner, other = uuid4(), uuid4()
    candidate = profile(owner)
    monkeypatch.delenv(candidate.secret_env, raising=False)
    config = settings(model_profiles_enabled=True, model_profiles=[candidate])
    capability = research_capability_metadata(config, owner)
    assert len(capability.model_profiles) == 2
    assert capability.live_model_calls is True
    assert capability.defaults.model.provider == "fixture"
    assert capability.model_profiles[1].config == candidate.public_config()
    rendered = capability.model_dump_json()
    for private in (candidate.base_url, candidate.secret_env, str(owner), "owner_ids"):
        assert private not in rendered
    assert len(research_capability_metadata(config, other).model_profiles) == 1
    with pytest.raises(DomainError) as error:
        validate_model_selection(candidate.public_config(), config, other)
    assert error.value.code == "model_profile_unavailable"


@pytest.mark.parametrize(
    "update",
    [
        {"base_url": "https://other.example.com/v1"},
        {"secret_env": "OTHER_MODEL_KEY"},
        {"max_response_bytes": 2048},
        {"request_timeout_seconds": 2},
        {"temperature": 1},
        {"model": "model-b"},
        {"max_output_tokens": 1024},
        {"owner_ids": [uuid4()]},
        {"label": "Changed operator profile"},
    ],
)
def test_changed_profile_rejected_for_same_frozen_run_or_recovery(update):
    owner = uuid4()
    original = profile(owner)
    changed = profile(owner, **update)
    assert changed.sha256 != original.sha256
    with pytest.raises(ModelProfileError):
        resolve_profile(
            original.public_config(),
            settings(
                model_profiles_enabled=True,
                model_profiles=[changed],
            ),
            owner,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "openai"},
        {"temperature": 0.5},
        {"model": "unlisted"},
        {"profile_sha256": "0" * 64},
        {"max_output_tokens": 2048},
        {"profile_id": "unknown"},
    ],
)
def test_browser_cannot_change_any_allowlisted_model_parameter(change):
    owner = uuid4()
    candidate = profile(owner)
    values = candidate.public_config().model_dump(mode="json") | change
    with pytest.raises(ModelProfileError):
        resolve_profile(
            ModelConfig.model_validate(values),
            settings(
                model_profiles_enabled=True,
                model_profiles=[candidate],
            ),
            owner,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"credential_ref": "OPERATOR_MODEL_KEY"},
        {"secret_env": "OPERATOR_MODEL_KEY"},
        {"base_url": "https://unlisted.example.com/v1"},
        {"api_key": None},
        {"profile_id": "https://unlisted.example.com"},
    ],
)
def test_public_request_never_names_secret_or_endpoint(change):
    candidate = profile(uuid4())
    if "api_key" in change:
        change = {"api_key": secrets.token_urlsafe(32)}
    with pytest.raises(ValidationError):
        ModelConfig.model_validate(candidate.public_config().model_dump() | change)


@pytest.mark.parametrize(
    "case",
    [
        "http",
        "userinfo",
        "query",
        "fragment",
        "loopback",
        "metadata",
        "localhost",
        "internal",
        "port",
        "path",
        "encoded",
    ],
)
def test_production_endpoint_fails_closed(case):
    material = secrets.token_urlsafe(32)
    urls = {
        "http": "http://models.example.com/v1",
        "userinfo": f"https://user:{material}@models.example.com/v1",
        "query": f"https://models.example.com/v1?api_key={material}",
        "fragment": f"https://models.example.com/v1#{material}",
        "loopback": "https://127.0.0.1/v1",
        "metadata": "https://169.254.169.254/v1",
        "localhost": "https://localhost/v1",
        "internal": "https://models.internal/v1",
        "port": "https://models.example.com:8443/v1",
        "path": "https://models.example.com/../../v1",
        "encoded": "https://models.example.com/v1%3fkey",
    }
    with pytest.raises(ValidationError):
        profile(uuid4(), base_url=urls[case])


def test_registry_duplicates_and_synthetic_insecure_fallback_are_rejected(monkeypatch):
    owner = uuid4()
    candidate = profile(owner)
    with pytest.raises(ValidationError):
        settings(model_profiles=[candidate, candidate])
    synthetic = profile(owner, synthetic=True, base_url="http://127.0.0.1:12345/v1")
    for name in ("GITHUB_ACTIONS", "TRADEGRAD_REQUIRE_MODEL_INTEGRATION"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        settings(
            model_profiles=[synthetic],
            model_synthetic_test_mode=True,
            data_provider_mode="fixture",
        )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("TRADEGRAD_REQUIRE_MODEL_INTEGRATION", "1")
    config = settings(
        model_profiles_enabled=True,
        model_profiles=[synthetic],
        model_synthetic_test_mode=True,
        data_provider_mode="fixture",
    )
    assert resolve_profile(synthetic.public_config(), config, owner) == synthetic
    assert not research_capability_metadata(config, owner).live_model_calls
    monkeypatch.delenv("TRADEGRAD_REQUIRE_MODEL_INTEGRATION")
    with pytest.raises(ModelProfileError):
        resolve_profile(synthetic.public_config(), config, owner)


def test_operator_repr_validation_errors_hide_input_values():
    candidate = profile(uuid4())
    for text in (repr(candidate), repr(settings(model_profiles=[candidate]))):
        assert candidate.base_url not in text and candidate.secret_env not in text
    values = candidate.model_dump(mode="json") | {
        "secret_env": secrets.token_urlsafe(32)
    }
    with pytest.raises(ValidationError) as error:
        OperatorModelProfile.model_validate(values)
    if values["secret_env"] in str(error.value):
        pytest.fail("Configuration diagnostic leaked generated material", pytrace=False)
    assert "input_value" not in str(error.value)
    assert (
        json.loads(candidate.public_config().model_dump_json())["credential_ref"]
        is None
    )
