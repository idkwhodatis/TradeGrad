"""Immutable comparison business objects and durable cache-submission aliases."""

from alembic import op

revision = "008_comparisons"
down_revision = "007_research_specs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE comparisons (
      id UUID PRIMARY KEY,
      owner_id UUID NOT NULL REFERENCES users(id),
      run_id UUID NOT NULL UNIQUE REFERENCES research_runs(id),
      left_evaluation_id UUID NOT NULL REFERENCES research_evaluations(id),
      right_evaluation_id UUID NOT NULL REFERENCES research_evaluations(id),
      input_sha256 VARCHAR(64) NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
      engine_input_sha256 VARCHAR(64) NOT NULL CHECK (engine_input_sha256 ~ '^[0-9a-f]{64}$'),
      input_snapshot JSONB NOT NULL CHECK (jsonb_typeof(input_snapshot) = 'object'),
      published_attempt_id UUID REFERENCES execution_attempts(id),
      manifest_artifact_id UUID REFERENCES artifacts(id),
      manifest_sha256 VARCHAR(64) CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_comparison_owner_input UNIQUE (owner_id, input_sha256),
      CHECK ((published_attempt_id IS NULL AND manifest_artifact_id IS NULL AND manifest_sha256 IS NULL)
        OR (published_attempt_id IS NOT NULL AND manifest_artifact_id IS NOT NULL AND manifest_sha256 IS NOT NULL))
    );
    CREATE INDEX ix_comparisons_owner_id ON comparisons(owner_id);
    CREATE TABLE comparison_submissions (
      owner_id UUID NOT NULL REFERENCES users(id),
      idempotency_key VARCHAR(128) NOT NULL CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
      comparison_id UUID NOT NULL REFERENCES comparisons(id),
      request_sha256 VARCHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY(owner_id, idempotency_key)
    );
    CREATE INDEX ix_comparison_submissions_comparison_id ON comparison_submissions(comparison_id);
    CREATE FUNCTION validate_comparison_binding() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Comparisons are append-only'; END IF;
      IF TG_OP = 'UPDATE' AND (
        NEW.id IS DISTINCT FROM OLD.id OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
        OR NEW.run_id IS DISTINCT FROM OLD.run_id
        OR NEW.left_evaluation_id IS DISTINCT FROM OLD.left_evaluation_id
        OR NEW.right_evaluation_id IS DISTINCT FROM OLD.right_evaluation_id
        OR NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256
        OR NEW.engine_input_sha256 IS DISTINCT FROM OLD.engine_input_sha256
        OR NEW.input_snapshot IS DISTINCT FROM OLD.input_snapshot
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR OLD.published_attempt_id IS NOT NULL
      ) THEN RAISE EXCEPTION 'Comparison inputs and published results are immutable'; END IF;
      IF NOT EXISTS (SELECT 1 FROM research_runs r WHERE r.id = NEW.run_id AND r.owner_id = NEW.owner_id
          AND r.config->>'schema_version' = 'tradegrad.comparison.v1'
          AND r.config->>'comparison_id' = NEW.id::text
          AND r.config->>'input_sha256' = NEW.input_sha256)
        OR NOT EXISTS (SELECT 1 FROM research_evaluations e JOIN research_runs r ON r.id = e.run_id
          WHERE e.id = NEW.left_evaluation_id AND r.owner_id = NEW.owner_id)
        OR NOT EXISTS (SELECT 1 FROM research_evaluations e JOIN research_runs r ON r.id = e.run_id
          WHERE e.id = NEW.right_evaluation_id AND r.owner_id = NEW.owner_id)
      OR NEW.input_snapshot->>'owner_id' IS DISTINCT FROM NEW.owner_id::text
        OR NEW.input_snapshot->'request'->'left'->>'evaluation_id' IS DISTINCT FROM NEW.left_evaluation_id::text
        OR NEW.input_snapshot->'request'->'right'->>'evaluation_id' IS DISTINCT FROM NEW.right_evaluation_id::text
      THEN RAISE EXCEPTION 'Comparison owner or source binding differs'; END IF;
      IF NEW.published_attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM execution_attempts a JOIN artifacts f ON f.attempt_id = a.id
          WHERE a.id = NEW.published_attempt_id AND a.run_id = NEW.run_id
          AND f.id = NEW.manifest_artifact_id AND f.run_id = NEW.run_id AND f.sha256 = NEW.manifest_sha256
      ) THEN RAISE EXCEPTION 'Comparison publication binding differs'; END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_comparison_binding BEFORE INSERT OR UPDATE OR DELETE ON comparisons
      FOR EACH ROW EXECUTE FUNCTION validate_comparison_binding();
    CREATE FUNCTION validate_comparison_submission() RETURNS trigger AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Comparison submissions are append-only'; END IF;
      IF NOT EXISTS (SELECT 1 FROM comparisons c WHERE c.id = NEW.comparison_id AND c.owner_id = NEW.owner_id
          AND c.input_snapshot->>'request_sha256' = NEW.request_sha256)
        OR EXISTS (SELECT 1 FROM research_runs r WHERE r.owner_id = NEW.owner_id AND r.idempotency_key = NEW.idempotency_key
          AND r.id <> (SELECT c.run_id FROM comparisons c WHERE c.id = NEW.comparison_id))
      THEN RAISE EXCEPTION 'Comparison submission owner or idempotency binding differs'; END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_comparison_submission BEFORE INSERT OR UPDATE OR DELETE ON comparison_submissions
      FOR EACH ROW EXECUTE FUNCTION validate_comparison_submission();
    CREATE FUNCTION protect_comparison_run_key() RETURNS trigger AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM comparison_submissions s WHERE s.owner_id = NEW.owner_id AND s.idempotency_key = NEW.idempotency_key)
      THEN RAISE EXCEPTION 'Run key is reserved by a comparison submission'; END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER protect_comparison_run_key BEFORE INSERT ON research_runs
      FOR EACH ROW EXECUTE FUNCTION protect_comparison_run_key();
    """)


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM comparisons) OR EXISTS (SELECT 1 FROM comparison_submissions)
        OR EXISTS (SELECT 1 FROM research_runs WHERE config->>'schema_version' = 'tradegrad.comparison.v1')
      THEN RAISE EXCEPTION 'Comparison evidence must be archived before downgrade'; END IF;
    END $$;
    DROP TRIGGER protect_comparison_run_key ON research_runs;
    DROP TABLE comparison_submissions, comparisons;
    DROP FUNCTION validate_comparison_binding();
    DROP FUNCTION validate_comparison_submission();
    DROP FUNCTION protect_comparison_run_key();
    """)
