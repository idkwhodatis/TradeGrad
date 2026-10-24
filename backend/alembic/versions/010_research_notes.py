"""Append-only human research notes and retained immutable evidence inventories."""

from alembic import op

revision = "010_research_notes"
down_revision = "009_independent_evaluations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE research_notes (
      id UUID PRIMARY KEY, owner_id UUID NOT NULL REFERENCES users(id),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_research_notes_owner_id ON research_notes(owner_id);
    CREATE TABLE research_note_revisions (
      id UUID PRIMARY KEY, note_id UUID NOT NULL REFERENCES research_notes(id),
      owner_id UUID NOT NULL REFERENCES users(id),
      revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 10000),
      revision_sha256 VARCHAR(64) NOT NULL CHECK (revision_sha256 ~ '^[0-9a-f]{64}$'),
      pins_sha256 VARCHAR(64) NOT NULL CHECK (pins_sha256 ~ '^[0-9a-f]{64}$'),
      content JSONB NOT NULL CHECK (jsonb_typeof(content) = 'object'),
      evidence_snapshot JSONB NOT NULL CHECK (jsonb_typeof(evidence_snapshot) = 'array'),
      admission_snapshot JSONB NOT NULL CHECK (jsonb_typeof(admission_snapshot) = 'array'),
      title VARCHAR(240) NOT NULL CHECK (length(trim(title)) > 0),
      tags JSONB NOT NULL CHECK (jsonb_typeof(tags) = 'array'),
      status VARCHAR(40) NOT NULL CHECK (status IN ('pending','supported_under_current_tests','mixed','refuted_under_current_tests')),
      search_text TEXT NOT NULL, change_note TEXT CHECK (length(change_note) <= 2000),
      idempotency_key VARCHAR(128) NOT NULL CHECK (idempotency_key ~ '^[!-~]{1,128}$'),
      request_sha256 VARCHAR(64) NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_note_revision UNIQUE(note_id,revision),
      CONSTRAINT uq_note_owner_key UNIQUE(owner_id,idempotency_key),
      CHECK (content->>'title' = title AND content->>'status' = status AND content->'tags' = tags),
      CHECK (octet_length(content::text) <= 262144),
      CHECK (jsonb_array_length(content->'evidence') <= 32)
    );
    CREATE INDEX ix_research_note_revisions_note_id ON research_note_revisions(note_id);
    CREATE INDEX ix_research_note_revisions_owner_id ON research_note_revisions(owner_id);
    CREATE TABLE research_note_evidence_pins (
      id UUID PRIMARY KEY, revision_id UUID NOT NULL REFERENCES research_note_revisions(id),
      owner_id UUID NOT NULL REFERENCES users(id),
      resource_kind VARCHAR(30) NOT NULL CHECK (resource_kind IN ('artifact','dataset','universe','experiment','run','evaluation','comparison','strategy_version','candidate','spec_revision','controlled_experiment')),
      resource_id UUID NOT NULL,
      sha256 VARCHAR(64) CHECK (sha256 ~ '^[0-9a-f]{64}$'),
      details JSONB NOT NULL CHECK (jsonb_typeof(details) = 'object'),
      CONSTRAINT uq_note_evidence_pin UNIQUE(revision_id,resource_kind,resource_id),
      CHECK (resource_kind <> 'artifact' OR (sha256 IS NOT NULL AND details ?& ARRAY['storage_key','size_bytes','name','run_id','attempt_id','media_type']))
    );
    CREATE INDEX ix_research_note_evidence_pins_revision_id ON research_note_evidence_pins(revision_id);
    CREATE INDEX ix_research_note_evidence_pins_owner_id ON research_note_evidence_pins(owner_id);
    CREATE INDEX ix_research_note_evidence_pins_resource_id ON research_note_evidence_pins(resource_id);
    CREATE FUNCTION reject_research_note_change() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'Research notes and evidence revisions are append-only'; END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER immutable_research_note BEFORE UPDATE OR DELETE ON research_notes FOR EACH ROW EXECUTE FUNCTION reject_research_note_change();
    CREATE TRIGGER immutable_research_note_revision BEFORE UPDATE OR DELETE ON research_note_revisions FOR EACH ROW EXECUTE FUNCTION reject_research_note_change();
    CREATE TRIGGER immutable_research_note_pin BEFORE UPDATE OR DELETE ON research_note_evidence_pins FOR EACH ROW EXECUTE FUNCTION reject_research_note_change();
    CREATE FUNCTION validate_research_note_revision() RETURNS trigger AS $$
    DECLARE latest INTEGER;
    BEGIN
      PERFORM 1 FROM research_notes WHERE id=NEW.note_id AND owner_id=NEW.owner_id FOR UPDATE;
      IF NOT FOUND THEN RAISE EXCEPTION 'Research note ownership differs'; END IF;
      SELECT coalesce(max(revision),0) INTO latest FROM research_note_revisions WHERE note_id=NEW.note_id;
      IF NEW.revision <> latest + 1 THEN RAISE EXCEPTION 'Research note revisions must be consecutive'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_research_note_revision BEFORE INSERT ON research_note_revisions FOR EACH ROW EXECUTE FUNCTION validate_research_note_revision();
    CREATE FUNCTION validate_research_note_pin() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM research_note_revisions r WHERE r.id=NEW.revision_id AND r.owner_id=NEW.owner_id)
      THEN RAISE EXCEPTION 'Research note evidence owner differs'; END IF;
      IF NEW.resource_kind='artifact' AND NOT EXISTS (
        SELECT 1 FROM artifacts a JOIN research_runs r ON r.id=a.run_id
        WHERE a.id=NEW.resource_id AND r.owner_id=NEW.owner_id AND a.sha256=NEW.sha256
          AND a.storage_key=NEW.details->>'storage_key' AND a.size_bytes=(NEW.details->>'size_bytes')::bigint
          AND a.name=NEW.details->>'name' AND a.run_id::text=NEW.details->>'run_id'
          AND a.attempt_id::text=NEW.details->>'attempt_id' AND a.media_type=NEW.details->>'media_type'
      ) AND NOT EXISTS (
        SELECT 1 FROM research_note_evidence_pins old
          JOIN research_note_revisions prior ON prior.id=old.revision_id
          JOIN research_note_revisions current ON current.id=NEW.revision_id
        WHERE old.owner_id=NEW.owner_id AND prior.note_id=current.note_id AND prior.revision<current.revision
          AND old.resource_kind=NEW.resource_kind AND old.resource_id=NEW.resource_id
          AND old.sha256=NEW.sha256 AND old.details=NEW.details
      ) THEN RAISE EXCEPTION 'Research note artifact registration differs'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_research_note_pin BEFORE INSERT ON research_note_evidence_pins FOR EACH ROW EXECUTE FUNCTION validate_research_note_pin();
    """)


def downgrade():
    op.execute("""
    DO $$ BEGIN IF EXISTS (SELECT 1 FROM research_notes)
      THEN RAISE EXCEPTION 'Research notes and retained evidence must be archived before downgrade'; END IF; END $$;
    DROP TABLE research_note_evidence_pins, research_note_revisions, research_notes;
    DROP FUNCTION reject_research_note_change();
    DROP FUNCTION validate_research_note_revision();
    DROP FUNCTION validate_research_note_pin();
    """)
