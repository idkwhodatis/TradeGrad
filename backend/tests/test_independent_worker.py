"""Independent orchestration doubles, NOT PostgreSQL/Docker acceptance.

Persistence, sandbox execution, staging and evidence publishers are explicit,
signature-checked doubles. Real attempt/storage flocks, ComparisonControl and a
bounded DuckDB query exercise containment ordering. No SQLite, host candidate
execution, provider calls or fake economic result admission is claimed here.
"""

import copy
from contextlib import contextmanager
from threading import Event
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.app import benchmark_services
from backend.app.comparison_guard import comparison_query_confirmed_stopped
from backend.app.core.maintenance import MaintenanceBusy
from backend.app.core.storage_barrier import storage_guard
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import FenceLost
from packages.research_trace.comparison_contracts import ComparisonError
from packages.tradegrad_adapter import benchmarks
from packages.tradegrad_adapter.independent_config import NATIVE_SCHEMA, PORTABLE_SCHEMA
from runtimes.evaluator import portable_runner, runner
from tests.maintenance_fakes import InertMaintenanceEngine
from workers.research import (
    diagnostic_parity_publication,
    environment_evidence,
    portable_optimization,
)
from workers.research import (
    independent_evaluation as worker,
)


@pytest.fixture(params=["native", "portable"])
def harness(request, tmp_path, monkeypatch):
    portable = request.param == "portable"
    h = SimpleNamespace(
        portable=portable,
        mode="success",
        trace=[],
        artifacts=[],
        completed=[],
        evaluation=None,
        control=None,
        attempt=SimpleNamespace(id=uuid4(), lease_token=uuid4(), status="running"),
        run=SimpleNamespace(id=uuid4(), owner_id=uuid4(), status="preparing"),
        config=SimpleNamespace(
            schema_version=PORTABLE_SCHEMA if portable else NATIVE_SCHEMA,
            environment_digest="a" * 64,
            research_image_digest="sha256:" + "b" * 64,
            portable_policy_image_digest="sha256:" + "c" * 64,
            selected_strategy=SimpleNamespace(sha256="d" * 64),
            sha256="e" * 64,
            max_runtime_seconds=300,
            window={"split": "train", "start": "2020-01-01", "end": "2020-12-31"},
            purpose="diagnostic",
        ),
        settings=SimpleNamespace(storage_root=tmp_path, max_artifact_bytes=8 * 1024**2),
    )

    def held():
        assert not comparison_query_confirmed_stopped(tmp_path, h.run.id, h.attempt.id)
        with pytest.raises(MaintenanceBusy), storage_guard(tmp_path, exclusive=True):
            pytest.fail("Worker released its storage guard before terminal commit")

    def closed():
        held()
        assert h.control is not None
        assert h.control.closure_confirmed and h.control.active_connections == 0

    def cancel(*, observed=True):
        h.run.status = "cancel_requested"
        if observed:
            h.lease.cancelled.set()

    h.cancel = cancel

    class Factory:
        def __init__(self):
            self.kw = {"bind": InertMaintenanceEngine()}

        @contextmanager
        def begin(self):
            session = create_autospec(Session, instance=True)
            session.pending, session.after_commit = [], []
            session.leaf_outcome = None
            session.get.side_effect = lambda model, key: (
                h.run
                if model is ResearchRun
                else h.evaluation
                if model is Evaluation
                else None
            )
            session.add.side_effect = session.pending.append
            yield session
            if session.after_commit and h.mode in {
                "result_commit_failure",
                "cancel_on_result_commit_failure",
            }:
                if h.mode == "cancel_on_result_commit_failure":
                    cancel()
                h.mode = "after_result_commit_failure"
                h.trace.append("result_rollback")
                raise OSError("Synthetic result commit failure")
            for row in session.pending:
                if isinstance(row, Evaluation):
                    h.evaluation = row
                    h.trace.append("registered_commit")
                else:
                    assert isinstance(row, Artifact)
                    h.artifacts.append(row)
            for callback in session.after_commit:
                held()
                callback()

    class Lease:
        def __init__(self, *args):
            self.cancelled, self.lost = Event(), Event()
            self.thread = SimpleNamespace(is_alive=lambda: False)
            h.lease = self

        def start(self):
            h.trace.append("lease_start")

        def pulse(self, force=False):
            if self.lost.is_set():
                raise FenceLost()

        def close(self):
            held()
            h.trace.append("lease_close")

    def patch(module, name, effect):
        monkeypatch.setattr(
            module, name, create_autospec(getattr(module, name), side_effect=effect)
        )

    def fenced(session, attempt_id, generation, token):
        held()
        assert (attempt_id, generation, token) == (
            h.attempt.id,
            1,
            h.attempt.lease_token,
        )
        if h.lease.lost.is_set():
            raise FenceLost()
        if h.mode == "cancel_at_publication_fence" and "parity_query" in h.trace:
            # The database can observe cancellation before the lease heartbeat.
            cancel(observed=False)
        h.trace.append("fence")
        return h.run, h.attempt

    def running(*args):
        fenced(*args)
        if h.mode == "cancel_before_registration":
            cancel()
            return False
        h.run.status = "running"
        return True

    def inputs(*args):
        held()
        assert "fence" in h.trace
        return (
            None,
            None,
            SimpleNamespace(manifest={}, storage_key="datasets/orchestration-double"),
            SimpleNamespace(
                code="raise AssertionError('Candidate must never execute on the host')",
                manifest={},
                parameters={},
            ),
            None,
        )

    def begin(session, attempt_id, generation, token):
        fenced(session, attempt_id, generation, token)
        assert "environment" in h.trace
        item = Evaluation(
            id=uuid4(),
            run_id=h.run.id,
            attempt_id=attempt_id,
            generation=generation,
            status="registered",
            finished_at=None,
            sandbox_stopped=False,
        )
        session.add(item)
        return item

    def complete(
        session,
        attempt_id,
        generation,
        token,
        evaluation_id,
        *,
        outcome,
        sandbox_stopped,
        settings,
    ):
        fenced(session, attempt_id, generation, token)
        closed()
        assert sandbox_stopped and "stop_leaf" in h.trace and "stop_attempt" in h.trace
        assert evaluation_id == h.evaluation.id
        assert h.evaluation.finished_at is None, "Finished leaf must never be rewritten"
        if h.mode in {"completion_failure", "cancel_on_completion_failure"}:
            if h.mode == "cancel_on_completion_failure":
                cancel()
            h.mode = "after_completion_failure"
            raise ValueError("Synthetic completion failure")
        value = copy.deepcopy(outcome)
        session.leaf_outcome = value

        def commit():
            h.evaluation.status = value["status"]
            h.evaluation.payload = value
            h.evaluation.finished_at = True
            h.evaluation.sandbox_stopped = True
            h.completed.append(value)
            h.trace.append("leaf_commit")
            if h.mode == "cancel_after_leaf_commit":
                cancel()

        session.after_commit.append(commit)
        return h.evaluation

    def finish(session, attempt_id, generation, token, outcome, sandbox_stopped):
        fenced(session, attempt_id, generation, token)
        closed()
        if sandbox_stopped:
            if h.evaluation is not None:
                assert (
                    h.evaluation.finished_at is not None
                    or session.leaf_outcome is not None
                )
            else:
                assert outcome["status"] == "cancelled"
            h.run.status = (
                "cancelled" if h.run.status == "cancel_requested" else outcome["status"]
            )
            h.attempt.status = h.run.status
            session.after_commit.append(lambda: h.trace.append("terminal_commit"))
        return h.run

    patch(worker, "claim_attempt", lambda *args: h.attempt)
    patch(worker, "mark_running", running)
    patch(worker, "fenced_attempt", fenced)
    patch(worker, "independent_config", lambda run: h.config)
    patch(worker, "validate_independent_inputs", inputs)
    patch(worker, "begin_independent_evaluation", begin)
    patch(worker, "complete_independent_evaluation", complete)
    patch(worker, "finish_independent_attempt", finish)
    monkeypatch.setattr(worker, "AttemptLease", Lease)
    patch(
        environment_evidence,
        "publish_native_environment",
        lambda **kwargs: h.trace.append("environment"),
    )
    patch(
        portable_optimization,
        "publish_portable_environment",
        lambda **kwargs: h.trace.append("environment"),
    )
    patch(benchmark_services, "prepare_for_window", lambda *args: None)
    patch(benchmarks, "attach_comparison", lambda bundle, benchmark: bundle)

    def stage(**kwargs):
        held()
        assert "registered_commit" in h.trace
        if h.mode == "cancel_before_launch":
            cancel()
        return (
            SimpleNamespace(
                engine_root=tmp_path / "engine", policy_root=tmp_path / "policy"
            )
            if portable
            else tmp_path / "input"
        )

    patch(worker, "stage_independent_input", stage)
    sandbox = create_autospec(
        portable_runner.PortableDockerEvaluator if portable else runner.DockerEvaluator,
        instance=True,
    )
    images = (
        {
            "engine": h.config.research_image_digest,
            "policy": h.config.portable_policy_image_digest,
        }
        if portable
        else h.config.research_image_digest
    )
    sandbox.bind_environment.return_value = images

    def execute(*args, **kwargs):
        held()
        assert "registered_commit" in h.trace
        assert kwargs["expected_config_sha256"] == h.config.sha256
        h.trace.append("execute")
        if h.mode in {
            "cancel_running",
            "cancel_execution_exception",
            "cancel_unknown_stop",
            "cancel_fence_loss",
        }:
            cancel()
            if h.mode == "cancel_fence_loss":
                h.lease.lost.set()
            if h.mode == "cancel_execution_exception":
                raise RuntimeError("Synthetic interruption during sandbox execution")
            return {"status": "cancelled", "exit_code": None, "logs": {}}
        if h.mode == "execution_failure":
            raise RuntimeError("Synthetic execution failure without cancellation")
        return {
            "status": "succeeded",
            "exit_code": 0,
            "logs": {},
            "metrics": {"synthetic": True},
            "bundle": {
                "summary": {"synthetic": True},
                "result_trust": "test-double",
                "ledger_origin": "test-double",
            },
        }

    def stop(identifier):
        held()
        h.trace.append(
            "stop_leaf"
            if h.evaluation and identifier == str(h.evaluation.id)
            else "stop_attempt"
        )
        return h.mode != "cancel_unknown_stop"

    sandbox.stop.side_effect = stop
    if portable:
        sandbox.run.side_effect = execute
        sandbox.stop_attempt.side_effect = stop
        monkeypatch.setattr(portable_runner, "PortableDockerEvaluator", lambda: sandbox)
    else:
        sandbox.evaluate.side_effect = execute
        monkeypatch.setattr(runner, "DockerEvaluator", lambda: sandbox)

    def records(result):
        h.trace.append("evidence")
        if h.mode == "cancel_evidence_failure":
            cancel()
            raise ValueError("Synthetic evidence interruption")
        if h.mode == "evidence_failure":
            raise ValueError("Synthetic malformed evidence without cancellation")
        return (
            [
                {
                    "name": "portable-result.json"
                    if portable
                    else "result-bundle.json",
                    "media_type": "application/json",
                    "storage_key": "orchestration-double.json",
                    "sha256": "f" * 64,
                    "size_bytes": 1,
                }
            ]
            if result["status"] == "succeeded"
            else []
        )

    patch(
        worker,
        "publish_independent_native_artifacts",
        lambda settings, root, result: records(result),
    )
    patch(
        portable_optimization,
        "publish_portable_artifacts",
        lambda **kwargs: (records(kwargs["result"]), kwargs["result"].get("bundle")),
    )
    patch(worker, "prepare_trace_artifacts", lambda **kwargs: [])
    patch(worker, "prepare_runtime_artifacts", lambda **kwargs: [])

    def parity(*, control, result, **kwargs):
        h.control = control
        held()
        if result["status"] != "succeeded":
            return []
        with control.connection() as connection:
            assert connection.execute("SELECT 42").fetchone() == (42,)
            h.trace.append("parity_query")
            if h.mode == "cancel_parity_query":
                cancel()
                control.check()
            if h.mode == "parity_timeout":
                raise ComparisonError(
                    "comparison_timeout", "Synthetic bounded query timeout"
                )
            if h.mode == "parity_disposal_without_cancel":
                raise ComparisonError(
                    "comparison_cancelled",
                    "Synthetic internal disposal, not a user request",
                )
        if h.mode == "cancel_after_parity":
            cancel()
        return []

    patch(diagnostic_parity_publication, "prepare_independent_parity_artifacts", parity)
    # Capture the real control even if execution/publication fails before parity.
    from packages.research_trace import comparison

    real_control = comparison.ComparisonControl

    def make_control(**kwargs):
        h.control = real_control(**kwargs)
        return h.control

    monkeypatch.setattr(comparison, "ComparisonControl", make_control)
    h.execute = lambda: worker.execute_independent(
        str(h.run.id), 1, h.settings, Factory()
    )
    return h


