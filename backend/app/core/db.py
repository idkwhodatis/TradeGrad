from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core import maintenance  # noqa: F401 -- installs transaction admission guard
from backend.app.core.config import Settings

# This executable requires the complete increment schema, not merely a prior
# supported backup revision. An offline contract test pins this to Alembic head.
REQUIRED_SCHEMA_REVISION = "011_note_pin_trigger"


def create_db_engine(settings: Settings):
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        connect_args={
            "connect_timeout": 5,
            "options": "-cstatement_timeout=10000 -clock_timeout=5000 -cidle_in_transaction_session_timeout=30000",
        },
    )


def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)
