"""Unit/controller evidence only. Actual TradeTGD; fake persistence and runner.

No database, Docker, native engine execution, generated policy imports, paid
provider or network. Separate opt-in system tests own boundary certification.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import secrets
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import UUID, uuid4

import pytest

from backend.app.core.config import Settings
from backend.app.models import DatasetVersion, ResearchRun, StrategyVersion
from backend.app.portable_services import reference_state
from backend.app.services import FenceLost
from packages.strategy_sdk import (
    DataRequirements,
    EngineProfile,
    PriceAssumptions,
    StrategyManifest,
    UniverseAssumptions,
    hash_json,
)
from packages.tradegrad_adapter.portable_config import (
    PortableRunConfig,
    portable_evaluation_windows,
)
from packages.tradegrad_adapter.portable_environment import portable_environment_digest
from packages.tradegrad_adapter.portable_prompts import (
    PROMPT_VERSION,
    parse_portable_code,
    portable_step_method,
    template_source,
    validate_policy_source,
)
from runtimes.portable_engine.runtime import profile_sha256
from workers.research import portable_optimization as worker

IMAGES = {"engine": "sha256:" + "a" * 64, "policy": "sha256:" + "b" * 64}


def inputs(kind="event", iterations=1):
    source = template_source(kind)
    strategy_id = str(uuid4())
    state_schema, state = reference_state(kind)
    parameters = {"quantity": 1} if kind == "event" else {"top_n": 1}
    env = portable_environment_digest()
    manifest = StrategyManifest(
        strategy_kind=kind,
        strategy_id=strategy_id,
        code_sha256=hashlib.sha256(source.encode()).hexdigest(),
        parameters_sha256=hash_json(parameters),
        market="US",
        currency="USD",
        calendar="XNYS",
        timezone="America/New_York",
        data=DataRequirements(lookback_bars=2, warmup_bars=2, max_listings=1),
        universe=UniverseAssumptions(
            semantics="user_fixed",
            snapshot_sha256="d" * 64,
            survivorship_bias="possible",
        ),
        prices=PriceAssumptions(actions_snapshot_sha256="e" * 64),
        state_schema=state_schema,
        engine_profile=EngineProfile(
            profile_sha256=profile_sha256("US", 1_000_000), environment_sha256=env
        ),
    )
    config = PortableRunConfig(
        task="us_time" if kind == "event" else "us_cross",
        strategy_kind=kind,
        execution_mode="optimization",
        dataset={"id": str(uuid4()), "sha256": "c" * 64},
        seed_strategy={"id": strategy_id, "sha256": manifest.code_sha256},
        seed_manifest={"id": strategy_id + "/manifest", "sha256": hash_json(manifest)},
        seed_parameters={
            "id": strategy_id + "/parameters",
            "sha256": hash_json(parameters),
        },
        universe={"id": str(uuid4()), "sha256": "d" * 64},
        initial_state=state,
        windows={
            "train": {"start": "2025-01-06", "end": "2025-01-10"},
            "validation": {"start": "2025-01-13", "end": "2025-01-17"},
            "holdout": {"start": "2025-01-20", "end": "2025-01-24"},
        },
        warmup={"sessions": 2},
        optimizer={
            "max_iterations": iterations,
            "large_revision_prob": 1,
            "memory_window": 1,
        },
        budgets={
            "max_model_calls": 100,
            "max_tokens": 2_000_000,
            "max_runtime_seconds": 60,
            "max_repairs": 0,
        },
        environment_digest=env,
    )
    return source, config, manifest.model_dump(mode="json"), parameters


@pytest.mark.parametrize("kind", ["event", "portfolio"])
def test_source_only_exact_protected_regions(kind):
    source = template_source(kind)
    validate_policy_source(source, kind)
    assert parse_portable_code("```python\n" + source + "\n```") == source
    for bad in (
        source + "\n",
        source.replace("StateCodec", "Danger", 1),
        source.replace("### EDIT END", "END"),
        source.replace("if observation.warmup", "if False", 1),
    ):
        with pytest.raises((ValueError, SyntaxError)):
            validate_policy_source(bad, kind)
    business = (
        source.replace(
            "rising = bars[-1].close > bars[-2].close",
            "rising = bars[-1].close >= bars[-2].close",
        )
        if kind == "event"
        else source.replace(
            "value=bars[-1].close / bars[0].close - 1",
            "value=bars[-1].close / bars[0].close - 2",
        )
    )
    validate_policy_source(business, kind)


def research_runtime():
    if (
        importlib.util.find_spec("numpy") is None
        or importlib.util.find_spec("openai") is None
    ):
        pytest.skip("Actual upstream optimizer requires .venv-research")


def test_step_binding_retains_exact_source_bytecode_without_global_mutation():
    research_runtime()
    from src.optimizer import TradeTGD
    from src.util import parse_code

    private = portable_step_method(TradeTGD.step)
    assert private.__code__ is TradeTGD.step.__code__
    index = TradeTGD.step.__code__.co_freevars.index("func")
    original = TradeTGD.step.__closure__[index].cell_contents
    replacement = private.__closure__[index].cell_contents
    assert replacement.__code__ is original.__code__
    assert original.__globals__["parse_code"] is parse_code
    assert replacement.__globals__["parse_code"] is parse_portable_code
    assert original.__globals__ is not replacement.__globals__
    with pytest.raises(ValueError):
        parse_code("```python\n" + template_source("event") + "\n```")


@pytest.mark.parametrize("kind", ["event", "portfolio"])
@pytest.mark.parametrize("large", [0.0, 1.0])
def test_actual_trade_tgd_both_revisions_memory_hashes_and_no_holdout(
    tmp_path, kind, large
):
    research_runtime()
    from packages.tradegrad_adapter.checkpoint import CheckpointStore, json_digest
    from packages.tradegrad_adapter.model_calls import FrozenModel
    from packages.tradegrad_adapter.optimizer import run_optimization

    source, config, _, _ = inputs(kind, 2)
    config = config.model_copy(
        update={
            "optimizer": config.optimizer.model_copy(
                update={"large_revision_prob": large}
            )
        }
    )
    seen = []

    def evaluate(code, key, window):
        validate_policy_source(code, kind)
        seen.append(window)
        return {"status": "succeeded", "combined_score": 2.5}

    result = run_optimization(
        config=config.model_dump(mode="json"),
        initial_code=source,
        windows=portable_evaluation_windows(config),
        evaluate=evaluate,
        root=tmp_path,
        model=FrozenModel(["```python\n" + source + "\n```"] * 100),
    )
    assert result.status == "succeeded" and len(result.candidates) == 3
    assert all(row["valid"] for row in result.candidates)
    assert all(window["split"] != "holdout" for window in seen)
    state = CheckpointStore.load(
        result.checkpoint, expected_input_digest=result.checkpoint.input_digest
    )
    assert state["optimizer"]["memory_ids"] and state["random_state"]["numpy"]
    assert any(
        "memory" in row["call_id"] or "Individual Strategy" in row["user_prompt"]
        for row in result.model_calls
    )
    for row in result.model_calls:
        assert PROMPT_VERSION in row["user_prompt"]
        assert "self.get_history_map" not in row["user_prompt"]
        assert row["request_sha256"] == json_digest(
            {
                "user_prompt": row["user_prompt"],
                "system_prompt": row["system_prompt"],
                "model": row["model"],
                "provider": row["provider"],
                "prompt_version": PROMPT_VERSION,
                "parameters": {
                    "temperature": config.model.temperature,
                    "max_output_tokens": 4096,
                },
            }
        )
    # Finished checkpoint replay performs no model requests or evaluations.
    replay = FrozenModel([])
    resumed = run_optimization(
        config=config.model_dump(mode="json"),
        initial_code=source,
        windows=portable_evaluation_windows(config),
        evaluate=lambda *a: pytest.fail("Finished checkpoint re-evaluated"),
        root=tmp_path / "resume",
        model=replay,
        resume=result.checkpoint,
    )
    assert resumed.status == "succeeded" and not replay.requests
    assert resumed.best_candidate_id == result.best_candidate_id


def test_parser_retry_preserves_upstream_five_attempt_boundary():
    research_runtime()
    from packages.tradegrad_adapter.optimizer import _source_optimizer

    requests = []
    source = template_source("event")
    llm = SimpleNamespace(
        query=lambda *args: requests.append(args) or "not a code block"
    )
    optimizer = _source_optimizer(llm, {"large_revision_prob": 1}, portable=True)
    optimizer.database = [
        SimpleNamespace(id="seed", code=source, performance={"combined_score": 1})
    ]
    optimizer.current_revision_mode = "large"
    with pytest.raises(Exception, match="5 retries"):
        optimizer.step("feedback")
    assert len(requests) == 5


def test_seed_no_model_calls_and_budget_end(tmp_path):
    research_runtime()
    from packages.tradegrad_adapter.model_calls import FrozenModel
    from packages.tradegrad_adapter.optimizer import run_optimization

    source, config, _, _ = inputs(iterations=0)
    cfg = config.model_dump(mode="json") | {"execution_mode": "seed_evaluation"}
    result = run_optimization(
        config=cfg,
        initial_code=source,
        windows=portable_evaluation_windows(config),
        evaluate=lambda *a: {"status": "succeeded", "combined_score": 0},
        root=tmp_path / "seed",
        model=FrozenModel([]),
    )
    assert (
        result.status == "succeeded"
        and len(result.candidates) == 1
        and not result.model_calls
    )
    cfg["schema_version"] = "tradegrad.run.v1"
    with pytest.raises(ValueError, match="optimization"):
        run_optimization(
            config=cfg,
            initial_code=source,
            windows=portable_evaluation_windows(config),
            evaluate=lambda *a: None,
            root=tmp_path / "native",
            model=FrozenModel([]),
        )


class FakePersistence:
    def __init__(self, records, trace):
        self.records, self.trace = records, trace

    @contextmanager
    def begin(self):
        session = SimpleNamespace(
            get=lambda model, key: self.records.get((model, key)),
            registered=[],
            add=lambda record: self.trace.append(("artifact", record)),
        )
        yield session
        self.trace.extend(("registered_commit", item) for item in session.registered)

    def __call__(self):
        return self.begin()


@pytest.fixture
def controller(tmp_path, monkeypatch):
    research_runtime()
    from packages.tradegrad_adapter.checkpoint import CheckpointStore
    from runtimes.evaluator import portable_runner

    source, config, manifest, parameters = inputs()
    owner_id, run_id, attempt_id, token = uuid4(), uuid4(), uuid4(), uuid4()
    run = SimpleNamespace(
        id=run_id,
        owner_id=owner_id,
        config=config.model_dump(mode="json"),
        status="running",
        dataset_version_id=UUID(config.dataset.id),
        strategy_version_id=UUID(config.seed_strategy.id),
        resume_checkpoint_id=None,
    )
    dataset = SimpleNamespace(
        owner_id=owner_id,
        content_sha256=config.dataset.sha256,
        manifest={},
        storage_key="data",
    )
    strategy = SimpleNamespace(
        owner_id=owner_id,
        code=source,
        mode="portable",
        manifest=manifest,
        parameters=parameters,
    )
    attempt = SimpleNamespace(id=attempt_id, lease_token=token)
    trace, completed, checkpoints = [], {}, []
    factory = FakePersistence(
        {
            (ResearchRun, run_id): run,
            (DatasetVersion, run.dataset_version_id): dataset,
            (StrategyVersion, run.strategy_version_id): strategy,
        },
        trace,
    )
    settings = Settings(
        database_url="postgresql://unused.invalid/controller_unit",
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path,
    )
    h = SimpleNamespace(
        mode="success",
        source=source,
        config=config,
        trace=trace,
        completed=completed,
        checkpoints=checkpoints,
        run=run,
        settings=settings,
        owner_id=owner_id,
        factory=factory,
    )

    def patch(name, effect):
        fake = create_autospec(getattr(worker, name), side_effect=effect)
        monkeypatch.setattr(worker, name, fake)
        return fake

    patch("claim_attempt", lambda *a: attempt)
    patch("fenced_attempt", lambda *a: (run, attempt))
    patch("mark_running", lambda *a: True)
    patch("pin_portable_images", lambda *a: trace.append(("pin", a[-1])))
    patch("append_event", lambda *a: None)
    # Fake DB cannot prove account/workspace admission. Dedicated profile tests
    # exercise those services; this controller test checks launch/stop ordering.
    from backend.app import workspace_policy

    monkeypatch.setattr(workspace_policy, "enforce_model_policy", lambda *a: None)

    class Lease:
        def __init__(self, *args):
            self.cancelled, self.lost = threading.Event(), threading.Event()

        def start(self):
            pass

        def close(self):
            pass

        def pulse(self, force=False):
            if h.mode == "cancel":
                self.cancelled.set()

    monkeypatch.setattr(worker, "AttemptLease", Lease)

    def begin(session, *args, candidate_key, code_sha256, window):
        row = SimpleNamespace(id=uuid4())
        session.registered.append(str(row.id))
        trace.append(("register", str(row.id), candidate_key))
        return row

    patch("begin_portable_evaluation", begin)

    def complete(session, *args, outcome, sandbox_stopped, settings):
        assert sandbox_stopped
        assert ("stop", outcome["evaluation_id"]) in trace
        completed[outcome["evaluation_id"]] = copy.deepcopy(outcome)
        return SimpleNamespace(payload=copy.deepcopy(outcome))

    patch("complete_portable_evaluation", complete)

    def checkpoint(session, *args):
        state = CheckpointStore.load(
            args[-2], expected_input_digest=args[-2].input_digest
        )
        for row in state["candidates"]:
            for outcome in row["window_results"]:
                assert outcome["evaluation_id"] in completed
        checkpoints.append(state)

    patch("publish_checkpoint", checkpoint)

    def finish(session, *args, sandbox_stopped):
        if h.mode == "finish_fence":
            raise FenceLost()
        outcome = args[-1]
        run.status = outcome["status"]
        trace.append(("finish", run.status, sandbox_stopped, outcome))
        return run

    patch("finish_research_attempt", finish)

    def stage(**kwargs):
        trace.append(("stage", kwargs["code"]))
        root = kwargs["target"]
        return SimpleNamespace(engine_root=root / "engine", policy_root=root / "policy")

    h.stage = patch("stage_portable_input", stage)
    # Deliberately fake result publication/ledger: this test asserts control order only.
    patch(
        "publish_portable_artifacts",
        lambda **kwargs: (
            (
                [],
                {
                    "summary": {"total_return_pct": 2.5, "max_drawdown_pct": 0},
                    "result_trust": "isolated_policy_trusted_akquant_engine",
                    "ledger_origin": "trusted_engine",
                },
            )
            if kwargs["result"].get("status") == "succeeded"
            else ([], None)
        ),
    )
    sandbox = create_autospec(portable_runner.PortableDockerEvaluator, instance=True)
    sandbox.bind_environment.return_value = IMAGES

    def launch(evaluation_id, engine_input_dir, policy_input_dir, output_dir, **kwargs):
        assert ("registered_commit", evaluation_id) in trace
        assert ("pin", IMAGES) in trace
        artifacts = [
            row[1]
            for row in trace
            if row[0] == "artifact" and row[1].name == "execution-environment.json"
        ]
        assert len(artifacts) == 1
        evidence = json.loads((tmp_path / artifacts[0].storage_key).read_bytes())
        assert evidence["image_ids"] == IMAGES
        assert kwargs["attempt_id"] == str(attempt_id) and kwargs["run_id"] == str(
            run_id
        )
        trace.append(("launch", evaluation_id))
        return {"status": "succeeded", "logs": {}}

    sandbox.run.side_effect = launch
    sandbox.stop.side_effect = lambda key: (
        trace.append(("stop", key)) or h.mode != "stop_failure"
    )
    sandbox.stop_attempt.side_effect = lambda key: (
        trace.append(("stop_attempt", key)) or h.mode != "stop_failure"
    )
    monkeypatch.setattr(
        portable_runner,
        "PortableDockerEvaluator",
        create_autospec(portable_runner.PortableDockerEvaluator, return_value=sandbox),
    )
    h.execute = lambda: worker.execute_portable_configured(
        str(run_id), 1, settings, factory
    )
    return h


def test_worker_actual_optimizer_registers_commits_environment_then_both_siblings(
    controller,
):
    h = controller
    assert h.execute() == {"status": "succeeded", "sandbox_stopped": True}, h.trace
    assert len(h.completed) == 4 and len(h.checkpoints) > 4
    assert all(row["status"] == "succeeded" for row in h.completed.values())
    assert any(row["model_calls"] for row in h.checkpoints)
    assert any(row[0] == "stop_attempt" for row in h.trace)


@pytest.mark.parametrize(
    "mode,status",
    [
        ("stop_failure", "recovery_blocked"),
        ("finish_fence", "fence_lost"),
        ("cancel", "cancelled"),
    ],
)
def test_worker_unknown_cleanup_fence_and_cancellation_never_claim_success(
    controller, mode, status
):
    controller.mode = mode
    assert controller.execute()["status"] == status
    if mode == "stop_failure":
        assert not controller.completed
    if mode == "cancel":
        assert not any(row[0] == "launch" for row in controller.trace)


def test_worker_rejects_protected_edit_before_staging_or_sandbox(
    controller, monkeypatch
):
    from packages.tradegrad_adapter import model_calls

    original = model_calls.FrozenModel
    bad = controller.source.replace("StateCodec", "UntrustedCodec", 1)

    class ChangedModel(original):
        def __init__(self, _):
            super().__init__(["```python\n" + bad + "\n```"] * 100)

    monkeypatch.setattr(model_calls, "FrozenModel", ChangedModel)
    assert controller.execute()["status"] == "succeeded"  # valid seed remains the best
    assert controller.stage.call_count == 2
    assert sum(row["status"] == "failed" for row in controller.completed.values()) == 2


def test_native_and_portable_concurrent_calls_do_not_mutate_source_parser(tmp_path):
    research_runtime()
    from concurrent.futures import ThreadPoolExecutor

    from packages.tradegrad_adapter.model_calls import FrozenModel
    from packages.tradegrad_adapter.optimizer import run_optimization
    from src import optimizer as source_module

    original = source_module.parse_code
    source, portable, _, _ = inputs(iterations=1)
    native_source = "from akquant import Strategy\nclass TestStrategy(Strategy):\n    ### EDIT START\n    value = 1\n    ### EDIT END\n"
    native = {
        key: portable.model_dump(mode="json")[key]
        for key in ("optimizer", "budgets", "model")
    }

    def run_one(label, config, code):
        return run_optimization(
            config=config,
            initial_code=code,
            windows=portable_evaluation_windows(portable),
            evaluate=lambda *a: {"status": "succeeded", "combined_score": 1},
            root=tmp_path / label,
            model=FrozenModel(["```python\n" + code + "\n```"] * 100),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        native_future = pool.submit(run_one, "native", native, native_source)
        portable_future = pool.submit(
            run_one, "portable", portable.model_dump(mode="json"), source
        )
        native_result, portable_result = (
            native_future.result(),
            portable_future.result(),
        )
    assert source_module.parse_code is original
    assert native_result.status == portable_result.status == "succeeded"
    assert all(
        PROMPT_VERSION not in row["user_prompt"] for row in native_result.model_calls
    )
    assert all(
        PROMPT_VERSION in row["user_prompt"] for row in portable_result.model_calls
    )
    assert all(
        row["valid"] for row in portable_result.candidates + native_result.candidates
    )


@pytest.mark.parametrize("budget,limit", [("max_model_calls", 1), ("max_tokens", 1)])
def test_portable_actual_optimizer_enforces_model_budgets_without_online_calls(
    tmp_path, budget, limit
):
    research_runtime()
    from packages.tradegrad_adapter.model_calls import FrozenModel
    from packages.tradegrad_adapter.optimizer import run_optimization

    source, config, _, _ = inputs(iterations=3)
    values = config.model_dump(mode="json")
    values["budgets"][budget] = limit
    model = FrozenModel(["```python\n" + source + "\n```"] * 100)
    result = run_optimization(
        config=values,
        initial_code=source,
        windows=portable_evaluation_windows(config),
        evaluate=lambda *a: {"status": "succeeded", "combined_score": 0},
        root=tmp_path,
        model=model,
    )
    assert result.stop_reason == budget
    assert len(model.requests) < 2


def test_prompt_projection_preserves_fenced_policy_bytes_and_replaces_memory_reward():
    from packages.tradegrad_adapter.portable_prompts import transform_prompt

    source = template_source("event")
    prose = "`combined_score` is ranged from 0 to 100, with higher values indicating better performance. And above 50 is considered good."
    changed = source.replace(
        "bars[-1].close > bars[-2].close",
        repr(prose) + " and bars[-1].close > bars[-2].close",
    )
    projected = transform_prompt(prose + "\n```python\n" + changed + "\n```")
    assert "```python\n" + changed + "\n```" in projected
    assert projected.count(prose) == 1
    assert "distinct portable objective" in projected


@pytest.mark.parametrize("kind", ["event", "portfolio"])
def test_actual_small_revision_applies_business_diff_without_rewrite_fallback(
    tmp_path, kind
):
    research_runtime()
    from packages.tradegrad_adapter.model_calls import FrozenModel, ModelResponse
    from packages.tradegrad_adapter.optimizer import run_optimization

    source, config, _, _ = inputs(kind, 1)
    before, after = (
        ("bars[-1].close > bars[-2].close", "bars[-1].close >= bars[-2].close")
        if kind == "event"
        else ("(-score.value, score.listing_id)", "(score.value, score.listing_id)")
    )
    expected = source.replace(before, after)
    config = config.model_copy(
        update={
            "optimizer": config.optimizer.model_copy(
                update={"large_revision_prob": 0.0}
            )
        }
    )

    class DiffFixture(FrozenModel):
        def complete(self, request):
            self.requests.append(request)
            if "Output with SEARCH/REPLACE diff format." in (
                request.system_prompt or ""
            ):
                return ModelResponse(
                    f"<<<<<<< SEARCH\n{before}\n=======\n{after}\n>>>>>>> REPLACE",
                    0,
                    0,
                    usage_source="synthetic_fixture",
                )
            return ModelResponse(
                "Frozen reasoning fixture", 0, 0, usage_source="synthetic_fixture"
            )

    def evaluate(code, *args):
        validate_policy_source(code, kind)
        return {"status": "succeeded", "combined_score": 1 if code == source else 2}

    result = run_optimization(
        config=config.model_dump(mode="json"),
        initial_code=source,
        windows=portable_evaluation_windows(config),
        evaluate=evaluate,
        root=tmp_path,
        model=DiffFixture([]),
    )
    assert result.status == "succeeded" and result.candidates[-1]["code"] == expected
    assert result.candidates[-1]["parent_id"] == "candidate-0000"
    assert (
        len([row for row in result.model_calls if ":generate:" in row["call_id"]]) == 1
    )
