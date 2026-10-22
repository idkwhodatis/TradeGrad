"""Local unit evidence only: fake persistence and fake sandbox, NOT PG/Docker.

Staging tests are SDK-free. Orchestration tests use the real research optimizer
and frozen model, but all persistence and Docker operations are signature-checked
fakes. Candidate source is inert text and deliberately raises if host-executed.
The opt-in real boundary smoke lives in tests/system/test_research_optimization.py.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import io
import json
import secrets
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest

from backend.app.core.config import Settings
from backend.app.models import DatasetVersion, ResearchRun, StrategyVersion
from backend.app.services import DomainError, FenceLost
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    canonical_bytes,
    evaluation_windows,
)
from packages.tradegrad_adapter.environment import environment_digest
from packages.tradegrad_adapter.windows import PreflightError
from workers.research import optimization as worker

ROOT = Path(__file__).resolve().parents[2]
CODE = """raise RuntimeError("Candidate source must never execute on the coordinator")
from akquant import Strategy
class DemoStrategy(Strategy):
    ### EDIT START
    value = 0
    ### EDIT END
"""
IMAGE = "sha256:" + "a" * 64


def sha(value):
    return hashlib.sha256(value).hexdigest()


def inputs(root, *, task="cn_time", iterations=1):
    """Disposable synthetic sessions, not provider or trading-quality evidence."""
    days = [date(2023, 12, 1) + timedelta(days=i) for i in range(55)]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["date", "open", "high", "low", "close", "volume"])
    writer.writerows([[str(day), 10, 11, 9, 10, 100] for day in days])
    files = {
        "data/SOURCE.csv": output.getvalue().encode(),
        "data/FUTURE.csv": output.getvalue().encode(),
        "universe/members_list_2023-12-01.csv": b"updateDate,code,code_name\n2023-12-01,SOURCE,Synthetic\n",
        "universe/members_list_2024-01-16.csv": b"updateDate,code,code_name\n2024-01-16,FUTURE,Future fixture\n",
    }
    universe_id, universe_sha = str(uuid4()), sha(b"synthetic universe")
    manifest = {
        "files": {name: sha(value) for name, value in files.items()},
        "universe": {
            "id": universe_id,
            "content_sha256": universe_sha,
            "state": "historical_membership",
        },
        "evaluator_symbol_mapping": {
            "source-listing": "SOURCE",
            "future-listing": "FUTURE",
        },
        "listings": [
            {"listing_id": "source-listing"},
            {"listing_id": "future-listing"},
        ],
    }
    config = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": task,
            "dataset": {"id": str(uuid4()), "sha256": sha(canonical_bytes(manifest))},
            "seed_strategy": {"id": str(uuid4()), "sha256": sha(CODE.encode())},
            "universe": {"id": universe_id, "sha256": universe_sha},
            "windows": {
                "train": {"start": "2023-12-25", "end": "2024-01-07"},
                "validation": {"start": "2024-01-09", "end": "2024-01-15"},
                "holdout": {"start": "2024-01-16", "end": "2024-01-21"},
            },
            "warmup": {"sessions": 20 if task.endswith("_time") else 3},
            "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
            "optimizer": {"max_iterations": iterations},
            "budgets": {
                "max_runtime_seconds": 30,
                "max_model_calls": 10,
                "max_repairs": 0,
            },
            "environment_digest": environment_digest(),
        }
    )
    for name, value in files.items():
        path = root / "datasets" / "fixture" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return config, manifest, days


def stage(root, config, manifest, window, target):
    return worker.stage_research_input(
        config=config,
        manifest=manifest,
        storage_root=root,
        dataset_storage_key="datasets/fixture",
        code=CODE,
        window=window,
        target=target,
        max_bytes=1_000_000,
    )


def test_staging_imports_no_research_or_engine_sdks():
    code = (
        "import sys; import workers.research.optimization; "
        "assert not set(('numpy','openai','akquant','openbb','src.optimizer')) & set(sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()


@pytest.mark.parametrize("task", ["cn_time", "cn_cross"])
def test_exact_yearly_windows_warmup_alias_and_json_safe_private_report(tmp_path, task):
    config, manifest, days = inputs(tmp_path, task=task)
    windows = evaluation_windows(config)
    assert windows == (
        {"split": "train", "start": "2023-12-25", "end": "2024-01-01"},
        {"split": "train", "start": "2024-01-01", "end": "2024-01-07"},
        {"split": "validation", "start": "2024-01-09", "end": "2024-01-15"},
    )
    for index, window in enumerate(windows):
        target = stage(
            tmp_path, config, manifest, window, tmp_path / str(index) / "input"
        )
        name = "INDEX.csv" if task.endswith("_time") else "SOURCE.csv"
        rows = list(csv.DictReader((target / "data" / name).open()))
        start, end = (
            date.fromisoformat(window["start"]),
            date.fromisoformat(window["end"]),
        )
        expected = [day for day in days if day < start][-config.warmup.sessions :]
        expected += [day for day in days if start <= day < end]
        assert [row["date"] for row in rows] == [str(day) for day in expected]
        assert not (target / "data/FUTURE.csv").exists()
        assert not (target / "universe/members_list_2024-01-16.csv").exists()
        if task.endswith("_time"):
            assert not (target / "data/SOURCE.csv").exists()
        request = json.loads((target / "request.json").read_bytes())
        assert request["evaluation_config"]["run_config_sha256"] == config.sha256
        assert request["evaluation_config"]["window"] == {
            "start": window["start"],
            "end": window["end"],
            "role": window["split"],
        }
        report = json.loads((target.parent / "window-report.json").read_bytes())
        assert report["window"] == {"start": window["start"], "end": window["end"]}
        assert report["listings"][0]["staged_warmup_sessions"] == config.warmup.sessions
        assert report["listings"][0]["listing_id"] == "source-listing"
        assert not report["point_in_time_proven"]
        assert not (target / "window-report.json").exists()
        assert (target / "strategy.py").read_text() == CODE
        assert all(not path.stat().st_mode & 0o222 for path in target.rglob("*"))
        json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("mutation", ["manifest", "bytes", "partial_window", "holdout"])
def test_staging_rejects_immutable_or_unplanned_input_before_publication(
    tmp_path, mutation
):
    config, manifest, _ = inputs(tmp_path)
    window = dict(evaluation_windows(config)[0])
    if mutation == "manifest":
        manifest["universe"]["state"] = "user_fixed"
    elif mutation == "bytes":
        (tmp_path / "datasets/fixture/data/SOURCE.csv").write_text("changed")
    elif mutation == "partial_window":
        window["start"] = "2023-12-26"
    else:
        window = {"split": "holdout", "start": "2024-01-16", "end": "2024-01-21"}
    target = tmp_path / "rejected" / "input"
    with pytest.raises((PreflightError, DomainError)):
        stage(tmp_path, config, manifest, window, target)
    assert not target.exists()


def test_staging_never_reuses_attempt_input_namespace(tmp_path):
    config, manifest, _ = inputs(tmp_path)
    window, target = evaluation_windows(config)[0], tmp_path / "attempt" / "input"
    stage(tmp_path, config, manifest, window, target)
    with pytest.raises(FileExistsError):
        stage(tmp_path, config, manifest, window, target)


def test_event_summary_excludes_private_blobs_paths_and_redacts_credentials():
    credential = secrets.token_urlsafe(32)
    payload = {
        "iteration": 2,
        "candidate_id": "candidate-0001",
        "input_tokens": 30,
        "error_class": "Bearer " + credential,
        "stage": "x" * 300,
        "code": CODE,
        "prompt": "private",
        "response": "private",
        "manifest_path": "/private/host/checkpoint",
        "nested": {"code": CODE},
        "model_calls": {"private": True},
    }
    result = worker.safe_event_payload("model_call_completed", payload)
    assert set(result) == {
        "iteration",
        "candidate_id",
        "input_tokens",
        "error_class",
        "stage",
    }
    assert credential not in json.dumps(result, allow_nan=False)
    assert len(result["stage"]) <= 256


class FakePersistence:
    """No database: records worker transaction ordering, never PostgreSQL evidence."""

    def __init__(self, records, trace):
        self.records, self.trace = records, trace

    @contextmanager
    def begin(self):
        session = SimpleNamespace(
            get=lambda model, key: self.records.get((model, key)),
            execute=lambda statement: None,
            add=lambda record: self.trace.append(("artifact", record)),
            registered=[],
        )
        yield session
        self.trace.extend(("registered_commit", item) for item in session.registered)

    def __call__(self):
        return self.begin()


@pytest.fixture
def orchestration(tmp_path, monkeypatch):
    if (
        importlib.util.find_spec("numpy") is None
        or importlib.util.find_spec("openai") is None
    ):
        pytest.skip("Actual optimizer needs .venv-research; SDK-free staging still ran")
    from packages.tradegrad_adapter import model_calls, optimizer
    from packages.tradegrad_adapter.checkpoint import CheckpointStore
    from runtimes.evaluator import runner as runner_module

    config, manifest, _ = inputs(tmp_path)
    owner_id, run_id, attempt_id, token = uuid4(), uuid4(), uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id,
        owner_id=owner_id,
        config=config.model_dump(mode="json"),
        config_hash=config.sha256,
        status="running",
        resume_checkpoint_id=None,
        dataset_version_id=UUID(config.dataset.id),
        strategy_version_id=UUID(config.seed_strategy.id),
    )
    dataset = SimpleNamespace(
        owner_id=owner_id,
        manifest=manifest,
        storage_key="datasets/fixture",
        content_sha256=config.dataset.sha256,
    )
    strategy = SimpleNamespace(owner_id=owner_id, code=CODE)
    attempt = SimpleNamespace(id=attempt_id, lease_token=token)
    trace, events, checkpoints, completed, model_requests = [], [], [], {}, []
    factory = FakePersistence(
        {
            (ResearchRun, run.id): run,
            (DatasetVersion, run.dataset_version_id): dataset,
            (StrategyVersion, run.strategy_version_id): strategy,
        },
        trace,
    )
    settings = Settings(
        database_url="postgresql://unused.invalid/unit_test",
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path,
    )
    h = SimpleNamespace(
        config=config,
        run=run,
        dataset=dataset,
        strategy=strategy,
        attempt=attempt,
        factory=factory,
        settings=settings,
        trace=trace,
        events=events,
        checkpoints=checkpoints,
        completed=completed,
        model_requests=model_requests,
        mode="success",
        mocks={},
    )

    def patch(name, effect):
        fake = create_autospec(getattr(worker, name), side_effect=effect)
        monkeypatch.setattr(worker, name, fake)
        h.mocks[name] = fake
        return fake

    patch("claim_attempt", lambda *args: attempt)
    patch("research_config", lambda current: h.config)
    patch("mark_running", lambda *args: True)
    patch("fenced_attempt", lambda *args: (run, attempt))
    patch("heartbeat", lambda *args: h.mode == "cancel")
    patch("pin_research_image", lambda *args: trace.append(("pin", args[-1])))
    patch(
        "append_event",
        lambda session, current, kind, payload, attempt_id: events.append(
            (kind, payload)
        ),
    )

    def begin(
        session,
        attempt_id,
        generation,
        lease_token,
        *,
        candidate_key,
        code_sha256,
        window,
    ):
        physical = SimpleNamespace(id=uuid4())
        session.registered.append(str(physical.id))
        trace.append(
            ("register", str(physical.id), candidate_key, copy.deepcopy(window))
        )
        assert code_sha256 in {sha(CODE.encode()), sha(CODE.rstrip().encode())}
        return physical

    def complete(
        session,
        attempt_id,
        generation,
        lease_token,
        evaluation_id,
        *,
        outcome,
        sandbox_stopped,
    ):
        assert sandbox_stopped
        assert ("stop", str(evaluation_id)) in trace
        completed[str(evaluation_id)] = copy.deepcopy(outcome)
        trace.append(("complete", str(evaluation_id)))

    def checkpoint(session, attempt_id, generation, lease_token, reference, settings):
        # Real verification proves files were complete before pointer publication.
        state = CheckpointStore.load(
            reference, expected_input_digest=reference.input_digest
        )
        assert Path(reference.manifest_path).is_file()
        for row in state["candidates"]:
            for result in row.get("window_results", []):
                assert result["evaluation_id"] in completed
        checkpoints.append((reference, state))
        trace.append(("checkpoint", reference.manifest_sha256))
        events.append(("checkpoint_committed", {"sha256": reference.manifest_sha256}))

    def finish(
        session,
        attempt_id,
        generation,
        lease_token,
        outcome,
        sandbox_stopped,
        artifacts=None,
    ):
        trace.append(("finish", copy.deepcopy(outcome), sandbox_stopped))
        if sandbox_stopped:
            events.append(("run_finished", {"status": outcome["status"]}))
        run.status = outcome["status"]
        return run

    patch("begin_evaluation", begin)
    patch("complete_evaluation", complete)
    patch("publish_checkpoint", checkpoint)
    patch("finish_research_attempt", finish)
    sandbox = create_autospec(runner_module.DockerEvaluator, instance=True)
    sandbox.bind_environment.side_effect = lambda digest: (
        trace.append(("bind", digest)) or IMAGE
    )

    def fake_evaluate(
        input_dir,
        output_dir,
        run_id,
        attempt_id,
        cancelled=lambda: False,
        heartbeat=lambda: None,
        expected_config_sha256=None,
    ):
        assert ("registered_commit", attempt_id) in trace
        assert ("pin", IMAGE) in trace
        assert expected_config_sha256 == h.config.sha256
        assert (input_dir / "strategy.py").read_text().rstrip() == CODE.rstrip()
        assert callable(cancelled) and callable(heartbeat)
        assert 0 < sandbox.timeout <= h.config.budgets.max_runtime_seconds
        trace.append(("launch", attempt_id))
        heartbeat()
        if h.mode == "fence_loss":
            h.mocks["heartbeat"].side_effect = FenceLost()
            heartbeat(force=True)
        return {
            "status": "succeeded",
            "exit_code": 0,
            "logs": {},
            "metrics": {
                "combined_score": 2.5,
                "metrics": {
                    "sharpe_ratio": 1.2,
                    "annualized_return": 15.0,
                    "max_drawdown": 4.0,
                    "trade_per_day": 1.0,
                    "win_rate": 60.0,
                    "return_per_trade": 0.5,
                },
            },
        }

    def stop(identifier):
        trace.append(("stop", identifier))
        return h.mode != "stop_failure"

    sandbox.evaluate.side_effect = fake_evaluate
    sandbox.stop.side_effect = stop
    constructor = create_autospec(runner_module.DockerEvaluator, return_value=sandbox)
    monkeypatch.setattr(runner_module, "DockerEvaluator", constructor)
    h.sandbox = sandbox
    real_model = model_calls.FrozenModel

    class ObservedFrozenModel(real_model):
        def complete(self, request):
            assert checkpoints, (
                "No model request may precede durable checkpoint publication"
            )
            model_requests.append(request)
            trace.append(("model_call", request.call_id))
            return super().complete(request)

    monkeypatch.setattr(model_calls, "FrozenModel", ObservedFrozenModel)
    h.optimizer = create_autospec(
        optimizer.run_optimization, wraps=optimizer.run_optimization
    )
    # autospec's side_effect preserves the actual optimizer, unlike a default Mock return.
    h.optimizer.side_effect = optimizer.run_optimization
    monkeypatch.setattr(optimizer, "run_optimization", h.optimizer)
    h.execute = lambda: worker.execute_configured(str(run_id), 1, settings, factory)
    return h


def test_actual_optimizer_frozen_model_with_signature_checked_fake_boundaries(
    orchestration,
):
    h = orchestration
    assert h.execute() == {"status": "succeeded", "sandbox_stopped": True}
    h.optimizer.assert_called_once()
    assert h.optimizer.call_args.kwargs["windows"] == list(evaluation_windows(h.config))
    h.sandbox.bind_environment.assert_called_once_with(h.config.environment_digest)
    assert h.model_requests and len(h.completed) == 6
    assert all(row["image_digest"] == IMAGE for row in h.completed.values())
    assert h.checkpoints[-1][1]["next_stage"] == "finished"
    assert (
        next(row[1] for row in h.trace if row[0] == "finish")["best_candidate_id"]
        is not None
    )
    assert len([kind for kind, _ in h.events if kind == "run_finished"]) == 1
    assert len([kind for kind, _ in h.events if kind == "checkpoint_committed"]) == len(
        h.checkpoints
    )
    assert all(row[3]["split"] != "holdout" for row in h.trace if row[0] == "register")


@pytest.mark.parametrize("change", ["environment", "strategy", "ownership", "image"])
def test_immutable_environment_and_inputs_fail_before_launch(orchestration, change):
    h = orchestration
    if change == "environment":
        h.config = h.config.model_copy(update={"environment_digest": "0" * 64})
    elif change == "strategy":
        h.strategy.code += "# changed\n"
    elif change == "ownership":
        h.dataset.owner_id = uuid4()
    else:
        h.sandbox.bind_environment.side_effect = RuntimeError("Image mismatch")
    assert h.execute()["status"] == "failed"
    h.sandbox.evaluate.assert_not_called()
    h.mocks["begin_evaluation"].assert_not_called()
    assert not h.model_requests


@pytest.mark.parametrize("mode,status", [("cancel", "cancelled"), ("pause", "paused")])
def test_cancel_and_pause_publish_no_new_sandbox(orchestration, mode, status):
    h = orchestration
    h.mode = mode
    h.run.status = "pause_requested" if mode == "pause" else "cancel_requested"
    assert h.execute()["status"] == status
    h.sandbox.evaluate.assert_not_called()
    assert h.checkpoints and not h.model_requests
    assert h.checkpoints[-1][1]["stop_reason"] == status
    assert len([kind for kind, _ in h.events if kind == "run_finished"]) == 1


@pytest.mark.parametrize("mode", ["fence_loss", "stop_failure"])
def test_lost_authority_or_unconfirmed_stop_never_publish_success(orchestration, mode):
    h = orchestration
    h.mode = mode
    result = h.execute()
    assert result["status"] != "succeeded"
    assert h.sandbox.evaluate.call_count == 1
    h.mocks["complete_evaluation"].assert_not_called()
    h.mocks["finish_research_attempt"].assert_not_called()
    assert not h.model_requests
    assert not any(kind == "run_finished" for kind, _ in h.events)
    assert ("stop", str(h.attempt.id)) in h.trace
    if mode == "stop_failure":
        assert result == {"status": "recovery_blocked", "sandbox_stopped": False}


def test_staging_delay_cannot_launch_after_absolute_runtime_budget(
    orchestration, monkeypatch
):
    h = orchestration
    monotonic = worker.time.monotonic
    elapsed = [0.0]
    original_stage = worker.stage_research_input
    monkeypatch.setattr(worker.time, "monotonic", lambda: monotonic() + elapsed[0])

    def delayed_staging(**kwargs):
        staged = original_stage(**kwargs)
        elapsed[0] += h.config.budgets.max_runtime_seconds + 1
        return staged

    monkeypatch.setattr(
        worker,
        "stage_research_input",
        create_autospec(original_stage, side_effect=delayed_staging),
    )
    result = h.execute()
    h.sandbox.evaluate.assert_not_called()
    assert result["status"] != "succeeded"
    assert not h.model_requests
    assert ("stop", str(h.attempt.id)) in h.trace


def test_teardown_stop_exception_checks_all_children_and_ends_lease_thread(
    orchestration, monkeypatch
):
    h = orchestration
    original_thread = worker.threading.Thread
    threads, counts = [], {}

    def thread(*args, **kwargs):
        value = original_thread(*args, **kwargs)
        threads.append(value)
        return value

    monkeypatch.setattr(
        worker.threading, "Thread", create_autospec(original_thread, side_effect=thread)
    )

    def stop(identifier):
        h.trace.append(("stop", identifier))
        counts[identifier] = counts.get(identifier, 0) + 1
        first = next(row[1] for row in h.trace if row[0] == "register")
        if identifier == first:
            raise RuntimeError("Synthetic supervisor stop failure")
        return True

    h.sandbox.stop.side_effect = stop
    result = h.execute()
    assert result == {"status": "recovery_blocked", "sandbox_stopped": False}
    assert not h.completed
    registered = [row[1] for row in h.trace if row[0] == "register"]
    assert registered and all(counts[identifier] >= 2 for identifier in registered)
    assert counts[str(h.attempt.id)] == 1
    assert threads and not any(thread.is_alive() for thread in threads)
    assert not any(kind == "run_finished" for kind, _ in h.events)
