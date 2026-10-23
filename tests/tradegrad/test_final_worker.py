"""Final coordinator component tests, NOT PostgreSQL/Docker integration.

The persistence, lease and sandbox boundaries are signature-checked fakes. Real
staging, code/config checks, environment evidence, result validation and artifact
hashing run locally. Candidate text deliberately raises if accidentally executed.
No optimizer, model client, candidate or engine is invoked by this harness.
"""

import builtins
import copy
import json
import secrets
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import DomainError, FenceLost
from backend.app.storage import read_verified
from backend.tests.final_result_fixtures import synthetic_final_result
from runtimes.evaluator import runner as runner_module
from tests.maintenance_fakes import InertMaintenanceEngine
from tests.tradegrad.test_final_staging import CODE, IMAGE, final_inputs, sha
from workers.research import (
    environment_evidence,
    executor,
)
from workers.research import (
    final_evaluation as worker,
)
from workers.research import (
    lease as lease_module,
)


class FakePersistence:
    """Explicit commit ordering only; not an ORM/transaction integration substitute."""

    def __init__(self, harness):
        self.kw = {"bind": InertMaintenanceEngine()}
        self.h = harness
        self.records = {(ResearchRun, harness.run.id): harness.run}
        self.artifacts = []

    @contextmanager
    def begin(self):
        session = create_autospec(Session, instance=True)
        session.pending, session.after_commit = [], []
        session.get.side_effect = lambda model, key: self.records.get((model, key))
        session.add.side_effect = session.pending.append
        yield session
        is_result = any(
            isinstance(item, Artifact) and item.name.startswith("evaluation-")
            for item in session.pending
        )
        if self.h.fail_result_commit and is_result:
            self.h.fail_result_commit = False
            self.h.trace.append(("result_rollback",))
            raise OSError("Synthetic commit fault")
        for item in session.pending:
            self.records[type(item), item.id] = item
            if isinstance(item, Artifact):
                self.artifacts.append(item)
                self.h.trace.append(("artifact_commit", item.name))
            if isinstance(item, Evaluation):
                self.h.trace.append(("registered_commit", str(item.id)))
        for callback in session.after_commit:
            callback()

    def __call__(self):
        return self.begin()


