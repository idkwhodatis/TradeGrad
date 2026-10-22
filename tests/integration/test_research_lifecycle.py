"""Real migrated PostgreSQL only: research outbox, publication, ownership and fencing.

These are intentionally collected/skipped without TEST_DATABASE_URL. They never
use SQLite or mocked persistence and do not claim real Docker/model acceptance.
"""

import copy
import hashlib
import json
import random
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from packages.tradegrad_adapter.checkpoint import CheckpointStore, json_digest
from packages.tradegrad_adapter.config import (
    MarketConfig,
    RunConfig,
    evaluation_windows,
)

pytestmark = pytest.mark.postgres


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def research_payload(migrated_engine, settings, owner):
    from backend.app.models import DatasetVersion, UniverseVersion, utcnow
    from packages.tradegrad_adapter.environment import environment_digest

    listing_id, universe_id = uuid4(), uuid4()
    universe_hash = sha(str(universe_id).encode())
    code = owner.strategy.code
    rows = ["date,open,high,low,close,volume"]
    for offset in range(91):
        day = date(2024, 1, 1) + timedelta(days=offset)
        rows.append(f"{day},10,11,9,10,100")
    files = {
        "data/INDEX.csv": ("\n".join(rows) + "\n").encode(),
        "universe/fixed_list_2024-01.csv": b"updateDate,code,code_name\n2024-01-01,INDEX,Synthetic fixture\n",
    }
    manifest = {
        "schema_version": 1,
        "is_synthetic": True,
        "files": {name: sha(data) for name, data in files.items()},
        "listings": [{"listing_id": str(listing_id)}],
        "evaluator_symbol_mapping": {str(listing_id): "INDEX"},
        "universe": {
            "id": str(universe_id),
            "content_sha256": universe_hash,
            "state": "user_fixed",
        },
        "evaluation_compatibility": {"compatible": True},
        "quality": {"unknown_flags": []},
    }
    manifest_bytes = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    root = (
        settings.storage_root / "datasets" / str(owner.user.id) / str(owner.dataset.id)
    )
    root.mkdir(parents=True)
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "manifest.json").write_bytes(manifest_bytes)
    with Session(migrated_engine) as session:
        session.add(
            UniverseVersion(
                id=universe_id,
                owner_id=owner.user.id,
                name="Synthetic fixture universe",
                state="user_fixed",
                as_of=utcnow(),
                members=[{"listing_id": str(listing_id)}],
                evidence=[],
                content_sha256=universe_hash,
            )
        )
        dataset = session.get(DatasetVersion, owner.dataset.id)
        dataset.manifest = manifest
        dataset.content_sha256 = sha(manifest_bytes)
        dataset.storage_key = str(root.relative_to(settings.storage_root))
        session.commit()
    config = RunConfig.model_validate(
        {
            "execution_mode": "optimization",
            "task": "cn_time",
            "dataset": {"id": str(owner.dataset.id), "sha256": sha(manifest_bytes)},
            "seed_strategy": {
                "id": str(owner.strategy.id),
                "sha256": sha(code.encode()),
            },
            "universe": {"id": str(universe_id), "sha256": universe_hash},
            "windows": {
                "train": {"start": "2024-02-01", "end": "2024-02-10"},
                "validation": {"start": "2024-02-15", "end": "2024-02-20"},
                "holdout": {"start": "2024-03-01", "end": "2024-03-05"},
            },
            "warmup": {"sessions": 20},
            "market_profile": MarketConfig.for_market("CN").model_dump(mode="json"),
            "optimizer": {"max_iterations": 0},
            "environment_digest": environment_digest(),
        }
    )
    return config.model_dump(mode="json")


@pytest.fixture
def submit_research(api, owner_headers, research_payload):
    def submit(key=None, payload=None, headers=None):
        return api.post(
            "/api/v1/research-runs",
            headers={
                **(headers or owner_headers),
                "Idempotency-Key": key or uuid4().hex,
            },
            json=payload or research_payload,
        )

    return submit


