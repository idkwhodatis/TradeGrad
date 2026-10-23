"""Pure configuration tests; never load or execute a policy template."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from packages.strategy_sdk import StateCodec, StateSchema, hash_json
from packages.tradegrad_adapter.config import RunConfig
from packages.tradegrad_adapter.portable_config import (
    PortableRunConfig,
    portable_evaluation_windows,
)
from runtimes.portable_engine.runtime import reviewed_profile


def config_values(market="US", kind="event"):
    return {
        "task": f"{market.lower()}_{'time' if kind == 'event' else 'cross'}",
        "strategy_kind": kind,
        "dataset": {"id": str(UUID(int=1)), "sha256": "a" * 64},
        "seed_strategy": {"id": str(UUID(int=2)), "sha256": "b" * 64},
        "seed_manifest": {"id": f"{UUID(int=2)}/manifest", "sha256": "c" * 64},
        "seed_parameters": {"id": f"{UUID(int=2)}/parameters", "sha256": "d" * 64},
        "universe": {"id": str(UUID(int=3)), "sha256": "e" * 64},
        "windows": {
            "train": {"start": "2023-12-01", "end": "2024-02-01"},
            "validation": {"start": "2024-02-01", "end": "2024-03-01"},
            "holdout": {"start": "2024-03-01", "end": "2024-04-01"},
        },
        "warmup": {"sessions": 2},
        "initial_state": StateCodec(StateSchema(name="test", version=1, fields=()))
        .encode({})
        .model_dump(mode="json"),
        "environment_digest": "f" * 64,
    }


@pytest.mark.parametrize("market", ["CN", "US"])
@pytest.mark.parametrize("kind", ["event", "portfolio"])
def test_distinct_portable_schema_preserves_exact_cash_long_only_profile(market, kind):
    config = PortableRunConfig.model_validate(config_values(market, kind))
    assert config.schema_version == "tradegrad.portable.run.v1"
    assert config.engine_profile.model_dump(mode="json") == reviewed_profile(
        market, config.initial_cash
    )
    assert config.engine_profile.risk_config.account_mode == "cash"
    assert config.engine_profile.risk_config.enable_short_sell is False
    assert (
        PortableRunConfig.from_verified_json(config.canonical_bytes(), config.sha256)
        == config
    )
    with pytest.raises(ValidationError):
        RunConfig.model_validate_json(config.canonical_bytes())
    with pytest.raises(ValidationError):
        config.engine_profile.risk_config.account_mode = "margin"


def test_holdout_never_admitted_and_yearly_train_exact():
    config = PortableRunConfig.model_validate(config_values())
    assert portable_evaluation_windows(config) == (
        {"split": "train", "start": "2023-12-01", "end": "2024-01-01"},
        {"split": "train", "start": "2024-01-01", "end": "2024-02-01"},
        {"split": "validation", "start": "2024-02-01", "end": "2024-03-01"},
    )
    forged = config.model_copy(update={"profile": "source_compat"})
    with pytest.raises(ValidationError):
        portable_evaluation_windows(forged)


@pytest.mark.parametrize(
    "edit",
    [
        {"profile": "source_compat"},
        {"strategy_kind": "portfolio"},
        {"warmup": {"sessions": 0}},
        {"optimizer": {"max_iterations": 1}},
        {"objective": {"definition_version": "source_reward.v1"}},
        {"prompt_version": "source_prompts.v1"},
        {
            "model": {
                "provider": "openai",
                "model": "gpt-test",
                "credential_ref": "server/ref",
            }
        },
    ],
)
def test_unsupported_native_or_unbound_options_fail(edit):
    with pytest.raises(ValidationError):
        PortableRunConfig.model_validate(config_values() | edit)


def test_forged_reviewed_profile_and_digest_and_duplicate_json_fail():
    raw = config_values()
    config = PortableRunConfig.model_validate(raw)
    changed = config.model_dump(mode="json")
    changed["engine_profile"]["commission_rate"] = 0
    with pytest.raises(ValidationError):
        PortableRunConfig.model_validate(changed)
    with pytest.raises(ValueError, match="digest"):
        PortableRunConfig.from_verified_json(config.canonical_bytes(), "0" * 64)
    duplicate = config.canonical_bytes()[:-1] + b',"task":"us_time"}'
    with pytest.raises(ValueError):
        PortableRunConfig.from_verified_json(duplicate, config.sha256)


def test_next_open_sell_profile_is_explicit_and_invalidates_v1_hash():
    config = PortableRunConfig.model_validate(config_values("CN"))
    profile = config.engine_profile.model_dump(mode="json")
    assert profile["profile_version"] == "akquant.portable.cash-daily.controller-t1.v3"
    assert profile["sell_budget"] == "decision_close_holdings_less_pending_sells"
    assert profile["sellability_observation"] == "next_open_unreserved_holdings"
    assert profile["unsettled_next_open_sell"] == "admit_after_close_with_controller_t1"
    assert profile["t_plus_one"] is False  # Explicit native T+0, not native T+1 parity.
    assert profile["engine_settlement_mode"] == "T+0"
    assert profile["application_t_plus_one"] is True
    assert (
        profile["application_settlement_enforcement"]
        == "trusted_controller_daily_next_open_inventory.v1"
    )
    legacy = dict(profile)
    legacy["profile_version"] = "akquant.portable.cash-daily.v1"
    for key in ("sell_budget", "sellability_observation", "unsettled_next_open_sell"):
        del legacy[key]
    assert hash_json(legacy) != config.engine_profile.sha256
    with pytest.raises(ValidationError):
        PortableRunConfig.model_validate(
            config.model_dump(mode="json") | {"engine_profile": legacy}
        )
