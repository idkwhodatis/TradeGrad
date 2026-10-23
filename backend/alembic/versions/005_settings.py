"""Mutable workspace preferences, bounded quotas and profile restrictions only.

No historical configuration, request hash or published version is rewritten.
"""

from alembic import op

revision = "005_settings"
down_revision = "004_m5"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE user_workspace_settings (
      owner_id UUID PRIMARY KEY REFERENCES users(id),
      defaults JSONB NOT NULL DEFAULT '{}'::jsonb,
      max_active_runs INTEGER CHECK (max_active_runs BETWEEN 0 AND 100),
      max_active_imports INTEGER CHECK (max_active_imports BETWEEN 0 AND 100),
      disabled_model_profiles JSONB NOT NULL DEFAULT '[]'::jsonb,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CHECK (jsonb_typeof(defaults) = 'object'),
      CHECK (jsonb_typeof(disabled_model_profiles) = 'array')
    );
    """)


def downgrade():
    # Mutable preferences only. Immutable evidence remains in the older schema.
    op.execute("DROP TABLE user_workspace_settings")
