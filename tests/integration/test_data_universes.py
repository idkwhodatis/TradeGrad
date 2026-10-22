"""Persisted listing identities and private, explicit non-PIT universe snapshots."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres


@pytest.fixture
def register_listing(migrated_engine):
    from backend.app.data_services import persist_listings
    from packages.marketdata.contracts import make_listing

    def register(account, symbol="AAPL", mic="XNAS"):
        listing = make_listing(
            symbol=symbol,
            mic=mic,
            currency="USD",
            timezone="America/New_York",
            calendar="XNYS",
            vendor="yfinance",
        )
        with Session(migrated_engine) as session:
            persist_listings(session, account.user.id, [listing])
            session.commit()
        return listing

    return register


@pytest.fixture
def owner_listing(register_listing, owner):
    return register_listing(owner)


@pytest.fixture
def universe_payload(owner_listing):
    return {
        "name": "Explicit integration selection, not historical index membership",
        "state": "user_fixed",
        "as_of": "2024-04-01T12:00:00Z",
        "members": [{"listing_id": owner_listing.listing_id}],
        "evidence": [],
    }


@pytest.fixture
def created_universe(api, owner_headers, universe_payload):
    response = api.post(
        "/api/v1/data/universes", headers=owner_headers, json=universe_payload
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_unknown_universe_is_not_disclosed(api, owner_headers):
    response = api.get(f"/api/v1/data/universes/{uuid4()}", headers=owner_headers)
    assert response.status_code == 404, response.text


def test_migrations_persist_listing_registry_and_owner_links(
    migrated_engine, register_listing, owner, other
):
    from backend.app.models import Instrument, Listing, OwnerListing, VendorMapping

    first = register_listing(owner)
    second = register_listing(owner)
    shared = register_listing(other)
    assert first.listing_id == second.listing_id == shared.listing_id
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Instrument)) == 1
        assert session.scalar(select(func.count()).select_from(Listing)) == 1
        assert session.scalar(select(func.count()).select_from(VendorMapping)) == 1
        assert session.scalar(select(func.count()).select_from(OwnerListing)) == 2


def test_symbol_identity_is_scoped_to_venue(migrated_engine, register_listing, owner):
    from backend.app.models import Listing

    nasdaq = register_listing(owner, symbol="SAME", mic="XNAS")
    nyse = register_listing(owner, symbol="SAME", mic="XNYS")
    assert nasdaq.listing_id != nyse.listing_id
    assert nasdaq.instrument_id != nyse.instrument_id
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 2


def test_listing_list_is_private_even_with_shared_global_registry(
    api, register_listing, owner_listing, other, owner_headers, other_headers
):
    foreign = register_listing(other, symbol="MSFT")
    first = api.get("/api/v1/data/listings", headers=owner_headers)
    second = api.get("/api/v1/data/listings", headers=other_headers)
    assert first.status_code == second.status_code == 200
    assert {item["listing_id"] for item in first.json()["data"]} == {
        owner_listing.listing_id
    }
    assert {item["listing_id"] for item in second.json()["data"]} == {
        foreign.listing_id
    }
    filtered = api.get(
        "/api/v1/data/listings",
        headers=owner_headers,
        params=[
            ("listing_ids", owner_listing.listing_id),
            ("listing_ids", foreign.listing_id),
        ],
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["count"] == 1
    assert [item["listing_id"] for item in filtered.json()["data"]] == [
        owner_listing.listing_id
    ]


def test_universe_is_immutable_private_and_explicitly_non_pit(
    api, migrated_engine, owner, owner_headers, other_headers, created_universe
):
    from backend.app.models import UniverseVersion

    universe_id = created_universe["id"]
    assert created_universe["state"] == "user_fixed"
    assert len(created_universe["content_sha256"]) == 64
    with Session(migrated_engine) as session:
        record = session.get(UniverseVersion, UUID(universe_id))
        assert record.owner_id == owner.user.id
        assert record.state == "user_fixed"
        assert record.members == created_universe["members"]
        assert record.content_sha256 == created_universe["content_sha256"]
    own = api.get(f"/api/v1/data/universes/{universe_id}", headers=owner_headers)
    foreign = api.get(f"/api/v1/data/universes/{universe_id}", headers=other_headers)
    anonymous = api.get(f"/api/v1/data/universes/{universe_id}")
    assert own.status_code == 200, own.text
    assert own.json() == created_universe
    assert foreign.status_code == 404, foreign.text
    assert anonymous.status_code == 401, anonymous.text
    foreign_list = api.get("/api/v1/data/universes", headers=other_headers)
    assert foreign_list.status_code == 200, foreign_list.text
    assert foreign_list.json()["data"] == []
    assert foreign_list.json()["count"] == 0


def test_repeated_universe_creation_preserves_content_identity(
    api, owner_headers, universe_payload, created_universe
):
    repeated = api.post(
        "/api/v1/data/universes", headers=owner_headers, json=universe_payload
    )
    assert repeated.status_code in (200, 201), repeated.text
    assert repeated.json()["content_sha256"] == created_universe["content_sha256"]
    changed = api.post(
        "/api/v1/data/universes",
        headers=owner_headers,
        json={**universe_payload, "as_of": "2024-04-02T12:00:00Z"},
    )
    assert changed.status_code in (200, 201), changed.text
    assert changed.json()["content_sha256"] != created_universe["content_sha256"]
    original = api.get(
        f"/api/v1/data/universes/{created_universe['id']}", headers=owner_headers
    )
    assert original.json() == created_universe


def test_universe_cannot_reference_another_owners_listing(
    api, migrated_engine, other_headers, universe_payload
):
    from backend.app.models import UniverseVersion

    response = api.post(
        "/api/v1/data/universes", headers=other_headers, json=universe_payload
    )
    assert response.status_code == 404, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(UniverseVersion)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"state": "point_in_time_verified"},
        {"state": "historical_membership"},
        {"state": "current_snapshot"},
        {"state": "current_snapshot", "evidence": [{}]},
        {"state": "current_snapshot", "evidence": [{"reference": "x" * 16384}]},
        {"as_of": "2024-04-01T12:00:00"},
        {"members": []},
        {"owner_id": "00000000-0000-0000-0000-000000000000"},
    ],
    ids=[
        "unverified-pit",
        "missing-history-evidence",
        "missing-current-evidence",
        "empty-current-evidence",
        "oversized-current-evidence",
        "naive-asof",
        "empty",
        "owner-spoof",
    ],
)
def test_invalid_universes_are_rejected_without_persistence(
    api, migrated_engine, owner_headers, universe_payload, change
):
    from backend.app.models import UniverseVersion

    response = api.post(
        "/api/v1/data/universes",
        headers=owner_headers,
        json={**universe_payload, **change},
    )
    assert response.status_code == 422, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(UniverseVersion)) == 0


def test_user_supplied_evidence_cannot_promote_a_universe_to_verified_pit(
    api, owner_headers, universe_payload, owner_listing
):
    response = api.post(
        "/api/v1/data/universes",
        headers=owner_headers,
        json={
            **universe_payload,
            "state": "point_in_time_verified",
            "members": [
                {
                    "listing_id": owner_listing.listing_id,
                    "valid_from": "2024-01-01",
                    "valid_to": "2024-04-01",
                    "known_at": "2023-12-31T12:00:00Z",
                }
            ],
            "evidence": [{"claim": "This client says the membership was known"}],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("invalid", ["duplicate", "reversed", "missing-start"])
def test_invalid_membership_intervals_and_duplicate_members_are_rejected(
    api, owner_headers, universe_payload, owner_listing, invalid
):
    member = {"listing_id": owner_listing.listing_id}
    if invalid == "duplicate":
        members = [member, member]
    else:
        member["valid_to"] = "2024-01-01"
        if invalid == "reversed":
            member["valid_from"] = "2024-04-01"
        members = [member]
    response = api.post(
        "/api/v1/data/universes",
        headers=owner_headers,
        json={**universe_payload, "members": members},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("state", ["current_snapshot", "historical_membership"])
def test_supported_membership_states_are_not_silently_upgraded_to_pit(
    api, owner_headers, universe_payload, owner_listing, state
):
    response = api.post(
        "/api/v1/data/universes",
        headers=owner_headers,
        json={
            **universe_payload,
            "state": state,
            "members": [
                {
                    "listing_id": owner_listing.listing_id,
                    "valid_from": "2024-01-01",
                    "valid_to": "2024-04-01",
                }
            ],
            "evidence": [{"source": "integration-fixture", "verified_pit": False}],
        },
    )
    assert response.status_code in (200, 201), response.text
    assert response.json()["state"] == state


def test_import_rejects_foreign_universe_before_creating_outbox(
    api, migrated_engine, other_headers, created_universe
):
    from backend.app.models import DataImport, DataOutbox

    response = api.post(
        "/api/v1/data/imports",
        headers={**other_headers, "Idempotency-Key": uuid4().hex},
        json={
            "provider": "yfinance",
            "market": "US",
            "symbols": ["AAPL"],
            "start_date": "2024-01-01",
            "end_date": "2024-04-01",
            "universe_version_id": created_universe["id"],
        },
    )
    assert response.status_code == 404, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_import_cannot_use_foreign_listing_ids(
    api, migrated_engine, other_headers, owner_listing
):
    from backend.app.models import DataImport, DataOutbox

    response = api.post(
        "/api/v1/data/imports",
        headers={**other_headers, "Idempotency-Key": uuid4().hex},
        json={
            "provider": "yfinance",
            "market": "US",
            "listing_ids": [owner_listing.listing_id],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        },
    )
    assert response.status_code == 404, response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_vendor_symbol_validity_periods_are_preserved_not_overwritten(
    migrated_engine, owner
):
    from datetime import date

    from backend.app.data_services import listing_contracts, persist_listings
    from backend.app.models import VendorMapping
    from packages.marketdata.contracts import VendorSymbol, make_listing

    original = make_listing(
        symbol="AAPL",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        calendar="XNYS",
        vendor="yfinance",
    )
    old = original.model_copy(
        update={
            "vendor_symbols": [
                VendorSymbol(
                    vendor="yfinance",
                    symbol="AAPL",
                    valid_from=date(2020, 1, 1),
                    valid_to=date(2022, 1, 1),
                )
            ]
        }
    )
    later = original.model_copy(
        update={
            "vendor_symbols": [
                VendorSymbol(
                    vendor="yfinance",
                    symbol="AAPL",
                    valid_from=date(2024, 1, 1),
                    valid_to=None,
                )
            ]
        }
    )
    with Session(migrated_engine) as session:
        persist_listings(session, owner.user.id, [old])
        persist_listings(session, owner.user.id, [later])
        persist_listings(session, owner.user.id, [later])
        session.commit()
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(VendorMapping)) == 2
        stored = listing_contracts(session, owner.user.id, [original.listing_id])[0]
        assert len(stored.vendor_symbols) == 2
        assert stored.vendor_symbol("yfinance", date(2021, 1, 1)) == "AAPL"
        with pytest.raises(ValueError):
            stored.vendor_symbol("yfinance", date(2023, 1, 1))
        assert stored.vendor_symbol("yfinance", date(2024, 2, 1)) == "AAPL"
