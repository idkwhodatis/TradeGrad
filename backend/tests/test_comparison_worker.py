"""Worker orchestration seam: real DuckDB/flocks, explicitly stubbed persistence.

These tests do not substitute for the separate PostgreSQL worker/fence gate.
"""

from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.comparison_guard import comparison_query_confirmed_stopped
from backend.app.comparison_schemas import ComparisonConfig, ComparisonCreate
from backend.app.services import FenceLost
from backend.tests.test_comparison_contracts import side
from packages.research_trace.comparison_contracts import ComparisonError
from workers.research import comparison as worker


@pytest.fixture
def harness(tmp_path, monkeypatch):
    import packages.research_trace.comparison as engine

    run_id, attempt_id = uuid4(), uuid4()
    record = []
    item = SimpleNamespace(id=uuid4(), run_id=run_id)
    run = SimpleNamespace(id=run_id, status="preparing")
    attempt = SimpleNamespace(id=attempt_id, lease_token=uuid4())
    config = ComparisonConfig(
        comparison_id=item.id,
        input_sha256="a" * 64,
        engine_input_sha256="b" * 64,
        request=ComparisonCreate(left=side(), right=side()),
    )
    settings = SimpleNamespace(storage_root=tmp_path)
    state = SimpleNamespace(
        ready=True,
        claim=True,
        control=None,
        query_error=None,
        commit_error=False,
        cancelled_at_finish=False,
    )

    class Factory:
        @contextmanager
        def begin(self):
            record.append("transaction")
            yield SimpleNamespace(get=lambda *a: run, scalar=lambda *a: item)
            if state.control is not None:
                assert not comparison_query_confirmed_stopped(
                    tmp_path, run_id, attempt_id
                )
            record.append("commit")
            if state.commit_error and "finish" in record:
                raise RuntimeError("synthetic commit failure")

    class Lease:
        def __init__(self, *a):
            self.cancelled, self.lost = Event(), Event()
            self.thread = SimpleNamespace(is_alive=lambda: False)
            state.lease = self

        def start(self):
            record.append("lease_start")

        def pulse(self, **kwargs):
            record.append("pulse")

        def close(self):
            record.append("lease_close")

    def claim(*args):
        record.append("claim")
        return attempt if state.claim else None

    def running(*args):
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
        record.append("fence")
        if state.ready == "fenced":
            raise FenceLost()
        return state.ready

    def inputs(*args):
        assert "fence" in record
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
        record.append("source_reads")
        return (SimpleNamespace(input="left"), SimpleNamespace(input="right"))

    def compare(left, right, output_dir, *, control, **kwargs):
        state.control = control
        assert (left, right) == ("left", "right") and "source_reads" in record
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
        with control.connection() as connection:
            assert connection.execute("SELECT 42").fetchone() == (42,)
        record.append("query_closed")
        if state.query_error:
            raise ComparisonError(state.query_error, "Explicit synthetic failure")
        return object(), []

    def finish(*args, status, guard, publication_check=None, **kwargs):
        guard.assert_owned(run_id, attempt_id)
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
        if state.control:
            assert state.control.closure_confirmed
            assert state.control.active_connections == 0
        if status == "succeeded":
            assert not state.lease.cancelled.is_set(), (
                "Normal closure fabricated cancellation"
            )
            publication_check()
        record.append("finish")
        run.status = "cancelled" if state.cancelled_at_finish else status
        return item, run

    monkeypatch.setattr(worker, "claim_attempt", claim)
    monkeypatch.setattr(worker, "mark_running", running)
    monkeypatch.setattr(worker, "comparison_config", lambda *a: config)
    monkeypatch.setattr(worker, "validate_comparison_inputs", inputs)
    monkeypatch.setattr(worker, "finish_comparison_attempt", finish)
    monkeypatch.setattr(worker, "AttemptLease", Lease)
    monkeypatch.setattr(engine, "compare_evaluations", compare)
    return SimpleNamespace(
        state=state,
        record=record,
        run_id=run_id,
        attempt_id=attempt_id,
        invoke=lambda: worker.execute_comparison.__wrapped__(
            str(run_id), 1, settings, Factory()
        ),
        root=tmp_path,
    )