@pytest.fixture
def research_attempt(submit_research, migrated_engine, settings):
    from backend.app.research_services import pin_research_image
    from backend.app.services import claim_attempt

    response = submit_research()
    assert response.status_code == 202, response.text
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session, UUID(response.json()["id"]), 1, "research-pg-test", settings
        )
        assert attempt is not None
        pin_research_image(
            session, attempt.id, 1, attempt.lease_token, "sha256:" + "a" * 64
        )
        session.commit()
    return attempt


@pytest.fixture
def publish_initial(
    migrated_engine, settings, owner, research_payload, research_attempt
):
    from backend.app.research_services import publish_checkpoint

    config = RunConfig.model_validate(research_payload)
    windows = list(evaluation_windows(config))
    state = {
        "schema_version": 1,
        "inputs": {
            "config": research_payload,
            "initial_code_sha256": sha(owner.strategy.code.encode()),
            "windows": windows,
        },
        "next_stage": "evaluate",
        "iteration": 0,
        "current_id": "candidate-0000",
        "candidates": [
            {
                "candidate_id": "candidate-0000",
                "parent_id": None,
                "kind": "seed",
                "iteration": 0,
                "repair_index": 0,
                "code": owner.strategy.code,
                "code_sha256": sha(owner.strategy.code.encode()),
                "evaluation": None,
                "window_results": [],
                "valid": False,
            }
        ],
        "optimizer": {
            "archive": [],
            "memory_ids": [],
            "memory_summary": "",
            "sampled_ids": [],
            "revision_mode": "small",
            "pending_parent_id": None,
        },
        "random_state": {
            "python": list(random.Random(0).getstate()),
            "numpy": ["MT19937", [0] * 624, 0, 0, 0.0],
        },
        "model_calls": [],
        "feedback": "",
        "elapsed_seconds": 0.0,
        "stop_reason": None,
    }
    state["random_state"]["python"][1] = list(state["random_state"]["python"][1])
    root = (
        settings.storage_root
        / "runs"
        / str(research_attempt.run_id)
        / "attempts"
        / str(research_attempt.id)
        / "checkpoints"
    )
    store = CheckpointStore(root, json_digest(state["inputs"]))

    publications = 0

    def publish(value=None):
        nonlocal publications
        publications += 1
        value = copy.deepcopy(value or state)
        value["elapsed_seconds"] = float(publications)
        ref = store.commit(value)
        with Session(migrated_engine, expire_on_commit=False) as session:
            row = publish_checkpoint(
                session,
                research_attempt.id,
                1,
                research_attempt.lease_token,
                ref,
                settings,
            )
            session.commit()
        return row, ref

    return publish, state, store


