"""Pure arithmetic/schema/filesystem observations. Not PostgreSQL acceptance."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.operations_routes import operations
from backend.app.operations_views import (
    disk_observation,
    durations,
    model_usage,
    read_operations,
    seconds,
)
from backend.app.services import DomainError

NOW = datetime(2026, 10, 23, tzinfo=UTC)


def test_durations_do_not_convert_missing_invalid_or_negative_to_zero():
    result = durations([0, 1, 2, None, -1, float("nan"), float("inf"), True])
    assert result.count == 3 and result.unavailable_count == 5
    assert result.mean_seconds == 1 and result.p95_seconds == 2
    assert durations([]).mean_seconds is None
    assert seconds(NOW, NOW - timedelta(seconds=1)) is None
    assert seconds(NOW.replace(tzinfo=None), NOW) is None
    assert seconds(None, NOW) is None


def test_usage_partial_and_invalid_are_unknown_not_zero_cost():
    complete = model_usage([(2, 3, 5), (0, 1, 9)])
    assert complete.input_tokens == 2 and complete.charged_tokens == 14
    partial = model_usage([(2, None, 10), (1, 2, 4)], True)
    assert partial.input_tokens is None and partial.output_tokens is None
    assert (
        partial.charged_tokens == 14
        and partial.unknown_usage_calls == 1
        and partial.truncated
    )
    invalid = model_usage([(True, 1, 2), (-1, 2, 3), ("secret", 0, 0), (0, 0, None)])
    assert invalid.invalid_usage_calls == 3 and invalid.charged_tokens is None
    assert "secret" not in invalid.model_dump_json()


def test_disk_admin_only_counters_and_symlink_rejection(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    (storage / "private.txt").write_text("not read")
    assert disk_observation(storage, administrator=False).state == "restricted"
    observed = disk_observation(storage, administrator=True)
    assert (
        observed.state == "observed"
        and observed.capacity_bytes >= observed.available_bytes >= 0
    )
    assert str(storage) not in observed.model_dump_json()
    alias = tmp_path / "alias"
    alias.symlink_to(storage, target_is_directory=True)
    assert disk_observation(alias, administrator=True).state == "unavailable"
    assert disk_observation(alias / "other", administrator=True).state == "unavailable"
    assert (
        disk_observation(storage / "missing", administrator=True).state == "unavailable"
    )


def test_nonadmin_deployment_rejected_before_any_query():
    with pytest.raises(DomainError) as exc:
        operations(SimpleNamespace(is_superuser=False), None, None, "deployment", 7)
    assert exc.value.status_code == 403


class QueryInspection:
    """Only inspect generated PG SQL and arithmetic; never an integration substitute."""

    def __init__(self):
        self.statements = []

    def execute(self, query):
        self.statements.append(
            str(
                query.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                )
            )
        )
        return self

    scalars = execute

    def all(self):
        return []


def test_owner_predicates_and_query_bounds_are_generated_without_payload_reads(
    tmp_path,
):
    session = QueryInspection()
    owner = uuid4()
    result = read_operations(
        session,
        SimpleNamespace(id=owner, is_superuser=False),
        SimpleNamespace(storage_root=tmp_path),
        scope="owner",
        days=7,
    )
    assert result.queue_wait.count == 0 and result.imports.failure_fraction is None
    assert (
        result.disk.state == "restricted"
        and result.cancellation_latency.mean_seconds is None
    )
    assert len(session.statements) == 6
    assert "statement_timeout" in session.statements[0]
    for statement in session.statements[1:]:
        assert str(owner) in statement and "LIMIT " in statement
        assert (
            "research_runs.config" not in statement
            and "hashed_password" not in statement
        )
    assert "research_model_calls.usage" in session.statements[-1]
    assert "execution_attempts.generation" in session.statements[3]
    assert "run_events.run_id = execution_attempts.run_id" in session.statements[3]
