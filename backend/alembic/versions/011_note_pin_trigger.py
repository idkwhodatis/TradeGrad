"""Remove a table alias collision with PL/pgSQL's implicit OLD trigger record.

Only the function body changes. All ownership, artifact registration and retained
revision predicates remain identical; no note, revision or pin is rewritten.
"""

from alembic import op

revision = "011_note_pin_trigger"
down_revision = "010_research_notes"
branch_labels = None
depends_on = None

PIN_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION validate_research_note_pin() RETURNS trigger AS $$
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
    SELECT 1 FROM research_note_evidence_pins prior_pin
      JOIN research_note_revisions prior_revision ON prior_revision.id=prior_pin.revision_id
      JOIN research_note_revisions current_revision ON current_revision.id=NEW.revision_id
    WHERE prior_pin.owner_id=NEW.owner_id AND prior_revision.note_id=current_revision.note_id AND prior_revision.revision<current_revision.revision
      AND prior_pin.resource_kind=NEW.resource_kind AND prior_pin.resource_id=NEW.resource_id
      AND prior_pin.sha256=NEW.sha256 AND prior_pin.details=NEW.details
  ) THEN RAISE EXCEPTION 'Research note artifact registration differs'; END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
"""


def upgrade():
    op.execute(PIN_FUNCTION_SQL)


def downgrade():
    # Restore the precise historical 010 function for an explicit schema rollback.
    # Its known ambiguous alias must not be reintroduced into the current head.
    op.execute(
        PIN_FUNCTION_SQL.replace("prior_pin", "old")
        .replace("prior_revision", "prior")
        .replace("current_revision", "current")
    )
