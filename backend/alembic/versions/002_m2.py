"""Durable imports, stable listings, immutable universes and upstream rate budgets."""

from pathlib import Path

from alembic import op

revision = "002_m2"
down_revision = "001_m1"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(Path(__file__).with_name("002_m2.sql").read_text())


def downgrade():
    op.execute(
        "DROP TABLE provider_budgets, data_outbox, data_blocks, data_imports, universe_versions, vendor_mappings, owner_listings, listings, instruments;"
    )
