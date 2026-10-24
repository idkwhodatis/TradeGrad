"""Immutable version-bound descriptions and separate owner-scoped bookmarks."""

from alembic import op

revision = "007_research_specs"
down_revision = "006_provider_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE research_candidates ADD COLUMN lineage_context JSONB;
    CREATE TABLE strategy_spec_revisions (
      id UUID PRIMARY KEY,
      owner_id UUID NOT NULL REFERENCES users(id),
      strategy_version_id UUID REFERENCES strategy_versions(id),
      candidate_id UUID REFERENCES research_candidates(id),
      code_sha256 VARCHAR(64) NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
      revision INTEGER NOT NULL CHECK (revision > 0),
      spec_sha256 VARCHAR(64) NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
      spec JSONB NOT NULL CHECK (jsonb_typeof(spec) = 'object'),
      origin VARCHAR(20) NOT NULL CHECK (origin IN ('static', 'human')),
      change_note TEXT,
      request_sha256 VARCHAR(64),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CHECK ((strategy_version_id IS NULL) <> (candidate_id IS NULL)),
      CHECK (spec->>'code_sha256' = code_sha256),
      CHECK ((spec->>'revision')::integer = revision),
      CHECK (spec->>'schema_version' = 'tradegrad.strategy-spec.v1'),
      CONSTRAINT uq_strategy_spec_revision UNIQUE (strategy_version_id, revision),
      CONSTRAINT uq_candidate_spec_revision UNIQUE (candidate_id, revision)
    );
    CREATE INDEX ix_strategy_spec_revisions_owner_id ON strategy_spec_revisions(owner_id);
    CREATE TABLE candidate_bookmarks (
      owner_id UUID NOT NULL REFERENCES users(id),
      candidate_id UUID NOT NULL REFERENCES research_candidates(id),
      code_sha256 VARCHAR(64) NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (owner_id, candidate_id)
    );
    CREATE FUNCTION validate_research_spec_owner() RETURNS trigger AS $$
    BEGIN
      IF NEW.candidate_id IS NOT NULL THEN
        IF NOT EXISTS (
          SELECT 1 FROM research_candidates c JOIN research_runs r ON r.id = c.run_id
          WHERE c.id = NEW.candidate_id AND r.owner_id = NEW.owner_id AND c.code_sha256 = NEW.code_sha256
        ) THEN RAISE EXCEPTION 'Candidate description or bookmark ownership/hash mismatch'; END IF;
      ELSIF NOT EXISTS (
        SELECT 1 FROM strategy_versions s WHERE s.id = NEW.strategy_version_id
          AND s.owner_id = NEW.owner_id AND s.content_sha256 = NEW.code_sha256
      ) THEN RAISE EXCEPTION 'Strategy description ownership/hash mismatch'; END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER validate_strategy_spec_owner BEFORE INSERT ON strategy_spec_revisions
      FOR EACH ROW EXECUTE FUNCTION validate_research_spec_owner();
    CREATE TRIGGER validate_candidate_bookmark_owner BEFORE INSERT OR UPDATE ON candidate_bookmarks
      FOR EACH ROW EXECUTE FUNCTION validate_research_spec_owner();
    CREATE FUNCTION reject_research_spec_change() RETURNS trigger AS $$
    BEGIN RAISE EXCEPTION 'Strategy descriptions are append-only'; END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER immutable_strategy_spec_revision BEFORE UPDATE OR DELETE ON strategy_spec_revisions
      FOR EACH ROW EXECUTE FUNCTION reject_research_spec_change();
    """)


def downgrade():
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM strategy_spec_revisions) OR EXISTS (SELECT 1 FROM candidate_bookmarks)
        OR EXISTS (SELECT 1 FROM research_candidates WHERE lineage_context IS NOT NULL)
      THEN RAISE EXCEPTION 'Research descriptions, bookmarks and generation context must be archived before downgrade'; END IF;
    END $$;
    DROP TABLE candidate_bookmarks, strategy_spec_revisions;
    ALTER TABLE research_candidates DROP COLUMN lineage_context;
    DROP FUNCTION validate_research_spec_owner();
    DROP FUNCTION reject_research_spec_change();
    """)
