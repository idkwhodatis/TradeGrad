"""Prepared hosted real-PG temporary-fixture cleanup and recovery acceptance.

Opt-in cold-drill fixtures create disposable databases/files. Never execute this
suite against user storage or treat collection, pure checks or mocks as apply
acceptance. No candidate/provider/browser/Docker code runs in this module.
"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.integration.test_backup_restore import (
    APPLICATION_REF,
    _assert_historical_snapshot,
    _assert_storage,
    _engine,
    _historical_snapshot,
    _seed_m3,
    _upgrade,
)
from tests.integration.test_backup_restore import (
    drill_databases as drill_databases,  # noqa: PLC0414 -- explicit hosted opt-in
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def cold_source(drill_databases, tmp_path):
    from backend.app.operations_backup import PostgresDatabase

    engine = _engine(drill_databases.source)
    storage = tmp_path / "storage"
    try:
        _upgrade(engine, "003_m3")
        files = _seed_m3(engine, storage)
        with engine.connect() as connection:
            run, attempt = connection.execute(
                text("SELECT run_id, id FROM execution_attempts")
            ).one()
        base = f"runs/{run}/{attempt}"
        candidates = [
            f"{base}/.execution-environment.json.{index:032x}.tmp" for index in (1, 2)
        ]
        log_key = f"{base}/artifacts/logs.txt"
        (storage / log_key).parent.mkdir()
        (storage / log_key).write_bytes(
            b"immutable unreferenced run diagnostics must survive"
        )
        files[log_key] = (storage / log_key).read_bytes()
        for suffix, content in {
            "input/strategy.py": b"# inert frozen source, never executed",
            "model-replay/call.json": b'{"synthetic":"retained replay receipt"}',
            "model-replay/.call.json.unrecognized.tmp": b"interrupted replay is not disposable",
        }.items():
            key = f"{base}/{suffix}"
            (storage / key).parent.mkdir(parents=True, exist_ok=True)
            (storage / key).write_bytes(content)
            files[key] = content
        for key in candidates:
            (storage / key).write_bytes(b"abandoned atomic fixture, never executed")
        database = PostgresDatabase(
            drill_databases.source.render_as_string(hide_password=False)
        )
        yield {
            "engine": engine,
            "storage": storage,
            "files": files,
            "candidates": candidates,
            "database": database,
            "history": _historical_snapshot(engine),
            "arguments": {
                "database": database,
                "storage_root": storage,
                "quiesced": True,
                "now": datetime.now(UTC) + timedelta(days=31),
            },
        }
    finally:
        engine.dispose()


def _apply(source, tmp_path, *, name="receipt.jsonl"):
    from backend.app.operations_cold_retention import ACKNOWLEDGEMENT, cold_retention

    plan = cold_retention(**source["arguments"])
    result = cold_retention(
        **source["arguments"],
        apply=True,
        expected_plan_sha256=plan["plan_sha256"],
        acknowledgement=ACKNOWLEDGEMENT,
        receipt_path=tmp_path / name,
    )
    return plan, result


def test_real_cleanup_keeps_every_published_reference_then_backup_restores_history(
    cold_source, drill_databases, tmp_path
):
    from backend.app.operations_backup import (
        PgTools,
        PostgresDatabase,
        create_backup,
        restore_backup,
    )

    source = cold_source
    plan, report = _apply(source, tmp_path)
    assert plan["dry_run"] is True and plan["deleted_files"] == 0
    assert [item["path"] for item in plan["temporary_candidates"]] == source[
        "candidates"
    ]
    assert report["status"] == "applied" and report["deleted_files"] == 2
    assert report["aged_run_logs_retained"] == 1
    assert report["log_cleanup_supported"] is False
    _assert_storage(source["storage"], source["files"])
    _assert_historical_snapshot(source["engine"], source["history"])
    journal = [
        json.loads(line)
        for line in (tmp_path / "receipt.jsonl").read_text().splitlines()
    ]
    assert [row["action"] for row in journal] == [
        "begin",
        "unlink_intent",
        "unlinked",
        "unlink_intent",
        "unlinked",
        "complete",
    ]
    assert (tmp_path / "receipt.jsonl").stat().st_mode & 0o777 == 0o600

    tools = PgTools(
        pg_dump=(drill_databases.pg_dump,), pg_restore=(drill_databases.pg_restore,)
    )
    backup = tmp_path / "private-backup-after-cleanup"
    manifest = create_backup(
        database=source["database"],
        storage_root=source["storage"],
        destination=backup,
        durable_parent=tmp_path,
        application_ref=APPLICATION_REF,
        quiesced=True,
        tools=tools,
    )
    restored_storage = tmp_path / "restored-storage"
    restore_backup(
        database=PostgresDatabase(
            drill_databases.restored.render_as_string(hide_password=False)
        ),
        backup_root=backup,
        storage_root=restored_storage,
        durable_parent=tmp_path,
        application_ref=APPLICATION_REF,
        quiesced=True,
        trust_backup=True,
        tools=tools,
    )
    restored = _engine(drill_databases.restored)
    try:
        _assert_storage(restored_storage, source["files"])
        _assert_historical_snapshot(restored, source["history"])
        assert manifest["schema_revision"] == "003_m3"
        _upgrade(restored, "head")
        _assert_storage(restored_storage, source["files"])
        _assert_historical_snapshot(restored, source["history"])
    finally:
        restored.dispose()


def test_real_partial_cleanup_receipt_requires_fresh_plan_then_recovers(
    cold_source, tmp_path, monkeypatch
):
    import backend.app.operations_cold_retention as retention

    original = retention._unlink_candidate
    count = 0

    def fail_second(*args):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("injected disposable fixture fault")
        return original(*args)

    monkeypatch.setattr(retention, "_unlink_candidate", fail_second)
    with pytest.raises(OSError):
        _apply(cold_source, tmp_path, name="partial.jsonl")
    assert not (cold_source["storage"] / cold_source["candidates"][0]).exists()
    assert (cold_source["storage"] / cold_source["candidates"][1]).is_file()
    rows = [
        json.loads(line)
        for line in (tmp_path / "partial.jsonl").read_text().splitlines()
    ]
    assert rows[-1]["action"] == "incomplete"
    assert rows[-1]["confirmed_deleted_files"] == 1
    monkeypatch.setattr(retention, "_unlink_candidate", original)
    plan, result = _apply(cold_source, tmp_path, name="recovered.jsonl")
    assert len(plan["temporary_candidates"]) == result["deleted_files"] == 1
    _assert_storage(cold_source["storage"], cold_source["files"])
    _assert_historical_snapshot(cold_source["engine"], cold_source["history"])


@pytest.mark.parametrize(
    "mutation",
    [
        "queued",
        "unknown_stop",
        "pending_outbox",
        "unknown_model_stop",
        "candidate_replaced",
        "candidate_hardlink",
        "checkpoint_changed",
    ],
)
def test_real_apply_refuses_changed_state_before_any_deletion(
    cold_source, tmp_path, mutation
):
    from backend.app.operations_backup import BackupError
    from backend.app.operations_cold_retention import ACKNOWLEDGEMENT, cold_retention
    from backend.app.operations_retention import RetentionError

    source = cold_source
    plan = cold_retention(**source["arguments"])
    if mutation in {"queued", "unknown_stop", "pending_outbox", "unknown_model_stop"}:
        query = {
            "unknown_model_stop": "INSERT INTO run_events (id, run_id, attempt_id, seq, schema_version, occurred_at, type, payload) SELECT :id, run_id, id, 100, 1, now(), 'model_process_admitted', '{\"call_id\":\"inert-unconfirmed\"}'::jsonb FROM execution_attempts",
            "queued": "UPDATE research_runs SET status='queued'",
            "unknown_stop": "UPDATE execution_attempts SET sandbox_stopped=false",
            "pending_outbox": "INSERT INTO outbox_events (id, run_id, generation, created_at) SELECT :id, id, generation, now() FROM research_runs",
        }[mutation]
        with source["engine"].begin() as connection:
            connection.execute(text(query), {"id": uuid4()})
    elif mutation == "candidate_replaced":
        path = source["storage"] / source["candidates"][0]
        path.unlink()
        path.write_bytes(b"new writer identity")
    elif mutation == "candidate_hardlink":
        (source["storage"] / "uploads" / "alias").hardlink_to(
            source["storage"] / source["candidates"][0]
        )
    else:
        key = next(key for key in source["files"] if key.endswith("/state.json"))
        path = source["storage"] / key
        path.chmod(0o600)
        path.write_bytes(b"corrupt checkpoint fixture")
    with pytest.raises((RetentionError, BackupError, OSError)):
        cold_retention(
            **source["arguments"],
            apply=True,
            expected_plan_sha256=plan["plan_sha256"],
            acknowledgement=ACKNOWLEDGEMENT,
            receipt_path=tmp_path / "not-created.jsonl",
        )
    assert all((source["storage"] / key).exists() for key in source["candidates"])
    assert not (tmp_path / "not-created.jsonl").exists()


def test_real_connection_loss_before_unlink_records_intent_but_keeps_file(
    cold_source, tmp_path, monkeypatch
):
    import backend.app.operations_cold_retention as retention

    original = retention.Receipt.append

    def disconnect_after_intent(receipt, value):
        original(receipt, value)
        if value["action"] == "unlink_intent":
            # The next required connection assertion is denied, never bypassed.
            monkeypatch.setattr(
                retention.LockedDatabase,
                "assert_locked",
                lambda self: (_ for _ in ()).throw(
                    retention.RetentionError("injected own-connection loss")
                ),
            )

    monkeypatch.setattr(retention.Receipt, "append", disconnect_after_intent)
    with pytest.raises(retention.RetentionError):
        _apply(cold_source, tmp_path, name="lost-connection.jsonl")
    assert all(
        (cold_source["storage"] / key).exists() for key in cold_source["candidates"]
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "lost-connection.jsonl").read_text().splitlines()
    ]
    assert [row["action"] for row in rows] == ["begin", "unlink_intent", "incomplete"]


def test_real_candidate_replaced_after_durable_intent_is_not_unlinked(
    cold_source, tmp_path, monkeypatch
):
    import backend.app.operations_cold_retention as retention

    original = retention.Receipt.append

    def replace_after_intent(receipt, value):
        original(receipt, value)
        if value["action"] == "unlink_intent":
            path = cold_source["storage"] / value["path"]
            # Deliberately bypass cooperative protocol only in disposable fixtures
            # to establish the final same-inode check, not hostile-writer support.
            path.unlink()
            path.write_bytes(b"replacement fixture must not be deleted")

    monkeypatch.setattr(retention.Receipt, "append", replace_after_intent)
    with pytest.raises(retention.RetentionError):
        _apply(cold_source, tmp_path, name="replacement.jsonl")
    assert (
        cold_source["storage"] / cold_source["candidates"][0]
    ).read_bytes() == b"replacement fixture must not be deleted"
    assert (cold_source["storage"] / cold_source["candidates"][1]).exists()


def test_real_direct_artifact_reference_overrides_temporary_filename(
    cold_source, tmp_path
):
    import hashlib

    source = cold_source
    key = source["candidates"][0]
    content = (source["storage"] / key).read_bytes()
    with source["engine"].begin() as connection:
        connection.execute(
            text(
                "INSERT INTO artifacts (id, run_id, attempt_id, name, media_type, storage_key, sha256, size_bytes, created_at) "
                "SELECT :id, run_id, id, 'retained-temporary-name', 'application/octet-stream', :key, :sha, :size, now() FROM execution_attempts"
            ),
            {
                "id": uuid4(),
                "key": key,
                "sha": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            },
        )
    source["files"][key] = content
    source["history"] = _historical_snapshot(source["engine"])
    plan, result = _apply(source, tmp_path, name="referenced.jsonl")
    assert [entry["path"] for entry in plan["temporary_candidates"]] == [
        source["candidates"][1]
    ]
    assert result["deleted_files"] == 1
    _assert_storage(source["storage"], source["files"])
    _assert_historical_snapshot(source["engine"], source["history"])
