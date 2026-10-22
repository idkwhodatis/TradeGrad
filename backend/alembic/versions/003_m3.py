"""Fenced research provenance, complete checkpoint pointers and pause lifecycle."""

from pathlib import Path

from alembic import op

revision = "003_m3"
down_revision = "002_m2"
branch_labels = None
depends_on = None


def _status_constraint(statuses):
    op.execute("""DO $$ DECLARE item record; BEGIN
      FOR item IN SELECT conname FROM pg_constraint WHERE conrelid = 'research_runs'::regclass AND contype = 'c'
        AND pg_get_constraintdef(oid) LIKE '%status%'
      LOOP EXECUTE format('ALTER TABLE research_runs DROP CONSTRAINT %I', item.conname); END LOOP;
    END $$;""")
    values = ",".join("'" + value + "'" for value in statuses)
    op.execute(
        f"ALTER TABLE research_runs ADD CONSTRAINT ck_research_run_status CHECK (status IN ({values}))"
    )


def upgrade():
    _status_constraint(
        [
            "queued",
            "preparing",
            "running",
            "finalizing",
            "pause_requested",
            "paused",
            "cancel_requested",
            "cancelled",
            "failed",
            "interrupted",
            "succeeded",
        ]
    )
    op.execute(Path(__file__).with_name("003_m3.sql").read_text())
    op.execute("""ALTER TABLE research_runs
      ADD COLUMN research_image_digest VARCHAR(255),
      ADD COLUMN latest_checkpoint_id UUID REFERENCES research_checkpoints(id),
      ADD COLUMN resume_checkpoint_id UUID REFERENCES research_checkpoints(id);
      ALTER TABLE execution_attempts ADD COLUMN resumed_checkpoint_id UUID REFERENCES research_checkpoints(id);""")
    op.execute("""CREATE FUNCTION reject_research_version_update() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Published research versions are immutable'; END $$;
      CREATE TRIGGER immutable_research_candidate BEFORE UPDATE ON research_candidates FOR EACH ROW EXECUTE FUNCTION reject_research_version_update();
      CREATE TRIGGER immutable_research_checkpoint BEFORE UPDATE ON research_checkpoints FOR EACH ROW EXECUTE FUNCTION reject_research_version_update();
      CREATE FUNCTION protect_research_inputs() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.config IS DISTINCT FROM OLD.config OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
          OR NEW.dataset_version_id IS DISTINCT FROM OLD.dataset_version_id OR NEW.strategy_version_id IS DISTINCT FROM OLD.strategy_version_id
          OR NEW.request_sha256 IS DISTINCT FROM OLD.request_sha256 OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
          OR (OLD.research_image_digest IS NOT NULL AND NEW.research_image_digest IS DISTINCT FROM OLD.research_image_digest)
        THEN RAISE EXCEPTION 'Run input provenance is immutable'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_research_inputs BEFORE UPDATE ON research_runs FOR EACH ROW EXECUTE FUNCTION protect_research_inputs();
      CREATE FUNCTION protect_evaluation_payload() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
          OR NEW.generation IS DISTINCT FROM OLD.generation OR NEW.candidate_key IS DISTINCT FROM OLD.candidate_key
          OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256 OR NEW."window" IS DISTINCT FROM OLD."window"
          OR NEW.window_sha256 IS DISTINCT FROM OLD.window_sha256 OR NEW.split IS DISTINCT FROM OLD.split
          OR NEW.sandbox_id IS DISTINCT FROM OLD.sandbox_id OR NEW.image_digest IS DISTINCT FROM OLD.image_digest
          OR (OLD.candidate_id IS NOT NULL AND NEW.candidate_id IS DISTINCT FROM OLD.candidate_id)
          OR (OLD.finished_at IS NOT NULL AND (NEW.payload IS DISTINCT FROM OLD.payload OR NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256 OR NEW.status IS DISTINCT FROM OLD.status OR NEW.sandbox_stopped IS DISTINCT FROM OLD.sandbox_stopped))
        THEN RAISE EXCEPTION 'Evaluation provenance is immutable'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_evaluation_payload BEFORE UPDATE ON research_evaluations FOR EACH ROW EXECUTE FUNCTION protect_evaluation_payload();
      CREATE FUNCTION protect_model_call_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
          OR NEW.generation IS DISTINCT FROM OLD.generation OR NEW.logical_call_id IS DISTINCT FROM OLD.logical_call_id
          OR NEW.request_sha256 IS DISTINCT FROM OLD.request_sha256
          OR (OLD.adopted AND NOT NEW.adopted)
          OR (OLD.status <> 'started' AND ((NEW.payload - 'adopted') IS DISTINCT FROM (OLD.payload - 'adopted')
            OR NEW.response_sha256 IS DISTINCT FROM OLD.response_sha256 OR NEW.usage IS DISTINCT FROM OLD.usage))
        THEN RAISE EXCEPTION 'Model call provenance is immutable'; END IF;
        RETURN NEW;
      END $$;
      CREATE TRIGGER immutable_model_call_provenance BEFORE UPDATE ON research_model_calls FOR EACH ROW EXECUTE FUNCTION protect_model_call_provenance();""")


def downgrade():
    op.execute("""DROP TRIGGER immutable_research_inputs ON research_runs;
      DROP FUNCTION protect_research_inputs();
      DROP TRIGGER immutable_model_call_provenance ON research_model_calls;
      DROP FUNCTION protect_model_call_provenance();
      DROP TRIGGER immutable_evaluation_payload ON research_evaluations;
      DROP FUNCTION protect_evaluation_payload();
      DROP TRIGGER immutable_research_candidate ON research_candidates;
      DROP TRIGGER immutable_research_checkpoint ON research_checkpoints;
      DROP FUNCTION reject_research_version_update();
      ALTER TABLE research_runs DROP COLUMN latest_checkpoint_id, DROP COLUMN resume_checkpoint_id, DROP COLUMN research_image_digest;
      ALTER TABLE execution_attempts DROP COLUMN resumed_checkpoint_id;
      DROP TABLE research_checkpoints, research_model_calls, research_evaluations, research_candidates;""")
    op.execute(
        "UPDATE research_runs SET status = 'interrupted' WHERE status IN ('paused','pause_requested')"
    )
    _status_constraint(
        [
            "queued",
            "preparing",
            "running",
            "finalizing",
            "cancel_requested",
            "cancelled",
            "failed",
            "interrupted",
            "succeeded",
        ]
    )