def test_research_submission_idempotency_outbox_and_exact_snapshot(
    submit_research, migrated_engine, research_payload
):
    from backend.app.models import OutboxEvent, ResearchRun, RunEvent

    first, repeated = submit_research("same"), submit_research("same")
    assert first.status_code == repeated.status_code == 202, first.text
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["config"] == research_payload
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(ResearchRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 1
    changed = copy.deepcopy(research_payload)
    changed["optimizer"]["max_iterations"] = 1
    assert submit_research("same", changed).status_code == 409


@pytest.mark.parametrize("field", ["dataset", "seed_strategy", "universe"])
def test_research_rejects_wrong_owned_reference_hash(
    submit_research, research_payload, field
):
    changed = copy.deepcopy(research_payload)
    changed[field]["sha256"] = "0" * 64
    response = submit_research(payload=changed)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "reference_hash_mismatch"


def test_foreign_references_are_not_disclosed(submit_research, other_headers):
    response = submit_research(headers=other_headers)
    assert response.status_code == 404, response.text


def test_semantic_and_environment_preflight_is_transactional(
    submit_research, research_payload, migrated_engine
):
    from backend.app.models import OutboxEvent

    changed = copy.deepcopy(research_payload)
    changed["environment_digest"] = "0" * 64
    assert submit_research(payload=changed).status_code == 422
    changed = copy.deepcopy(research_payload)
    changed["windows"]["train"]["start"] = "2024-01-02"
    assert submit_research(payload=changed).status_code == 422
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_registered_child_sandbox_blocks_pause_until_verified_stopped(
    migrated_engine, research_attempt, publish_initial, owner, research_payload
):
    from backend.app.models import Evaluation, ResearchRun
    from backend.app.research_services import (
        begin_evaluation,
        complete_evaluation,
        finish_research_attempt,
        request_pause,
    )

    checkpoint, _ = publish_initial[0]()
    attempt = research_attempt
    with Session(migrated_engine, expire_on_commit=False) as session:
        child = begin_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            candidate_key="candidate-0000",
            code_sha256=research_payload["seed_strategy"]["sha256"],
            window=evaluation_windows(RunConfig.model_validate(research_payload))[0],
        )
        session.commit()
    with Session(migrated_engine) as session:
        request_pause(session, attempt.run_id, owner.user.id)
        run = finish_research_attempt(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            {"status": "paused", "stop_reason": "paused"},
            True,
        )
        assert run.status == "pause_requested"
        assert not session.get(Evaluation, child.id).sandbox_stopped
        session.commit()
    with Session(migrated_engine) as session:
        complete_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            child.id,
            outcome={"status": "failed", "exit_code": 1, "error": "fixture"},
            sandbox_stopped=True,
        )
        session.commit()
    checkpoint, _ = publish_initial[0]()
    with Session(migrated_engine) as session:
        run = finish_research_attempt(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            {"status": "paused", "stop_reason": "paused"},
            True,
        )
        assert run.status == "paused"
        session.commit()
    with Session(migrated_engine) as session:
        assert (
            session.get(ResearchRun, attempt.run_id).latest_checkpoint_id
            == checkpoint.id
        )


def test_resume_selects_only_verified_database_pointer_and_fences_prior_attempt(
    migrated_engine, settings, owner, research_attempt, publish_initial
):
    from backend.app.models import OutboxEvent, ResearchRun
    from backend.app.research_services import (
        finish_research_attempt,
        request_pause,
        resume_research_run,
    )
    from backend.app.services import FenceLost, heartbeat

    checkpoint, _ref = publish_initial[0]()
    attempt = research_attempt
    # Newer complete filesystem orphan deliberately is not a database candidate.
    publish_initial[2].commit(publish_initial[1])
    with Session(migrated_engine) as session:
        request_pause(session, attempt.run_id, owner.user.id)
        session.commit()
    checkpoint, _ref = publish_initial[0]()
    with Session(migrated_engine) as session:
        finish_research_attempt(
            session, attempt.id, 1, attempt.lease_token, {"status": "paused"}, True
        )
        session.commit()
    with Session(migrated_engine) as session:
        run = resume_research_run(session, attempt.run_id, owner.user.id, settings)
        assert run.generation == 2 and run.resume_checkpoint_id == checkpoint.id
        session.commit()
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2
        assert (
            session.get(ResearchRun, attempt.run_id).latest_checkpoint_id
            == checkpoint.id
        )
        with pytest.raises(FenceLost):
            heartbeat(session, attempt.id, 1, attempt.lease_token, settings)


