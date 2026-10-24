"""Pure SDK-free import-generation fence, not a substitute for PG recovery tests."""

import pytest

from packages.marketdata.contracts import (
    FIXTURE_VERSION,
    SCHEMA_VERSION,
    TRANSFORM_VERSION,
    execution_policy_compatible,
)


def policy(mode="fixture"):
    return {
        "schema_version": SCHEMA_VERSION,
        "transform_version": TRANSFORM_VERSION,
        "worker_contract": "data-import.v1",
        "provider_mode": mode,
        "block_days": 31,
        "fixture_version": FIXTURE_VERSION if mode == "fixture" else None,
    }


@pytest.mark.parametrize("mode", ["fixture", "live"])
def test_matching_policy_accepted(mode):
    assert execution_policy_compatible(policy(mode), mode)


@pytest.mark.parametrize("revision", [None, "synthetic-daily.v1", "synthetic-daily.v2"])
def test_old_fixture_prices_cannot_mix_with_new_checkpoints(revision):
    previous = {**policy(), "fixture_version": revision}
    assert not execution_policy_compatible(previous, "fixture")


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "future"),
        ("transform_version", "future"),
        ("worker_contract", "future"),
        ("provider_mode", "live"),
        ("block_days", True),
        ("block_days", 0),
        ("block_days", 367),
    ],
)
def test_incompatible_policy_rejected_before_provider_access(field, value):
    assert not execution_policy_compatible({**policy(), field: value}, "fixture")


def test_portable_fixture_has_independent_generation_fence():
    from packages.marketdata.contracts import PORTABLE_FIXTURE_VERSION

    current = {**policy(), "fixture_version": PORTABLE_FIXTURE_VERSION}
    assert execution_policy_compatible(
        current, "fixture", provider_name="portable_fixture"
    )
    assert not execution_policy_compatible(
        policy(), "fixture", provider_name="portable_fixture"
    )
    assert not execution_policy_compatible(current, "fixture", provider_name="yfinance")
    assert not execution_policy_compatible(current, "fixture")
    assert not execution_policy_compatible(
        policy("live"), "live", provider_name="portable_fixture"
    )


def test_new_probe_and_repair_contract_is_explicitly_admitted():
    assert execution_policy_compatible(
        {**policy("live"), "worker_contract": "data-import.v2"}, "live"
    )


def test_native_research_profile_has_a_separate_exact_generation_fence():
    from packages.marketdata.contracts import (
        NATIVE_RESEARCH_FIXTURE_PROFILE,
        NATIVE_RESEARCH_FIXTURE_VERSION,
        PORTABLE_FIXTURE_VERSION,
    )

    current = {
        **policy(),
        "fixture_profile": NATIVE_RESEARCH_FIXTURE_PROFILE,
        "fixture_version": NATIVE_RESEARCH_FIXTURE_VERSION,
    }
    assert execution_policy_compatible(
        current, "fixture", provider_name="portable_fixture"
    )
    for change in [
        {"fixture_version": PORTABLE_FIXTURE_VERSION},
        {"fixture_version": "controlled-cross.synthetic.future"},
        {"fixture_profile": None},
        {"fixture_profile": "unknown"},
    ]:
        assert not execution_policy_compatible(
            {**current, **change}, "fixture", provider_name="portable_fixture"
        )
    assert not execution_policy_compatible(current, "fixture", provider_name="yfinance")
    assert not execution_policy_compatible(
        current, "live", provider_name="portable_fixture"
    )
