"""Pure checks of the actual hosted audit helper, not PostgreSQL execution."""

import ast
import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.models import Evaluation, ExecutionAttempt


def audit_helper(name="_assert_queue_no_models_or_new_data"):
    # Compile only the exact pure audit function. Importing the surrounding
    # hosted harness would unnecessarily load unrelated execution runtimes.
    path = (
        Path(__file__).resolve().parents[2]
        / "tests/system/test_independent_pipeline.py"
    )
    module = ast.parse(path.read_text())
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )

    def require(condition, message):
        if not condition:
            raise AssertionError(message)

    namespace = {"require": require}
    exec(  # noqa: S102 - only the checked-in audit helper AST, never candidate source
        compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"),
        namespace,
    )
    return namespace[function.name]


class AuditRows:
    def __init__(self, *, late=False, missing=False, admission=True, queue_gap=False):
        self.queries = []
        start = datetime(2024, 1, 2, tzinfo=UTC)
        self.exposures = [
            (
                {"admission_before_dispatch": admission},
                start - timedelta(seconds=1),
                start,
            ),
            (
                {"admission_before_dispatch": True},
                start + timedelta(seconds=1) if late else start,
                start,
            ),
        ]
        if missing:
            self.exposures.pop()
        self.queue_gap = queue_gap

    def execute(self, query, parameters=None):
        self.queries.append(query)
        if "FROM research_model_calls" in query or "FROM research_checkpoints" in query:
            return SimpleNamespace(fetchone=lambda: (0,))
        if (
            "FROM data_imports" in query
            or "SELECT generation FROM research_runs" in query
        ):
            return SimpleNamespace(fetchone=lambda: (2,))
        if "FROM outbox_events" in query:
            generations = [1, 3] if self.queue_gap else [1, 2]
            return SimpleNamespace(
                fetchall=lambda: [(value, value, "succeeded") for value in generations]
            )
        if "FROM data_usage_exposures" in query:
            return SimpleNamespace(fetchall=lambda: self.exposures)
        raise AssertionError("Unexpected audit SQL")


def run_audit(rows):
    harness = SimpleNamespace(connection=lambda **kwargs: nullcontext(rows))
    audit_helper()(harness, ["synthetic-run-identity"], 2)


def test_actual_audit_query_uses_existing_attempt_start_and_generation_join():
    rows = AuditRows()
    run_audit(rows)
    query = next(
        value for value in rows.queries if "FROM data_usage_exposures" in value
    )
    attempt_columns = set(re.findall(r"\ba\.(\w+)", query))
    assert attempt_columns <= set(ExecutionAttempt.__table__.columns.keys())
    assert "started_at" in attempt_columns
    assert "a.generation=(x.provenance->>'generation')::integer" in query
    assert "x.purpose='independent_evaluation_admission'" in query
    assert all(value.startswith("SELECT ") for value in rows.queries)


@pytest.mark.parametrize(
    "change",
    [{"late": True}, {"missing": True}, {"admission": False}],
)
def test_actual_audit_rejects_late_missing_or_nonadmitted_exposure(change):
    with pytest.raises(AssertionError, match="prior durable exposure admission"):
        run_audit(AuditRows(**change))


def test_actual_audit_still_rejects_skipped_queue_generation():
    with pytest.raises(AssertionError, match="omitted or duplicated"):
        run_audit(AuditRows(queue_gap=True))


def test_cancelled_evidence_query_preserves_exact_leaf_attempt_and_valid_columns():
    queries = []
    evidence = ("cancelled", True, {"status": "cancelled"})

    def execute(query, parameters):
        queries.append((query, parameters))
        return SimpleNamespace(fetchone=lambda: evidence)

    connection = SimpleNamespace(execute=execute)
    harness = SimpleNamespace(connection=lambda **kwargs: nullcontext(connection))
    assert audit_helper("_cancelled_evidence")(harness, "synthetic-leaf") is evidence
    query, parameters = queries[0]
    assert parameters == ("synthetic-leaf",)
    assert query.startswith("SELECT ")
    assert "a.id=e.attempt_id" in query and "WHERE e.id=%s" in query
    for alias, model in (("e", Evaluation), ("a", ExecutionAttempt)):
        columns = set(re.findall(rf"\b{alias}\.(\w+)", query))
        assert columns <= set(model.__table__.columns.keys())
    assert "e.payload_sha256" in query and "a.generation" in query
