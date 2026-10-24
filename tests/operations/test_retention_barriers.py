# ruff: noqa: SIM117
"""Inert lock/contract tests. No PostgreSQL, subprocess, strategy or apply execution."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.core import maintenance
from backend.app.core.atomic_storage import publish_bytes
from backend.app.core.storage_barrier import storage_guard
from backend.app.operations_cold_retention import ACKNOWLEDGEMENT, cold_retention
from backend.app.operations_retention import RetentionError

ROOT = Path(__file__).resolve().parents[2]


class Connection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, acquired=True):
        self.acquired = acquired
        self.calls = []
        self.invalidated = False
        self.closed = False

    def execute(self, statement, parameters):
        self.calls.append((str(statement), parameters))
        return SimpleNamespace(scalar_one=lambda: self.acquired)

    def invalidate(self):
        self.invalidated = True

    def execution_options(self, **options):
        assert options == {"isolation_level": "AUTOCOMMIT"}
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        pass


def test_shared_transaction_uses_disjoint_two_integer_key_before_sql():
    connection = Connection()
    maintenance._admit_transaction(None, SimpleNamespace(nested=False), connection)
    assert connection.calls == [
        (
            "SELECT pg_try_advisory_xact_lock_shared(:namespace, :resource)",
            maintenance.LOCK_PARAMETERS,
        )
    ]
    maintenance._admit_transaction(None, SimpleNamespace(nested=True), connection)
    assert len(connection.calls) == 1


def test_pg_contention_fails_without_retry_or_wait():
    connection = Connection(False)
    with pytest.raises(maintenance.MaintenanceBusy, match="retry later"):
        maintenance._lock(connection, exclusive=True)
    assert len(connection.calls) == 1
    assert "pg_try_advisory_xact_lock(" in connection.calls[0][0]


def test_shared_root_excludes_exclusive_and_nested_shared_is_allowed(tmp_path):
    with storage_guard(tmp_path), storage_guard(tmp_path):
        with pytest.raises(maintenance.MaintenanceBusy):
            with storage_guard(tmp_path, exclusive=True):
                pytest.fail("Exclusive acquisition must refuse an active writer")
    with storage_guard(tmp_path, exclusive=True):
        with pytest.raises(maintenance.MaintenanceBusy):
            with storage_guard(tmp_path):
                pytest.fail("Writer admission must refuse active maintenance")


def test_atomic_publication_refuses_before_creating_any_file(tmp_path):
    import hashlib

    with storage_guard(tmp_path, exclusive=True):
        with pytest.raises(maintenance.MaintenanceBusy):
            publish_bytes(
                tmp_path / "uncreated",
                b"inert",
                sha256=hashlib.sha256(b"inert").hexdigest(),
                durable_root=tmp_path,
            )
    assert list(tmp_path.iterdir()) == []


def test_lifecycle_holds_storage_until_body_finally_and_reuses_nested_guard(tmp_path):
    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection)
    with maintenance.shared_lifecycle(engine, tmp_path):
        with maintenance.shared_lifecycle(engine, tmp_path):
            assert len(connection.calls) == 1
            with pytest.raises(maintenance.MaintenanceBusy):
                with storage_guard(tmp_path, exclusive=True):
                    pytest.fail("Lifecycle must remain guarded")
    assert "pg_advisory_unlock_shared" in connection.calls[-1][0]
    with storage_guard(tmp_path, exclusive=True):
        pass


def test_lost_database_lock_does_not_release_filesystem_guard_early(tmp_path):
    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection)
    with maintenance.shared_lifecycle(engine, tmp_path):
        connection.acquired = False  # session lock disappeared; no reconnect here
        with pytest.raises(maintenance.MaintenanceBusy):
            with storage_guard(tmp_path, exclusive=True):
                pytest.fail("DB loss cannot imply storage writer stopped")
    assert connection.invalidated


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"acknowledgement": ACKNOWLEDGEMENT},
        {
            "acknowledgement": ACKNOWLEDGEMENT,
            "expected_plan_sha256": "g" * 64,
            "receipt_path": Path("unused"),
        },
    ],
)
def test_apply_requires_plan_ack_receipt_before_database_or_storage(changes):
    with pytest.raises(RetentionError, match="Apply requires"):
        cold_retention(
            database=object(),
            storage_root=Path("/never-open"),
            quiesced=True,
            apply=True,
            **changes,
        )


@pytest.mark.parametrize(
    "path,name",
    [
        ("workers/data/executor.py", "execute"),
        ("workers/research/executor.py", "execute"),
        ("workers/research/optimization.py", "execute_configured"),
        ("workers/research/portable_optimization.py", "execute_portable_configured"),
        ("workers/research/final_evaluation.py", "execute_final"),
        ("workers/research/portable_final_evaluation.py", "execute_portable_final"),
        ("workers/research/comparison.py", "execute_comparison"),
    ],
)
def test_all_production_worker_entrypoints_wrap_entire_finally(path, name):
    tree = ast.parse((ROOT / path).read_text())
    entry = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    assert any(
        isinstance(node, ast.Name) and node.id == "guarded_worker"
        for node in entry.decorator_list
    )


def test_production_atomic_publishers_always_name_durable_root():
    paths = list((ROOT / "workers").rglob("*.py")) + list(
        (ROOT / "backend/app").rglob("*.py")
    )
    for path in paths:
        if path.name == "atomic_storage.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "publish_bytes"
            ):
                assert any(key.arg == "durable_root" for key in node.keywords), (
                    path.relative_to(ROOT)
                )


def test_lower_level_preview_still_cannot_be_used_as_an_apply_bypass():
    from backend.app.operations_retention import preview_retention

    with pytest.raises(RetentionError, match="exclusion protocol"):
        preview_retention(
            database=object(),
            storage_root=Path("/never-open"),
            quiesced=True,
            apply=True,
        )


@pytest.mark.parametrize(
    "events,accepted",
    [
        ([], True),
        (
            [
                ("model_process_admitted", {"call_id": "one"}),
                ("model_process_stopped", {"call_id": "one"}),
            ],
            True,
        ),
        ([("model_process_admitted", {"call_id": "one"})], False),
        ([("model_process_stopped", {"call_id": "one"})], False),
        (
            [
                ("model_process_admitted", {"call_id": "one"}),
                ("model_process_admitted", {"call_id": "one"}),
            ],
            False,
        ),
        ([("model_process_admitted", {"secret": "unexpected-shape"})], False),
    ],
)
def test_model_subprocess_unknown_stop_cannot_be_hidden_by_terminal_attempt(
    events, accepted
):
    from backend.app.operations_cold_retention import LockedDatabase

    rows = [
        {"attempt_id": "inert-attempt", "type": kind, "payload": value}
        for kind, value in events
    ]

    class Inspector:
        def execute(self, statement):
            if "count(*)" in str(statement):
                return SimpleNamespace(scalar_one=lambda: 0)
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    database = object.__new__(LockedDatabase)
    database.connection = Inspector()
    database.assert_locked = lambda: None
    if accepted:
        assert database._inspect(lambda connection: "inspected") == "inspected"
    else:
        with pytest.raises(RetentionError, match="model process"):
            database._inspect(
                lambda connection: pytest.fail("Unstopped process admitted")
            )


def test_failed_admission_invalidates_connection_against_same_session_reuse():
    connection = Connection(False)
    with pytest.raises(maintenance.MaintenanceBusy):
        maintenance._admit_transaction(None, SimpleNamespace(nested=False), connection)
    assert connection.invalidated


@pytest.mark.parametrize("fail", [False, True])
def test_worker_decorator_preserves_inert_database_guard_through_body_finally(
    tmp_path, fail
):
    from tests.maintenance_fakes import InertMaintenanceEngine

    engine = InertMaintenanceEngine()
    factory = SimpleNamespace(kw={"bind": engine})
    settings = SimpleNamespace(storage_root=tmp_path)
    observed = []

    @maintenance.guarded_worker
    def worker(run_id, generation, settings=None, factory=None):
        assert run_id == "inert" and generation == 1
        connection = engine.connections[-1]
        assert connection.held and not connection.closed
        try:
            with pytest.raises(maintenance.MaintenanceBusy):
                with storage_guard(tmp_path, exclusive=True):
                    pytest.fail("Component worker lost its real filesystem fence")
            if fail:
                raise ValueError("inert body fault")
            return "complete"
        finally:
            observed.append(connection.held)

    if fail:
        with pytest.raises(ValueError, match="inert body"):
            worker("inert", 1, settings, factory)
    else:
        assert worker("inert", 1, settings, factory) == "complete"
    assert observed == [True]
    assert len(engine.connections) == 1
    connection = engine.connections[0]
    assert connection.closed and not connection.held and not connection.invalidated
    assert len(connection.calls) == 2
    with storage_guard(tmp_path, exclusive=True):
        pass
