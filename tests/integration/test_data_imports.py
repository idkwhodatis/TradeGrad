"""M2 durable import boundaries backed by migrated, real PostgreSQL sessions.

This suite never starts a live provider or substitutes SQLite/mocked persistence.
Provider execution and browser/worker acceptance remain separate gates.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres


@pytest.fixture
def import_payload():
    return {
        "name": "PostgreSQL durability fixture, not live provider evidence",
        "provider": "yfinance",
        "market": "US",
        "symbols": ["AAPL"],
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "price_mode": "provider_native",
    }


@pytest.fixture
def submit_data_import(api, owner_headers, import_payload):
    def submit(*, key=None, payload=None, headers=None):
        return api.post(
            "/api/v1/data/imports",
            headers={
                **(headers or owner_headers),
                "Idempotency-Key": key or uuid4().hex,
            },
            json=payload or import_payload,
        )

    return submit


@pytest.fixture
def queued_import(submit_data_import):
    response = submit_data_import()
    assert response.status_code in (201, 202), response.text
    return response.json()


@pytest.fixture
def claimed_import(migrated_engine, settings, queued_import):
    from backend.app.data_services import claim_import

    with Session(migrated_engine) as session:
        token = claim_import(session, UUID(queued_import["id"]), 1, settings)
        assert token is not None
        session.commit()
    return UUID(queued_import["id"]), token


def test_readiness_accepts_the_applied_m2_migration_head(api):
    response = api.get("/api/v1/health/ready")
    assert response.status_code == 200, response.text
    assert response.json()["database"] is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/data/providers",
        "/api/v1/data/imports",
        "/api/v1/data/listings",
        "/api/v1/data/universes",
    ],
)
def test_data_catalog_and_job_lists_require_authentication(api, path):
    response = api.get(path)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("view", ["preview", "coverage"])
def test_dataset_data_views_do_not_disclose_other_owners(
    api, owner, other_headers, view
):
    path = f"/api/v1/datasets/{owner.dataset.id}/{view}"
    if view == "preview":
        path += f"?listing_id={uuid4()}"
    foreign = api.get(path, headers=other_headers)
    anonymous = api.get(path)
    assert foreign.status_code == 404, foreign.text
    assert anonymous.status_code == 401, anonymous.text


def test_refresh_hides_other_owners_dataset(api, owner, other_headers):
    response = api.post(
        f"/api/v1/datasets/{owner.dataset.id}/refresh",
        headers={**other_headers, "Idempotency-Key": uuid4().hex},
        json={},
    )
    assert response.status_code == 404, response.text


def test_migration_creates_durable_data_and_registry_tables(migrated_engine):
    assert {
        "instruments",
        "listings",
        "owner_listings",
        "vendor_mappings",
        "universe_versions",
        "data_imports",
        "data_blocks",
        "data_outbox",
        "provider_budgets",
    } <= set(inspect(migrated_engine).get_table_names())
    foreign_keys = inspect(migrated_engine).get_foreign_keys("data_blocks")
    assert {fk["referred_table"] for fk in foreign_keys} >= {"data_imports", "listings"}
    unique = inspect(migrated_engine).get_unique_constraints("data_imports")
    assert any(
        set(item["column_names"]) == {"owner_id", "idempotency_key"} for item in unique
    )


def test_import_submission_and_outbox_are_atomic(
    submit_data_import, migrated_engine, owner
):
    from backend.app.models import DataImport, DataOutbox

    response = submit_data_import(key="durable-import")
    assert response.status_code in (201, 202), response.text
    body = response.json()
    assert body["status"] == "requested"
    assert body["generation"] == 1
    assert body["dataset_version_id"] is None
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(body["id"]))
        assert record.owner_id == owner.user.id
        assert record.kind == "import"
        assert record.idempotency_key == "durable-import"
        events = session.scalars(
            select(DataOutbox).where(DataOutbox.import_id == record.id)
        ).all()
        assert len(events) == 1
        assert events[0].generation == 1
        assert events[0].job_id is None
        assert events[0].dispatched_at is None


def test_submit_rollback_removes_both_import_and_outbox(
    migrated_engine, settings, owner, import_payload
):
    from backend.app.data_schemas import ImportCreate
    from backend.app.data_services import submit_import
    from backend.app.models import DataImport, DataOutbox

    with Session(migrated_engine) as session:
        submit_import(
            session,
            owner.user.id,
            ImportCreate(**import_payload),
            "uncommitted-import",
            settings,
        )
        session.flush()
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_replayed_import_is_the_same_job_without_duplicate_outbox(
    submit_data_import, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    first = submit_data_import(key="response-lost")
    second = submit_data_import(key="response-lost")
    assert first.status_code in (201, 202), first.text
    assert second.status_code in (200, 201, 202), second.text
    assert first.json()["id"] == second.json()["id"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_import_idempotency_conflict_leaves_original_unchanged(
    submit_data_import, import_payload, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    first = submit_data_import(key="import-conflict")
    assert first.status_code in (201, 202), first.text
    changed = submit_data_import(
        key="import-conflict", payload={**import_payload, "end_date": "2024-03-01"}
    )
    assert changed.status_code == 409, changed.text
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(first.json()["id"]))
        assert record.request["end_date"] == import_payload["end_date"]
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_concurrent_import_replays_create_exactly_one_durable_job(
    submit_data_import, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    barrier = Barrier(4)

    def submit(_):
        barrier.wait(timeout=10)
        return submit_data_import(key="simultaneous-import")

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(submit, range(4)))
    assert all(r.status_code in (200, 201, 202) for r in responses), [
        (r.status_code, r.text) for r in responses
    ]
    assert len({r.json()["id"] for r in responses}) == 1
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_import_idempotency_is_scoped_to_authenticated_owner(
    submit_data_import, other_headers, migrated_engine
):
    from backend.app.models import DataImport

    first = submit_data_import(key="same-key")
    second = submit_data_import(key="same-key", headers=other_headers)
    assert first.status_code in (201, 202), first.text
    assert second.status_code in (201, 202), second.text
    assert first.json()["id"] != second.json()["id"]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 2


@pytest.mark.parametrize("key", [None, "", "x" * 129])
def test_import_requires_a_bounded_idempotency_key(
    api, owner_headers, import_payload, migrated_engine, key
):
    from backend.app.models import DataImport, DataOutbox

    headers = dict(owner_headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = api.post("/api/v1/data/imports", headers=headers, json=import_payload)
    assert response.status_code in (400, 422), response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"symbols": []},
        {"symbols": ["AAPL", "AAPL"]},
        {"listing_ids": ["00000000-0000-0000-0000-000000000001"]},
        {"start_date": "2024-04-01"},
        {"end_date": "2023-01-01"},
        {"start_date": "2000-01-01"},
        {"provider": "arbitrary-command"},
        {"owner_id": "00000000-0000-0000-0000-000000000001"},
    ],
)
def test_invalid_import_does_not_create_durable_partial_state(
    submit_data_import, import_payload, migrated_engine, changes
):
    from backend.app.models import DataImport, DataOutbox

    response = submit_data_import(payload={**import_payload, **changes})
    assert response.status_code in (400, 422), response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_resolution_is_a_durable_queued_job_not_inline_provider_work(
    api, owner_headers, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    response = api.post(
        "/api/v1/data/resolve",
        headers={**owner_headers, "Idempotency-Key": "resolve-durably"},
        json={"provider": "yfinance", "market": "US", "symbols": ["AAPL"]},
    )
    assert response.status_code in (201, 202), response.text
    body = response.json()
    assert body["kind"] == "resolve"
    assert body["status"] == "requested"
    assert body["listing_ids"] == []
    assert body["dataset_version_id"] is None
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(body["id"]))
        assert record.kind == "resolve"
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_import_details_and_actions_enforce_ownership(
    api, owner_headers, other_headers, queued_import, migrated_engine
):
    from backend.app.models import DataImport

    path = f"/api/v1/data/imports/{queued_import['id']}"
    detail = api.get(path, headers=owner_headers)
    assert detail.status_code == 200, detail.text
    assert "lease_token" not in detail.json()
    assert "storage_key" not in detail.text
    assert api.get(path, headers=other_headers).status_code == 404
    assert api.get(path).status_code == 401
    for action in ("cancel", "retry"):
        response = api.post(f"{path}/{action}", headers=other_headers)
        assert response.status_code == 404, response.text
        assert api.post(f"{path}/{action}").status_code == 401
    listing = api.get("/api/v1/data/imports", headers=other_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"] == []
    assert listing.json()["count"] == 0
    with Session(migrated_engine) as session:
        assert session.get(DataImport, UUID(queued_import["id"])).status == "requested"


def test_concurrent_claims_grant_only_one_current_lease(
    migrated_engine, settings, queued_import
):
    from backend.app.data_services import claim_import
    from backend.app.models import DataImport

    import_id = UUID(queued_import["id"])
    barrier = Barrier(4)

    def claim(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            token = claim_import(session, import_id, 1, settings)
            session.commit()
            return token

    with ThreadPoolExecutor(max_workers=4) as executor:
        tokens = list(executor.map(claim, range(4)))
    granted = [token for token in tokens if token is not None]
    assert len(granted) == 1
    with Session(migrated_engine) as session:
        record = session.get(DataImport, import_id)
        assert record.lease_token == granted[0]
        assert record.status == "fetching"


@pytest.mark.parametrize("generation", [0, 2])
def test_stale_or_future_generation_cannot_claim(
    migrated_engine, settings, queued_import, generation
):
    from backend.app.data_services import claim_import
    from backend.app.models import DataImport

    with Session(migrated_engine) as session:
        assert (
            claim_import(session, UUID(queued_import["id"]), generation, settings)
            is None
        )
        session.commit()
        assert session.get(DataImport, UUID(queued_import["id"])).lease_token is None


def test_queued_cancel_is_idempotent_and_prevents_late_delivery(
    api, owner_headers, queued_import, migrated_engine, settings
):
    from backend.app.data_services import claim_import

    path = f"/api/v1/data/imports/{queued_import['id']}/cancel"
    first = api.post(path, headers=owner_headers)
    second = api.post(path, headers=owner_headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["status"] == second.json()["status"] == "cancelled"
    with Session(migrated_engine) as session:
        assert claim_import(session, UUID(queued_import["id"]), 1, settings) is None


def test_cancel_immediately_fences_running_publication(
    api, owner_headers, migrated_engine, settings, claimed_import
):
    from backend.app.data_services import fence_import
    from backend.app.services import FenceLost

    import_id, token = claimed_import
    response = api.post(
        f"/api/v1/data/imports/{import_id}/cancel", headers=owner_headers
    )
    assert response.status_code == 202, response.text
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        fence_import(session, import_id, 1, token, settings)


@pytest.mark.parametrize("wrong", ["token", "generation", "expired"])
def test_invalid_or_expired_fence_cannot_renew_or_publish(
    migrated_engine, settings, claimed_import, wrong
):
    from backend.app.data_services import fence_import
    from backend.app.models import DataImport, utcnow
    from backend.app.services import FenceLost

    import_id, token = claimed_import
    if wrong == "expired":
        with Session(migrated_engine) as session:
            session.get(DataImport, import_id).lease_expires_at = utcnow() - timedelta(
                seconds=1
            )
            session.commit()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        fence_import(
            session,
            import_id,
            2 if wrong == "generation" else 1,
            uuid4() if wrong == "token" else token,
            settings,
        )


def test_retry_increments_generation_and_retains_checkpoint_metadata(
    api, owner_headers, migrated_engine, settings, claimed_import
):
    from backend.app.data_services import claim_import, fence_import
    from backend.app.models import DataBlock, DataImport, DataOutbox
    from backend.app.services import FenceLost

    import_id, old_token = claimed_import
    checksum = "a" * 64
    with Session(migrated_engine, expire_on_commit=False) as session:
        record = session.get(DataImport, import_id)
        record.status = "failed"
        record.error = "deterministic provider fixture interruption"
        record.lease_token = None
        record.lease_expires_at = None
        checkpoint = DataBlock(
            import_id=import_id,
            symbol="AAPL",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 2, 1),
            status="ready",
            attempts=1,
            storage_key=f"data/checkpoints/{import_id}/completed.json",
            content_sha256=checksum,
            coverage=[{"status": "complete"}],
        )
        session.add(checkpoint)
        session.commit()
    response = api.post(
        f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers
    )
    assert response.status_code == 202, response.text
    assert response.json()["generation"] == 2
    assert response.json()["status"] == "requested"
    with Session(migrated_engine) as session:
        assert claim_import(session, import_id, 1, settings) is None
        new_token = claim_import(session, import_id, 2, settings)
        assert new_token is not None and new_token != old_token
        session.commit()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        fence_import(session, import_id, 1, old_token, settings)
    with Session(migrated_engine) as session:
        saved = session.get(DataBlock, checkpoint.id)
        assert saved.status == "ready"
        assert saved.content_sha256 == checksum
        assert saved.attempts == 1
        assert session.scalars(
            select(DataOutbox.generation)
            .where(DataOutbox.import_id == import_id)
            .order_by(DataOutbox.generation)
        ).all() == [1, 2]


def test_concurrent_retry_creates_one_new_generation(
    migrated_engine, settings, owner, queued_import
):
    from backend.app.data_services import cancel_import, retry_import
    from backend.app.models import DataImport, DataOutbox
    from backend.app.services import DomainError

    import_id = UUID(queued_import["id"])
    with Session(migrated_engine) as session:
        cancel_import(session, import_id, owner.user.id)
        session.commit()
    barrier = Barrier(2)

    def retry(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            try:
                record = retry_import(session, import_id, owner.user.id, settings)
                session.commit()
                return record.generation
            except DomainError as error:
                session.rollback()
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(retry, range(2)))
    assert sorted(results) == [2, 409]
    with Session(migrated_engine) as session:
        assert session.get(DataImport, import_id).generation == 2
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 2


def test_expired_import_is_reconciled_once_and_old_worker_is_fenced(
    migrated_engine, settings, claimed_import
):
    from backend.app.data_services import claim_import, fence_import, reconcile_imports
    from backend.app.models import DataImport, DataOutbox, utcnow
    from backend.app.services import FenceLost

    import_id, old_token = claimed_import
    with Session(migrated_engine) as session:
        session.get(DataImport, import_id).lease_expires_at = utcnow() - timedelta(
            seconds=1
        )
        session.commit()
    with Session(migrated_engine) as session:
        assert reconcile_imports(session, settings) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert reconcile_imports(session, settings) == 0
        record = session.get(DataImport, import_id)
        assert record.status == "requested"
        assert record.generation == 2
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 2
        assert claim_import(session, import_id, 1, settings) is None
        assert claim_import(session, import_id, 2, settings) is not None
        session.commit()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        fence_import(session, import_id, 1, old_token, settings)


def test_live_import_lease_is_not_reconciled(migrated_engine, settings, claimed_import):
    from backend.app.data_services import reconcile_imports
    from backend.app.models import DataImport

    import_id, token = claimed_import
    with Session(migrated_engine) as session:
        assert reconcile_imports(session, settings) == 0
        session.commit()
        record = session.get(DataImport, import_id)
        assert record.generation == 1
        assert record.lease_token == token


def test_data_dispatch_is_transactional_and_replay_safe(migrated_engine, queued_import):
    from backend.app.models import DataOutbox
    from workers.data.dispatcher import dispatch_data_outbox

    with Session(migrated_engine) as session:
        assert dispatch_data_outbox(session) == 1
        session.rollback()
    with Session(migrated_engine) as session:
        event = session.scalar(select(DataOutbox))
        assert event.dispatched_at is None
        assert event.job_id is None
        assert session.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 0
        assert dispatch_data_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert dispatch_data_outbox(session) == 0
        event = session.scalar(select(DataOutbox))
        jobs = (
            session.execute(text("SELECT id, args, status FROM procrastinate_jobs"))
            .mappings()
            .all()
        )
        assert len(jobs) == 1
        assert event.dispatched_at is not None
        assert event.job_id == jobs[0]["id"]
        assert set(jobs[0]["args"]) == {"import_id", "generation"}
        assert jobs[0]["args"]["import_id"] == queued_import["id"]
        assert jobs[0]["args"]["generation"] == 1
        assert jobs[0]["status"] == "todo"


def test_concurrent_data_dispatchers_enqueue_once(migrated_engine, queued_import):
    from workers.data.dispatcher import dispatch_data_outbox

    barrier = Barrier(2)

    def dispatch(_):
        with Session(migrated_engine) as session:
            barrier.wait(timeout=10)
            result = dispatch_data_outbox(session)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, range(2)))
    assert sorted(results) == [0, 1]
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT count(*) FROM procrastinate_jobs")) == 1


def test_stale_dispatched_delivery_cannot_claim_retried_generation(
    migrated_engine, settings, owner, queued_import
):
    from backend.app.data_services import cancel_import, claim_import, retry_import
    from workers.data.dispatcher import dispatch_data_outbox

    import_id = UUID(queued_import["id"])
    with Session(migrated_engine) as session:
        assert dispatch_data_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        cancel_import(session, import_id, owner.user.id)
        retry_import(session, import_id, owner.user.id, settings)
        session.commit()
    with Session(migrated_engine) as session:
        assert dispatch_data_outbox(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert claim_import(session, import_id, 1, settings) is None
        assert claim_import(session, import_id, 2, settings) is not None
        session.commit()
        jobs = (
            session.execute(text("SELECT args FROM procrastinate_jobs ORDER BY id"))
            .mappings()
            .all()
        )
        assert [item["args"]["generation"] for item in jobs] == [1, 2]


def test_data_queue_crash_before_claim_is_transactionally_redelivered_once(
    migrated_engine, settings, queued_import
):
    from backend.app.data_services import claim_import
    from backend.app.models import DataImport
    from workers.data.dispatcher import dispatch_data_outbox, recover_stalled_data_jobs

    import_id = UUID(queued_import["id"])
    with Session(migrated_engine) as session:
        assert dispatch_data_outbox(session) == 1
        worker = session.scalar(
            text("SELECT worker_id FROM procrastinate_register_worker_v1()")
        )
        job_id = session.scalar(
            text(
                "SELECT (procrastinate_fetch_job_v2(ARRAY['data']::varchar[], :worker)).id"
            ),
            {"worker": worker},
        )
        assert job_id is not None
        session.execute(
            text(
                "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes' WHERE id = :worker"
            ),
            {"worker": worker},
        )
        session.commit()
    with Session(migrated_engine) as session:
        assert session.get(DataImport, import_id).status == "requested"
        assert recover_stalled_data_jobs(session) == 1
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.scalar(text("SELECT status FROM procrastinate_jobs")) == "doing"
        assert recover_stalled_data_jobs(session) == 1
        session.commit()
    with Session(migrated_engine) as session:
        assert recover_stalled_data_jobs(session) == 0
        row = session.execute(
            text("SELECT status, attempts FROM procrastinate_jobs")
        ).one()
        assert tuple(row) == ("todo", 1)
        assert claim_import(session, import_id, 1, settings) is not None
        session.commit()
    with Session(migrated_engine) as session:
        assert claim_import(session, import_id, 1, settings) is None


def test_reconciliation_rollback_does_not_advance_generation_or_enqueue(
    migrated_engine, settings, claimed_import
):
    from backend.app.data_services import reconcile_imports
    from backend.app.models import DataImport, DataOutbox, utcnow

    import_id, token = claimed_import
    with Session(migrated_engine) as session:
        session.get(DataImport, import_id).lease_expires_at = utcnow() - timedelta(
            seconds=1
        )
        session.commit()
    with Session(migrated_engine) as session:
        assert reconcile_imports(session, settings) == 1
        session.rollback()
    with Session(migrated_engine) as session:
        original = session.get(DataImport, import_id)
        assert original.generation == 1
        assert original.lease_token == token
        assert original.status == "fetching"
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_retry_respects_the_same_active_import_quota_as_submission(
    api, owner_headers, submit_data_import, settings, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    settings.max_active_imports_per_user = 1
    first = submit_data_import()
    assert first.status_code == 202, first.text
    import_id = first.json()["id"]
    cancelled = api.post(
        f"/api/v1/data/imports/{import_id}/cancel", headers=owner_headers
    )
    assert cancelled.status_code == 202, cancelled.text
    second = submit_data_import()
    assert second.status_code == 202, second.text
    retry = api.post(f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers)
    assert retry.status_code == 429, retry.text
    with Session(migrated_engine) as session:
        first_record = session.get(DataImport, UUID(import_id))
        assert first_record.generation == 1
        assert first_record.status == "cancelled"
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 2


def test_concurrent_submission_and_retry_cannot_exceed_import_quota(
    api, owner_headers, submit_data_import, settings, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox

    settings.max_active_imports_per_user = 1
    first = submit_data_import()
    assert first.status_code == 202, first.text
    import_id = first.json()["id"]
    cancelled = api.post(
        f"/api/v1/data/imports/{import_id}/cancel", headers=owner_headers
    )
    assert cancelled.status_code == 202, cancelled.text
    barrier = Barrier(2)

    def compete(action):
        barrier.wait(timeout=10)
        if action == "submit":
            return submit_data_import()
        return api.post(
            f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(compete, ["submit", "retry"]))
    assert sorted(item.status_code for item in responses) == [202, 429], [
        (item.status_code, item.text) for item in responses
    ]
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataImport)
                .where(
                    DataImport.status.not_in(
                        {"ready", "partial", "failed", "cancelled"}
                    )
                )
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 2


@pytest.fixture
def published_data_fixture(migrated_engine, settings, owner, import_payload):
    """Small frozen JSON preview plus durable metadata; not a provider simulation."""
    from types import SimpleNamespace

    from backend.app.models import DatasetVersion

    dataset_id, listing_id = uuid4(), uuid4()
    storage_key = f"datasets/{dataset_id}"
    relative_path = "preview/2024-01.json"
    path = settings.storage_root / storage_key / relative_path
    path.parent.mkdir(parents=True)
    rows = [
        {
            "listing_id": str(listing_id),
            "session_date": f"2024-01-0{day}",
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1000.0,
            "currency": "USD",
            "volume_unit": "shares",
            "flags": [],
            "adj_close": None,
            "amount": None,
        }
        for day in (2, 3, 4)
    ]
    content = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(content)
    coverage = [
        {
            "listing_id": str(listing_id),
            "status": "partial",
            "requested_start": "2024-01-01",
            "requested_end": "2024-04-01",
            "actual_start": "2024-01-02",
            "actual_end": "2024-01-05",
            "missing_sessions": ["2024-01-05"],
            "calendar": "XNYS",
        }
    ]
    manifest = {
        "schema_version": "marketdata.v1",
        "import_request": import_payload,
        "preview_files": {
            str(listing_id): [
                {
                    "path": relative_path,
                    "sha256": sha256(content).hexdigest(),
                    "start_date": "2024-01-01",
                    "end_date": "2024-02-01",
                }
            ]
        },
        "coverage": coverage,
        "quality": {"status": "partial"},
        "provenance": [{"provider": "integration_fixture", "evidence": "fixture"}],
        "universe": {"state": "user_fixed"},
    }
    digest = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    with Session(migrated_engine, expire_on_commit=False) as session:
        dataset = DatasetVersion(
            id=dataset_id,
            owner_id=owner.user.id,
            name="Synthetic preview fixture",
            market="US",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 4, 1),
            source="yfinance",
            is_synthetic=True,
            content_sha256=digest,
            manifest=manifest,
            storage_key=storage_key,
        )
        session.add(dataset)
        session.commit()
    return SimpleNamespace(
        dataset=dataset,
        listing_id=listing_id,
        path=path,
        content=content,
        rows=rows,
        coverage=coverage,
    )


def test_preview_uses_half_open_dates_and_bounded_pagination(
    api, owner_headers, published_data_fixture
):
    fixture = published_data_fixture
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/preview",
        headers=owner_headers,
        params={
            "listing_id": str(fixture.listing_id),
            "start_date": "2024-01-02",
            "end_date": "2024-01-04",
            "offset": 1,
            "limit": 1,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert body["offset"] == body["limit"] == 1
    assert body["data"] == [fixture.rows[1]]
    assert "storage_key" not in response.text
    assert str(fixture.path) not in response.text


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 501},
        {"limit": 0},
        {"offset": -1},
        {"offset": 100001},
        {"start_date": "2024-01-04", "end_date": "2024-01-04"},
    ],
)
def test_preview_rejects_unbounded_or_invalid_windows(
    api, owner_headers, published_data_fixture, params
):
    fixture = published_data_fixture
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/preview",
        headers=owner_headers,
        params={"listing_id": str(fixture.listing_id), **params},
    )
    assert response.status_code == 422, response.text


def test_preview_unknown_listing_does_not_scan_other_data(
    api, owner_headers, published_data_fixture
):
    response = api.get(
        f"/api/v1/datasets/{published_data_fixture.dataset.id}/preview",
        headers=owner_headers,
        params={"listing_id": str(uuid4())},
    )
    assert response.status_code == 404, response.text


def test_corrupt_preview_bytes_fail_closed(api, owner_headers, published_data_fixture):
    fixture = published_data_fixture
    fixture.path.write_bytes(b"tampered-private-preview-content")
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/preview",
        headers=owner_headers,
        params={"listing_id": str(fixture.listing_id)},
    )
    assert response.status_code == 409, response.text
    assert "tampered-private-preview-content" not in response.text


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_preview_storage_traversal_is_rejected(
    api, owner_headers, published_data_fixture, migrated_engine, settings, escape
):
    from backend.app.models import DatasetVersion

    fixture = published_data_fixture
    outside = settings.storage_root.parent / "outside-preview.json"
    outside.write_bytes(fixture.content)
    if escape == "symlink":
        fixture.path.unlink()
        fixture.path.symlink_to(outside)
    else:
        with Session(migrated_engine) as session:
            dataset = session.get(DatasetVersion, fixture.dataset.id)
            dataset.storage_key = (
                "../outside" if escape == "parent" else str(outside.parent)
            )
            session.commit()
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/preview",
        headers=owner_headers,
        params={"listing_id": str(fixture.listing_id)},
    )
    assert response.status_code == 500, response.text
    assert response.json()["code"] == "unsafe_storage_path"
    assert str(outside) not in response.text
    assert fixture.content not in response.content


def test_preview_verifies_listing_identity_after_checksum(
    api, owner_headers, published_data_fixture, migrated_engine
):
    from backend.app.models import DatasetVersion

    fixture = published_data_fixture
    malformed = [{**fixture.rows[0], "listing_id": str(uuid4())}]
    content = json.dumps(malformed).encode()
    fixture.path.write_bytes(content)
    with Session(migrated_engine) as session:
        dataset = session.get(DatasetVersion, fixture.dataset.id)
        manifest = dict(dataset.manifest)
        refs = manifest["preview_files"][str(fixture.listing_id)]
        refs[0]["sha256"] = sha256(content).hexdigest()
        dataset.manifest = manifest
        # Deep JSON changes must be assigned as a fresh value for SQLAlchemy.
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(dataset, "manifest")
        session.commit()
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/preview",
        headers=owner_headers,
        params={"listing_id": str(fixture.listing_id)},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "snapshot_integrity"


def test_coverage_preserves_partial_and_non_pit_evidence(
    api, owner_headers, published_data_fixture
):
    fixture = published_data_fixture
    response = api.get(
        f"/api/v1/datasets/{fixture.dataset.id}/coverage", headers=owner_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["coverage"] == fixture.coverage
    assert body["quality"]["status"] == "partial"
    assert body["universe"]["state"] == "user_fixed"
    assert body["provenance"][0]["evidence"] == "fixture"


def test_refresh_creates_new_job_with_immutable_parent_and_deduplicates(
    api, owner_headers, published_data_fixture, migrated_engine
):
    from backend.app.models import DataImport, DataOutbox, DatasetVersion

    fixture = published_data_fixture
    path = f"/api/v1/datasets/{fixture.dataset.id}/refresh"
    headers = {**owner_headers, "Idempotency-Key": "refresh-existing-snapshot"}
    first = api.post(path, headers=headers)
    second = api.post(path, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["parent_version_id"] == str(fixture.dataset.id)
    assert first.json()["dataset_version_id"] is None
    with Session(migrated_engine) as session:
        original = session.get(DatasetVersion, fixture.dataset.id)
        assert original.content_sha256 == fixture.dataset.content_sha256
        assert original.manifest == fixture.dataset.manifest
        assert original.storage_key == fixture.dataset.storage_key
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1
    assert fixture.path.read_bytes() == fixture.content


@pytest.fixture
def upload_mapping():
    return {
        "name": "Explicit uploaded fixture",
        "symbol": "LOCAL",
        "market": "US",
        "mic": "XNAS",
        "currency": "USD",
        "timezone": "America/New_York",
        "calendar": "XNYS",
        "start_date": "2024-01-01",
        "end_date": "2024-04-01",
        "columns": {
            "session_date": "Date",
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        },
        "volume_unit": "shares",
        "price_semantics": {
            "native_mode": "user_declared",
            "split_basis": "unknown",
            "dividend_basis": "unknown",
            "note": "Unknown adjustment evidence",
        },
        "unknown_flags": ["price_adjustment_unknown"],
    }


CSV_BYTES = b"Date,Open,High,Low,Close,Volume\n2024-01-02,10,11,9,10.5,1000\n"


def test_upload_is_private_durable_and_idempotent(
    api, owner_headers, upload_mapping, migrated_engine, settings, owner
):
    from backend.app.models import DataImport, DataOutbox

    def upload():
        return api.post(
            "/api/v1/data/uploads",
            headers={**owner_headers, "Idempotency-Key": "same-upload"},
            data={"mapping": json.dumps(upload_mapping)},
            files={"file": ("../../untrusted-name.csv", CSV_BYTES, "text/csv")},
        )

    first, second = upload(), upload()
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["kind"] == "upload"
    assert first.json()["dataset_version_id"] is None
    assert "storage_key" not in first.text
    with Session(migrated_engine) as session:
        record = session.get(DataImport, UUID(first.json()["id"]))
        assert record.owner_id == owner.user.id
        stored = record.request["upload"]
        assert stored["sha256"] == sha256(CSV_BYTES).hexdigest()
        target = (settings.storage_root / stored["storage_key"]).resolve()
        assert target.is_relative_to(settings.storage_root.resolve())
        assert target.read_bytes() == CSV_BYTES
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


@pytest.mark.parametrize(
    "invalid", ["type", "empty", "oversized", "mapping", "units", "timezone"]
)
def test_invalid_upload_does_not_enqueue_work(
    api, owner_headers, upload_mapping, migrated_engine, settings, invalid
):
    from backend.app.models import DataImport, DataOutbox

    filename = "fixture.exe" if invalid == "type" else "fixture.csv"
    content = CSV_BYTES
    settings.max_upload_bytes = 1024
    if invalid == "empty":
        content = b""
    elif invalid == "oversized":
        content = b"x" * 1025
    elif invalid == "units":
        upload_mapping["volume_unit"] = "unknown"
        upload_mapping["unknown_flags"] = []
    elif invalid == "timezone":
        upload_mapping["timezone"] = "Not/A_Real_Zone"
    mapping = "{" if invalid == "mapping" else json.dumps(upload_mapping)
    response = api.post(
        "/api/v1/data/uploads",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        data={"mapping": mapping},
        files={"file": (filename, content, "text/csv")},
    )
    assert response.status_code == (
        413 if invalid in {"empty", "oversized"} else 422
    ), response.text
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0


def test_upload_idempotency_conflict_does_not_replace_original_bytes(
    api, owner_headers, upload_mapping, migrated_engine, settings
):
    from backend.app.models import DataImport, DataOutbox

    responses = [
        api.post(
            "/api/v1/data/uploads",
            headers={**owner_headers, "Idempotency-Key": "conflicting-upload"},
            data={"mapping": json.dumps(upload_mapping)},
            files={"file": ("fixture.csv", content, "text/csv")},
        )
        for content in (CSV_BYTES, CSV_BYTES.replace(b"1000", b"2000"))
    ]
    assert responses[0].status_code == 202, responses[0].text
    assert responses[1].status_code == 409, responses[1].text
    with Session(migrated_engine) as session:
        record = session.scalar(select(DataImport))
        assert (
            settings.storage_root / record.request["upload"]["storage_key"]
        ).read_bytes() == CSV_BYTES
        assert session.scalar(select(func.count()).select_from(DataImport)) == 1
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


@pytest.fixture
def settings(settings):
    # The API and data worker share an explicit immutable fixture execution policy.
    return settings.model_copy(
        update={"data_provider_mode": "fixture", "data_block_days": 2}
    )


@pytest.fixture
def data_worker(migrated_engine, settings):
    """Run the actual worker against real SQL and explicit synthetic SDK fixtures.

    Data-runtime dependencies are mandatory when this PostgreSQL gate is enabled.
    Do not skip missing packages or fall back to a mocked worker/provider.
    """
    from types import SimpleNamespace

    from sqlalchemy.orm import sessionmaker

    from workers.data.executor import execute

    worker_settings = settings.model_copy(
        update={
            "data_provider_mode": "fixture",
            "data_block_days": 2,
        }
    )
    factory = sessionmaker(migrated_engine, expire_on_commit=False)

    def run(import_id, generation=1):
        execute(import_id, generation, settings=worker_settings, factory=factory)

    return SimpleNamespace(run=run, settings=worker_settings, factory=factory)


@pytest.mark.parametrize(
    "provider,market,symbol",
    [("akshare", "CN", "600000"), ("yfinance", "US", "AAPL"), ("openbb", "US", "AAPL")],
)
def test_real_data_worker_publishes_complete_immutable_fixture_snapshots(
    api,
    owner_headers,
    submit_data_import,
    import_payload,
    data_worker,
    migrated_engine,
    provider,
    market,
    symbol,
):
    from backend.app.models import DataBlock, DataImport, DatasetVersion
    from packages.marketdata.storage import verify_bundle

    response = submit_data_import(
        payload={
            **import_payload,
            "provider": provider,
            "market": market,
            "symbols": [symbol],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        }
    )
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    data_worker.run(import_id)
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "ready", (item.status, item.error, item.progress)
        assert item.dataset_version_id is not None
        dataset = session.get(DatasetVersion, item.dataset_version_id)
        assert dataset.is_synthetic is True
        assert dataset.manifest["universe"]["state"] == "user_fixed"
        assert all(p["evidence"] == "fixture" for p in dataset.manifest["provenance"])
        assert all(c["status"] == "complete" for c in dataset.manifest["coverage"])
        snapshot = data_worker.settings.storage_root / dataset.storage_key
        assert (
            sha256((snapshot / "manifest.json").read_bytes()).hexdigest()
            == dataset.content_sha256
        )
        verify_bundle(snapshot, dataset.manifest)
        assert (snapshot / "normalized.parquet").is_file()
        assert (snapshot / "raw.json").is_file()
        blocks = session.scalars(
            select(DataBlock).where(DataBlock.import_id == import_id)
        ).all()
        assert len(blocks) == 2
        assert all(block.status == "ready" and block.attempts == 1 for block in blocks)
        for block in blocks:
            stored = data_worker.settings.storage_root / block.storage_key
            assert sha256(stored.read_bytes()).hexdigest() == block.content_sha256
        dataset_id = dataset.id
        listing_id = item.listing_ids[0]
    preview = api.get(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=owner_headers,
        params={"listing_id": listing_id, "limit": 2},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["count"] == 4
    assert len(preview.json()["data"]) == 2
    # Queue replay cannot republish or mutate the completed snapshot.
    data_worker.run(import_id)
    with Session(migrated_engine) as session:
        assert session.get(DataImport, import_id).dataset_version_id == dataset_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(DatasetVersion)
                .where(DatasetVersion.source == provider)
            )
            == 1
        )


@pytest.mark.parametrize("failure", ["missing-session", "unresolved-security"])
def test_worker_never_publishes_incomplete_or_silently_shrunk_dataset(
    api,
    owner_headers,
    submit_data_import,
    import_payload,
    data_worker,
    migrated_engine,
    failure,
):
    from backend.app.models import DataBlock, DataImport, DatasetVersion

    payload = {
        **import_payload,
        "start_date": "2024-01-02",
        "end_date": "2024-01-06",
    }
    if failure == "missing-session":
        # Explicit synthetic provider fixtures end at the exclusive 2024-07-01.
        payload["start_date"] = "2024-07-01"
        payload["end_date"] = "2024-07-03"
    else:
        payload["symbols"] = ["AAPL", "UNRESOLVED"]
    response = submit_data_import(payload=payload)
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    data_worker.run(import_id)
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "partial", (item.status, item.error, item.progress)
        assert item.dataset_version_id is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(DatasetVersion)
                .where(DatasetVersion.source == "yfinance")
            )
            == 0
        )
        if failure == "missing-session":
            blocks = session.scalars(
                select(DataBlock).where(DataBlock.import_id == import_id)
            ).all()
            assert any(block.status in {"partial", "failed"} for block in blocks)
        else:
            assert item.progress["unresolved"] == ["UNRESOLVED"]
            assert item.request["symbols"] == payload["symbols"]
            assert len(item.listing_ids) == 1
    retry = api.post(f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers)
    assert retry.status_code == 202, retry.text
    data_worker.run(import_id, generation=2)
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "partial", (item.status, item.error, item.progress)
        assert item.generation == 2
        assert item.dataset_version_id is None
        assert item.request["symbols"] == payload["symbols"]
        if failure == "unresolved-security":
            # A partial listing_ids cache cannot replace the original requested basket.
            assert item.progress["unresolved"] == ["UNRESOLVED"]
            assert len(item.listing_ids) == 1


@pytest.fixture
def checkpointed_worker_import(submit_data_import, import_payload, data_worker):
    """Reach the real worker's durable pre-publication checkpoint boundary."""
    from backend.app.data_services import claim_import
    from packages.marketdata.providers.registry import get_provider
    from workers.data.executor import _fetch_blocks, _resolve

    response = submit_data_import(
        payload={
            **import_payload,
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        }
    )
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    with data_worker.factory.begin() as session:
        token = claim_import(session, import_id, 1, data_worker.settings)
    provider = get_provider("yfinance", mode="fixture")
    listings, unresolved = _resolve(
        data_worker.factory, import_id, 1, token, data_worker.settings, provider
    )
    assert unresolved == []
    results, failed = _fetch_blocks(
        data_worker.factory,
        import_id,
        1,
        token,
        data_worker.settings,
        provider,
        listings,
    )
    assert failed == 0
    assert len(results) == 2
    return import_id, token, listings, results


