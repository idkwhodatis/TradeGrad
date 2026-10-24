"""Frozen independent executions and manual control protocols with owner binding."""

from alembic import op

revision = "009_independent_evaluations"
down_revision = "008_comparisons"
branch_labels = None
depends_on = None


def _replace_run_binding(*, independent):
    # Keep every M5 ownership/immutability/final gate, extending only the known
    # portable schema set. Native executions must still never bind a policy image.
    schemas = ["tradegrad.portable.run.v1", "tradegrad.portable.final-evaluation.v1"]
    if independent:
        schemas.append("tradegrad.portable.independent-evaluation.v1")
    portable_schemas = ", ".join("'" + schema + "'" for schema in schemas)
    op.execute(f"""
      CREATE OR REPLACE FUNCTION protect_m5_run_binding() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP = 'UPDATE' THEN
          IF NEW.submission_binding IS DISTINCT FROM OLD.submission_binding
            OR NEW.experiment_id IS DISTINCT FROM OLD.experiment_id
            OR NEW.split_version_id IS DISTINCT FROM OLD.split_version_id
            OR NEW.final_selection_id IS DISTINCT FROM OLD.final_selection_id
            OR (OLD.portable_policy_image_digest IS NOT NULL AND NEW.portable_policy_image_digest IS DISTINCT FROM OLD.portable_policy_image_digest)
          THEN RAISE EXCEPTION 'Run experiment binding is immutable'; END IF;
        END IF;
        IF (NEW.experiment_id IS NULL) <> (NEW.split_version_id IS NULL) THEN
          RAISE EXCEPTION 'Experiment and split must be bound together';
        END IF;
        IF NEW.split_version_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM split_versions s WHERE s.id = NEW.split_version_id
            AND s.owner_id = NEW.owner_id AND s.experiment_id = NEW.experiment_id
            AND s.dataset_version_id = NEW.dataset_version_id
        ) THEN RAISE EXCEPTION 'Run split is not owned or does not match'; END IF;
        IF NEW.final_selection_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM final_selections f WHERE f.id = NEW.final_selection_id
            AND f.owner_id = NEW.owner_id AND f.experiment_id = NEW.experiment_id
            AND f.split_version_id = NEW.split_version_id AND f.strategy_version_id = NEW.strategy_version_id
        ) THEN RAISE EXCEPTION 'Final run selection binding differs'; END IF;
        IF NEW.config->>'schema_version' IN ('tradegrad.final-evaluation.v1', 'tradegrad.portable.final-evaluation.v1') AND NEW.final_selection_id IS NULL THEN
          RAISE EXCEPTION 'Final evaluation requires immutable selection';
        END IF;
        IF COALESCE(NEW.config->>'schema_version', '') IN ({portable_schemas}) THEN
          IF (NEW.research_image_digest IS NULL) <> (NEW.portable_policy_image_digest IS NULL) THEN
            RAISE EXCEPTION 'Portable images must be bound together';
          END IF;
        ELSIF NEW.portable_policy_image_digest IS NOT NULL THEN
          RAISE EXCEPTION 'Policy image is exclusive to portable execution';
        END IF;
        RETURN NEW;
      END $$;
    """)


