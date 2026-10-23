"""Inert model admission and real TradeTGD/fake-boundary control tests.

Never constructs an SDK provider, launches a process, calls HTTP, imports a policy,
opens PostgreSQL or runs Docker. Hosted integration is a separate unrun gate.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_config import config_values
from test_research_pipeline import controller as controller  # noqa: PLC0414

from backend.app import portable_services as services
from backend.app import workspace_policy
from backend.app.core.config import Settings
from backend.app.services import DomainError
from packages.tradegrad_adapter.model_profiles import (
    OperatorModelProfile,
    available_profiles,
)
from packages.tradegrad_adapter.portable_config import PortableRunConfig


def profile(owner):
    return OperatorModelProfile(
        id="portable-model",
        label="Pinned operator model",
        model="reviewed-model",
        base_url="https://models.example.com/v1",
        secret_env="PORTABLE_UNUSED_KEY",
        owner_ids=(owner,),
    )


def enabled_settings(owner):
    import secrets

    return Settings(
        database_url="postgresql://unused/no_connection",
        secret_key=secrets.token_urlsafe(48),
        model_profiles_enabled=True,
        model_profiles=(profile(owner),),
    )


def test_exact_model_config_is_identity_not_deployment_authority(monkeypatch):
    owner = uuid4()
    settings = enabled_settings(owner)
    model = settings.model_profiles[0].public_config()
    config = PortableRunConfig.model_validate(config_values() | {"model": model})
    assert config.model == model
    assert (
        PortableRunConfig.from_verified_json(config.canonical_bytes(), config.sha256)
        == config
    )
    assert available_profiles(settings, owner)[1]["strategy_modes"] == [
        "native_akquant",
        "portable",
    ]
    # A previously stored fixture remains the original four-field wire contract.
    fixture = PortableRunConfig.model_validate(config_values())
    assert set(fixture.model.model_dump()) == {
        "provider",
        "model",
        "credential_ref",
        "temperature",
    }
    monkeypatch.setattr(services, "portable_config", lambda _: config)


@pytest.mark.parametrize(
    "case", ["disabled", "foreign", "changed", "workspace_disabled"]
)
def test_api_rejects_unavailable_profile_before_reading_market_or_policy(
    monkeypatch, case
):
    from packages.tradegrad_adapter.portable_environment import (
        portable_environment_digest,
    )

    owner = uuid4()
    settings = enabled_settings(owner)
    model = settings.model_profiles[0].public_config()
    if case == "disabled":
        settings = settings.model_copy(update={"model_profiles_enabled": False})
    if case == "foreign":
        owner = uuid4()
    if case == "changed":
        model = model.model_copy(update={"profile_sha256": "0" * 64})
    config = PortableRunConfig.model_validate(
        config_values()
        | {
            "environment_digest": portable_environment_digest(),
            "model": model,
        }
    )

    def enforce(*_):
        if case == "workspace_disabled":
            raise DomainError("model_profile_unavailable", "Disabled", 422)

    monkeypatch.setattr(services, "enforce_model_policy", enforce)

    def forbidden(*_):
        pytest.fail("Profile rejection reached dataset or strategy reads")

    monkeypatch.setattr(services, "_dataset_universe", forbidden)
    with pytest.raises(DomainError) as error:
        services.validate_portable_submission(None, owner, config, settings)
    assert error.value.code == (
        "model_profile_changed" if case == "changed" else "model_profile_unavailable"
    )


def test_resume_demands_durable_model_receipt_even_when_attempt_claims_stop(
    monkeypatch,
):
    from backend.app import model_containment

    owner, run_id, attempt_id = uuid4(), uuid4(), uuid4()
    settings = enabled_settings(owner)
    config = PortableRunConfig.model_validate(
        config_values() | {"model": profile(owner).public_config()}
    )
    run = SimpleNamespace(
        status="paused",
        id=run_id,
        active_attempt_id=attempt_id,
        config=config.model_dump(mode="json"),
    )
    session = SimpleNamespace(
        get=lambda *_: SimpleNamespace(sandbox_stopped=True), scalar=lambda *_: None
    )
    monkeypatch.setattr(services, "advisory_lock", lambda *_: None)
    monkeypatch.setattr(services, "owned_portable_run", lambda *_, **__: run)
    monkeypatch.setattr(services, "enforce_model_policy", lambda *_: None)
    monkeypatch.setattr(
        model_containment, "model_processes_confirmed_stopped", lambda *_: False
    )
    with pytest.raises(DomainError) as error:
        services.resume_portable_run(session, run_id, owner, settings)
    assert error.value.code == "model_process_not_stopped"


@pytest.mark.parametrize("close_state", [True, False, "raise"])
def test_portable_worker_reuses_factory_receipts_and_attempts_all_cleanup(
    controller, monkeypatch, close_state
):
    from backend.app import model_containment
    from backend.app import services as common
    from packages.tradegrad_adapter.model_calls import FrozenModel
    from workers.research import model_provider

    h = controller
    selected = profile(h.owner_id)
    h.settings.model_profiles_enabled = True
    h.settings.model_profiles = (selected,)
    h.run.config["model"] = selected.public_config().model_dump(mode="json")
    calls = []
    monkeypatch.setattr(
        workspace_policy, "enforce_model_policy", lambda *_: calls.append("policy")
    )
    monkeypatch.setattr(common, "advisory_lock", lambda *_: calls.append("lock"))
    monkeypatch.setattr(
        model_containment,
        "admit_model_process",
        lambda *args: calls.append(("admitted", args[-1])),
    )
    monkeypatch.setattr(
        model_containment,
        "confirm_model_process_stopped",
        lambda *args: calls.append(("stopped", args[-1])),
    )

    def factory(config, settings, owner, source, *, boundary):
        assert config.model == selected.public_config() and owner == h.owner_id
        calls.append("factory")

        class InertProvider(FrozenModel):
            provider, model = selected.provider, selected.model

            def complete(self, request):
                boundary("admitted", request.call_id)
                response = super().complete(request)
                boundary("stopped", request.call_id)
                return response

            def close(self):
                assert any(row[0] == "stop_attempt" for row in h.trace)
                calls.append("close")
                if close_state == "raise":
                    raise RuntimeError("Inert unknown close")
                return close_state

        return InertProvider([f"```python\n{source}\n```"] * 100)

    monkeypatch.setattr(model_provider, "create_model", factory)
    result = h.execute()
    assert result["status"] == (
        "succeeded" if close_state is True else "recovery_blocked"
    )
    assert result["sandbox_stopped"] is (close_state is True)
    assert calls[0:2] == ["policy", "factory"]
    assert calls[-1] == "close"
    receipts = [row for row in calls if isinstance(row, tuple)]
    assert receipts and len(receipts) % 2 == 0
    for index in range(0, len(receipts), 2):
        assert receipts[index][0] == "admitted" and receipts[index + 1] == (
            "stopped",
            receipts[index][1],
        )
    assert calls.count("lock") == len(receipts) // 2
    assert calls.count("policy") == 1 + len(receipts) // 2
    checkpoint_calls = h.checkpoints[-1]["model_calls"]
    assert checkpoint_calls and all(
        row["provider"] == selected.provider for row in checkpoint_calls
    )
    assert h.trace[-1][-1]["model_mode"] == "operator_profile"
