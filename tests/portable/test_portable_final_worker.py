"""Explicit fake DB/lease/transport boundary checks, not integration acceptance."""

import copy
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from test_portable_final_exports import IMAGES, final_evidence

from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.app.services import FenceLost
from packages.tradegrad_adapter.portable_results import canonical_json_bytes
from runtimes.evaluator import portable_runner
from workers.research import portable_final_evaluation as worker


@pytest.fixture
def harness(tmp_path, monkeypatch):
    f = final_evidence()
    f.run.status = "queued"
    f.run.result = None
    h = SimpleNamespace(
        f=f,
        trace=[],
        mode="success",
        committed=False,
        evaluation=None,
        attempt=SimpleNamespace(id=f.run.active_attempt_id, lease_token=uuid4()),
        settings=SimpleNamespace(
            storage_root=tmp_path, max_artifact_bytes=8 * 1_048_576
        ),
    )

    class Factory:
        @contextmanager
        def begin(self):
            session = create_autospec(Session, instance=True)
            session.pending = []
            session.get.side_effect = lambda model, key: (
                f.run
                if model is ResearchRun
                else h.evaluation
                if model is Evaluation
                else None
            )
            session.add.side_effect = session.pending.append
            yield session
            if h.mode == "result_commit_failure" and any(
                isinstance(row, Artifact) for row in session.pending
            ):
                h.mode = "after_commit_failure"
                raise OSError("Fake result commit failed")
            for row in session.pending:
                if isinstance(row, Evaluation):
                    h.evaluation = row
                    h.trace.append("registered_commit")
            if getattr(session, "is_finish", False) is True:
                if h.mode == "terminal_fence_loss":
                    raise FenceLost()
                h.committed = True
                h.trace.append("terminal_commit")

        def __call__(self):
            return self.begin()

    h.factory = Factory()

    class Lease:
        def __init__(self, *args):
            self.lost, self.cancelled = threading.Event(), threading.Event()
            h.lease = self

        def start(self):
            h.trace.append("lease_start")

        def pulse(self, **kwargs):
            if self.lost.is_set():
                raise FenceLost()

        def close(self):
            h.trace.append("lease_close")

    monkeypatch.setattr(worker, "AttemptLease", Lease)

    def patch(name, effect):
        monkeypatch.setattr(
            worker, name, create_autospec(getattr(worker, name), side_effect=effect)
        )

    patch("claim_attempt", lambda *args: None if h.mode == "duplicate" else h.attempt)
    patch(
        "validate_final_inputs",
        lambda *args: (
            None,
            f.source,
            f.dataset,
            SimpleNamespace(
                code=f.package.strategy_code.decode(),
                manifest=f.package.policy_manifest,
                parameters=f.package.parameters,
            ),
            None,
        ),
    )
    f.dataset.storage_key = "data"
    patch("fenced_attempt", lambda *args: (f.run, h.attempt))
    patch(
        "publish_portable_environment",
        lambda **kwargs: h.trace.append("environment_committed"),
    )
    patch("mark_running", lambda *args: h.mode != "cancel_before_create")
    patch(
        "stage_portable_final_input",
        lambda **kwargs: SimpleNamespace(
            engine_root=tmp_path / "engine", policy_root=tmp_path / "policy"
        ),
    )

    def begin(*args):
        session = args[0]
        assert "environment_committed" in h.trace
        row = Evaluation(
            id=f.evaluation.id,
            run_id=f.run.id,
            attempt_id=h.attempt.id,
            generation=1,
            window=f.evaluation.window,
            status="registered",
            finished_at=None,
            sandbox_stopped=False,
        )
        session.add(row)
        return row

    patch("begin_final_evaluation", begin)

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
        assert h.evaluation.id == evaluation_id and sandbox_stopped
        assert "stop_both" in h.trace
        if h.mode == "completion_failure" and outcome["status"] == "succeeded":
            raise ValueError("Fake completion verifier failed")
        h.evaluation.payload = dict(outcome)
        h.evaluation.status = outcome["status"]
        if h.mode != "result_commit_failure":
            h.evaluation.finished_at, h.evaluation.sandbox_stopped = True, True
        return h.evaluation

    patch("complete_final_evaluation", complete)

    def finish(
        session, attempt_id, generation, token, outcome, sandbox_stopped, artifacts=None
    ):
        h.trace.append("finish")
        session.is_finish = True
        if sandbox_stopped:
            f.run.status = outcome["status"]
        return f.run

    patch("finish_final_attempt", finish)

    class Runner:
        timeout = 0

        def bind_environment(self, digest):
            return (
                IMAGES
                if h.mode != "changed_policy_image"
                else {**IMAGES, "policy": "sha256:" + "f" * 64}
            )

        def run(self, evaluation_id, engine, policy, output, **kwargs):
            assert "registered_commit" in h.trace
            assert evaluation_id == str(h.evaluation.id)
            assert kwargs["attempt_id"] == str(h.attempt.id)
            assert (
                kwargs["expected_config_sha256"] == f.config.sha256 != f.source.sha256
            )
            h.trace.append("create_both")
            if h.mode == "fence_loss":
                h.lease.lost.set()
            if h.mode == "cancel_running":
                h.lease.cancelled.set()
                return {"status": "cancelled", "logs": {}}
            output.mkdir(parents=True)
            content = canonical_json_bytes(f.bundle)
            (output / "portable-result.json").write_bytes(content)
            return {
                "status": "succeeded",
                "bundle": copy.deepcopy(f.bundle),
                "image_digests": IMAGES,
                "result_trust": f.bundle["result_trust"],
                "ledger_origin": "trusted_engine",
                "runtime_certified": False,
                "logs": {},
            }

        def stop(self, evaluation_id):
            h.trace.append("stop_both")
            return h.mode != "unknown_stop"

        def stop_attempt(self, attempt_id):
            h.trace.append("stop_attempt_both")
            return h.mode != "unknown_stop"

    monkeypatch.setattr(portable_runner, "PortableDockerEvaluator", Runner)
    h.execute = lambda: worker.execute_portable_final(
        str(f.run.id), 1, h.settings, h.factory
    )
    return h


def test_exact_one_holdout_dual_group_and_terminal_commit(harness):
    h = harness
    result = h.execute()
    assert result == {"status": "succeeded", "sandbox_stopped": True}
    assert h.trace.count("create_both") == 1
    assert h.trace.index("registered_commit") < h.trace.index("create_both")
    assert h.trace.index("stop_both") < h.trace.index("terminal_commit")
    assert h.committed and h.trace[-1] == "lease_close"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("cancel_before_create", "cancelled"),
        ("cancel_running", "cancelled"),
        ("changed_policy_image", "failed"),
        ("completion_failure", "failed"),
        ("result_commit_failure", "failed"),
        ("fence_loss", "fence_lost"),
        ("terminal_fence_loss", "fence_lost"),
        ("unknown_stop", "recovery_blocked"),
    ],
)
def test_failure_fence_cancel_and_uncertain_stop_never_report_success(
    harness, mode, expected
):
    h = harness
    h.mode = mode
    result = h.execute()
    assert result["status"] == expected
    assert result["sandbox_stopped"] is (mode != "unknown_stop")
    assert "stop_attempt_both" in h.trace
    if mode in {"cancel_before_create", "changed_policy_image"}:
        assert "create_both" not in h.trace
    if mode in {"fence_loss", "terminal_fence_loss"}:
        assert not h.committed


def test_duplicate_final_delivery_creates_nothing(harness):
    harness.mode = "duplicate"
    assert harness.execute() == {"status": "duplicate_or_stale"}
    assert harness.trace == []
