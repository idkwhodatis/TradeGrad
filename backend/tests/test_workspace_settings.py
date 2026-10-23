"""Pure contract/policy tests. No PostgreSQL, browser, provider or candidate execution."""

import ast
import json
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.models import User, UserWorkspaceSettings
from backend.app.services import DomainError
from backend.app.settings_routes import profile_references
from backend.app.settings_schemas import (
    ManagedUserCreate,
    QuotaUpdate,
    SecretReferenceUpdate,
    SelfProfileUpdate,
    WorkspaceDefaults,
)
from backend.app.workspace_policy import (
    admitted_model_settings,
    enforce_model_policy,
    quota_limit,
)
from packages.tradegrad_adapter.model_profiles import (
    OperatorModelProfile,
    available_profiles,
)

ROOT = Path(__file__).resolve().parents[2]


class PolicyFixture:
    """Explicit policy-only fake; never advertised as persistence/integration evidence."""

    def __init__(self, user, record):
        self.user, self.record = user, record

    def get(self, model, key, **kwargs):
        return self.user if model is User else self.record


def config(**updates):
    return Settings(
        database_url="postgresql://localhost/not_used",
        secret_key=secrets.token_urlsafe(48),
        **updates,
    )


@pytest.mark.parametrize(
    "model, extra",
    [
        (SelfProfileUpdate, {"is_superuser": True}),
        (SelfProfileUpdate, {"is_active": True}),
        (SelfProfileUpdate, {"email": "other@example.com"}),
        (WorkspaceDefaults, {"secret_key": "not-a-key"}),
        (WorkspaceDefaults, {"base_url": "https://untrusted.example/v1"}),
        (SecretReferenceUpdate, {"enabled": True, "secret_env": "NEW_SECRET"}),
        (QuotaUpdate, {"max_active_runs": -1}),
        (QuotaUpdate, {"max_active_imports": 101}),
    ],
)
def test_management_input_rejects_escalation_untrusted_routing_and_invalid_limits(
    model, extra
):
    with pytest.raises(ValidationError):
        model.model_validate(extra)


def test_account_password_not_in_repr_and_name_validation():
    body = ManagedUserCreate(email="a@example.com", password="disposable-test-password")
    assert "disposable-test-password" not in repr(body)
    with pytest.raises(ValidationError):
        SelfProfileUpdate(full_name="unsafe\nname")
    assert SelfProfileUpdate(full_name="  Alice  ").full_name == "Alice"


@pytest.mark.parametrize(
    "override, deployment, expected", [(None, 4, 4), (0, 4, 0), (2, 4, 2), (8, 4, 4)]
)
def test_quota_never_expands_deployment_cap(override, deployment, expected):
    owner = uuid4()
    session = PolicyFixture(
        User(id=owner, is_active=True),
        UserWorkspaceSettings(owner_id=owner, max_active_runs=override),
    )
    assert (
        quota_limit(session, owner, config(max_active_runs_per_user=deployment))
        == expected
    )


def test_inactive_user_cannot_be_admitted_after_authentication_race():
    owner = uuid4()
    session = PolicyFixture(User(id=owner, is_active=False), None)
    with pytest.raises(DomainError, match="inactive"):
        quota_limit(session, owner, config())


def test_reference_toggle_only_narrows_allowlist_and_does_not_change_profile_identity():
    owner, other = uuid4(), uuid4()
    profile = OperatorModelProfile(
        id="trusted",
        label="Trusted model",
        model="model-a",
        base_url="https://models.example.com/v1",
        secret_env="PRIVATE_MODEL_ENV",
        owner_ids=[owner],
    )
    settings = config(model_profiles_enabled=True, model_profiles=[profile])
    record = UserWorkspaceSettings(owner_id=owner, disabled_model_profiles=["trusted"])
    session = PolicyFixture(User(id=owner, is_active=True), record)
    scoped = admitted_model_settings(session, owner, settings)
    assert [p["id"] for p in available_profiles(scoped, owner)] == ["fixture"]
    with pytest.raises(DomainError):
        enforce_model_policy(session, owner, profile.public_config())
    visible = profile_references(session, owner, settings)
    public_json = json.dumps([p.model_dump() for p in visible])
    assert "PRIVATE_MODEL_ENV" not in public_json and "https://" not in public_json
    assert not visible[0].enabled
    record.disabled_model_profiles = []
    scoped = admitted_model_settings(session, owner, settings)
    assert scoped.model_profiles[0].sha256 == profile.sha256
    assert [p["id"] for p in available_profiles(scoped, other)] == ["fixture"]
    enforce_model_policy(session, owner, profile.public_config())
    assert settings.model_profiles == (profile,)


def test_every_admission_and_retry_uses_persisted_quota_and_owner_lock():
    expected = {
        "services": ["submit_run", "resume_run"],
        "research_services": ["submit_research_run", "resume_research_run"],
        "portable_services": ["submit_portable_run", "resume_portable_run"],
        "final_evaluation_services": [
            "submit_final_evaluation",
            "retry_final_evaluation",
        ],
        "portable_final_services": [
            "submit_final_evaluation",
            "retry_final_evaluation",
        ],
        "data_services": ["submit_import", "retry_import"],
    }
    for module, functions in expected.items():
        source = (ROOT / "backend/app" / f"{module}.py").read_text()
        tree = ast.parse(source)
        for name in functions:
            fn = next(
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == name
            )
            segment = ast.get_source_segment(source, fn)
            assert "advisory_lock(session," in segment, (module, name)
            assert (
                "quota_limit(session, owner_id, settings" in segment
                or "_quota(session, owner_id, settings)" in segment
            ), (module, name)
        assert "settings.max_active_runs_per_user" not in source
        assert "settings.max_active_imports_per_user" not in source


def test_worker_preparation_and_model_admission_recheck_persisted_restriction():
    source = (ROOT / "workers/research/optimization.py").read_text()
    assert "enforce_model_policy(session, run.owner_id, config.model)" in source
    assert 'advisory_lock(session, f"submit:{model_owner_id}")' in source
    assert "enforce_model_policy(session, model_owner_id, config.model)" in source


def test_openapi_declares_management_but_no_secret_material_and_head_is_linear():
    schema = create_app(config()).openapi()
    for path in (
        "/settings/defaults",
        "/settings/profile",
        "/settings/users",
        "/settings/users/{user_id}/quota",
        "/settings/users/{user_id}/secret-references/{profile_id}",
    ):
        assert "/api/v1" + path in schema["paths"]
    references = schema["components"]["schemas"]["SecretReferencePublic"]["properties"]
    assert not {"secret_env", "base_url", "secret", "api_key"} & references.keys()
    migration = (ROOT / "backend/alembic/versions/005_settings.py").read_text()
    assert 'down_revision = "004_m5"' in migration
    assert "UPDATE research_runs" not in migration
