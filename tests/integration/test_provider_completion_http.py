"""Real PostgreSQL admission, ownership and durable receipt tests.

Collection is safe offline. Execution requires the existing migrated PG fixture;
no SDK calls, containers or strategy execution occur in this suite. Real worker
and browser execution are tested separately by the hosted provider gates.
"""

import hashlib
import json
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.atomic_storage import publish_bytes
from backend.app.data_services import persist_listings
from backend.app.models import DataImport, DataOutbox, DatasetVersion
from packages.marketdata.contracts import make_listing
from packages.marketdata.probes import build_probe_receipt

pytestmark = pytest.mark.postgres


@pytest.fixture
def parent_snapshot(migrated_engine, settings, owner):
    listing = make_listing(
        symbol="AAPL",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        calendar="XNYS",
        vendor="yfinance",
    )
    raw, provenance = b"[]", b"[]"
    manifest = {
        "content_hash": "a" * 64,
        "listings": [listing.model_dump(mode="json")],
        "price_semantics": {
            "native_mode": "yahoo_split_adjusted_ohlc",
            "split_basis": "adjusted",
            "dividend_basis": "unadjusted",
            "algorithm_version": "yahoo-native;repair=False",
        },
        "artifact_files": {
            "raw.json": hashlib.sha256(raw).hexdigest(),
            "provenance.json": hashlib.sha256(provenance).hexdigest(),
        },
        "import_request": {
            "name": "Immutable original Yahoo fixture",
            "provider": "yfinance",
            "market": "US",
            "symbols": ["AAPL"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
            "price_mode": "provider_native",
        },
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    key = f"datasets/{owner.user.id}/{uuid4()}"
    for name, content in (
        ("manifest.json", encoded),
        ("raw.json", raw),
        ("provenance.json", provenance),
    ):
        publish_bytes(
            settings.storage_root / key / name,
            content,
            durable_root=settings.storage_root,
        )
    with Session(migrated_engine) as session:
        persist_listings(session, owner.user.id, [listing])
        value = DatasetVersion(
            owner_id=owner.user.id,
            name="Immutable original Yahoo fixture",
            market="US",
            source="yfinance",
            is_synthetic=True,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 6),
            manifest=manifest,
            storage_key=key,
            content_sha256=hashlib.sha256(encoded).hexdigest(),
        )
        session.add(value)
        session.commit()
        return {
            "id": str(value.id),
            "sha256": value.content_sha256,
            "key": key,
            "listing_id": listing.listing_id,
            "bytes": encoded,
        }


def test_yahoo_repair_is_explicit_parent_pinned_and_idempotent(
    api, owner_headers, parent_snapshot, settings, migrated_engine
):
    parent = parent_snapshot
    path = f"/api/v1/datasets/{parent['id']}/yahoo-repair"
    headers = {**owner_headers, "Idempotency-Key": uuid4().hex}
    first = api.post(path, headers=headers)
    assert first.status_code == 202, first.text
    second = api.post(path, headers=headers)
    assert second.json()["id"] == first.json()["id"]
    job = first.json()
    assert job["parent_version_id"] == parent["id"]
    assert job["request"]["price_mode"] == "repair"
    assert job["request"]["_execution"]["worker_contract"] == "data-import.v2"
    assert job["request"]["symbols"] == []
    assert job["request"]["listing_ids"] == [parent["listing_id"]]
    assert job["request"]["_derivation"]["manifest_sha256"] == parent["sha256"]
    assert (
        settings.storage_root / parent["key"] / "manifest.json"
    ).read_bytes() == parent["bytes"]
    with Session(migrated_engine) as session:
        outbox = session.scalars(
            select(DataOutbox).where(DataOutbox.import_id == UUID(job["id"]))
        ).all()
        assert len(outbox) == 1


def test_yahoo_repair_hides_foreign_parent(api, other_headers, parent_snapshot):
    response = api.post(
        f"/api/v1/datasets/{parent_snapshot['id']}/yahoo-repair",
        headers={**other_headers, "Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 404


def test_repair_cannot_be_smuggled_into_generic_import(api, owner_headers):
    response = api.post(
        "/api/v1/data/imports",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json={
            "provider": "yfinance",
            "market": "US",
            "symbols": ["AAPL"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
            "price_mode": "repair",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "repair_parent_required"


def test_repair_rejects_tampered_parent_before_enqueue(
    api, owner_headers, parent_snapshot, settings, migrated_engine
):
    path = settings.storage_root / parent_snapshot["key"] / "manifest.json"
    path.chmod(0o600)
    path.write_bytes(b"{}")
    response = api.post(
        f"/api/v1/datasets/{parent_snapshot['id']}/yahoo-repair",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
    )
    assert response.status_code in {409, 422}
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(DataImport).where(
                    DataImport.parent_version_id == UUID(parent_snapshot["id"])
                )
            )
            is None
        )


def test_non_yahoo_original_repair_rejected(api, owner_headers, owner):
    response = api.post(
        f"/api/v1/datasets/{owner.dataset.id}/yahoo-repair",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "repair_unavailable"


@pytest.mark.parametrize(
    "provider,symbol",
    [
        ("akshare", "600000"),
        ("akshare", "000001"),
        ("yfinance", "AAPL"),
        ("openbb", "MSFT"),
    ],
)
def test_probe_admission_is_durable_and_idempotent(
    api, owner_headers, other_headers, provider, symbol, migrated_engine
):
    headers = {**owner_headers, "Idempotency-Key": uuid4().hex}
    body = {"symbol": symbol, "start_date": "2024-01-02", "end_date": "2024-01-06"}
    path = f"/api/v1/data/providers/{provider}/test"
    first = api.post(path, headers=headers, json=body)
    assert first.status_code == 202, first.text
    second = api.post(path, headers=headers, json=body)
    assert second.json()["id"] == first.json()["id"]
    job = first.json()
    assert job["kind"] == "probe" and job["dataset_version_id"] is None
    assert job["request"]["_execution"]["worker_contract"] == "data-import.v2"
    assert (
        api.get(f"/api/v1/data/imports/{job['id']}", headers=other_headers).status_code
        == 404
    )
    with Session(migrated_engine) as session:
        assert (
            len(
                session.scalars(
                    select(DataOutbox).where(DataOutbox.import_id == UUID(job["id"]))
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize(
    "provider,body",
    [
        ("portable_fixture", {"symbol": "AAPL"}),
        ("yfinance", {"symbol": "UNKNOWN"}),
        ("yfinance", {"symbol": "AAPL", "end_date": "2024-02-01"}),
        (
            "yfinance",
            {"symbol": "AAPL", "start_date": "2099-01-02", "end_date": "2099-01-06"},
        ),
        ("yfinance", {"symbol": "AAPL", "credential": "not-allowed"}),
        ("yfinance", {"symbol": "AAPL", "provider_mode": "fixture"}),
    ],
)
def test_probe_bounds_before_any_fetch(api, owner_headers, provider, body):
    response = api.post(
        f"/api/v1/data/providers/{provider}/test",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json={"start_date": "2024-01-02", "end_date": "2024-01-06", **body},
    )
    assert response.status_code == 422


def test_scoped_fixture_receipt_is_private_not_global_verified(
    api, owner_headers, other_headers, migrated_engine
):
    response = api.post(
        "/api/v1/data/providers/yfinance/test",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json={"symbol": "AAPL", "start_date": "2024-01-02", "end_date": "2024-01-06"},
    )
    assert response.status_code == 202
    job_id = UUID(response.json()["id"])
    with Session(migrated_engine) as session:
        job = session.get(DataImport, job_id)
        receipt = build_probe_receipt(
            import_id=job.id,
            provider="yfinance",
            request=job.request,
            provenance=[
                {
                    "endpoint": "Ticker.history",
                    "evidence": "fixture",
                    "package_versions": {"yfinance": "1.7.0"},
                }
            ],
            warnings=[],
            complete=True,
        )
        job.status, job.progress = "ready", {"probe_receipt": receipt}
        session.commit()
    owner_catalog = api.get("/api/v1/data/providers", headers=owner_headers).json()
    yahoo = next(p for p in owner_catalog if p["id"] == "yfinance")
    assert yahoo["latest_test"]["outcome"] == "fixture_passed"
    assert all(c["status"] != "verified" for c in yahoo["capabilities"])
    stranger_catalog = api.get("/api/v1/data/providers", headers=other_headers).json()
    assert (
        next(p for p in stranger_catalog if p["id"] == "yfinance")["latest_test"]
        is None
    )
    retry = api.post(f"/api/v1/data/imports/{job_id}/retry", headers=owner_headers)
    assert retry.status_code == 422
    assert retry.json()["code"] == "new_probe_required"
