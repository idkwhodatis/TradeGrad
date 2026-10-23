"""Append-only public provider preferences; preserve old jobs and evidence bytes."""

from alembic import op

revision = "006_provider_connections"
down_revision = "005_settings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE provider_connections (
      connection_id UUID NOT NULL,
      revision INTEGER NOT NULL CHECK (revision > 0),
      owner_id UUID NOT NULL REFERENCES users(id),
      provider VARCHAR(20) NOT NULL CHECK (provider IN ('akshare', 'yfinance', 'openbb')),
      config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
      config_sha256 VARCHAR(64) NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (connection_id, revision),
      UNIQUE (owner_id, provider, revision),
      CHECK (config->>'auth_mode' = 'public_no_auth'),
      CHECK (config->>'schema_version' = 'provider-connection.v1'),
      CHECK (config->>'provider' = provider),
      CHECK (config->>'connection_id' = connection_id::text),
      CHECK ((config->>'revision')::integer = revision),
      CHECK (config->>'config_sha256' = config_sha256)
    );
    CREATE INDEX ix_provider_connections_owner ON provider_connections(owner_id, provider, revision DESC);
    CREATE FUNCTION prevent_provider_connection_mutation() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'Provider connection revisions are immutable';
    END;
    $$ LANGUAGE plpgsql;
    CREATE TRIGGER immutable_provider_connections BEFORE UPDATE OR DELETE ON provider_connections
    FOR EACH ROW EXECUTE FUNCTION prevent_provider_connection_mutation();
    """)


def downgrade():
    op.execute(
        "DROP TABLE provider_connections; DROP FUNCTION prevent_provider_connection_mutation()"
    )
