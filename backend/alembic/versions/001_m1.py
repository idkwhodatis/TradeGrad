"""M1 ownership, immutable inputs, runs/attempts, events, outbox and real queue.

Revision ID: 001_m1
Frozen SQL avoids retroactively changing a migration when ORM models evolve.
Procrastinate DDL is pinned to the 3.10.0 runtime lock.
"""

from pathlib import Path

from alembic import op

revision = "001_m1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_name("001_m1.sql").read_text()
    op.execute(sql)


def downgrade():
    # Only objects created by this revision are removed, within current_schema().
    # Enum CASCADE drops dependent queue tables/functions; additional non-enum
    # helpers are removed by their exact schema-qualified pg_proc signatures.
    op.execute("""
    DO $$ DECLARE r record; BEGIN
      FOR r IN SELECT p.oid::regprocedure AS signature
               FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
               WHERE n.nspname=current_schema() AND p.proname LIKE 'procrastinate_%'
      LOOP EXECUTE 'DROP FUNCTION IF EXISTS ' || r.signature || ' CASCADE'; END LOOP;
    END $$;
    DROP TYPE IF EXISTS procrastinate_job_status CASCADE;
    DROP TYPE IF EXISTS procrastinate_job_event_type CASCADE;
    DROP TYPE IF EXISTS procrastinate_job_to_defer_v1 CASCADE;
    DROP TABLE IF EXISTS procrastinate_workers, procrastinate_events, procrastinate_jobs, procrastinate_periodic_defers CASCADE;
    DROP TABLE IF EXISTS worker_heartbeats, outbox_events, artifacts, run_events,
      execution_attempts, research_runs, strategy_versions, dataset_versions, users;
    """)
