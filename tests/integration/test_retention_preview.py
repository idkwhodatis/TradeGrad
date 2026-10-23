"""Opt-in real PostgreSQL retention snapshot/immutability test, not apply evidence.

Reuse the public-schema cold-drill fixture: RUN_BACKUP_RESTORE_DRILL=1 and its
hosted PostgreSQL/tool prerequisites are mandatory. No services are started.
Local collection is not execution evidence. This test invokes no PG client,
strategy/SDK subprocess, browser, Docker or signal operation.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration.test_backup_restore import (
    _assert_historical_snapshot,
    _assert_storage,
    _engine,
    _historical_snapshot,
    _seed_m3,
    _sha,
    _upgrade,
)
from tests.integration.test_backup_restore import (
    drill_databases as drill_databases,  # noqa: PLC0414 -- shared opt-in fixture
)

pytestmark = pytest.mark.postgres


def test_real_cold_preview_preserves_history_and_rejects_active_work(
    drill_databases, tmp_path
):
    from backend.app.operations_backup import PostgresDatabase
    from backend.app.operations_retention import RetentionError, preview_retention

    engine = _engine(drill_databases.source)
    storage = tmp_path / "retention-source"
    try:
        _upgrade(engine, "003_m3")
        expected = _seed_m3(engine, storage)
        with engine.begin() as connection:
            run_id, attempt_id = connection.execute(
                text("SELECT run_id, id FROM execution_attempts")
            ).one()
            base = f"runs/{run_id}/{attempt_id}"
            log_key = f"{base}/artifacts/logs.txt"
            log = storage / log_key
            log.parent.mkdir()
            log.write_bytes(b"synthetic retained diagnostics")
            expected[log_key] = log.read_bytes()
            connection.execute(
                text("""
                INSERT INTO artifacts (id, run_id, attempt_id, name, media_type,
                    storage_key, sha256, size_bytes, created_at)
                VALUES (:id, :run, :attempt, 'logs.txt', 'text/plain', :key,
                    :sha, :size, now())
            """),
                {
                    "id": uuid4(),
                    "run": run_id,
                    "attempt": attempt_id,
                    "key": log_key,
                    "sha": _sha(expected[log_key]),
                    "size": len(expected[log_key]),
                },
            )
        temporary_key = f"{base}/.execution-environment.json.{uuid4().hex}.tmp"
        (storage / temporary_key).write_bytes(b"abandoned fixture temporary")
        expected[temporary_key] = (storage / temporary_key).read_bytes()
        history = _historical_snapshot(engine)
        database = PostgresDatabase(
            drill_databases.source.render_as_string(hide_password=False)
        )
        arguments = {
            "database": database,
            "storage_root": storage,
            "quiesced": True,
            "now": datetime.now(UTC) + timedelta(days=31),
        }
        report = preview_retention(**arguments)
        assert report["deleted_files"] == 0
        assert report["aged_run_logs_retained"] == 1
        assert [entry["path"] for entry in report["temporary_review_candidates"]] == [
            temporary_key
        ]
        _assert_storage(storage, expected)
        _assert_historical_snapshot(engine, history)

        # The production snapshot gate, not a fake session, rejects both states.
        with engine.begin() as connection:
            connection.execute(text("UPDATE research_runs SET status='queued'"))
        with pytest.raises(RetentionError):
            preview_retention(**arguments)
        with engine.begin() as connection:
            connection.execute(text("UPDATE research_runs SET status='succeeded'"))
            connection.execute(
                text("UPDATE execution_attempts SET sandbox_stopped=false")
            )
        with pytest.raises(RetentionError):
            preview_retention(**arguments)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE execution_attempts SET sandbox_stopped=true")
            )
        _assert_storage(storage, expected)
        _assert_historical_snapshot(engine, history)
    finally:
        engine.dispose()
