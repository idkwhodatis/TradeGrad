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


@pytest.mark.parametrize("revision", [None, "synthetic-daily.v1"])
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
