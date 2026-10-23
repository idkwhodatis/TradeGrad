"""Immutable experiment splits, selected code, owner-wide exposure and final runs.

Old tradegrad.run.v1 JSON/hash bytes and historical unbound rows are untouched.
"""

from pathlib import Path

from alembic import op

revision = "004_m5"
down_revision = "003_m3"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_name("004_m5.sql").read_text())
    op.execute("""
      ALTER TABLE strategy_versions ADD COLUMN manifest JSONB, ADD COLUMN parameters JSONB;
      ALTER TABLE universe_versions ADD COLUMN definition JSONB;
      CREATE FUNCTION protect_universe_definition() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.definition IS DISTINCT FROM OLD.definition THEN RAISE EXCEPTION 'Original universe definition is immutable'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_universe_definition BEFORE UPDATE ON universe_versions FOR EACH ROW EXECUTE FUNCTION protect_universe_definition();
      ALTER TABLE research_runs
        ADD COLUMN portable_policy_image_digest VARCHAR(255),
        ADD COLUMN submission_binding JSONB,
        ADD COLUMN experiment_id UUID REFERENCES experiments(id),
        ADD COLUMN split_version_id UUID REFERENCES split_versions(id),
        ADD COLUMN final_selection_id UUID REFERENCES final_selections(id);
      DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT conname FROM pg_constraint WHERE conrelid = 'research_evaluations'::regclass
          AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%split%'
        LOOP EXECUTE format('ALTER TABLE research_evaluations DROP CONSTRAINT %I', item.conname); END LOOP;
      END $$;
      ALTER TABLE research_evaluations ADD CONSTRAINT ck_evaluation_split CHECK (split IN ('train','validation','holdout','backtest'));
      CREATE FUNCTION reject_m5_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Published selection, split, exposure and package records are immutable'; END $$;
      CREATE TRIGGER immutable_experiment BEFORE UPDATE OR DELETE ON experiments FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE TRIGGER immutable_split_version BEFORE UPDATE OR DELETE ON split_versions FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE TRIGGER immutable_final_selection BEFORE UPDATE OR DELETE ON final_selections FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE TRIGGER append_only_data_exposure BEFORE UPDATE OR DELETE ON data_usage_exposures FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE TRIGGER immutable_strategy_package BEFORE UPDATE OR DELETE ON strategy_packages FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE TRIGGER immutable_strategy_version BEFORE UPDATE OR DELETE ON strategy_versions FOR EACH ROW EXECUTE FUNCTION reject_m5_immutable_change();
      CREATE FUNCTION protect_m5_run_binding() RETURNS trigger LANGUAGE plpgsql AS $$
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
        IF COALESCE(NEW.config->>'schema_version', '') IN ('tradegrad.portable.run.v1', 'tradegrad.portable.final-evaluation.v1') THEN
          IF (NEW.research_image_digest IS NULL) <> (NEW.portable_policy_image_digest IS NULL) THEN
            RAISE EXCEPTION 'Portable images must be bound together';
          END IF;
        ELSIF NEW.portable_policy_image_digest IS NOT NULL THEN
          RAISE EXCEPTION 'Policy image is exclusive to portable execution';
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_m5_run_binding BEFORE INSERT OR UPDATE ON research_runs FOR EACH ROW EXECUTE FUNCTION protect_m5_run_binding();
      CREATE FUNCTION protect_m5_owner_reference() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_TABLE_NAME = 'experiments' THEN
          IF NEW.parent_experiment_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM experiments e WHERE e.id = NEW.parent_experiment_id AND e.owner_id = NEW.owner_id
              AND coalesce(e.root_experiment_id, e.id) = NEW.root_experiment_id
          ) THEN RAISE EXCEPTION 'Experiment parent/root ownership differs'; END IF;
          IF NEW.parent_experiment_id IS NULL AND NEW.root_experiment_id IS NOT NULL THEN
            RAISE EXCEPTION 'Root experiment cannot point to another root'; END IF;
        ELSIF TG_TABLE_NAME = 'split_versions' THEN
          IF NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = NEW.experiment_id AND e.owner_id = NEW.owner_id)
            OR NOT EXISTS (SELECT 1 FROM dataset_versions d WHERE d.id = NEW.dataset_version_id AND d.owner_id = NEW.owner_id)
            OR NOT EXISTS (SELECT 1 FROM universe_versions u WHERE u.id = NEW.universe_version_id AND u.owner_id = NEW.owner_id)
          THEN RAISE EXCEPTION 'Split references must have the same owner'; END IF;
        ELSIF TG_TABLE_NAME = 'final_selections' THEN
          IF NOT EXISTS (SELECT 1 FROM research_runs r WHERE r.id = NEW.source_run_id AND r.owner_id = NEW.owner_id
            AND r.experiment_id = NEW.experiment_id AND r.split_version_id = NEW.split_version_id AND r.status = 'succeeded')
            OR NOT EXISTS (SELECT 1 FROM research_candidates c WHERE c.id = NEW.source_candidate_id AND c.run_id = NEW.source_run_id)
            OR NOT EXISTS (SELECT 1 FROM research_checkpoints c WHERE c.id = NEW.source_checkpoint_id AND c.run_id = NEW.source_run_id AND c.complete)
            OR NOT EXISTS (SELECT 1 FROM strategy_versions s WHERE s.id = NEW.strategy_version_id AND s.owner_id = NEW.owner_id)
          THEN RAISE EXCEPTION 'Selection references must be owned and bound'; END IF;
        ELSIF TG_TABLE_NAME = 'data_usage_exposures' THEN
          IF (NEW.experiment_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = NEW.experiment_id AND e.owner_id = NEW.owner_id))
            OR (NEW.split_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM split_versions s WHERE s.id = NEW.split_version_id AND s.owner_id = NEW.owner_id AND s.experiment_id = NEW.experiment_id))
            OR (NEW.run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM research_runs r WHERE r.id = NEW.run_id AND r.owner_id = NEW.owner_id))
            OR (NEW.dataset_version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dataset_versions d WHERE d.id = NEW.dataset_version_id AND d.owner_id = NEW.owner_id))
          THEN RAISE EXCEPTION 'Exposure references must have the same owner'; END IF;
        ELSIF TG_TABLE_NAME = 'strategy_packages' THEN
          IF NOT EXISTS (SELECT 1 FROM final_selections f WHERE f.id = NEW.final_selection_id AND f.owner_id = NEW.owner_id)
          THEN RAISE EXCEPTION 'Package selection must have the same owner'; END IF;
        END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER owner_experiment BEFORE INSERT ON experiments FOR EACH ROW EXECUTE FUNCTION protect_m5_owner_reference();
      CREATE TRIGGER owner_split BEFORE INSERT ON split_versions FOR EACH ROW EXECUTE FUNCTION protect_m5_owner_reference();
      CREATE TRIGGER owner_selection BEFORE INSERT ON final_selections FOR EACH ROW EXECUTE FUNCTION protect_m5_owner_reference();
      CREATE TRIGGER owner_exposure BEFORE INSERT ON data_usage_exposures FOR EACH ROW EXECUTE FUNCTION protect_m5_owner_reference();
      CREATE TRIGGER owner_package BEFORE INSERT ON strategy_packages FOR EACH ROW EXECUTE FUNCTION protect_m5_owner_reference();
    """)