def test_success_closes_queries_and_retains_guards_through_both_commits(harness):
    h = harness
    assert h.execute() == {
        "status": "succeeded",
        "sandbox_stopped": True,
        "query_stopped": True,
    }
    assert h.evaluation.status == h.attempt.status == h.run.status == "succeeded"
    assert h.trace.index("registered_commit") < h.trace.index("execute")
    assert (
        h.trace.index("stop_attempt")
        < h.trace.index("parity_query")
        < h.trace.index("leaf_commit")
        < h.trace.index("terminal_commit")
    )
    assert h.artifacts and len(h.completed) == 1
    assert not h.lease.cancelled.is_set()
    assert h.trace[-1] == "lease_close"
    assert comparison_query_confirmed_stopped(
        h.settings.storage_root, h.run.id, h.attempt.id
    )


@pytest.mark.parametrize(
    "mode",
    [
        "cancel_before_launch",
        "cancel_running",
        "cancel_execution_exception",
        "cancel_evidence_failure",
        "cancel_parity_query",
        "cancel_after_parity",
        "cancel_at_publication_fence",
        "cancel_on_completion_failure",
        "cancel_on_result_commit_failure",
    ],
)
def test_cancelled_leaf_matches_attempt_and_run_without_success_artifacts(
    harness, mode
):
    h = harness
    h.mode = mode
    assert h.execute() == {
        "status": "cancelled",
        "sandbox_stopped": True,
        "query_stopped": True,
    }
    assert h.evaluation.status == h.attempt.status == h.run.status == "cancelled"
    assert h.evaluation.sandbox_stopped and h.evaluation.finished_at
    assert len(h.completed) == 1
    assert h.evaluation.payload["status"] == "cancelled"
    assert h.evaluation.payload["exit_code"] is None
    assert not h.evaluation.payload.get("error")
    assert "metrics" not in h.evaluation.payload
    assert (
        "result_bundle" not in h.evaluation.payload
        and "portable_result" not in h.evaluation.payload
    )
    assert not h.artifacts
    assert h.control.closure_confirmed and h.control.active_connections == 0
    assert h.trace[-1] == "lease_close"
    if mode == "cancel_before_launch":
        assert "execute" not in h.trace
    if mode == "cancel_at_publication_fence":
        assert not h.lease.cancelled.is_set()
    assert comparison_query_confirmed_stopped(
        h.settings.storage_root, h.run.id, h.attempt.id
    )


