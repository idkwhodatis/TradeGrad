"""Apply the deployed migration chain instead of manufacturing tables in tests."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


def test_migrations_create_application_and_real_queue_schema(migrated_engine):
    tables = set(inspect(migrated_engine).get_table_names())
    assert {
        "users",
        "dataset_versions",
        "strategy_versions",
        "research_runs",
        "execution_attempts",
        "run_events",
        "artifacts",
        "outbox_events",
        "worker_heartbeats",
        "procrastinate_jobs",
        "procrastinate_events",
        "procrastinate_workers",
        "alembic_version",
        "strategy_spec_revisions",
        "candidate_bookmarks",
    } <= tables
    with migrated_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT current_schema()"))
            .scalar_one()
            .startswith("tradegrad_test_")
        )
        assert (
            connection.execute(text("SELECT version()"))
            .scalar_one()
            .startswith("PostgreSQL")
        )
        config = Config(str(ROOT / "backend" / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
        assert (
            connection.execute(text("SELECT version_num FROM alembic_version"))
            .scalars()
            .all()
            == ScriptDirectory.from_config(config).get_heads()
        )


def test_migration_downgrade_upgrade_round_trip_preserves_schema_contract(
    migrated_engine,
):
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    original_tables = set(inspect(migrated_engine).get_table_names())
    with migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    remaining = set(inspect(migrated_engine).get_table_names())
    assert remaining <= {"alembic_version"}
    with migrated_engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    assert set(inspect(migrated_engine).get_table_names()) == original_tables
