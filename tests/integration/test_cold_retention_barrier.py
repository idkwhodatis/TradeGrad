# ruff: noqa: SIM117
"""Prepared real-PG concurrency gates. Collect locally; execute only on hosted PG.

No Docker/provider/strategy is used. No test stops a foreign backend. Disposable
fixture connections and inert files are the only writers exercised here.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.core.maintenance import MaintenanceBusy, _lock, shared_lifecycle
from backend.app.core.storage_barrier import storage_guard
from backend.app.services import advisory_lock

pytestmark = pytest.mark.postgres


def test_real_pg_shared_transaction_blocks_exclusive_until_commit(postgres_engine):
    with Session(postgres_engine) as writer, postgres_engine.connect() as cleaner:
        writer.execute(text("SELECT 1"))
        with pytest.raises(MaintenanceBusy):
            _lock(cleaner, exclusive=True)
        cleaner.rollback()
        writer.commit()
        _lock(cleaner, exclusive=True)
        with pytest.raises(MaintenanceBusy):
            writer.execute(text("SELECT 1"))
        writer.rollback()
        cleaner.rollback()
        assert writer.execute(text("SELECT 1")).scalar_one() == 1


def test_real_pg_owner_order_cannot_wait_behind_maintenance(postgres_engine):
    with postgres_engine.connect() as cleaner:
        _lock(cleaner, exclusive=True)
        with Session(postgres_engine) as writer:
            with pytest.raises(MaintenanceBusy):
                advisory_lock(writer, "submit:inert-lock-order-probe")
            writer.rollback()
        cleaner.rollback()
    with Session(postgres_engine) as writer:
        advisory_lock(writer, "submit:inert-lock-order-probe")
        writer.rollback()


def test_real_worker_full_lifecycle_excludes_cleanup_between_transactions(
    postgres_engine, tmp_path
):
    admitted, finish = Event(), Event()

    def writer():
        with shared_lifecycle(postgres_engine, tmp_path):
            with Session(postgres_engine) as session:
                session.execute(text("SELECT 1"))
                session.commit()
            admitted.set()
            assert finish.wait(10)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        try:
            assert admitted.wait(10)
            with postgres_engine.connect() as cleaner:
                with pytest.raises(MaintenanceBusy):
                    _lock(cleaner, exclusive=True)
        finally:
            finish.set()
        future.result(timeout=10)
    with postgres_engine.connect() as cleaner:
        _lock(cleaner, exclusive=True)
        with storage_guard(tmp_path, exclusive=True):
            pass


def test_real_pg_lost_own_connection_still_refuses_storage_writer(
    postgres_engine, tmp_path
):
    # Closing our own connection is not pg_terminate_backend or a signal operation.
    with postgres_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as writer:
        _lock(writer, transaction=False)
        with storage_guard(tmp_path):
            writer.invalidate()
            with postgres_engine.connect() as cleaner:
                _lock(cleaner, exclusive=True)
                with pytest.raises(MaintenanceBusy):
                    with storage_guard(tmp_path, exclusive=True):
                        pytest.fail(
                            "Lost PG session must not release the filesystem guard"
                        )


def test_real_api_health_and_admission_refuse_maintenance_without_side_effects(
    migrated_engine, api, owner_headers, run_payload
):
    with migrated_engine.connect() as cleaner:
        _lock(cleaner, exclusive=True)
        assert api.get("/api/v1/health/live").status_code == 200
        assert api.get("/api/v1/health/ready").status_code == 503
        response = api.post(
            "/api/v1/runs",
            headers={**owner_headers, "Idempotency-Key": "maintenance-admission"},
            json=run_payload,
        )
        assert response.status_code == 503
        assert response.json()["code"] == "database_unavailable"
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM research_runs WHERE idempotency_key='maintenance-admission'"
                )
            ).scalar_one()
            == 0
        )


def test_real_denied_session_cannot_reuse_transaction_without_readmission(
    postgres_engine,
):
    from sqlalchemy.exc import SQLAlchemyError

    with postgres_engine.connect() as cleaner, Session(postgres_engine) as writer:
        _lock(cleaner, exclusive=True)
        with pytest.raises(MaintenanceBusy):
            writer.execute(text("SELECT 1"))
        # Deliberately omit rollback. No later statement may slip through the
        # already-fired after_begin hook after an application catches denial.
        with pytest.raises(SQLAlchemyError):
            writer.execute(text("SELECT 1"))
        writer.rollback()
        with pytest.raises(MaintenanceBusy):
            writer.execute(text("SELECT 1"))
        writer.rollback()
        cleaner.rollback()
        assert writer.execute(text("SELECT 1")).scalar_one() == 1