def test_corrupt_checkpoint_blocks_resume_without_new_outbox(
    migrated_engine, settings, owner, research_attempt, publish_initial
):
    from pathlib import Path

    from backend.app.models import OutboxEvent
    from backend.app.research_services import (
        finish_research_attempt,
        request_pause,
        resume_research_run,
    )
    from backend.app.services import DomainError

    publish_initial[0]()
    attempt = research_attempt
    with Session(migrated_engine) as session:
        request_pause(session, attempt.run_id, owner.user.id)
        session.commit()
    _checkpoint, ref = publish_initial[0]()
    with Session(migrated_engine) as session:
        finish_research_attempt(
            session, attempt.id, 1, attempt.lease_token, {"status": "paused"}, True
        )
        session.commit()
    Path(ref.manifest_path).write_text("{}")
    with Session(migrated_engine) as session, pytest.raises(DomainError, match="hash"):
        resume_research_run(session, attempt.run_id, owner.user.id, settings)
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_expired_attempt_stops_every_registered_child_before_release(
    migrated_engine, settings, research_attempt, research_payload
):
    from backend.app.models import ExecutionAttempt, ResearchRun, utcnow
    from backend.app.research_services import begin_evaluation
    from backend.app.services import reconcile_attempt

    attempt = research_attempt
    children = []
    with Session(migrated_engine) as session:
        for window in evaluation_windows(RunConfig.model_validate(research_payload)):
            children.append(
                begin_evaluation(
                    session,
                    attempt.id,
                    1,
                    attempt.lease_token,
                    candidate_key="candidate-0000",
                    code_sha256=research_payload["seed_strategy"]["sha256"],
                    window=window,
                ).id
            )
        session.get(ExecutionAttempt, attempt.id).lease_expires_at = (
            utcnow() - timedelta(seconds=1)
        )
        session.commit()
    calls = []

    def blocked(identifier):
        calls.append(identifier)
        return identifier != str(children[-1])

    with Session(migrated_engine) as session:
        assert not reconcile_attempt(session, attempt.id, blocked)
        session.commit()
    assert set(calls) == {str(attempt.id), *(str(child) for child in children)}
    with Session(migrated_engine) as session:
        assert not session.get(ExecutionAttempt, attempt.id).sandbox_stopped
        assert session.get(ResearchRun, attempt.run_id).generation == 1
        assert reconcile_attempt(session, attempt.id, lambda _: True)
        session.commit()


def test_provenance_queries_are_owner_scoped_and_paginated(
    api, owner_headers, other_headers, research_attempt, publish_initial
):
    checkpoint, _ = publish_initial[0]()
    run_id = research_attempt.run_id
    for kind in ("candidates", "evaluations", "model-calls", "checkpoints"):
        path = f"/api/v1/research-runs/{run_id}/{kind}"
        assert api.get(path, headers=other_headers).status_code == 404
        assert (
            api.get(path, headers=owner_headers, params={"limit": 101}).status_code
            == 422
        )
        own = api.get(path, headers=owner_headers)
        assert own.status_code == 200, own.text
    path = f"/api/v1/research-checkpoints/{checkpoint.id}"
    assert api.get(path, headers=other_headers).status_code == 404
    own = api.get(path, headers=owner_headers)
    assert own.status_code == 200, own.text
    assert "manifest_storage_key" not in own.json()


def test_m3_migration_has_real_fks_and_run_input_update_guard(
    migrated_engine, research_attempt
):
    assert {
        "research_candidates",
        "research_evaluations",
        "research_model_calls",
        "research_checkpoints",
    } <= set(inspect(migrated_engine).get_table_names())
    assert any(
        key["referred_table"] == "research_checkpoints"
        for key in inspect(migrated_engine).get_foreign_keys("research_runs")
    )
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text("UPDATE research_runs SET config = '{}'::jsonb WHERE id = :id"),
            {"id": research_attempt.run_id},
        )


def test_image_digest_is_pinned_once(migrated_engine, research_attempt):
    from backend.app.research_services import pin_research_image
    from backend.app.services import DomainError

    attempt = research_attempt
    with Session(migrated_engine) as session:
        pin_research_image(
            session, attempt.id, 1, attempt.lease_token, "sha256:" + "a" * 64
        )
        session.commit()
    with Session(migrated_engine) as session:
        pin_research_image(
            session, attempt.id, 1, attempt.lease_token, "sha256:" + "a" * 64
        )
        with pytest.raises(DomainError):
            pin_research_image(
                session, attempt.id, 1, attempt.lease_token, "sha256:" + "b" * 64
            )


