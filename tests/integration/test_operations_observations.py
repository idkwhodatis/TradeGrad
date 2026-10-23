"""Prepared real PostgreSQL/API operational aggregate isolation and timing tests.

Collection only in restricted environments. No executor/provider/model calls.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.app.models import (
    DataImport,
    ExecutionAttempt,
    ModelCall,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    User,
    utcnow,
)
from backend.app.operations_schemas import BackupReceipt

pytestmark = pytest.mark.postgres


def seed_observations(engine, account):
    now = utcnow()
    with Session(engine) as session:
        run = ResearchRun(
            owner_id=account.user.id,
            idempotency_key=uuid4().hex,
            request_sha256="1" * 64,
            config={},
            dataset_version_id=account.dataset.id,
            strategy_version_id=account.strategy.id,
            status="cancelled",
            created_at=now - timedelta(seconds=12),
            updated_at=now,
        )
        session.add(run)
        session.flush()
        attempt = ExecutionAttempt(
            run_id=run.id,
            generation=1,
            worker_id="synthetic-not-executed",
            lease_expires_at=now,
            heartbeat_at=now,
            status="cancelled",
            sandbox_id="synthetic-not-executed",
            sandbox_stopped=True,
            started_at=now - timedelta(seconds=10),
            finished_at=now - timedelta(seconds=2),
        )
        session.add(attempt)
        session.flush()
        session.add(
            OutboxEvent(
                run_id=run.id,
                generation=1,
                created_at=now - timedelta(seconds=12),
                dispatched_at=now - timedelta(seconds=11),
            )
        )
        session.add(
            RunEvent(
                run_id=run.id,
                attempt_id=attempt.id,
                seq=1,
                type="stage_changed",
                payload={
                    "status": "cancel_requested",
                    "private_diagnostic": "must-not-appear",
                },
                occurred_at=now - timedelta(seconds=5),
            )
        )
        session.add(
            ModelCall(
                run_id=run.id,
                attempt_id=attempt.id,
                generation=1,
                logical_call_id="synthetic",
                provider="fixture",
                model="fixture",
                prompt_version="v1",
                status="completed",
                adopted=False,
                request_sha256="2" * 64,
                usage={
                    "input_tokens": 11,
                    "output_tokens": 9,
                    "charged_tokens": 20,
                    "private": "must-not-appear",
                },
                latency_seconds=0.1,
                payload={},
                payload_sha256="3" * 64,
                created_at=now,
            )
        )
        for state in ("ready", "failed", "partial", "cancelled"):
            session.add(
                DataImport(
                    owner_id=account.user.id,
                    kind="provider",
                    provider="fixture",
                    idempotency_key=uuid4().hex,
                    request_sha256="4" * 64,
                    request={},
                    status=state,
                    created_at=now - timedelta(minutes=1),
                    updated_at=now,
                )
            )
        pending = ResearchRun(
            owner_id=account.user.id,
            idempotency_key=uuid4().hex,
            request_sha256="5" * 64,
            config={},
            dataset_version_id=account.dataset.id,
            strategy_version_id=account.strategy.id,
            status="queued",
            created_at=now - timedelta(seconds=20),
        )
        session.add(pending)
        session.flush()
        session.add(OutboxEvent(run_id=pending.id, generation=1, dispatched_at=now))
        session.commit()


def test_operational_samples_use_actual_pg_owner_filters_and_recorded_durations(
    api, owner, other, owner_headers, other_headers, migrated_engine
):
    seed_observations(migrated_engine, owner)
    response = api.get("/api/v1/operations", headers=owner_headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["queued_runs"] == 1  # Already-dispatched domain queue work is included.
    assert data["queue_wait"]["mean_seconds"] == 2
    assert data["attempt_runtime"]["mean_seconds"] == 8
    assert data["cancellation_latency"]["mean_seconds"] == 3
    assert data["imports"]["failure_fraction"] == pytest.approx(1 / 3)
    assert data["imports"]["unsuccessful_fraction"] == pytest.approx(2 / 3)
    assert data["model_usage"]["calls"] == 1
    assert data["model_usage"]["input_tokens"] == 11
    assert data["disk"]["state"] == "restricted"
    assert "must-not-appear" not in response.text and "worker_id" not in response.text
    other_data = api.get("/api/v1/operations", headers=other_headers).json()
    assert other_data["queued_runs"] == 0 and other_data["model_usage"]["calls"] == 0
    assert other_data["queue_wait"]["mean_seconds"] is None
    assert (
        api.get(
            "/api/v1/operations?scope=deployment", headers=other_headers
        ).status_code
        == 403
    )
    assert (
        api.get("/api/v1/operations?days=32", headers=owner_headers).status_code == 422
    )
    assert api.get("/api/v1/operations").status_code == 401
    with Session(migrated_engine) as session:
        session.get(User, other.user.id).is_superuser = True
        session.commit()
    global_data = api.get(
        "/api/v1/operations?scope=deployment", headers=other_headers
    ).json()
    assert (
        global_data["scope"] == "deployment"
        and global_data["model_usage"]["calls"] == 1
    )
    assert global_data["disk"]["state"] == "observed"


def test_bounded_sample_discloses_truncation_and_unconfirmed_stop(
    api, owner, owner_headers, migrated_engine, monkeypatch
):
    from backend.app import operations_views

    seed_observations(migrated_engine, owner)
    with Session(migrated_engine) as session:
        attempt = session.query(ExecutionAttempt).one()
        attempt.sandbox_stopped = False
        session.commit()
    monkeypatch.setattr(operations_views, "SAMPLE_LIMIT", 1)
    result = api.get("/api/v1/operations", headers=owner_headers).json()
    assert result["imports"]["truncated"] is True
    assert result["imports"]["sampled_completed"] == 1
    assert result["cancellation_latency"]["count"] == 0
    assert result["cancellation_latency"]["unavailable_count"] == 1


def test_receipts_are_admin_only_version_bound_historical_observations(
    api, owner, owner_headers, migrated_engine, settings
):
    now = utcnow()
    settings.operations_application_ref = "a" * 40
    settings.operations_backup_receipts = (
        BackupReceipt(
            operation="create",
            application_ref="a" * 40,
            schema_revision="006_provider_connections",
            manifest_sha256="b" * 64,
            backup_created_at=now,
            observed_at=now,
        ),
    )
    owner_data = api.get("/api/v1/settings/status", headers=owner_headers).json()
    assert (
        owner_data["backup_evidence"] == []
        and owner_data["backup_status"] == "restricted"
    )
    with Session(migrated_engine) as session:
        session.get(User, owner.user.id).is_superuser = True
        session.commit()
    data = api.get("/api/v1/settings/status", headers=owner_headers).json()
    assert data["backup_status"] == "observations_registered"
    assert data["backup_evidence"][0]["receipt"]["operation"] == "create"
    assert data["backup_evidence"][0]["state"] == "observed"
    assert "does not prove restoration" in data["backup_note"]
    settings.operations_application_ref = "c" * 40
    assert (
        api.get("/api/v1/settings/status", headers=owner_headers).json()[
            "backup_evidence"
        ][0]["state"]
        == "version_mismatch"
    )
    settings.operations_application_ref = "a" * 40
    settings.operations_backup_receipts = (
        settings.operations_backup_receipts[0].model_copy(
            update={
                "observed_at": now - timedelta(days=2),
                "backup_created_at": now - timedelta(days=3),
            }
        ),
    )
    assert (
        api.get("/api/v1/settings/status", headers=owner_headers).json()[
            "backup_evidence"
        ][0]["state"]
        == "stale"
    )