@pytest.fixture
def final_worker(tmp_path, monkeypatch):
    config, manifest, _ = final_inputs(tmp_path)
    fixture = synthetic_final_result(config=config, dataset_manifest=manifest)
    run, dataset = fixture.run, fixture.dataset
    run.status, run.request_sha256, run.generation = "queued", config.sha256, 1
    dataset.storage_key = "datasets/fixture"
    strategy = SimpleNamespace(code=CODE)
    settings = Settings(
        database_url="postgresql://unused.invalid/final_unit",
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path,
    )
    h = SimpleNamespace(
        config=config,
        run=run,
        dataset=dataset,
        strategy=strategy,
        bundle=fixture.bundle,
        settings=settings,
        trace=[],
        completed=[],
        mocks={},
        mode="success",
        attempt=SimpleNamespace(id=uuid4(), lease_token=uuid4()),
        generation=1,
        fail_result_commit=False,
    )
    h.factory = FakePersistence(h)

    def patch(name, effect):
        mocked = create_autospec(getattr(worker, name), side_effect=effect)
        monkeypatch.setattr(worker, name, mocked)
        h.mocks[name] = mocked
        return mocked

    def guard(session, attempt_id, generation, token):
        assert (attempt_id, generation, token) == (
            h.attempt.id,
            h.generation,
            h.attempt.lease_token,
        )
        if h.lease.lost.is_set():
            raise FenceLost()
        return run, h.attempt

    def claim(session, run_id, generation, worker_id, settings):
        assert run_id == run.id and generation == h.generation
        return None if h.mode == "duplicate" else h.attempt

    patch("claim_attempt", claim)
    patch(
        "validate_final_inputs",
        lambda session, current, settings: (
            None,
            config.source_config,
            dataset,
            strategy,
            None,
        ),
    )
    patch("mark_running", lambda *args: h.mode != "cancel_before_registration")
    patch("fenced_attempt", guard)
    monkeypatch.setattr(
        environment_evidence,
        "fenced_attempt",
        create_autospec(environment_evidence.fenced_attempt, side_effect=guard),
    )

    def begin(session, attempt_id, generation, token):
        guard(session, attempt_id, generation, token)
        assert any(
            row.name == "execution-environment.json" and row.attempt_id == attempt_id
            for row in h.factory.artifacts
        )
        item = Evaluation(
            id=uuid4(),
            run_id=run.id,
            attempt_id=attempt_id,
            generation=generation,
            code_sha256=config.selected_strategy.sha256,
            split="holdout",
            status="registered",
            sandbox_stopped=False,
            finished_at=None,
        )
        session.add(item)
        h.trace.append(("register", str(item.id)))
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
    ):
        guard(session, attempt_id, generation, token)
        assert sandbox_stopped and ("stop", str(evaluation_id), True) in h.trace
        if h.mode == "completion_failure" and outcome["status"] == "succeeded":
            raise ValueError("Synthetic result completion fault")
        item = h.factory.records[Evaluation, evaluation_id]
        value = copy.deepcopy(outcome)

        def commit():
            item.status, item.sandbox_stopped, item.finished_at = (
                value["status"],
                True,
                True,
            )
            item.payload = value
            h.completed.append(value)
            h.trace.append(("complete_commit", str(evaluation_id), value["status"]))

        session.after_commit.append(commit)

    def finish(
        session, attempt_id, generation, token, outcome, sandbox_stopped, artifacts=None
    ):
        guard(session, attempt_id, generation, token)
        h.trace.append(("finish", copy.deepcopy(outcome), sandbox_stopped))
        if sandbox_stopped:
            run.status = (
                "cancelled" if run.status == "cancel_requested" else outcome["status"]
            )
        return run

    patch("begin_final_evaluation", begin)
    patch("complete_final_evaluation", complete)
    patch("finish_final_attempt", finish)
    real_lease = worker.AttemptLease

    def lease_factory(factory, settings, attempt_id, generation, token, worker_id):
        lease = create_autospec(real_lease, instance=True)
        lease.cancelled, lease.lost = threading.Event(), threading.Event()

        def pulse(force=False):
            h.trace.append(("pulse", force))
            if h.mode == "cancel_before_registration":
                run.status = "cancel_requested"
                lease.cancelled.set()
            if h.mode == "fence_before_registration":
                lease.lost.set()
                lease.cancelled.set()
                raise FenceLost()

        lease.pulse.side_effect = pulse
        h.lease = lease
        return lease

    monkeypatch.setattr(
        worker, "AttemptLease", create_autospec(real_lease, side_effect=lease_factory)
    )
    sandbox = create_autospec(runner_module.DockerEvaluator, instance=True)
    sandbox.bind_environment.side_effect = lambda digest: IMAGE

    def evaluate(
        input_dir,
        output_dir,
        run_id,
        attempt_id,
        cancelled=lambda: False,
        heartbeat=lambda: None,
        expected_config_sha256=None,
    ):
        assert ("registered_commit", attempt_id) in h.trace
        assert expected_config_sha256 == h.config.sha256
        assert (input_dir / "strategy.py").read_text() == CODE
        assert 0 < sandbox.timeout <= config.max_runtime_seconds
        assert any(
            row[0] == "artifact_commit" and row[1] == "execution-environment.json"
            for row in h.trace
        )
        h.trace.append(("launch", attempt_id))
        if h.mode == "execution_failure":
            raise OSError("Synthetic sandbox error")
        if h.mode == "fence_during_execution":
            h.lease.lost.set()
            h.lease.cancelled.set()
            raise FenceLost()
        if h.mode == "cancel_during_execution":
            run.status = "cancel_requested"
            h.lease.cancelled.set()
        heartbeat()
        return {
            "status": "cancelled" if cancelled() else "succeeded",
            "exit_code": None if cancelled() else 0,
            "logs": {},
            "metrics": h.bundle["legacy_performance"]["value"],
            "bundle": h.bundle,
        }

    def stop(identifier):
        value = h.mode not in {"stop_false", "stop_raises"}
        h.trace.append(("stop", identifier, value))
        if h.mode == "stop_raises":
            raise OSError("Synthetic unavailable stop")
        return value

    sandbox.evaluate.side_effect, sandbox.stop.side_effect = evaluate, stop
    h.constructor = create_autospec(runner_module.DockerEvaluator, return_value=sandbox)
    monkeypatch.setattr(runner_module, "DockerEvaluator", h.constructor)
    h.sandbox = sandbox
    original_import = builtins.__import__

    def no_optimization_import(name, *args, **kwargs):
        assert name not in {
            "packages.tradegrad_adapter.optimizer",
            "packages.tradegrad_adapter.model_calls",
            "src.optimizer",
            "openai",
            "akquant",
        }, name
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_optimization_import)
    h.execute = lambda: worker.execute_final(
        str(run.id), h.generation, settings, h.factory
    )
    return h


