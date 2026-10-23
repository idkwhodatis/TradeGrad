"""Real migrated PostgreSQL owner/reference/admission tests; collect only locally."""

import copy
import hashlib
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import DatasetVersion, DataUsageExposure, ResearchRun
from packages.tradegrad_adapter.config import canonical_bytes
from tests.integration import test_research_lifecycle as lifecycle

base_research_payload = lifecycle.research_payload
pytestmark = pytest.mark.postgres


@pytest.fixture
def benchmark_payload(base_research_payload, migrated_engine, owner):
    """Publish two explicit synthetic versions with matching pinned test calendars."""
    payload = copy.deepcopy(base_research_payload)
    with Session(migrated_engine) as session:
        previous = session.get(DatasetVersion, UUID(payload["dataset"]["id"]))
        manifest = copy.deepcopy(previous.manifest)
        listing = manifest["listings"][0]
        listing.update(currency="CNY", timezone="Asia/Shanghai", calendar="XSHG")
        days = [str(date(2024, 1, 1) + timedelta(days=i)) for i in range(91)]
        manifest.update(
            request={"interval": "1d", "as_of": "2026-10-23T00:00:00Z"},
            price_semantics={
                "native_mode": "synthetic_raw",
                "split_basis": "unadjusted",
                "dividend_basis": "unadjusted",
            },
            coverage=[
                {
                    "listing_id": listing["listing_id"],
                    "calendar": "XSHG",
                    "calendar_version": "synthetic-admission-test.v1",
                    "expected_sessions": days,
                }
            ],
        )
        records = []
        for label in ("strategy", "explicit benchmark"):
            item = DatasetVersion(
                id=uuid4(),
                owner_id=owner.user.id,
                name=f"Synthetic {label}",
                market="CN",
                start_date=previous.start_date,
                end_date=previous.end_date,
                source="synthetic_fixture",
                is_synthetic=True,
                manifest=manifest,
                content_sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
                storage_key=previous.storage_key,
            )
            session.add(item)
            records.append({"id": str(item.id), "sha256": item.content_sha256})
        session.commit()
    payload["dataset"] = records[0]
    payload["benchmark"] = {
        "definition_version": "daily_close_observed_anchor.v1",
        "dataset": records[1],
        "listing_id": listing["listing_id"],
        "price_field": "close",
    }
    return payload


def test_owned_published_selection_preflight_is_read_only_then_submission_pins_it(
    api, owner_headers, benchmark_payload, migrated_engine
):
    preflight = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=benchmark_payload
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["holdout_execution"] is False
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
    key = uuid4().hex
    response = api.post(
        "/api/v1/research-runs",
        headers={**owner_headers, "Idempotency-Key": key},
        json=benchmark_payload,
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["config"]["benchmark"] == benchmark_payload["benchmark"]
    again = api.post(
        "/api/v1/research-runs",
        headers={**owner_headers, "Idempotency-Key": key},
        json=benchmark_payload,
    )
    assert again.status_code == 202 and again.json()["id"] == run["id"]
    changed = copy.deepcopy(benchmark_payload)
    changed["benchmark"] = None
    assert (
        api.post(
            "/api/v1/research-runs",
            headers={**owner_headers, "Idempotency-Key": key},
            json=changed,
        ).status_code
        == 409
    )
    with Session(migrated_engine) as session:
        exposures = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.run_id == UUID(run["id"]),
                DataUsageExposure.purpose.like("benchmark_%"),
            )
        ).all()
        assert {x.purpose for x in exposures} == {
            "benchmark_train_admission",
            "benchmark_validation_admission",
        }
        assert all(x.end_date < date(2024, 3, 1) for x in exposures)
        assert all(x.provenance["optimizer_feedback"] is False for x in exposures)


@pytest.mark.parametrize(
    "mutation,code",
    [("foreign", 404), ("hash", 422), ("listing", 422), ("unknown", 422), ("gap", 422)],
)
def test_benchmark_preflight_rejects_owner_hash_listing_semantics_and_coverage(
    api, owner_headers, benchmark_payload, migrated_engine, other, mutation, code
):
    body = copy.deepcopy(benchmark_payload)
    if mutation == "hash":
        body["benchmark"]["dataset"]["sha256"] = "f" * 64
    elif mutation == "listing":
        body["benchmark"]["listing_id"] = str(uuid4())
    else:
        with Session(migrated_engine) as session:
            old = session.get(DatasetVersion, UUID(body["benchmark"]["dataset"]["id"]))
            manifest = copy.deepcopy(old.manifest)
            if mutation == "unknown":
                manifest["price_semantics"]["dividend_basis"] = "unknown"
            if mutation == "gap":
                manifest["coverage"][0]["expected_sessions"].remove("2024-02-02")
            item = DatasetVersion(
                id=uuid4(),
                owner_id=other.user.id if mutation == "foreign" else old.owner_id,
                name="Synthetic rejected benchmark",
                market=old.market,
                start_date=old.start_date,
                end_date=old.end_date,
                source=old.source,
                is_synthetic=True,
                content_sha256=hashlib.sha256(canonical_bytes(manifest)).hexdigest(),
                manifest=manifest,
                storage_key=old.storage_key,
            )
            session.add(item)
            body["benchmark"]["dataset"] = {
                "id": str(item.id),
                "sha256": item.content_sha256,
            }
            session.commit()
    response = api.post(
        "/api/v1/research-runs/preflight", headers=owner_headers, json=body
    )
    assert response.status_code == code
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 0