@pytest.mark.parametrize("deployed_block_days", [2, 17])
def test_worker_retry_reuses_verified_checkpoint_bytes_without_refetch(
    api,
    owner_headers,
    checkpointed_worker_import,
    data_worker,
    migrated_engine,
    deployed_block_days,
):
    from backend.app.models import DataBlock, DataImport
    from workers.data.executor import _set_status

    import_id, token, _, _ = checkpointed_worker_import
    with Session(migrated_engine) as session:
        blocks = session.scalars(
            select(DataBlock).where(DataBlock.import_id == import_id)
        ).all()
        previous = {
            block.id: (
                block.attempts,
                block.storage_key,
                block.content_sha256,
                (data_worker.settings.storage_root / block.storage_key).read_bytes(),
                (data_worker.settings.storage_root / block.storage_key)
                .stat()
                .st_mtime_ns,
            )
            for block in blocks
        }
    _set_status(
        data_worker.factory,
        import_id,
        1,
        token,
        data_worker.settings,
        "failed",
        error="Simulated interruption after committed checkpoints and before publication",
    )
    retry = api.post(f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers)
    assert retry.status_code == 202, retry.text
    from workers.data.executor import execute

    execute(
        import_id,
        2,
        settings=data_worker.settings.model_copy(
            update={"data_block_days": deployed_block_days}
        ),
        factory=data_worker.factory,
    )
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "ready", (item.status, item.error, item.progress)
        current = session.scalars(
            select(DataBlock).where(DataBlock.import_id == import_id)
        ).all()
        assert {block.id for block in current} == set(previous)
        for block in current:
            path = data_worker.settings.storage_root / block.storage_key
            assert (
                block.attempts,
                block.storage_key,
                block.content_sha256,
                path.read_bytes(),
                path.stat().st_mtime_ns,
            ) == previous[block.id]


