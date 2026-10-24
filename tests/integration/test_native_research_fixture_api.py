"""Actual PostgreSQL and locked data worker, never a live-data assertion."""

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import DataBlock, DataImport, DataOutbox, DatasetVersion
from packages.marketdata.contracts import PORTABLE_FIXTURE_VERSION
from packages.marketdata.native_research_fixture import PROFILE, VERSION
from tests.integration.test_data_imports import data_worker as data_worker  # noqa: PLC0414 -- re-export pytest fixture
from tests.integration.test_data_imports import settings as settings  # noqa: PLC0414 -- re-export pytest fixture

pytestmark = pytest.mark.postgres


def test_required_research_fixture_postgres_selector_cannot_silently_skip():
    if os.environ.get("TRADEGRAD_RESEARCH_FIXTURE_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), "Real PostgreSQL URL is required"


def payload(**changes):
    return {
        "name": "Frozen synthetic research profile, not observed market data",
        "provider": "portable_fixture",
        "market": "US",
        "symbols": ["A", "B", "C", "D"],
        "start_date": "2024-01-02",
        "end_date": "2024-03-01",
        "price_mode": "raw",
        **changes,
    }


def submit(api, headers, body, *, resolve=False):
    return api.post(
        "/api/v1/data/resolve" if resolve else "/api/v1/data/imports",
        headers={**headers, "Idempotency-Key": uuid4().hex},
        json=body,
    )


@pytest.mark.parametrize("by_listing", [False, True])
def test_real_research_profile_publishes_all_164_frozen_bars(
    api, owner_headers, migrated_engine, data_worker, by_listing
):
    body = payload()
    if by_listing:
        resolved = submit(
            api,
            owner_headers,
            {key: body[key] for key in ("provider", "market", "symbols")},
            resolve=True,
        )
        assert resolved.status_code == 202, resolved.text
        resolve_id = UUID(resolved.json()["id"])
        data_worker.run(resolve_id)
        with Session(migrated_engine) as session:
            job = session.get(DataImport, resolve_id)
            assert job.status == "ready", job.error
            assert job.request["_execution"]["fixture_profile"] == PROFILE
            body["listing_ids"] = job.listing_ids
            body.pop("symbols")
    response = submit(api, owner_headers, body)
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    data_worker.run(import_id)
    with Session(migrated_engine) as session:
        job = session.get(DataImport, import_id)
        assert job.status == "ready", (job.error, job.progress)
        assert job.request["_execution"]["fixture_profile"] == PROFILE
        assert job.request["_execution"]["fixture_version"] == VERSION
        dataset = session.get(DatasetVersion, job.dataset_version_id)
        assert dataset.is_synthetic is True
        assert dataset.manifest["import_execution_policy"] == job.request["_execution"]
        assert len(dataset.manifest["listings"]) == 4
        assert all(p["evidence"] == "fixture" for p in dataset.manifest["provenance"])
        from packages.marketdata.storage import verify_bundle

        verify_bundle(
            data_worker.settings.storage_root / dataset.storage_key, dataset.manifest
        )
        dataset_id = str(dataset.id)
        ids = list(job.listing_ids)
    for listing_id in ids:
        preview = api.get(
            f"/api/v1/datasets/{dataset_id}/preview",
            headers=owner_headers,
            params={"listing_id": listing_id, "limit": 100},
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["count"] == 41
        assert len(preview.json()["data"]) == 41


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": ["A", "AAPL"]},
        {"market": "CN"},
        {"end_date": "2024-03-02"},
    ],
)
def test_api_rejects_invalid_profile_without_outbox_side_effects(
    api, owner_headers, migrated_engine, changes
):
    response = submit(api, owner_headers, payload(**changes))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "research_fixture_unavailable"
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_legacy_fixture_execution_identity_remains_unmodified(
    api, owner_headers, migrated_engine
):
    response = submit(api, owner_headers, payload(symbols=["AAPL", "MSFT"]))
    assert response.status_code == 202, response.text
    with Session(migrated_engine) as session:
        policy = session.get(DataImport, UUID(response.json()["id"])).request[
            "_execution"
        ]
        assert policy["fixture_version"] == PORTABLE_FIXTURE_VERSION
        assert "fixture_profile" not in policy


def test_legacy_profile_policy_fails_before_provider_or_checkpoint_reuse(
    api, owner_headers, migrated_engine, data_worker, monkeypatch
):
    response = submit(api, owner_headers, payload())
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    with Session(migrated_engine) as session:
        job = session.get(DataImport, import_id)
        policy = {
            **job.request["_execution"],
            "fixture_version": PORTABLE_FIXTURE_VERSION,
        }
        policy.pop("fixture_profile")
        job.request = {**job.request, "_execution": policy}
        session.commit()
    called = []

    def unexpected_provider(*args, **kwargs):
        called.append(True)
        raise AssertionError("Old profile must fail before provider construction")

    monkeypatch.setattr(
        "packages.marketdata.providers.registry.get_provider", unexpected_provider
    )
    data_worker.run(import_id)
    assert not called
    with Session(migrated_engine) as session:
        assert session.get(DataImport, import_id).status == "failed"
        assert session.get(DataImport, import_id).dataset_version_id is None
        assert session.scalar(select(func.count()).select_from(DataBlock)) == 0