def test_one_exact_final_evaluation_committed_before_sandbox_and_environment_pinned(
    final_worker,
):
    h = final_worker
    assert h.execute() == {"status": "succeeded", "sandbox_stopped": True}
    h.sandbox.evaluate.assert_called_once()
    h.sandbox.bind_environment.assert_called_once_with(h.config.environment_digest)
    assert len(h.completed) == 1 and h.completed[0]["image_digest"] == IMAGE
    evaluation_id = h.sandbox.evaluate.call_args.kwargs["attempt_id"]
    assert UUID(evaluation_id) != h.attempt.id
    assert h.trace.index(("registered_commit", evaluation_id)) < h.trace.index(
        ("launch", evaluation_id)
    )
    assert h.trace.index(("stop", evaluation_id, True)) < h.trace.index(
        ("complete_commit", evaluation_id, "succeeded")
    )
    environment = next(
        row for row in h.factory.artifacts if row.name == "execution-environment.json"
    )
    content = read_verified(
        h.settings.storage_root,
        environment.storage_key,
        environment.sha256,
        h.settings.max_artifact_bytes,
    )
    evidence = json.loads(content)
    assert (
        evidence["image_id"] == IMAGE
        and evidence["environment_digest"] == h.config.environment_digest
    )
    assert evidence["provenance"] == "captured_at_execution"
    assert set(evidence["dependency_locks"]) == {
        "runtimes/evaluator/requirements.lock",
        "runtimes/research/requirements.lock",
    }
    for name, record in evidence["dependency_locks"].items():
        assert (
            sha(record["content"].encode())
            == record["sha256"]
            == evidence["build_inputs"]["files"][name]
        )
    reference = h.completed[0]["result_bundle"]
    artifact = next(
        row for row in h.factory.artifacts if str(row.id) == reference["artifact_id"]
    )
    result = json.loads(
        read_verified(
            h.settings.storage_root,
            artifact.storage_key,
            reference["sha256"],
            h.settings.max_artifact_bytes,
        )
    )
    assert result["identity"]["strategy_sha256"] == h.config.selected_strategy.sha256
    assert result["identity"]["run_config_sha256"] == h.config.sha256
    assert result["window"]["role"] == "holdout"
    h.lease.close.assert_called_once()


def test_duplicate_dispatch_does_not_create_runner_or_artifacts(final_worker):
    h = final_worker
    h.mode = "duplicate"
    assert h.execute() == {"status": "duplicate_or_stale"}
    h.constructor.assert_not_called()
    assert not h.factory.artifacts


