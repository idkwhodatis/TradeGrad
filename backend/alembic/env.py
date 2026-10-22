from alembic import context
from sqlalchemy import create_engine

from backend.app.core.config import get_settings
from backend.app.models import Base

config = context.config


def configure(connection):
    context.configure(
        connection=connection, target_metadata=Base.metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    configure(config.attributes["connection"])
else:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as connection:
        configure(connection)