@pytest.fixture
def complete_candidate(
    migrated_engine, research_attempt, research_payload, publish_initial
):
    from backend.app.research_services import begin_evaluation, complete_evaluation

    publish, initial, _ = publish_initial
    publish()
    state = copy.deepcopy(initial)
    outcomes = []
    attempt = research_attempt
    for window in evaluation_windows(RunConfig.model_validate(research_payload)):
        score = 10.0 if window["split"] == "train" else 9.0
        outcome = {
            "status": "succeeded",
            "exit_code": 0,
            "metrics": {
                "metrics": {
                    "sharpe_ratio": 1.0,
                    "annualized_return": 0.1,
                    "max_drawdown": 0.05,
                    "trade_per_day": 1.0,
                    "win_rate": 0.5,
                    "return_per_trade": 0.01,
                },
                "combined_score": score,
            },
            "image_digest": "sha256:" + "a" * 64,
            "combined_score": score,
        }
        with Session(migrated_engine, expire_on_commit=False) as session:
            item = begin_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                candidate_key="candidate-0000",
                code_sha256=research_payload["seed_strategy"]["sha256"],
                window=window,
            )
            session.commit()
        with Session(migrated_engine) as session:
            from backend.app.models import Artifact

            # Deliberately synthetic DB transaction evidence; native bundle bytes
            # and Docker enforcement are covered only by the real system gate.
            artifact = Artifact(
                id=uuid4(),
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                name=f"evaluation-{item.id}-result-bundle.json",
                media_type="application/json",
                storage_key=f"synthetic-test-only/{item.id}.json",
                sha256=sha(b"{}"),
                size_bytes=2,
            )
            session.add(artifact)
            outcome["result_bundle"] = {
                "artifact_id": str(artifact.id),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
            complete_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                item.id,
                outcome=outcome,
                sandbox_stopped=True,
            )
            session.commit()
        outcomes.append(
            {
                **outcome,
                "evaluation_id": str(item.id),
                "window": window,
                "split": window["split"],
                "valid": True,
            }
        )
    state["candidates"][0].update(
        window_results=outcomes,
        evaluation={
            "combined_score": 10.0,
            "windows": outcomes,
            "validation_valid": True,
            "validation_score": 9.0,
        },
        valid=True,
    )
    state["optimizer"]["archive"] = ["candidate-0000"]
    state.update(next_stage="finished", stop_reason="max_iterations")
    return publish, state


def test_success_requires_complete_checkpoint_and_verified_objective(
    migrated_engine, research_attempt, complete_candidate
):
    from backend.app.research_services import finish_research_attempt

    publish, state = complete_candidate
    checkpoint, _ = publish(state)
    assert checkpoint.summary["candidates"]["candidate-0000"]["valid"] is True
    with Session(migrated_engine) as session:
        run = finish_research_attempt(
            session,
            research_attempt.id,
            1,
            research_attempt.lease_token,
            {
                "status": "succeeded",
                "exit_code": 0,
                "stop_reason": "max_iterations",
                "best_candidate_id": "candidate-0000",
                "combined_score": 10.0,
            },
            True,
        )
        assert run.status == "succeeded"
        assert run.result["holdout_executed"] is False
        assert (
            run.result["best_candidate_id"]
            == checkpoint.summary["candidates"]["candidate-0000"]["id"]
        )
        session.commit()