@pytest.mark.parametrize(
    "change",
    [
        "wrong_image",
        "invalid_config",
        "source_changed",
        "validation_error",
        "environment_write",
    ],
)
def test_wrong_or_unpersisted_frozen_inputs_never_launch(
    final_worker, monkeypatch, change
):
    h = final_worker
    if change == "wrong_image":
        h.sandbox.bind_environment.side_effect = lambda digest: "sha256:" + "b" * 64
    elif change == "invalid_config":
        h.run.request_sha256 = "0" * 64
    elif change == "source_changed":
        h.strategy.code += "# substituted\n"
    elif change == "validation_error":
        h.mocks["validate_final_inputs"].side_effect = DomainError(
            "invalid", "Changed binding"
        )
    else:
        monkeypatch.setattr(
            environment_evidence,
            "publish_bytes",
            create_autospec(
                environment_evidence.publish_bytes, side_effect=OSError("Disk full")
            ),
        )
    assert h.execute()["status"] == "failed"
    h.sandbox.evaluate.assert_not_called()
    if change != "source_changed":
        h.mocks["begin_final_evaluation"].assert_not_called()
    assert ("stop", str(h.attempt.id), True) in h.trace
    h.lease.close.assert_called_once()


@pytest.mark.parametrize(
    "mode", ["cancel_before_registration", "cancel_during_execution"]
)
def test_cancel_releases_lease_and_stops_all_owned_sandboxes(final_worker, mode):
    h = final_worker
    h.mode = mode
    assert h.execute() == {"status": "cancelled", "sandbox_stopped": True}
    assert h.sandbox.evaluate.call_count == (mode == "cancel_during_execution")
    assert all(item["status"] == "cancelled" for item in h.completed)
    assert ("stop", str(h.attempt.id), True) in h.trace
    h.lease.close.assert_called_once()


@pytest.mark.parametrize(
    "mode", ["fence_before_registration", "fence_during_execution"]
)
def test_fence_loss_never_publishes_results_or_finishes_stale_generation(
    final_worker, mode
):
    h = final_worker
    h.mode = mode
    assert h.execute()["status"] != "succeeded"
    h.mocks["complete_final_evaluation"].assert_not_called()
    h.mocks["finish_final_attempt"].assert_not_called()
    assert h.sandbox.evaluate.call_count == (mode == "fence_during_execution")
    assert not any(row.name.startswith("evaluation-") for row in h.factory.artifacts)
    assert ("stop", str(h.attempt.id), True) in h.trace
    h.lease.close.assert_called_once()


@pytest.mark.parametrize("mode", ["stop_false", "stop_raises"])
def test_unconfirmed_stop_cannot_publish_terminal_success(final_worker, mode):
    h = final_worker
    h.mode = mode
    assert h.execute() == {"status": "recovery_blocked", "sandbox_stopped": False}
    h.mocks["complete_final_evaluation"].assert_not_called()
    assert not any(row.name.startswith("evaluation-") for row in h.factory.artifacts)
    assert h.run.status != "succeeded"
    assert h.mocks["finish_final_attempt"].call_args.kwargs["sandbox_stopped"] is False
    h.lease.close.assert_called_once()


@pytest.mark.parametrize(
    "fault", ["execution", "artifact_disk", "completion", "commit"]
)
def test_failures_complete_registered_evaluation_only_after_confirmed_stop(
    final_worker, monkeypatch, fault
):
    h = final_worker
    if fault == "execution":
        h.mode = "execution_failure"
    elif fault == "artifact_disk":
        monkeypatch.setattr(
            executor,
            "publish_artifacts",
            create_autospec(
                executor.publish_artifacts, side_effect=OSError("Disk full")
            ),
        )
    elif fault == "completion":
        h.mode = "completion_failure"
    else:
        h.fail_result_commit = True
    assert h.execute() == {"status": "failed", "sandbox_stopped": True}
    assert len(h.completed) == 1 and h.completed[0]["status"] == "failed"
    item = next(
        row for (model, _), row in h.factory.records.items() if model is Evaluation
    )
    assert item.finished_at and item.sandbox_stopped and item.status == "failed"
    assert "result_bundle" not in item.payload
    assert ("stop", str(item.id), True) in h.trace
    h.lease.close.assert_called_once()


