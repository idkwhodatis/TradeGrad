"""Explicit inert PostgreSQL lifecycle boundary for component-only worker tests.

Production guarded_worker/shared_lifecycle and real storage-root flock still run.
This fake implements only the two reviewed advisory-lock commands, never sockets,
SQLAlchemy sessions, provider/model code or real database acceptance.
"""

from types import SimpleNamespace


class InertMaintenanceEngine:
    def __init__(self):
        self.connections = []

    def connect(self):
        connection = InertMaintenanceConnection()
        self.connections.append(connection)
        return connection


class InertMaintenanceConnection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self):
        self.closed = self.invalidated = self.held = False
        self.calls = []

    def execution_options(self, **options):
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def execute(self, statement, parameters):
        from backend.app.core.maintenance import LOCK_PARAMETERS

        assert parameters == LOCK_PARAMETERS
        command = str(statement)
        self.calls.append(command)
        if command == "SELECT pg_try_advisory_lock_shared(:namespace, :resource)":
            assert not self.held
            self.held = True
        elif command == "SELECT pg_advisory_unlock_shared(:namespace, :resource)":
            assert self.held
            self.held = False
        else:
            raise AssertionError("Component maintenance fake received unexpected SQL")
        return SimpleNamespace(scalar_one=lambda: True)

    def invalidate(self):
        self.invalidated = True

    def __enter__(self):
        assert not self.closed
        return self

    def __exit__(self, *_):
        self.closed = True