def test_final_best_score_cannot_be_fabricated(
    migrated_engine, research_attempt, complete_candidate
):
    from backend.app.research_services import finish_research_attempt

    publish, state = complete_candidate
    publish(state)
    with Session(migrated_engine) as session:
        run = finish_research_attempt(
            session,
            research_attempt.id,
            1,
            research_attempt.lease_token,
            {
                "status": "succeeded",
                "exit_code": 0,
                "stop_reason": "max_iterations",
                "best_candidate_id": "candidate-0000",
                "combined_score": 10000.0,
            },
            True,
        )
        assert run.status == "failed"
        assert run.result["stop_reason"] == "invalid_optimization_result"
        session.commit()


def test_forged_checkpoint_metrics_leave_latest_pointer_unchanged(
    migrated_engine, research_attempt, complete_candidate
):
    from backend.app.models import ResearchRun
    from backend.app.services import DomainError

    publish, state = complete_candidate
    checkpoint, _ = publish(state)
    forged = copy.deepcopy(state)
    forged["candidates"][0]["window_results"][0]["metrics"]["metrics"]["return"] = 999.0
    with pytest.raises(DomainError, match="metrics differ"):
        publish(forged)
    with Session(migrated_engine) as session:
        assert (
            session.get(ResearchRun, research_attempt.run_id).latest_checkpoint_id
            == checkpoint.id
        )


def test_checkpoint_publication_is_fenced_before_files_or_database_write(
    migrated_engine, settings, research_attempt, publish_initial
):
    from backend.app.research_services import publish_checkpoint
    from backend.app.services import FenceLost

    _, ref = publish_initial[0]()
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        publish_checkpoint(
            session, research_attempt.id, 2, research_attempt.lease_token, ref, settings
        )


def test_pause_does_not_accept_checkpoint_from_before_request(
    migrated_engine, owner, research_attempt, publish_initial
):
    from backend.app.research_services import finish_research_attempt, request_pause
    from backend.app.services import DomainError

    publish_initial[0]()
    with Session(migrated_engine) as session:
        request_pause(session, research_attempt.run_id, owner.user.id)
        with pytest.raises(DomainError, match="after the pause request"):
            finish_research_attempt(
                session,
                research_attempt.id,
                1,
                research_attempt.lease_token,
                {"status": "paused"},
                True,
            )


def test_completed_evaluation_payload_is_immutable(
    migrated_engine, research_attempt, complete_candidate
):
    from backend.app.research_services import complete_evaluation
    from backend.app.services import DomainError

    _, state = complete_candidate
    physical_id = UUID(state["candidates"][0]["window_results"][0]["evaluation_id"])
    with (
        Session(migrated_engine) as session,
        pytest.raises(DomainError, match="cannot be changed"),
    ):
        complete_evaluation(
            session,
            research_attempt.id,
            1,
            research_attempt.lease_token,
            physical_id,
            outcome={
                "status": "succeeded",
                "exit_code": 0,
                "metrics": {"score": 999},
                "combined_score": 999,
            },
            sandbox_stopped=True,
        )


def test_completed_model_call_preserves_response_usage_and_owner_scope(
    api,
    owner_headers,
    other_headers,
    migrated_engine,
    research_attempt,
    publish_initial,
):
    from backend.app.models import ModelCall
    from backend.app.services import DomainError

    publish, state, _ = publish_initial
    state = copy.deepcopy(state)
    row = {
        "call_id": "iteration:0000:backward:0000",
        "logical_index": 0,
        "input_digest": json_digest(state["inputs"]),
        "provider": "fixture",
        "model": "deterministic-fixture-v1",
        "prompt_version": "source_prompts.v1",
        "parameters": {"temperature": 0, "max_output_tokens": 1000},
        "request_sha256": "d" * 64,
        "user_prompt": "Synthetic fixture prompt",
        "system_prompt": None,
        "status": "completed",
        "response": "Synthetic fixture response",
        "input_tokens": 4,
        "output_tokens": 5,
        "charged_tokens": 9,
        "usage_source": "provider",
        "latency_seconds": 0.1,
        "error_class": None,
        "adopted": False,
        "price": None,
        "provider_request_id": None,
    }
    state["model_calls"] = [row]
    publish(state)
    state["model_calls"][0]["adopted"] = True
    publish(state)
    with Session(migrated_engine) as session:
        item = session.scalar(
            select(ModelCall).where(ModelCall.run_id == research_attempt.run_id)
        )
        assert item.adopted and item.usage["charged_tokens"] == 9
        assert item.response_sha256 == sha(b"Synthetic fixture response")
        identifier = item.id
    own = api.get(f"/api/v1/model-calls/{identifier}", headers=owner_headers)
    assert own.status_code == 200, own.text
    assert own.json()["response_artifact"]["sha256"]
    assert own.json()["payload"]["response"] == "Synthetic fixture response"
    assert (
        api.get(f"/api/v1/model-calls/{identifier}", headers=other_headers).status_code
        == 404
    )
    state["model_calls"][0]["response"] = "Overwritten response"
    with pytest.raises(DomainError, match="cannot change"):
        publish(state)