def test_cancelled_worker_checkpoint_cannot_publish_late_snapshot(
    api, owner_headers, checkpointed_worker_import, data_worker, migrated_engine
):
    from backend.app.models import DataImport, DatasetVersion
    from backend.app.services import FenceLost
    from workers.data.executor import _publish

    import_id, token, listings, results = checkpointed_worker_import
    cancel = api.post(f"/api/v1/data/imports/{import_id}/cancel", headers=owner_headers)
    assert cancel.status_code == 202, cancel.text
    with pytest.raises(FenceLost):
        _publish(
            data_worker.factory,
            import_id,
            1,
            token,
            data_worker.settings,
            listings,
            results,
        )
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "cancelled"
        assert item.dataset_version_id is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(DatasetVersion)
                .where(DatasetVersion.source == "yfinance")
            )
            == 0
        )


@pytest.mark.parametrize("format_name", ["csv", "parquet"])
def test_actual_upload_normalization_preserves_unknown_semantics_and_blocks_evaluation(
    api, owner_headers, upload_mapping, data_worker, migrated_engine, format_name
):
    import io

    from backend.app.models import DataImport, DatasetVersion, Listing

    upload_mapping.update(start_date="2024-01-02", end_date="2024-01-03")
    content = CSV_BYTES
    if format_name == "parquet":
        import pandas as pd

        stream = io.BytesIO()
        pd.read_csv(io.BytesIO(CSV_BYTES)).to_parquet(stream, index=False)
        content = stream.getvalue()
    response = api.post(
        "/api/v1/data/uploads",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        data={"mapping": json.dumps(upload_mapping)},
        files={"file": (f"fixture.{format_name}", content, "application/octet-stream")},
    )
    assert response.status_code == 202, response.text
    import_id = UUID(response.json()["id"])
    data_worker.run(import_id)
    with Session(migrated_engine) as session:
        item = session.get(DataImport, import_id)
        assert item.status == "ready", (item.status, item.error)
        dataset = session.get(DatasetVersion, item.dataset_version_id)
        listing = session.get(Listing, UUID(item.listing_ids[0]))
        assert listing.metadata_json["identity_status"] == "user_supplied"
        assert dataset.source == "upload" and dataset.is_synthetic is False
        assert dataset.manifest["quality"]["unknown_flags"] == [
            "price_adjustment_unknown"
        ]
        assert dataset.manifest["evaluation_compatibility"]["compatible"] is False
        assert dataset.manifest["files"] == {}
        assert dataset.manifest["price_semantics"]["split_basis"] == "unknown"
        assert (
            data_worker.settings.storage_root
            / dataset.storage_key
            / "normalized.parquet"
        ).is_file()
        assert all(p["evidence"] == "uploaded" for p in dataset.manifest["provenance"])
        dataset_id, listing_id = dataset.id, listing.id
    preview = api.get(
        f"/api/v1/datasets/{dataset_id}/preview",
        headers=owner_headers,
        params={"listing_id": str(listing_id)},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["count"] == 1


def test_provider_refresh_publishes_new_version_without_retargeting_old_run(
    api,
    owner,
    owner_headers,
    submit_data_import,
    import_payload,
    data_worker,
    migrated_engine,
):
    from pathlib import Path

    from backend.app.models import (
        DataImport,
        DatasetVersion,
        ResearchRun,
        StrategyVersion,
    )

    response = submit_data_import(
        payload={**import_payload, "start_date": "2024-01-02", "end_date": "2024-01-06"}
    )
    assert response.status_code == 202, response.text
    data_worker.run(response.json()["id"])
    with Session(migrated_engine, expire_on_commit=False) as session:
        old = session.get(
            DatasetVersion,
            session.get(DataImport, UUID(response.json()["id"])).dataset_version_id,
        )
        original_hash, original_manifest = old.content_sha256, old.manifest
        original_path = data_worker.settings.storage_root / old.storage_key
        original_bytes = {
            str(p.relative_to(original_path)): p.read_bytes()
            for p in original_path.rglob("*")
            if p.is_file()
        }
        code = (
            Path(__file__).resolve().parents[2] / "seed_strategies/us_time.py"
        ).read_text()
        strategy = StrategyVersion(
            owner_id=owner.user.id,
            name="Original US seed snapshot pin test",
            task="us_time",
            code=code,
            content_sha256=sha256(code.encode()).hexdigest(),
            mode="native_akquant",
        )
        session.add(strategy)
        session.commit()
    run = api.post(
        "/api/v1/runs",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        json={
            "task": "us_time",
            "dataset_version_id": str(old.id),
            "strategy_version_id": str(strategy.id),
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        },
    )
    assert run.status_code == 202, run.text
    refresh = api.post(
        f"/api/v1/datasets/{old.id}/refresh",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
    )
    assert refresh.status_code == 202, refresh.text
    data_worker.run(refresh.json()["id"])
    with Session(migrated_engine) as session:
        refreshed = session.get(DataImport, UUID(refresh.json()["id"]))
        assert refreshed.status == "ready", (refreshed.status, refreshed.error)
        assert refreshed.dataset_version_id != old.id
        assert refreshed.parent_version_id == old.id
        new = session.get(DatasetVersion, refreshed.dataset_version_id)
        assert new.manifest["parent_version_id"] == str(old.id)
        pinned = session.get(ResearchRun, UUID(run.json()["id"]))
        assert pinned.dataset_version_id == old.id
        assert pinned.config["dataset_sha256"] == original_hash
        original = session.get(DatasetVersion, old.id)
        assert original.manifest == original_manifest
        assert original.content_sha256 == original_hash
    assert {
        str(p.relative_to(original_path)): p.read_bytes()
        for p in original_path.rglob("*")
        if p.is_file()
    } == original_bytes


def test_same_security_resolved_by_yahoo_and_openbb_shares_stable_listing_without_collision(
    api, owner_headers, import_payload, data_worker, migrated_engine
):
    from backend.app.models import DataImport, DatasetVersion, Listing, VendorMapping

    resolved_ids = []
    dataset_ids = []
    for provider in ["yfinance", "openbb"]:
        resolved = api.post(
            "/api/v1/data/resolve",
            headers={**owner_headers, "Idempotency-Key": uuid4().hex},
            json={"provider": provider, "market": "US", "symbols": ["AAPL"]},
        )
        assert resolved.status_code == 202, resolved.text
        data_worker.run(resolved.json()["id"])
        detail = api.get(
            f"/api/v1/data/imports/{resolved.json()['id']}", headers=owner_headers
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["status"] == "ready", detail.json()
        resolved_ids.append(detail.json()["listing_ids"][0])
        payload = {
            **import_payload,
            "provider": provider,
            "symbols": [],
            "listing_ids": detail.json()["listing_ids"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-06",
        }
        imported = api.post(
            "/api/v1/data/imports",
            headers={**owner_headers, "Idempotency-Key": uuid4().hex},
            json=payload,
        )
        assert imported.status_code == 202, imported.text
        data_worker.run(imported.json()["id"])
        with Session(migrated_engine) as session:
            job = session.get(DataImport, UUID(imported.json()["id"]))
            assert job.status == "ready", (job.status, job.error, job.progress)
            dataset = session.get(DatasetVersion, job.dataset_version_id)
            assert {p["connector"] for p in dataset.manifest["provenance"]} == {
                provider
            }
            assert dataset.manifest["listings"][0]["listing_id"] == resolved_ids[-1]
            dataset_ids.append(dataset.id)
    assert resolved_ids[0] == resolved_ids[1]
    assert dataset_ids[0] != dataset_ids[1]
    with Session(migrated_engine) as session:
        listing = session.get(Listing, UUID(resolved_ids[0]))
        assert listing.mic == "XNAS"
        assert {m["vendor"] for m in listing.metadata_json["vendor_symbols"]} == {
            "yfinance",
            "nasdaq",
        }
        assert session.scalar(select(func.count()).select_from(Listing)) == 1
        assert session.scalar(select(func.count()).select_from(VendorMapping)) == 2
        first = session.get(DatasetVersion, dataset_ids[0])
        assert {
            m["vendor"] for m in first.manifest["listings"][0]["vendor_symbols"]
        } == {"yfinance"}


def test_long_upload_is_parsed_once_and_publishes_exact_original_only_once(
    api, owner_headers, upload_mapping, data_worker, migrated_engine, monkeypatch
):
    from backend.app.models import DataBlock, DataImport, DatasetVersion
    from packages.marketdata import normalize
    from packages.marketdata.storage import verify_bundle

    upload_mapping.update(start_date="2024-01-02", end_date="2024-01-20")
    days = [
        "02",
        "03",
        "04",
        "05",
        "08",
        "09",
        "10",
        "11",
        "12",
        "16",
        "17",
        "18",
        "19",
    ]
    content = (
        "Date,Open,High,Low,Close,Volume\n"
        + "".join(f"2024-01-{day},10,12,9,11,100\n" for day in days)
    ).encode()
    real_normalize = normalize.normalize_upload
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs.get("source_reference"))
        return real_normalize(*args, **kwargs)

    monkeypatch.setattr(normalize, "normalize_upload", counted)
    response = api.post(
        "/api/v1/data/uploads",
        headers={**owner_headers, "Idempotency-Key": uuid4().hex},
        data={"mapping": json.dumps(upload_mapping)},
        files={"file": ("source.csv", content, "text/csv")},
    )
    assert response.status_code == 202, response.text
    data_worker.run(response.json()["id"])
    expected = {
        "artifact": "source.csv",
        "sha256": sha256(content).hexdigest(),
        "size_bytes": len(content),
    }
    assert calls == [expected]
    with Session(migrated_engine) as session:
        job = session.get(DataImport, UUID(response.json()["id"]))
        assert job.status == "ready", (job.status, job.error, job.progress)
        blocks = session.scalars(
            select(DataBlock).where(DataBlock.import_id == job.id)
        ).all()
        assert len(blocks) == 1
        assert blocks[0].start_date.isoformat() == "2024-01-02"
        assert blocks[0].end_date.isoformat() == "2024-01-20"
        checkpoint = json.loads(
            (data_worker.settings.storage_root / blocks[0].storage_key).read_text()
        )
        assert len(checkpoint["raw"]) == 1
        assert checkpoint["raw"][0]["content"] == expected
        dataset = session.get(DatasetVersion, job.dataset_version_id)
        root = data_worker.settings.storage_root / dataset.storage_key
        assert (root / "source.csv").read_bytes() == content
        assert dataset.manifest["upload_source"] == expected
        assert (
            dataset.manifest["artifact_files"]["source.csv"]
            == sha256(content).hexdigest()
        )
        raw = json.loads((root / "raw.json").read_text())
        assert len(raw) == 1 and raw[0]["content"] == expected
        assert not any("arrow" in str(item).lower() for item in raw)
        assert (root / "raw.json").stat().st_size < 2048
        verify_bundle(root)


def test_retry_rejects_changed_transport_before_fetch_or_checkpoint_reuse(
    api,
    owner_headers,
    checkpointed_worker_import,
    data_worker,
    migrated_engine,
    monkeypatch,
):
    from backend.app.models import DataBlock, DataImport, DatasetVersion
    from packages.marketdata.providers import registry
    from workers.data.executor import _set_status, execute

    import_id, token, _, _ = checkpointed_worker_import
    with Session(migrated_engine) as session:
        dataset_count = session.scalar(select(func.count()).select_from(DatasetVersion))
        prior = {
            block.id: (block.attempts, block.content_sha256, block.storage_key)
            for block in session.scalars(
                select(DataBlock).where(DataBlock.import_id == import_id)
            )
        }
    _set_status(
        data_worker.factory,
        import_id,
        1,
        token,
        data_worker.settings,
        "failed",
        error="Test interrupted checkpoint owner",
    )
    retry = api.post(f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers)
    assert retry.status_code == 202, retry.text

    def forbidden_provider(*args, **kwargs):
        raise AssertionError(
            "Transport mismatch must fail before constructing a live provider"
        )

    with monkeypatch.context() as patch:
        patch.setattr(registry, "get_provider", forbidden_provider)
        execute(
            import_id,
            2,
            settings=data_worker.settings.model_copy(
                update={"data_provider_mode": "live"}
            ),
            factory=data_worker.factory,
        )
    with Session(migrated_engine) as session:
        job = session.get(DataImport, import_id)
        assert job.status == "failed" and "execution policy" in job.error
        assert job.request["_execution"]["provider_mode"] == "fixture"
        assert (
            session.scalar(select(func.count()).select_from(DatasetVersion))
            == dataset_count
        )
        assert job.dataset_version_id is None
        assert {
            block.id: (block.attempts, block.content_sha256, block.storage_key)
            for block in session.scalars(
                select(DataBlock).where(DataBlock.import_id == import_id)
            )
        } == prior
    retry = api.post(f"/api/v1/data/imports/{import_id}/retry", headers=owner_headers)
    assert retry.status_code == 202, retry.text
    data_worker.run(import_id, generation=3)
    with Session(migrated_engine) as session:
        job = session.get(DataImport, import_id)
        assert job.status == "ready", (job.status, job.error)
        dataset = session.get(DatasetVersion, job.dataset_version_id)
        assert dataset.is_synthetic
        assert dataset.manifest["import_execution_policy"]["provider_mode"] == "fixture"


def test_import_block_detail_is_owner_scoped_and_server_bounded(
    api, owner_headers, other_headers, checkpointed_worker_import
):
    import_id, _, _, _ = checkpointed_worker_import
    first = api.get(
        f"/api/v1/data/imports/{import_id}",
        headers=owner_headers,
        params={"block_limit": 1},
    )
    second = api.get(
        f"/api/v1/data/imports/{import_id}",
        headers=owner_headers,
        params={"block_limit": 1, "block_offset": 1},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["blocks_total"] == second.json()["blocks_total"] == 2
    assert first.json()["blocks_offset"] == 0 and second.json()["blocks_offset"] == 1
    assert len(first.json()["blocks"]) == len(second.json()["blocks"]) == 1
    assert first.json()["blocks"][0]["id"] != second.json()["blocks"][0]["id"]
    assert (
        api.get(
            f"/api/v1/data/imports/{import_id}",
            headers=owner_headers,
            params={"block_limit": 1001},
        ).status_code
        == 422
    )
    assert (
        api.get(
            f"/api/v1/data/imports/{import_id}",
            headers=owner_headers,
            params={"block_offset": -1},
        ).status_code
        == 422
    )
    assert (
        api.get(
            f"/api/v1/data/imports/{import_id}",
            headers=other_headers,
            params={"block_limit": 1},
        ).status_code
        == 404
    )


def test_upload_interrupted_atomic_write_rolls_back_job_then_identical_retry_succeeds(
    api, owner_headers, upload_mapping, settings, migrated_engine, monkeypatch
):
    import errno

    from backend.app.core import atomic_storage
    from backend.app.models import DataImport, DataOutbox

    original = atomic_storage.os.write
    count = 0

    def interrupted(fd, value):
        nonlocal count
        count += 1
        if count == 1:
            return original(fd, value[:7])
        raise OSError(errno.ENOSPC, "Injected upload disk failure")

    arguments = {
        "headers": {**owner_headers, "Idempotency-Key": uuid4().hex},
        "data": {"mapping": json.dumps(upload_mapping)},
        "files": {"file": ("source.csv", CSV_BYTES, "text/csv")},
    }
    with monkeypatch.context() as patch:
        patch.setattr(atomic_storage.os, "write", interrupted)
        with pytest.raises(OSError, match="Injected"):
            api.post("/api/v1/data/uploads", **arguments)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataImport)) == 0
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 0
    assert not [
        p for p in (settings.storage_root / "uploads").rglob("*") if p.is_file()
    ]
    retried = api.post("/api/v1/data/uploads", **arguments)
    assert retried.status_code == 202, retried.text
    with Session(migrated_engine) as session:
        item = session.get(DataImport, UUID(retried.json()["id"]))
        assert (
            settings.storage_root / item.request["upload"]["storage_key"]
        ).read_bytes() == CSV_BYTES
        assert session.scalar(select(func.count()).select_from(DataOutbox)) == 1


def test_upload_idempotency_conflict_does_not_publish_orphan_content(
    api, owner_headers, upload_mapping, settings
):
    headers = {**owner_headers, "Idempotency-Key": uuid4().hex}
    arguments = {"headers": headers, "data": {"mapping": json.dumps(upload_mapping)}}
    first = api.post(
        "/api/v1/data/uploads",
        **arguments,
        files={"file": ("source.csv", CSV_BYTES, "text/csv")},
    )
    assert first.status_code == 202, first.text
    before = {p for p in (settings.storage_root / "uploads").rglob("*") if p.is_file()}
    changed = api.post(
        "/api/v1/data/uploads",
        **arguments,
        files={"file": ("source.csv", CSV_BYTES + b"\n", "text/csv")},
    )
    assert changed.status_code == 409, changed.text
    assert {
        p for p in (settings.storage_root / "uploads").rglob("*") if p.is_file()
    } == before
