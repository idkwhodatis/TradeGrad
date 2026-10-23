"""Pure connection identity/binding checks. Actual PostgreSQL and worker gates are separate."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.data_schemas import (
    ImportCreate,
    ProviderConnectionPublic,
    ProviderConnectionUpdate,
)
from packages.marketdata.connections import (
    connection_hash,
    make_snapshot,
    validate_request_connection,
)
from packages.marketdata.probes import build_probe_receipt


def bound_request(owner, provider="akshare", **changes):
    pin = make_snapshot(owner, provider, 1, **changes).model_dump(mode="json")
    return {
        "provider": provider,
        "_execution": {"worker_contract": "data-import.v3"},
        "_provider_connection": pin,
    }


def test_hash_is_canonical_and_owner_revision_config_specific():
    owner = uuid4()
    baseline = make_snapshot(owner, "akshare", 1)
    assert baseline == make_snapshot(owner, "akshare", 1)
    assert baseline.config_sha256 == connection_hash(
        dict(
            reversed(
                list(
                    baseline.model_dump(mode="json", exclude={"config_sha256"}).items()
                )
            )
        )
    )
    variants = [
        make_snapshot(uuid4(), "akshare", 1),
        make_snapshot(owner, "akshare", 2),
        make_snapshot(owner, "akshare", 1, default_price_mode="raw"),
        make_snapshot(owner, "yfinance", 1),
    ]
    assert (
        len({baseline.config_sha256, *[item.config_sha256 for item in variants]}) == 5
    )
    public = ProviderConnectionPublic(
        **baseline.model_dump(), created_at="2026-10-23T00:00:00Z"
    )
    assert public.config_sha256 == baseline.config_sha256


@pytest.mark.parametrize(
    "provider,mode", [("yfinance", "raw"), ("openbb", "qfq"), ("yfinance", "repair")]
)
def test_no_unsupported_default_or_implicit_repair(provider, mode):
    with pytest.raises(ValidationError):
        make_snapshot(uuid4(), provider, 1, default_price_mode=mode)


def test_disabled_default_is_invalid():
    with pytest.raises(ValidationError):
        make_snapshot(uuid4(), "akshare", 1, enabled=False)


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "credential",
        "secret_ref",
        "endpoint",
        "base_url",
        "auth_mode",
        "owner_id",
    ],
)
def test_update_has_no_secret_or_endpoint_surface(field):
    with pytest.raises(ValidationError):
        ProviderConnectionUpdate.model_validate(
            {
                "expected_revision": 0,
                "enabled": True,
                "is_default": True,
                "default_price_mode": "raw",
                field: "forbidden",
            }
        )


def test_frozen_binding_rejects_tamper_owner_provider_revision_and_downgrade():
    owner = uuid4()
    request = bound_request(owner)
    assert validate_request_connection(request, "akshare", owner).revision == 1
    for pin in [
        {**request["_provider_connection"], "enabled": False},
        {**request["_provider_connection"], "config_sha256": "0" * 64},
        make_snapshot(owner, "akshare", 0).model_dump(mode="json"),
        make_snapshot(owner, "akshare", 1, enabled=False, is_default=False).model_dump(
            mode="json"
        ),
        make_snapshot(uuid4(), "akshare", 1).model_dump(mode="json"),
    ]:
        with pytest.raises(ValueError):
            validate_request_connection(
                {**request, "_provider_connection": pin}, "akshare", owner
            )
    with pytest.raises(ValueError):
        validate_request_connection(request, "yfinance", owner)
    with pytest.raises(ValueError):
        validate_request_connection(
            {**request, "_execution": {"worker_contract": "data-import.v2"}},
            "akshare",
            owner,
        )
    with pytest.raises(ValueError):
        validate_request_connection(
            {"_execution": {"worker_contract": "data-import.v3"}}, "akshare", owner
        )


def test_legacy_execution_is_unbound_and_not_rehashed():
    for contract in ["data-import.v1", "data-import.v2"]:
        request = {"_execution": {"worker_contract": contract}}
        original = connection_hash(request)
        assert validate_request_connection(request, "yfinance", uuid4()) is None
        assert connection_hash(request) == original


def test_request_schema_accepts_defaults_but_not_caller_owned_pin():
    value = {
        "market": "US",
        "symbols": ["AAPL"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-06",
    }
    body = ImportCreate.model_validate(value)
    assert body.provider is None and body.price_mode is None
    with pytest.raises(ValidationError):
        ImportCreate.model_validate(
            {
                **value,
                "_provider_connection": bound_request(uuid4())["_provider_connection"],
            }
        )


def test_receipt_adds_pin_without_mutating_upstream_provenance():
    request = bound_request(uuid4()) | {
        "symbols": ["600000"],
        "start_date": "2024-01-02",
        "end_date": "2024-01-06",
        "price_mode": "raw",
    }
    sources = [
        {
            "endpoint": "stock_zh_a_hist",
            "evidence": "fixture",
            "package_versions": {"akshare": "fixture"},
        }
    ]
    original = connection_hash(sources)
    receipt = build_probe_receipt(
        import_id=uuid4(),
        provider="akshare",
        request=request,
        provenance=sources,
        warnings=[],
        complete=True,
    )
    assert receipt["provider_connection"] == request["_provider_connection"]
    assert receipt["price_mode"] == "raw"
    assert receipt["outcome"] == "fixture_passed"
    assert original == connection_hash(sources)
    legacy = {
        key: value for key, value in request.items() if key != "_provider_connection"
    }
    assert "provider_connection" not in build_probe_receipt(
        import_id=uuid4(),
        provider="akshare",
        request=legacy,
        provenance=sources,
        warnings=[],
        complete=True,
    )
