from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import Settings


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
