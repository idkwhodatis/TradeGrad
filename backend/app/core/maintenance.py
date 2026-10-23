"""Cooperative cold-maintenance barriers for one PostgreSQL/storage installation.

Global PostgreSQL (two-int namespace) is always first, before owner/row/queue
locks and storage-root flock. API publishers can take their shared flock after
owner locks; workers retain it before inner transactions. Maintenance never
acquires owner/row locks, and all barrier acquisitions are nonblocking, so those
post-global orders cannot form a maintenance wait cycle. A worker retains both
global guards through its final stop/publication/cleanup. Transaction locks
are acquired before the first SQL statement, including reads which can later
mutate. Raw queue clients and migrations must be stopped by the cold operator.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# PostgreSQL's two-int keyspace is distinct from existing hashed bigint owner keys.
LOCK_NAMESPACE = 0x54475244  # TGRD
LOCK_RESOURCE = 0x434F4C44  # COLD
_lifecycle = ContextVar("tradegrad_storage_lifecycle", default=None)
LOCK_PARAMETERS = {"namespace": LOCK_NAMESPACE, "resource": LOCK_RESOURCE}


class MaintenanceBusy(SQLAlchemyError):
    """Safe, retryable refusal. Never carries SQL, paths or credentials."""


def _lock(connection, *, exclusive=False, transaction=True):
    if connection.dialect.name != "postgresql":
        raise MaintenanceBusy("Cold-maintenance guards require PostgreSQL")
    scope = "xact_" if transaction else ""
    mode = "" if exclusive else "_shared"
    acquired = connection.execute(
        text(f"SELECT pg_try_advisory_{scope}lock{mode}(:namespace, :resource)"),
        LOCK_PARAMETERS,
    ).scalar_one()
    if acquired is not True:
        raise MaintenanceBusy(
            "Cold maintenance or a storage writer is active; retry later"
        )


@event.listens_for(Session, "after_begin")
def _admit_transaction(session, transaction, connection):
    # SAVEPOINTs share the outer transaction lock; do not change migration engines.
    if not transaction.nested and connection.dialect.name == "postgresql":
        try:
            _lock(connection)
        except SQLAlchemyError:
            # SQLAlchemy may already have associated this connection with the
            # Session when an after_begin listener raises. Poison that transaction
            # so catching the denial cannot reuse it without a fresh admission.
            connection.invalidate()
            raise


@contextmanager
def shared_lifecycle(engine, storage_root):
    """Dedicated autocommit PG session + root flock, spanning worker transactions.

    The filesystem guard remains held if this PG connection is lost. Maintenance
    then refuses its filesystem try-lock. If this process dies, persisted attempt
    and evaluator unknown-stop fences prohibit cleanup until containment is proved.
    """
    from backend.app.core.storage_barrier import storage_guard

    identity = (engine, Path(storage_root).absolute())
    if _lifecycle.get() == identity:
        yield
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        _lock(connection, transaction=False)
        try:
            with storage_guard(storage_root):
                token = _lifecycle.set(identity)
                try:
                    yield
                finally:
                    _lifecycle.reset(token)
        finally:
            try:
                if connection.closed or connection.invalidated:
                    raise MaintenanceBusy(
                        "Lifecycle connection ended; do not reconnect"
                    )
                released = connection.execute(
                    text("SELECT pg_advisory_unlock_shared(:namespace, :resource)"),
                    LOCK_PARAMETERS,
                ).scalar_one()
                if released is not True:
                    connection.invalidate()
            except Exception:  # noqa: BLE001 -- never pool an uncertain session lock
                # Never return an uncertain session lock to the pool. No reconnect
                # is attempted; the root guard has covered the full body/finally.
                connection.invalidate()


def guarded_worker(function):
    """Guard complete worker entrypoints, including direct configured-mode calls."""
    parameters = signature(function)

    @wraps(function)
    def guarded(*args, **kwargs):
        from backend.app.core.config import get_settings
        from backend.app.core.db import create_db_engine, session_factory

        bound = parameters.bind(*args, **kwargs)
        bound.apply_defaults()
        settings = bound.arguments.get("settings") or get_settings()
        factory = bound.arguments.get("factory")
        owned_engine = None
        if factory is None:
            owned_engine = create_db_engine(settings)
            factory = session_factory(owned_engine)
        bound.arguments["settings"] = settings
        bound.arguments["factory"] = factory
        try:
            with shared_lifecycle(factory.kw["bind"], Path(settings.storage_root)):
                return function(*bound.args, **bound.kwargs)
        finally:
            if owned_engine is not None:
                owned_engine.dispose()

    return guarded