def test_staging_exhausted_budget_cannot_launch(final_worker, monkeypatch):
    h = final_worker
    original = worker.stage_final_input
    original_clock, elapsed = worker.time.monotonic, [0]
    monkeypatch.setattr(worker.time, "monotonic", lambda: original_clock() + elapsed[0])

    def stage(**kwargs):
        result = original(**kwargs)
        elapsed[0] = h.config.max_runtime_seconds + 1
        return result

    monkeypatch.setattr(
        worker, "stage_final_input", create_autospec(original, side_effect=stage)
    )
    assert h.execute()["status"] == "failed"
    h.sandbox.evaluate.assert_not_called()
    assert h.completed[0]["status"] == "failed"


def test_retry_uses_new_physical_uuid_and_identical_frozen_source(final_worker):
    h = final_worker
    h.mode = "execution_failure"
    assert h.execute()["status"] == "failed"
    first = h.sandbox.evaluate.call_args.kwargs
    h.mode, h.generation = "success", 2
    h.attempt = SimpleNamespace(id=uuid4(), lease_token=uuid4())
    assert h.execute()["status"] == "succeeded"
    second = h.sandbox.evaluate.call_args.kwargs
    assert first["attempt_id"] != second["attempt_id"]
    assert first["input_dir"] != second["input_dir"]
    assert (
        first["expected_config_sha256"]
        == second["expected_config_sha256"]
        == h.config.sha256
    )
    assert (
        (first["input_dir"] / "strategy.py").read_bytes()
        == (second["input_dir"] / "strategy.py").read_bytes()
        == CODE.encode()
    )


def test_final_schema_cannot_be_fed_back_to_optimizer_checkpoint(
    final_worker, monkeypatch
):
    from backend.app import research_services

    h = final_worker
    monkeypatch.setattr(
        research_services,
        "fenced_attempt",
        create_autospec(
            research_services.fenced_attempt, return_value=(h.run, h.attempt)
        ),
    )
    loader = create_autospec(
        research_services._load_checkpoint,
        side_effect=AssertionError("Final must not enter optimizer archive"),
    )
    monkeypatch.setattr(research_services, "_load_checkpoint", loader)
    session = create_autospec(Session, instance=True)
    with pytest.raises(DomainError, match="compatible immutable research"):
        research_services.publish_checkpoint(
            session, h.attempt.id, 1, h.attempt.lease_token, object(), h.settings
        )
    loader.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_actual_lease_pulse_revokes_authority_on_persistence_failure(
    monkeypatch, failure
):
    session = create_autospec(Session, instance=True)

    @contextmanager
    def transaction():
        yield session

    settings = SimpleNamespace(heartbeat_seconds=1, lease_seconds=6)
    factory = SimpleNamespace(begin=transaction)
    heartbeat = create_autospec(
        lease_module.heartbeat,
        side_effect=FenceLost() if failure else None,
        return_value=True,
    )
    monkeypatch.setattr(lease_module, "heartbeat", heartbeat)
    lease = lease_module.AttemptLease(
        factory, settings, uuid4(), 1, uuid4(), "unit-lease"
    )
    if failure:
        with pytest.raises(FenceLost):
            lease.pulse(force=True)
        assert lease.lost.is_set()
        session.execute.assert_not_called()
    else:
        lease.pulse(force=True)
        assert not lease.lost.is_set()
        session.execute.assert_called_once()
    assert lease.cancelled.is_set()
    lease.start()
    lease.close()
    assert not lease.thread.is_alive()