@pytest.mark.parametrize(
    "mode",
    [
        "execution_failure",
        "evidence_failure",
        "parity_timeout",
        "parity_disposal_without_cancel",
        "completion_failure",
        "result_commit_failure",
    ],
)
def test_failure_without_cancel_request_stays_failed(harness, mode):
    h = harness
    h.mode = mode
    assert h.execute()["status"] == "failed"
    assert h.evaluation.status == h.attempt.status == h.run.status == "failed"
    assert h.evaluation.payload["error"]
    assert not h.lease.cancelled.is_set()
    assert h.control.closure_confirmed
    assert not h.artifacts and len(h.completed) == 1


@pytest.mark.parametrize(
    "mode,expected",
    [("cancel_unknown_stop", "recovery_blocked"), ("cancel_fence_loss", "fence_lost")],
)
def test_cancellation_never_bypasses_containment_or_fence(harness, mode, expected):
    h = harness
    h.mode = mode
    result = h.execute()
    assert result["status"] == expected
    assert result["sandbox_stopped"] is (mode != "cancel_unknown_stop")
    assert h.evaluation.status == "registered" and h.evaluation.finished_at is None
    assert not h.completed and not h.artifacts
    assert "stop_leaf" in h.trace and "stop_attempt" in h.trace
    assert "terminal_commit" not in h.trace
    assert h.control.closure_confirmed


def test_later_cancellation_does_not_rewrite_an_already_committed_leaf(harness):
    h = harness
    h.mode = "cancel_after_leaf_commit"
    assert h.execute()["status"] == "cancelled"
    assert h.attempt.status == h.run.status == "cancelled"
    assert h.evaluation.status == h.evaluation.payload["status"] == "succeeded"
    assert len(h.completed) == 1 and h.artifacts
    assert h.control.closure_confirmed


def test_cancellation_before_registration_creates_no_leaf_or_sandbox(harness):
    h = harness
    h.mode = "cancel_before_registration"
    assert h.execute() == {
        "status": "cancelled",
        "sandbox_stopped": True,
        "query_stopped": True,
    }
    assert h.evaluation is None and not h.completed and not h.artifacts
    assert h.attempt.status == h.run.status == "cancelled"
    assert "registered_commit" not in h.trace and "execute" not in h.trace
    assert "stop_attempt" in h.trace and "terminal_commit" in h.trace
    assert h.control.closure_confirmed