def test_repair_versions_preserve_failed_parent_code(migrated_engine, publish_initial):
    from backend.app.models import Candidate

    publish, state, _ = publish_initial
    state = copy.deepcopy(state)
    seed_code = state["candidates"][0]["code"]
    publish(state)
    repair_code = seed_code + "# A new immutable repaired version.\n"
    child = {
        "candidate_id": "candidate-0000-repair-01",
        "parent_id": "candidate-0000",
        "kind": "repair",
        "iteration": 0,
        "repair_index": 1,
        "code": repair_code,
        "code_sha256": sha(repair_code.encode()),
        "evaluation": None,
        "window_results": [],
        "valid": False,
    }
    state["candidates"].append(child)
    state["current_id"] = child["candidate_id"]
    publish(state)
    with Session(migrated_engine) as session:
        candidates = session.scalars(
            select(Candidate).order_by(Candidate.repair_index)
        ).all()
        assert len(candidates) == 2
        assert candidates[0].code == seed_code
        assert candidates[1].code == repair_code
        assert candidates[1].parent_id == candidates[0].id


def test_research_pause_can_be_requested_while_queued(
    api, owner_headers, submit_research, migrated_engine, settings
):
    from backend.app.models import ResearchRun
    from backend.app.services import claim_attempt

    response = submit_research()
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["id"])
    paused = api.post(f"/api/v1/research-runs/{run_id}/pause", headers=owner_headers)
    assert paused.status_code == 202, paused.text
    assert paused.json()["status"] == "pause_requested"
    with Session(migrated_engine) as session:
        attempt = claim_attempt(session, run_id, 1, "queued-pause-test", settings)
        assert attempt is not None
        assert session.get(ResearchRun, run_id).status == "pause_requested"
        session.commit()


def test_success_without_owned_bundle_is_persisted_as_failed(
    migrated_engine, research_attempt, research_payload
):
    from backend.app.research_services import begin_evaluation, complete_evaluation

    attempt = research_attempt
    window = evaluation_windows(RunConfig.model_validate(research_payload))[0]
    with Session(migrated_engine, expire_on_commit=False) as session:
        item = begin_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            candidate_key="candidate-0000",
            code_sha256=research_payload["seed_strategy"]["sha256"],
            window=window,
        )
        session.commit()
    with Session(migrated_engine) as session:
        result = complete_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            item.id,
            outcome={
                "status": "succeeded",
                "exit_code": 0,
                "image_digest": "sha256:" + "a" * 64,
                "metrics": {
                    "combined_score": 10.0,
                    "metrics": {
                        "sharpe_ratio": 1.0,
                        "annualized_return": 0.1,
                        "max_drawdown": 0.05,
                        "trade_per_day": 1.0,
                        "win_rate": 0.5,
                        "return_per_trade": 0.01,
                    },
                },
            },
            sandbox_stopped=True,
        )
        assert result.status == "failed" and result.sandbox_stopped
        assert "incomplete result evidence" in result.error
        session.commit()