@pytest.mark.parametrize("point", ["after_result_commit", "final_fence"])
def test_late_fence_loss_cannot_report_evaluator_success_as_terminal_run(
    final_worker, point
):
    h = final_worker
    if point == "after_result_commit":
        original = h.sandbox.stop.side_effect

        def stop(identifier):
            if h.completed:
                h.lease.lost.set()
            return original(identifier)

        h.sandbox.stop.side_effect = stop
    else:
        h.mocks["finish_final_attempt"].side_effect = FenceLost()
    assert h.execute() == {"status": "fence_lost", "sandbox_stopped": True}
    assert h.completed[0]["status"] == "succeeded"
    assert h.run.status != "succeeded"
    h.lease.close.assert_called_once()


def test_runner_initialization_failure_is_contained_without_false_stop(final_worker):
    h = final_worker
    h.constructor.side_effect = OSError("Synthetic runner state directory failure")
    assert h.execute() == {"status": "recovery_blocked", "sandbox_stopped": False}
    h.lease.start.assert_not_called()
    h.lease.close.assert_called_once()
    h.sandbox.evaluate.assert_not_called()
    assert not h.factory.artifacts


def test_lease_close_handles_thread_start_failure():
    lease = lease_module.AttemptLease(None, None, uuid4(), 1, uuid4(), "never-started")
    lease.close()
    lease.close()
    assert lease.done.is_set() and not lease.thread.is_alive()


def test_environment_fence_failure_does_not_publish_reference_or_launch(
    final_worker, monkeypatch
):
    h = final_worker
    environment_evidence.fenced_attempt.side_effect = FenceLost()
    assert h.execute()["status"] == "failed"
    assert not h.factory.artifacts
    h.sandbox.evaluate.assert_not_called()
    h.mocks["begin_final_evaluation"].assert_not_called()
    h.lease.close.assert_called_once()


def test_partial_artifact_disk_write_cannot_publish_successful_bundle_pointer(
    final_worker, monkeypatch
):
    from pathlib import Path

    h = final_worker
    original = Path.open

    def disk_fault(path, *args, **kwargs):
        if path.name == "result-bundle.json" and args and args[0] == "xb":
            raise OSError("Synthetic full disk after provenance and metrics")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disk_fault)
    assert h.execute() == {"status": "failed", "sandbox_stopped": True}
    assert h.completed[0]["status"] == "failed"
    assert "result_bundle" not in h.completed[0]
    assert not any(
        row.name.endswith("result-bundle.json") for row in h.factory.artifacts
    )
    assert list(h.settings.storage_root.glob("runs/**/artifacts/provenance.json"))


def test_final_publication_error_still_stops_every_identity_and_closes_lease(
    final_worker,
):
    h = final_worker
    h.mocks["finish_final_attempt"].side_effect = OSError(
        "Synthetic terminal persistence fault"
    )
    with pytest.raises(OSError, match="terminal persistence"):
        h.execute()
    physical = h.sandbox.evaluate.call_args.kwargs["attempt_id"]
    assert ("stop", physical, True) in h.trace
    assert ("stop", str(h.attempt.id), True) in h.trace
    h.lease.close.assert_called_once()


def test_lease_heartbeat_commit_failure_revokes_authority(monkeypatch):
    session = create_autospec(Session, instance=True)

    @contextmanager
    def transaction():
        yield session
        raise OSError("Synthetic lease commit fault")

    monkeypatch.setattr(
        lease_module,
        "heartbeat",
        create_autospec(lease_module.heartbeat, return_value=False),
    )
    lease = lease_module.AttemptLease(
        SimpleNamespace(begin=transaction),
        None,
        uuid4(),
        1,
        uuid4(),
        "unit-commit-fault",
    )
    with pytest.raises(OSError, match="lease commit"):
        lease.pulse(force=True)
    assert lease.cancelled.is_set() and lease.lost.is_set()
    assert lease.last_pulse == 0
    lease.close()