def upgrade():
    _replace_run_binding(independent=True)
    op.execute("""
    CREATE TABLE independent_evaluations (
      id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES users(id),
      run_id UUID NOT NULL UNIQUE REFERENCES research_runs(id),
      source_evaluation_id UUID NOT NULL REFERENCES research_evaluations(id),
      purpose VARCHAR(20) NOT NULL CHECK (purpose IN ('diagnostic','controlled')),
      request_sha256 VARCHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
      request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
      source_snapshot JSONB NOT NULL CHECK (jsonb_typeof(source_snapshot) = 'object'),
      source_snapshot_sha256 VARCHAR(64) NOT NULL CHECK (source_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
      protocol JSONB NOT NULL CHECK (jsonb_typeof(protocol) = 'object'),
      protocol_sha256 VARCHAR(64) NOT NULL CHECK (protocol_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_independent_evaluations_owner_id ON independent_evaluations(owner_id);
    CREATE TABLE controlled_experiments (
      id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES users(id),
      idempotency_key VARCHAR(128) NOT NULL CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
      experiment_id UUID NOT NULL UNIQUE REFERENCES experiments(id),
      split_version_id UUID NOT NULL REFERENCES split_versions(id),
      source_evaluation_id UUID NOT NULL REFERENCES research_evaluations(id),
      request_sha256 VARCHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
      request JSONB NOT NULL CHECK (jsonb_typeof(request) = 'object'),
      protocol JSONB NOT NULL CHECK (jsonb_typeof(protocol) = 'object'),
      protocol_sha256 VARCHAR(64) NOT NULL CHECK (protocol_sha256 ~ '^[0-9a-f]{64}$'),
      exposure_snapshot JSONB NOT NULL CHECK (jsonb_typeof(exposure_snapshot) = 'object'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_control_owner_key UNIQUE(owner_id,idempotency_key)
    );
    CREATE INDEX ix_controlled_experiments_owner_id ON controlled_experiments(owner_id);
    CREATE TABLE controlled_arms (
      id UUID PRIMARY KEY,
      controlled_experiment_id UUID NOT NULL REFERENCES controlled_experiments(id),
      independent_id UUID NOT NULL UNIQUE REFERENCES independent_evaluations(id),
      ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 0 AND 2),
      name VARCHAR(120) NOT NULL, planned_change TEXT,
      changed_fields JSONB NOT NULL CHECK (jsonb_typeof(changed_fields) = 'array'),
      interpretation VARCHAR(60) NOT NULL CHECK (interpretation IN ('baseline','single_parameter_change_not_causal_proof','not_individually_validated','unchanged')),
      CONSTRAINT uq_control_arm_ordinal UNIQUE(controlled_experiment_id,ordinal)
    );
    CREATE INDEX ix_controlled_arms_controlled_experiment_id ON controlled_arms(controlled_experiment_id);
    CREATE FUNCTION validate_independent_binding() RETURNS trigger AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Independent inputs are append-only'; END IF;
      IF NOT EXISTS (SELECT 1 FROM research_runs r WHERE r.id = NEW.run_id AND r.owner_id = NEW.owner_id
        AND r.config->>'schema_version' IN ('tradegrad.independent-evaluation.v1','tradegrad.portable.independent-evaluation.v1')
        AND r.config->>'independent_id' = NEW.id::text AND r.config->>'purpose' = NEW.purpose
        AND r.config->'source'->>'evaluation_id' = NEW.source_evaluation_id::text
        AND r.config->>'protocol_sha256' = NEW.protocol_sha256)
      OR NOT EXISTS (SELECT 1 FROM research_evaluations e JOIN research_runs r ON r.id = e.run_id
        WHERE e.id = NEW.source_evaluation_id AND r.owner_id = NEW.owner_id)
      OR NEW.request->'source'->>'evaluation_id' IS DISTINCT FROM NEW.source_evaluation_id::text
      THEN RAISE EXCEPTION 'Independent owner/source/config binding differs'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_independent_binding BEFORE INSERT OR UPDATE OR DELETE ON independent_evaluations
      FOR EACH ROW EXECUTE FUNCTION validate_independent_binding();
    CREATE FUNCTION validate_control_binding() RETURNS trigger AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Controlled experiment inputs are append-only'; END IF;
      IF NOT EXISTS (SELECT 1 FROM experiments x JOIN split_versions s ON s.experiment_id=x.id
        WHERE x.id=NEW.experiment_id AND x.owner_id=NEW.owner_id AND s.id=NEW.split_version_id AND s.owner_id=NEW.owner_id
        AND EXISTS (SELECT 1 FROM research_evaluations e JOIN research_runs r ON r.id=e.run_id
          JOIN experiments parent ON parent.id=r.experiment_id
          JOIN split_versions original ON original.id=r.split_version_id
          WHERE e.id=NEW.source_evaluation_id AND r.owner_id=NEW.owner_id
            AND x.parent_experiment_id=parent.id AND x.root_experiment_id=coalesce(parent.root_experiment_id,parent.id)
            AND s.definition=original.definition AND s.content_sha256=original.content_sha256))
      OR NOT EXISTS (SELECT 1 FROM research_evaluations e JOIN research_runs r ON r.id=e.run_id
        WHERE e.id=NEW.source_evaluation_id AND r.owner_id=NEW.owner_id)
      OR EXISTS (SELECT 1 FROM research_runs r WHERE r.owner_id=NEW.owner_id AND r.idempotency_key=NEW.idempotency_key)
      OR EXISTS (SELECT 1 FROM comparison_submissions s WHERE s.owner_id=NEW.owner_id AND s.idempotency_key=NEW.idempotency_key)
      THEN RAISE EXCEPTION 'Controlled owner/split/source or request key differs'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_control_binding BEFORE INSERT OR UPDATE OR DELETE ON controlled_experiments
      FOR EACH ROW EXECUTE FUNCTION validate_control_binding();
    CREATE FUNCTION validate_control_arm_binding() RETURNS trigger AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Controlled arms are append-only'; END IF;
      IF NOT EXISTS (SELECT 1 FROM controlled_experiments c JOIN independent_evaluations i ON i.owner_id=c.owner_id
        JOIN research_runs r ON r.id=i.run_id WHERE c.id=NEW.controlled_experiment_id AND i.id=NEW.independent_id
        AND i.purpose='controlled' AND r.experiment_id=c.experiment_id AND r.split_version_id=c.split_version_id
        AND i.source_evaluation_id=c.source_evaluation_id AND i.protocol_sha256=c.protocol_sha256
        AND r.config->>'split' IN ('train','validation'))
      THEN RAISE EXCEPTION 'Controlled arm owner/protocol/split binding differs'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_control_arm_binding BEFORE INSERT OR UPDATE OR DELETE ON controlled_arms
      FOR EACH ROW EXECUTE FUNCTION validate_control_arm_binding();
    CREATE FUNCTION protect_control_comparison_key() RETURNS trigger AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM controlled_experiments c WHERE c.owner_id=NEW.owner_id AND c.idempotency_key=NEW.idempotency_key)
      THEN RAISE EXCEPTION 'Comparison key is reserved by a control submission'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER protect_control_comparison_key BEFORE INSERT ON comparison_submissions FOR EACH ROW EXECUTE FUNCTION protect_control_comparison_key();
    CREATE FUNCTION protect_control_run_key() RETURNS trigger AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM controlled_experiments c WHERE c.owner_id=NEW.owner_id AND c.idempotency_key=NEW.idempotency_key)
      THEN RAISE EXCEPTION 'Run key is reserved by a control submission'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER protect_control_run_key BEFORE INSERT ON research_runs FOR EACH ROW EXECUTE FUNCTION protect_control_run_key();
    """)


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM independent_evaluations) OR EXISTS (SELECT 1 FROM controlled_experiments)
      THEN RAISE EXCEPTION 'Independent evidence must be archived before downgrade'; END IF;
    END $$;
    DROP TRIGGER protect_control_run_key ON research_runs;
    DROP TRIGGER protect_control_comparison_key ON comparison_submissions;
    DROP TABLE controlled_arms, controlled_experiments, independent_evaluations;
    DROP FUNCTION validate_independent_binding();
    DROP FUNCTION validate_control_binding();
    DROP FUNCTION validate_control_arm_binding();
    DROP FUNCTION protect_control_run_key();
    DROP FUNCTION protect_control_comparison_key();
    """)
    _replace_run_binding(independent=False)