def test_successful_worker_closes_query_then_commits_with_guard_held(harness):
    assert harness.invoke() == {"status": "succeeded", "query_stopped": True}
    assert (
        harness.record.index("claim")
        < harness.record.index("fence")
        < harness.record.index("source_reads")
        < harness.record.index("query_closed")
        < harness.record.index("finish")
    )
    assert harness.record[-2:] == ["commit", "lease_close"]
    assert comparison_query_confirmed_stopped(
        harness.root, harness.run_id, harness.attempt_id
    )


@pytest.mark.parametrize("state", [False, "fenced"])
def test_cancel_or_fence_before_source_reads_executes_no_query(harness, state):
    harness.state.ready = state
    result = harness.invoke()
    assert result["status"] == ("cancelled" if state is False else "recovery_blocked")
    assert "source_reads" not in harness.record and "query_closed" not in harness.record


def test_duplicate_delivery_does_not_start_flock_or_queries(harness):
    harness.state.claim = False
    assert harness.invoke() == {"status": "duplicate_or_stale"}
    assert harness.record == ["transaction", "claim", "commit"]
    assert not (harness.root / "runs").exists()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("comparison_timeout", "failed"),
        ("comparison_cancelled", "cancelled"),
        ("comparison_capacity", "failed"),
    ],
)
def test_failed_query_closes_before_status_completion(harness, code, expected):
    harness.state.query_error = code
    assert harness.invoke()["status"] == expected
    assert harness.record.index("query_closed") < harness.record.index("finish")


def test_cancel_committed_at_publication_is_reflected_in_worker_response(harness):
    harness.state.cancelled_at_finish = True
    assert harness.invoke()["status"] == "cancelled"


def test_commit_failure_never_claims_success_or_releases_before_query_close(harness):
    harness.state.commit_error = True
    with pytest.raises(RuntimeError, match="commit failure"):
        harness.invoke()
    assert harness.state.control.closure_confirmed
    assert harness.record[-1] == "lease_close"
    assert comparison_query_confirmed_stopped(
        harness.root, harness.run_id, harness.attempt_id
    )


@pytest.mark.parametrize("interrupted_at", ["close", "cancel", "backoff"])
def test_shutdown_exception_during_close_retry_keeps_both_guards(
    tmp_path, monkeypatch, interrupted_at
):
    """Real POSIX guards with injected close/shutdown failures, not a DB claim."""
    from backend.app.comparison_guard import comparison_attempt_guard
    from backend.app.core.maintenance import MaintenanceBusy
    from backend.app.core.storage_barrier import storage_guard

    run_id, attempt_id = uuid4(), uuid4()
    calls, injected = [], []

    def held(point):
        calls.append(point)
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
        with pytest.raises(MaintenanceBusy), storage_guard(tmp_path, exclusive=True):
            pytest.fail("Unconfirmed query closure released storage guard")
        if point == interrupted_at and not injected:
            injected.append(point)
            raise KeyboardInterrupt("Synthetic shutdown during unconfirmed closure")

    class Control:
        closure_confirmed = False
        attempts = 0

        def close(self):
            self.attempts += 1
            held("close")
            self.closure_confirmed = self.attempts >= 3

        def cancel(self):
            held("cancel")

    monkeypatch.setattr(worker.time, "sleep", lambda _: held("backoff"))
    control = Control()
    with comparison_attempt_guard(tmp_path, run_id, attempt_id):
        worker._confirm_query_closure(control)
        assert control.closure_confirmed and control.attempts == 3
        assert injected == [interrupted_at]
        assert not comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
    assert comparison_query_confirmed_stopped(tmp_path, run_id, attempt_id)
