"""Opt-in, real PostgreSQL cold-restore and genuine M3-to-M5 upgrade drill.

RUN_BACKUP_RESTORE_DRILL=1 is mandatory. TEST_DATABASE_URL is a maintenance
connection on a dedicated test server; its role must have CREATEDB. Only two
random databases created by this fixture are dropped, never with FORCE or by
terminating connections. No SQLite, mock database, create_all, Alembic stamp,
local PostgreSQL startup, or local Docker fallback constitutes drill evidence.

Client overrides are executable paths (not shell commands); CI may supply narrow
pg_dump/pg_restore wrappers using streamed stdin/stdout. Never upload this test's
temporary archives: they contain database and filesystem contents. Collection
and explicit skips are not evidence that the configured hosted drill passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[2]
APPLICATION_REF = "a" * 40
DATABASE_NAME = re.compile(r"tradegrad_backup_test_[0-9a-f]{32}\Z")
pytestmark = pytest.mark.postgres


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _engine(url: URL) -> Engine:
    from backend.app.operations_backup import PostgresDatabase

    # Apply the production profile before even the fixture's CREATE DATABASE:
    # never let ambient libpq selectors redirect the maintenance connection.
    PostgresDatabase(url.render_as_string(hide_password=False)).environment()
    return create_engine(
        url.set(port=5432 if url.port is None else url.port),
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 10,
            "passfile": "/dev/null",
            "sslmode": url.query.get("sslmode", "prefer"),
            "options": "-csearch_path=public -cstatement_timeout=30000",
        },
        echo=False,
        hide_parameters=True,
    )


@dataclass(frozen=True)
class DrillDatabases:
    source: URL
    restored: URL
    pg_dump: str
    pg_restore: str


@pytest.fixture
def drill_databases() -> Iterator[DrillDatabases]:
    # Do not depend on postgres_url: its unset-URL skip must not turn an opted-in
    # but misconfigured release gate green.
    if os.environ.get("RUN_BACKUP_RESTORE_DRILL") != "1":
        pytest.skip(
            "RUN_BACKUP_RESTORE_DRILL=1 is required; real PostgreSQL "
            "backup/restore drill was not run"
        )
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail(
            "Opted-in backup/restore drill requires TEST_DATABASE_URL",
            pytrace=False,
        )
    try:
        maintenance_url = make_url(value)
    except (ArgumentError, ValueError):
        pytest.fail("TEST_DATABASE_URL is not a valid PostgreSQL URL", pytrace=False)
    # Credentials stay inside the URL object and never become diagnostic text.
    del value
    if maintenance_url.get_backend_name() != "postgresql":
        pytest.fail("Backup/restore drill requires real PostgreSQL", pytrace=False)
    if not maintenance_url.database:
        pytest.fail("TEST_DATABASE_URL must name a maintenance database", pytrace=False)
    if set(maintenance_url.query) - {"sslmode"}:
        pytest.fail(
            "Backup/restore drill supports only sslmode URL query configuration; "
            "database, service and search-path overrides are forbidden",
            pytrace=False,
        )
    maintenance_url = maintenance_url.set(drivername="postgresql+psycopg")
    executable_paths = []
    for setting, default in (
        ("PG_DUMP_EXECUTABLE", "pg_dump"),
        ("PG_RESTORE_EXECUTABLE", "pg_restore"),
    ):
        executable = os.environ.get(setting, default)
        if not executable or shutil.which(executable) is None:
            pytest.fail(
                f"Opted-in backup/restore drill requires an executable {setting}",
                pytrace=False,
            )
        executable_paths.append(executable)

    names = [f"tradegrad_backup_test_{uuid4().hex}" for _ in range(2)]
    assert len(set(names)) == 2
    assert all(DATABASE_NAME.fullmatch(name) for name in names)
    assert maintenance_url.database not in names
    created: list[str] = []
    admin = _engine(maintenance_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        try:
            with admin.connect() as connection:
                assert (
                    connection.execute(text("SELECT version()"))
                    .scalar_one()
                    .startswith("PostgreSQL")
                )
                can_create = connection.execute(
                    text(
                        "SELECT rolsuper OR rolcreatedb FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                ).scalar_one()
                if not can_create:
                    pytest.fail(
                        "Opted-in backup/restore drill requires a CREATEDB role",
                        pytrace=False,
                    )
                for name in names:
                    connection.execute(
                        text(f'CREATE DATABASE "{name}" TEMPLATE template0')
                    )
                    # Never clean up a name whose CREATE did not complete.
                    created.append(name)
        except SQLAlchemyError:
            pytest.fail(
                "Could not create drill databases; check the configured "
                "PostgreSQL service and CREATEDB role (connection details withheld)",
                pytrace=False,
            )
        yield DrillDatabases(
            source=maintenance_url.set(database=names[0]),
            restored=maintenance_url.set(database=names[1]),
            pg_dump=executable_paths[0],
            pg_restore=executable_paths[1],
        )
    finally:
        failed_cleanup = []
        for name in reversed(created):
            assert DATABASE_NAME.fullmatch(name)
            try:
                with admin.connect() as connection:
                    # No FORCE and no pg_terminate_backend, including on failure.
                    connection.execute(text(f'DROP DATABASE "{name}"'))
            except SQLAlchemyError:
                failed_cleanup.append(name)
        admin.dispose()
        if failed_cleanup:
            pytest.fail(
                "Could not drop these fixture-created databases; no sessions "
                "were terminated: " + ", ".join(failed_cleanup),
                pytrace=False,
            )


def _upgrade(engine: Engine, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    with engine.begin() as connection:
        assert (
            connection.execute(text("SELECT current_schema()")).scalar_one() == "public"
        )
        config.attributes["connection"] = connection
        command.upgrade(config, revision)


def _seed_m3(engine: Engine, storage: Path) -> dict[str, bytes]:
    """Insert genuine historical rows, without importing current ORM models."""
    from backend.app.core.atomic_storage import provision_directory, sync_tree
    from packages.tradegrad_adapter.config import MarketConfig, RunConfig

    ids = {
        name: uuid4()
        for name in (
            "owner",
            "dataset",
            "strategy",
            "universe",
            "run",
            "attempt",
            "artifact",
            "checkpoint",
            "candidate",
            "event",
            "listing",
        )
    }
    dataset_key = f"datasets/{ids['owner']}/{ids['dataset']}"
    run_key = f"runs/{ids['run']}/{ids['attempt']}"
    checkpoint_key = f"{run_key}/checkpoints/{ids['checkpoint']}"
    listing = str(ids["listing"])
    dataset_files = {
        "data/INDEX.csv": (
            b"date,open,high,low,close,volume\n"
            b"2024-01-02,10,11,9,10.5,100\n"
            b"2024-01-03,10.5,12,10,11,120\n"
        ),
        "universe/fixed_list_2024-01.csv": (
            "updateDate,code,code_name\n2024-01-01,INDEX,Synthetic 演练\n".encode()
        ),
        "raw.json": _json({"source": "synthetic_fixture", "prices": [10.5, 11]}),
        f"preview/{listing}/0.json": _json(
            [{"session_date": "2024-01-02", "close": 10.5}]
        ),
    }
    universe = {"state": "user_fixed", "members": [{"listing_id": listing}]}
    universe_hash = _sha(_json(universe))
    manifest = {
        "schema_version": 2,
        "is_synthetic": True,
        "ready": True,
        "files": {
            name: _sha(content)
            for name, content in dataset_files.items()
            if name.endswith(".csv")
        },
        "artifact_files": {"raw.json": _sha(dataset_files["raw.json"])},
        "preview_files": {
            listing: [
                {
                    "path": f"preview/{listing}/0.json",
                    "sha256": _sha(dataset_files[f"preview/{listing}/0.json"]),
                    "start_date": "2024-01-02",
                    "end_date": "2024-01-03",
                }
            ]
        },
        "listings": [{"listing_id": listing}],
        "evaluator_symbol_mapping": {listing: "INDEX"},
        "universe": {
            "id": str(ids["universe"]),
            "content_sha256": universe_hash,
            "state": "user_fixed",
        },
        "evaluation_compatibility": {"compatible": True},
        "quality": {"unknown_flags": []},
    }
    manifest["content_hash"] = _sha(_json(manifest))
    manifest_bytes = _json(manifest)
    dataset_hash = _sha(manifest_bytes)
    code = "# Historical synthetic strategy: 演练. Never executed.\nLOOKBACK = 20\n"
    code_hash = _sha(code.encode())
    config = RunConfig.model_validate(
        {
            "task": "cn_time",
            "execution_mode": "optimization",
            "dataset": {"id": str(ids["dataset"]), "sha256": dataset_hash},
            "seed_strategy": {"id": str(ids["strategy"]), "sha256": code_hash},
            "universe": {"id": str(ids["universe"]), "sha256": universe_hash},
            "windows": {
                "train": {"start": "2024-02-01", "end": "2024-02-10"},
                "validation": {"start": "2024-02-15", "end": "2024-02-20"},
                "holdout": {"start": "2024-03-01", "end": "2024-03-05"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
            "optimizer": {"max_iterations": 0},
            "environment_digest": _sha(b"synthetic-old-environment"),
        }
    )
    artifact = _json({"schema_version": 1, "synthetic": True, "value": 0.125})
    checkpoint_state = _json({"complete": True, "next_stage": "complete"})
    checkpoint_manifest = _json(
        {
            "schema_version": 1,
            "complete": True,
            "input_digest": config.sha256,
            "files": [
                {
                    "path": "state.json",
                    "sha256": _sha(checkpoint_state),
                    "size_bytes": len(checkpoint_state),
                }
            ],
        }
    )
    files = {
        **{f"{dataset_key}/{name}": content for name, content in dataset_files.items()},
        f"{dataset_key}/manifest.json": manifest_bytes,
        f"{run_key}/metrics.json": artifact,
        f"{checkpoint_key}/manifest.json": checkpoint_manifest,
        f"{checkpoint_key}/state.json": checkpoint_state,
        # Preserve owned, unreferenced immutable bytes too, not only DB pointers.
        f"uploads/{ids['owner']}/historical.csv": b"date,close\n2024-01-02,10.5\n",
        f"data-checkpoints/{uuid4().hex}/block.json": _json({"rows": [10.5]}),
    }
    provision_directory(storage, durable_parent=storage.parent)
    for namespace in (
        "datasets",
        "runs",
        "data-checkpoints",
        "uploads",
        "strategy-packages",
    ):
        provision_directory(storage / namespace, durable_parent=storage)
    for key, content in files.items():
        path = storage / key
        provision_directory(path.parent, durable_parent=storage)
        path.write_bytes(content)
        path.chmod(0o400)
    sync_tree(storage)
    now = datetime(2024, 4, 1, tzinfo=UTC)
    values = {
        **ids,
        "now": now,
        "manifest": manifest_bytes.decode(),
        "dataset_key": dataset_key,
        "dataset_hash": dataset_hash,
        "code": code,
        "code_hash": code_hash,
        "universe_hash": universe_hash,
        "members": _json(universe["members"]).decode(),
        "config": config.canonical_bytes().decode(),
        "config_hash": config.sha256,
        "artifact_key": f"{run_key}/metrics.json",
        "artifact_hash": _sha(artifact),
        "artifact_size": len(artifact),
        "checkpoint_key": f"{checkpoint_key}/manifest.json",
        "checkpoint_hash": _sha(checkpoint_manifest),
    }
    statements = [
        """INSERT INTO users (id, email, hashed_password, full_name, is_active,
            is_superuser, created_at) VALUES (:owner, 'restore-drill@example.invalid',
            'not-an-authentication-credential', 'Historical 演练', true, false, :now)""",
        """INSERT INTO dataset_versions (id, owner_id, name, market, start_date,
            end_date, source, is_synthetic, content_sha256, manifest, storage_key,
            created_at) VALUES (:dataset, :owner, 'Historical synthetic snapshot',
            'CN', '2024-01-01', '2024-04-01', 'synthetic_fixture', true,
            :dataset_hash, CAST(:manifest AS jsonb), :dataset_key, :now)""",
        """INSERT INTO strategy_versions (id, owner_id, name, task, mode,
            content_sha256, code, created_at) VALUES (:strategy, :owner,
            'Historical native strategy', 'cn_time', 'native_akquant', :code_hash,
            :code, :now)""",
        """INSERT INTO universe_versions (id, owner_id, name, state, as_of,
            members, evidence, content_sha256, created_at) VALUES (:universe,
            :owner, 'Historical fixed universe', 'user_fixed', :now,
            CAST(:members AS jsonb), '[]', :universe_hash, :now)""",
        """INSERT INTO research_runs (id, owner_id, idempotency_key, request_sha256,
            config, dataset_version_id, strategy_version_id, status, generation,
            active_attempt_id, next_event_seq, result, research_image_digest,
            created_at, updated_at) VALUES (:run, :owner, 'historical-cold-restore',
            :config_hash, CAST(:config AS jsonb), :dataset, :strategy, 'succeeded',
            1, NULL, 2, '{"synthetic": true}', 'sha256:' || repeat('b', 64), :now, :now)""",
        """INSERT INTO execution_attempts (id, run_id, generation, worker_id,
            lease_token, lease_expires_at, heartbeat_at, status, sandbox_id,
            sandbox_stopped, exit_code, started_at, finished_at) VALUES (:attempt,
            :run, 1, 'stopped-historical-worker', :attempt, :now, :now, 'succeeded',
            'stopped-historical-sandbox', true, 0, :now, :now)""",
        """INSERT INTO artifacts (id, run_id, attempt_id, name, media_type,
            storage_key, sha256, size_bytes, created_at) VALUES (:artifact,
            :run, :attempt, 'metrics.json', 'application/json', :artifact_key,
            :artifact_hash, :artifact_size, :now)""",
        """INSERT INTO research_candidates (id, run_id, attempt_id, generation,
            candidate_key, kind, iteration, repair_index, code, code_sha256,
            created_at) VALUES (:candidate, :run, :attempt, 1, 'seed', 'seed',
            0, 0, :code, :code_hash, :now)""",
        """INSERT INTO research_checkpoints (id, run_id, attempt_id, generation,
            sequence, schema_version, manifest_storage_key, manifest_sha256,
            input_digest, config_sha256, summary, next_stage, iteration, complete,
            created_at) VALUES (:checkpoint, :run, :attempt, 1, 1, 1,
            :checkpoint_key, :checkpoint_hash, :config_hash, :config_hash,
            '{"synthetic": true}', 'complete', 0, true, :now)""",
        """UPDATE research_runs SET latest_checkpoint_id = :checkpoint WHERE id = :run""",
        """INSERT INTO run_events (id, run_id, attempt_id, seq, schema_version,
            occurred_at, type, payload) VALUES (:event, :run, :attempt, 1, 1,
            :now, 'succeeded', '{"synthetic": true}')""",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), values)
    return files


def _historical_snapshot(engine: Engine) -> dict[str, tuple[list[str], list[tuple]]]:
    """Capture all old columns/rows, including immutable config and FK pointers."""
    schema = inspect(engine)
    result = {}
    with engine.connect() as connection:
        for table in sorted(
            set(schema.get_table_names(schema="public")) - {"alembic_version"}
        ):
            columns = [
                column["name"] for column in schema.get_columns(table, schema="public")
            ]
            result[table] = (columns, _rows(connection, table, columns))
    return result


def _rows(connection, table: str, columns: list[str]) -> list[tuple]:
    # Names originate in PostgreSQL's inspector, not user-controlled identifiers.
    quote = connection.dialect.identifier_preparer.quote
    projection = ", ".join(quote(column) for column in columns)
    # All migrated tables have an id, except worker_heartbeats/provider_budgets.
    key = "id" if "id" in columns else columns[0]
    return [
        tuple(row)
        for row in connection.execute(
            text(
                f"SELECT {projection} FROM public.{quote(table)} ORDER BY {quote(key)}"
            )
        )
    ]


def _assert_historical_snapshot(engine: Engine, expected: dict) -> None:
    with engine.connect() as connection:
        for table, (columns, rows) in expected.items():
            assert _rows(connection, table, columns) == rows, table


def _config_identity(engine: Engine) -> tuple[str, str, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT config::text, request_sha256 FROM public.research_runs")
        ).one()
    config_text, request_hash = row
    assert json.loads(config_text)["schema_version"] == "tradegrad.run.v1"
    assert _sha(_json(json.loads(config_text))) == request_hash
    return config_text, _sha(config_text.encode()), request_hash


def _assert_storage(storage: Path, expected: dict[str, bytes]) -> None:
    for namespace in (
        "datasets",
        "runs",
        "data-checkpoints",
        "uploads",
        "strategy-packages",
    ):
        assert (storage / namespace).is_dir()
    actual = {
        path.relative_to(storage).as_posix(): path.read_bytes()
        for path in storage.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert not any(path.is_symlink() for path in storage.rglob("*"))


def test_real_m3_backup_restore_then_m5_upgrade_preserves_history(
    drill_databases: DrillDatabases,
    tmp_path: Path,
) -> None:
    # Import only after the explicit opt-in fixture has checked prerequisites.
    from backend.app.operations_backup import (
        BackupLimits,
        PgTools,
        PostgresDatabase,
        create_backup,
        restore_backup,
        verify_backup,
    )

    source = _engine(drill_databases.source)
    restored = _engine(drill_databases.restored)
    storage = tmp_path / "source-storage"
    restored_storage = tmp_path / "restored-storage"
    backup = tmp_path / "private-backup"
    tools = PgTools(
        pg_dump=(drill_databases.pg_dump,),
        pg_restore=(drill_databases.pg_restore,),
    )
    limits = BackupLimits()
    try:
        _upgrade(source, "003_m3")
        with source.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "003_m3"
            )
        assert "experiments" not in inspect(source).get_table_names()
        assert "manifest" not in {
            column["name"]
            for column in inspect(source).get_columns("strategy_versions")
        }
        expected_files = _seed_m3(source, storage)
        old_rows = _historical_snapshot(source)
        config_identity = _config_identity(source)
        _assert_storage(storage, expected_files)
        assert inspect(restored).get_table_names(schema="public") == []

        manifest = create_backup(
            database=PostgresDatabase(
                drill_databases.source.render_as_string(hide_password=False)
            ),
            storage_root=storage,
            destination=backup,
            durable_parent=tmp_path,
            application_ref=APPLICATION_REF,
            quiesced=True,
            tools=tools,
            limits=limits,
        )
        assert manifest["format"] == "tradegrad.cold-backup.v1"
        assert manifest["schema_revision"] == "003_m3"
        assert manifest["application_ref"] == APPLICATION_REF
        assert manifest["postgres_major"] >= 14
        assert (
            verify_backup(backup, application_ref=APPLICATION_REF, limits=limits)
            == manifest
        )
        assert (backup / "database.dump").read_bytes().startswith(b"PGDMP")
        inventory = {item["path"]: item for item in manifest["files"]}
        assert set(inventory) == {"database.dump"} | {
            f"storage/{key}" for key in expected_files
        }
        for key, content in expected_files.items():
            assert inventory[f"storage/{key}"]["sha256"] == _sha(content)
            assert inventory[f"storage/{key}"]["size_bytes"] == len(content)
        assert not restored_storage.exists()

        restored_manifest = restore_backup(
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
            limits=limits,
        )
        assert restored_manifest == manifest
        with restored.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "003_m3"
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM data_imports")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text(
                        "SELECT bool_and(sandbox_stopped AND status = 'succeeded') FROM execution_attempts"
                    )
                ).scalar_one()
                is True
            )
        _assert_historical_snapshot(restored, old_rows)
        assert _config_identity(restored) == config_identity
        _assert_storage(restored_storage, expected_files)

        # Upgrade the restored, genuinely old schema, not an M5 database stamped M3.
        _upgrade(restored, "004_m5")
        with restored.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                == "004_m5"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT experiment_id, split_version_id, final_selection_id, submission_binding, portable_policy_image_digest FROM research_runs"
                    )
                ).one()
                == (None,) * 5
            )
            assert connection.execute(
                text("SELECT manifest, parameters FROM strategy_versions")
            ).one() == (None, None)
            assert (
                connection.execute(
                    text("SELECT definition FROM universe_versions")
                ).scalar_one()
                is None
            )
        assert {
            "experiments",
            "split_versions",
            "final_selections",
            "data_usage_exposures",
            "strategy_packages",
        } <= set(inspect(restored).get_table_names())
        _assert_historical_snapshot(restored, old_rows)
        assert _config_identity(restored) == config_identity
        _assert_storage(restored_storage, expected_files)
        # Original database and immutable backup remain usable and unchanged.
        _assert_historical_snapshot(source, old_rows)
        assert (
            verify_backup(backup, application_ref=APPLICATION_REF, limits=limits)
            == manifest
        )
    finally:
        source.dispose()
        restored.dispose()
