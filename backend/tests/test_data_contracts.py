"""Offline M2 contract/security checks; not PostgreSQL or live-provider evidence."""

import asyncio
import csv
import io
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.api.body_limit import BodyLimitMiddleware
from backend.app.data_schemas import (
    ImportCreate,
    ImportPublic,
    ProviderPublic,
    UniverseCreate,
    UploadMapping,
)
from packages.marketdata.catalog import provider_catalog
from packages.marketdata.contracts import BarsRequest, make_listing
from workers.data.executor import _write_universe


def test_provider_catalog_is_public_sdk_free_contract():
    for item in provider_catalog():
        payload = {k: v for k, v in item.items() if k not in {"provider", "intervals"}}
        value = ProviderPublic(
            **payload, upstream=item["provider"], runtime_mode="live"
        )
        assert value.capabilities
        assert all(c.status != "verified" for c in value.capabilities)


def test_import_cannot_select_runtime_fixtures_credentials_or_paths():
    base = {
        "provider": "yfinance",
        "market": "US",
        "symbols": ["AAPL"],
        "start_date": "2024-01-02",
        "end_date": "2024-07-01",
    }
    for name, value in [
        ("mode", "fixture"),
        ("_execution", {"provider_mode": "fixture", "block_days": 2}),
        ("credential", "unsafe"),
        ("storage_key", "/tmp/input"),
        ("sql", "SELECT 1"),
    ]:
        with pytest.raises(ValidationError):
            ImportCreate(**base, **{name: value})
    with pytest.raises(ValidationError):
        ImportCreate(**base, listing_ids=[uuid4()])


def test_user_evidence_never_promotes_verified_pit():
    with pytest.raises(ValidationError):
        UniverseCreate(
            name="Not verified",
            state="point_in_time_verified",
            as_of=datetime.now(UTC),
            members=[
                {
                    "listing_id": str(uuid4()),
                    "valid_from": date(2024, 1, 1),
                    "known_at": datetime.now(UTC),
                }
            ],
            evidence=[{"url": "https://example.com/claims"}],
        )


@pytest.fixture
def mapping():
    return {
        "symbol": "AAPL",
        "market": "US",
        "mic": "XNAS",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "start_date": "2024-01-02",
        "end_date": "2024-07-01",
        "columns": {
            key: key
            for key in ["session_date", "open", "high", "low", "close", "volume"]
        },
        "volume_unit": "shares",
        "price_semantics": {"native_mode": "unknown"},
    }


def test_upload_unknown_values_must_remain_explicit(mapping):
    with pytest.raises(ValidationError):
        UploadMapping(**(mapping | {"currency": "unknown"}))
    accepted = UploadMapping(
        **(mapping | {"currency": "unknown", "unknown_flags": ["currency_unknown"]})
    )
    assert accepted.currency == "unknown"
    with pytest.raises(ValidationError):
        UploadMapping(**(mapping | {"timezone": "Invalid/IANA"}))


def test_private_upload_storage_reference_not_exposed():
    stamp = datetime.now(UTC)
    item = SimpleNamespace(
        id=uuid4(),
        kind="upload",
        provider="upload",
        status="requested",
        generation=1,
        request={"upload": {"storage_key": "uploads/private", "sha256": "a" * 64}},
        listing_ids=[],
        progress={},
        warnings=[],
        error=None,
        dataset_version_id=None,
        parent_version_id=None,
        cancel_requested=False,
        created_at=stamp,
        updated_at=stamp,
    )
    output = ImportPublic.model_validate(item).model_dump(mode="json")
    assert "storage_key" not in output["request"]["upload"]
    assert output["request"]["upload"]["sha256"] == "a" * 64