def downgrade():
    # Refuse a destructive rollback after final execution instead of silently
    # stripping reproducibility/exposure evidence from surviving run rows.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM final_selections) OR EXISTS (SELECT 1 FROM data_usage_exposures)
      THEN RAISE EXCEPTION 'M5 contains immutable evidence; archive explicitly before downgrade'; END IF;
    END $$;
    DROP TRIGGER immutable_m5_run_binding ON research_runs;
    DROP FUNCTION protect_m5_run_binding();
    DROP TRIGGER immutable_strategy_version ON strategy_versions;
    ALTER TABLE research_runs DROP COLUMN final_selection_id, DROP COLUMN split_version_id, DROP COLUMN experiment_id, DROP COLUMN portable_policy_image_digest, DROP COLUMN submission_binding;
    ALTER TABLE strategy_versions DROP COLUMN manifest, DROP COLUMN parameters;
    DROP TRIGGER immutable_universe_definition ON universe_versions;
    DROP FUNCTION protect_universe_definition();
    ALTER TABLE universe_versions DROP COLUMN definition;
    DROP TABLE strategy_packages, data_usage_exposures, final_selections, split_versions, experiments;
    DROP FUNCTION protect_m5_owner_reference();
    DROP FUNCTION reject_m5_immutable_change();
    ALTER TABLE research_evaluations DROP CONSTRAINT ck_evaluation_split;
    ALTER TABLE research_evaluations ADD CONSTRAINT ck_evaluation_split CHECK (split IN ('train','validation'));
    """)