def test_body_limit_rejects_chunked_body_before_app_parses_it():
    messages = iter(
        [
            {"type": "http.request", "body": b"a" * 8, "more_body": True},
            {"type": "http.request", "body": b"b" * 8, "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(value):
        sent.append(value)

    async def forbidden_app(*args):
        raise AssertionError("Oversized body reached parser")

    asyncio.run(
        BodyLimitMiddleware(forbidden_app, 10)(
            {"type": "http", "method": "POST"}, receive, send
        )
    )
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["code"] == "request_size"


def test_body_limit_preserves_bounded_body_for_application():
    messages = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ]
    )
    seen = []

    async def receive():
        return next(messages)

    async def send(value):
        pass

    async def app(scope, receive, send):
        seen.append(await receive())

    asyncio.run(
        BodyLimitMiddleware(app, 10)({"type": "http", "method": "POST"}, receive, send)
    )
    assert seen == [{"type": "http.request", "body": b"abcdef", "more_body": False}]


def test_historical_universe_csv_uses_actual_interval_boundaries(tmp_path):
    listings = [
        make_listing(
            symbol=s,
            mic="XNAS",
            currency="USD",
            timezone="America/New_York",
            calendar="XNYS",
            vendor="yfinance",
        )
        for s in ["AAPL", "MSFT"]
    ]
    request = BarsRequest(
        listings=listings, start=date(2024, 1, 2), end=date(2024, 2, 1)
    )
    manifest = {
        "files": {},
        "evaluator_symbol_mapping": {l.listing_id: l.symbol for l in listings},
    }
    (tmp_path / "universe").mkdir()
    universe = {
        "state": "historical_membership",
        "members": [
            {
                "listing_id": listings[0].listing_id,
                "valid_from": "2024-01-01",
                "valid_to": "2024-01-15",
            },
            {
                "listing_id": listings[1].listing_id,
                "valid_from": "2024-01-15",
                "valid_to": None,
            },
        ],
    }
    _write_universe(tmp_path, manifest, universe, request)
    assert set(manifest["files"]) == {
        "universe/members_list_2024-01-02.csv",
        "universe/members_list_2024-01-15.csv",
    }
    for path, symbol in [
        ("universe/members_list_2024-01-02.csv", "AAPL"),
        ("universe/members_list_2024-01-15.csv", "MSFT"),
    ]:
        rows = list(csv.DictReader(io.StringIO((tmp_path / path).read_text())))
        assert [row["code"] for row in rows] == [symbol]


def test_empty_historical_interval_is_not_silently_forward_filled(tmp_path):
    listing = make_listing(
        symbol="AAPL",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        calendar="XNYS",
        vendor="yfinance",
    )
    request = BarsRequest(
        listings=[listing], start=date(2024, 1, 2), end=date(2024, 2, 1)
    )
    (tmp_path / "universe").mkdir()
    with pytest.raises(ValueError, match="empty universe"):
        _write_universe(
            tmp_path,
            {"files": {}, "evaluator_symbol_mapping": {listing.listing_id: "INDEX"}},
            {
                "state": "historical_membership",
                "members": [
                    {
                        "listing_id": listing.listing_id,
                        "valid_from": "2024-01-10",
                        "valid_to": None,
                    }
                ],
            },
            request,
        )


def test_upload_calendar_must_match_declared_mic(mapping):
    with pytest.raises(ValidationError, match="[Cc]alendar"):
        UploadMapping(**(mapping | {"calendar": "XSHG"}))
    accepted = UploadMapping(**(mapping | {"calendar": "XNAS"}))
    assert accepted.calendar == "XNYS"


def test_current_snapshot_evidence_must_be_nonempty_bounded_and_referenced():
    values = {
        "name": "Current user claim",
        "state": "current_snapshot",
        "as_of": datetime.now(UTC),
        "members": [{"listing_id": str(uuid4())}],
    }
    for evidence in ([], [{}], [{"reference": ""}], [{"reference": "x" * 16384}]):
        with pytest.raises(ValidationError):
            UniverseCreate(**values, evidence=evidence)
    item = UniverseCreate(
        **values, evidence=[{"reference": "User-supplied fixture announcement"}]
    )
    assert item.state == "current_snapshot"
