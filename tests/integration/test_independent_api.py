"""Real PostgreSQL I5 source/admission/protocol/lifecycle gates.

Source bundles and RNG are explicitly synthetic boundary fixtures; this suite
never calls a model/provider/broker and is not Docker/engine certification.
"""

import hashlib
import os
import random
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.models import (
    Artifact,
    ControlledArm,
    ControlledExperiment,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    IndependentEvaluation,
    OutboxEvent,
    ResearchRun,
    SplitVersion,
)
from packages.tradegrad_adapter.runtime_observation import (
    NativeAuditObserver,
    canonical,
    capture_rng_state,
    make_runtime_observation,
)
from tests.integration import test_comparison_api as comparison

base_research_payload = comparison.base_research_payload
research_payload = comparison.research_payload
submit_research = comparison.submit_research
research_attempt = comparison.research_attempt
published_pair = comparison.published_pair
pytestmark = pytest.mark.postgres


def test_independent_required_postgres_gate():
    if os.environ.get("TRADEGRAD_INDEPENDENT_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Independent admission/lifecycle requires real PostgreSQL TEST_DATABASE_URL"
        )


@pytest.fixture
def runtime_pair(request, monkeypatch, settings):
    original = comparison.complete_evaluation

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
        evaluation = session.get(Evaluation, evaluation_id)
        artifact = session.get(Artifact, UUID(outcome["result_bundle"]["artifact_id"]))
        import json

        bundle = json.loads((settings.storage_root / artifact.storage_key).read_bytes())
        rng = capture_rng_state()
        # Deliberately synthetic valid serialized state, never claimed an actual
        # native run's captured state. Production state is emitted by the runner.
        gen = random.Random(729)
        rng["numpy_legacy"] = {
            "algorithm": "MT19937",
            "keys": [gen.getrandbits(32) for _ in range(624)],
            "position": 624,
            "has_gauss": 0,
            "cached_gaussian": 0.0,
        }
        rng["scope"] = ["python.random", "numpy.random.legacy"]
        value = make_runtime_observation(
            runtime_kind="native",
            identity=bundle["identity"],
            initialization={
                "policy": "reconstruct_from_original_start.v1",
                "window": bundle["window"],
            },
            rng_state=rng,
            audit=NativeAuditObserver().finish(),
        )
        raw = canonical(value)
        key = f"runs/{evaluation.run_id}/attempts/{attempt_id}/synthetic-{evaluation_id}-runtime.json"
        path = settings.storage_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        ref = Artifact(
            id=uuid4(),
            run_id=evaluation.run_id,
            attempt_id=attempt_id,
            name=f"evaluation-{evaluation_id}-runtime-observation.json",
            media_type="application/json",
            storage_key=key,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        )
        session.add(ref)
        session.flush()
        outcome = outcome | {
            "runtime_observation": {
                "artifact_id": str(ref.id),
                "sha256": ref.sha256,
                "size_bytes": ref.size_bytes,
            }
        }
        return original(
            session,
            attempt_id,
            generation,
            token,
            evaluation_id,
            outcome=outcome,
            sandbox_stopped=sandbox_stopped,
        )

    monkeypatch.setattr(comparison, "complete_evaluation", complete)
    return request.getfixturevalue("published_pair")


def diagnostic_body(values):
    return {
        "source": comparison.request_body(values)["left"],
        "research_capture": {
            "mode": "research",
            "observation_window": {"start": "2024-02-04", "end": "2024-02-06"},
        },
    }


def create(api, headers, values, key=None):
    body = diagnostic_body(values)
    response = api.post(
        f"/api/v1/evaluations/{body['source']['evaluation_id']}/diagnostic-replays",
        headers=headers | {"Idempotency-Key": key or uuid4().hex},
        json=body,
    )
    assert response.status_code == 202, response.text
    return response.json()


def control_body(values, code, count=3):
    return diagnostic_body(values) | {
        "name": "Three manually frozen arms",
        "hypothesis": "Code-only comment should not alter decisions",
        "planned_change": "Add one comment only",
        "arms": [
            {
                "name": f"arm-{i}",
                "code": code + (f"# explicit variant {i}\n" if i else ""),
                "planned_change": "baseline" if i == 0 else "comment-only patch",
            }
            for i in range(count)
        ],
    }


def test_diagnostic_is_distinct_new_run_and_copy_preserving_exact_source(
    api, owner_headers, published_pair, migrated_engine
):
    value = create(api, owner_headers, published_pair)
    original = published_pair[0].result
    assert value["id"] != str(original.run.id)
    assert value["config"]["selected_strategy"]["id"] != str(
        original.run.strategy_version_id
    )
    assert value["config"]["reconstruction_window"] == {
        key: original.evaluation.window[key] for key in ("start", "end")
    }
    assert (
        value["config"]["research_capture"]["observation_window"]["start"]
        == "2024-02-04"
    )
    assert value["purpose"] == "diagnostic" and value["status"] == "queued"
    assert value["config"]["rng_restore"] is None
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(value["id"]))
        assert run.latest_checkpoint_id is run.resume_checkpoint_id is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(Evaluation)
                .where(Evaluation.run_id == run.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == run.id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataUsageExposure)
                .where(DataUsageExposure.source_key == f"independent:{run.id}:1")
            )
            == 1
        )


def test_capability_discloses_missing_rng_without_certifying_parity(
    api, owner_headers, published_pair
):
    response = api.post(
        "/api/v1/independent-evaluations/capability",
        headers=owner_headers,
        json=diagnostic_body(published_pair)["source"],
    )
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["can_execute"] and not value["can_control"]
    assert "original_rng_not_recorded" in value["control_unavailable_reasons"]
    assert value["parity_prerequisites"]["status"] == "not_verifiable"


def test_diagnostic_idempotency_and_changed_capture_conflict(
    api, owner_headers, published_pair
):
    first = create(api, owner_headers, published_pair, "stable-diagnostic")
    assert (
        create(api, owner_headers, published_pair, "stable-diagnostic")["id"]
        == first["id"]
    )
    changed = diagnostic_body(published_pair)
    changed["max_runtime_seconds"] = 301
    response = api.post(
        f"/api/v1/evaluations/{changed['source']['evaluation_id']}/diagnostic-replays",
        headers=owner_headers | {"Idempotency-Key": "stable-diagnostic"},
        json=changed,
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_attempt_id", str(uuid4())),
        ("result_manifest_sha256", "f" * 64),
        ("trace_manifest_sha256", None),
        ("source_id", str(uuid4())),
        ("code_sha256", "f" * 64),
    ],
)
def test_source_pin_mismatch_cannot_dispatch(
    api, owner_headers, published_pair, migrated_engine, field, value
):
    body = diagnostic_body(published_pair)
    body["source"][field] = value
    response = api.post(
        f"/api/v1/evaluations/{body['source']['evaluation_id']}/diagnostic-replays",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert response.status_code in (404, 409), response.text
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(IndependentEvaluation)) == 0
        )


def test_foreign_sources_and_new_resources_are_hidden(
    api, owner_headers, other_headers, published_pair
):
    value = create(api, owner_headers, published_pair)
    for path in (
        f"/api/v1/independent-evaluations/{value['id']}",
        f"/api/v1/independent-evaluations/{value['id']}/parity",
    ):
        assert api.get(path, headers=other_headers).status_code == 404
    body = diagnostic_body(published_pair)
    assert (
        api.post(
            "/api/v1/independent-evaluations/capability",
            headers=other_headers,
            json=body["source"],
        ).status_code
        == 404
    )


def test_cancel_retry_new_generation_keeps_admissions_and_rejects_pause(
    api, owner_headers, published_pair, migrated_engine
):
    value = create(api, owner_headers, published_pair)
    path = f"/api/v1/independent-evaluations/{value['id']}"
    assert api.post(path + "/pause", headers=owner_headers).status_code == 409
    cancelled = api.post(path + "/cancel", headers=owner_headers)
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    retried = api.post(
        path + "/retry",
        headers=owner_headers | {"Idempotency-Key": "retry-original-start"},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["generation"] == 2
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.run_id == UUID(value["id"]),
                DataUsageExposure.purpose == "independent_evaluation_admission",
            )
        ).all()
        assert {row.provenance["generation"] for row in rows} == {1, 2}
        assert len({row.provenance["requested_window"]["start"] for row in rows}) == 1


def test_three_arms_clone_child_split_and_freeze_all_versions(
    api, owner, owner_headers, runtime_pair, migrated_engine
):
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": "three-manual-arms"},
        json=control_body(runtime_pair, owner.strategy.code),
    )
    assert response.status_code == 202, response.text
    value = response.json()
    assert len(value["arms"]) == 3 and value["arms"][0]["interpretation"] == "baseline"
    assert all(
        arm["interpretation"] == "not_individually_validated"
        for arm in value["arms"][1:]
    )
    assert len({arm["strategy_version_id"] for arm in value["arms"]}) == 3
    assert len({arm["run_id"] for arm in value["arms"]}) == 3
    with Session(migrated_engine) as session:
        source = session.get(ResearchRun, runtime_pair[0].result.run.id)
        split = session.get(SplitVersion, UUID(value["split_version_id"]))
        original = session.get(SplitVersion, source.split_version_id)
        assert split.id != original.id and split.definition == original.definition
        assert (
            split.experiment_id
            == UUID(value["experiment_id"])
            != original.experiment_id
        )
        assert value["parent_experiment_id"] == str(source.experiment_id)
        assert (
            value["exposure_snapshot"]["independent_unseen_holdout_established"]
            is False
        )
        for arm in value["arms"]:
            run = session.get(ResearchRun, UUID(arm["run_id"]))
            assert run.config["purpose"] == "controlled"
            assert run.config["protocol_sha256"] == value["protocol_sha256"]
            assert run.config["rng_restore"] is not None
    again = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": "three-manual-arms"},
        json=control_body(runtime_pair, owner.strategy.code),
    )
    assert again.status_code == 202 and again.json()["id"] == value["id"]


def test_control_requires_exact_baseline_and_recorded_rng(
    api, owner, owner_headers, published_pair
):
    body = control_body(published_pair, owner.strategy.code)
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert (
        response.status_code == 422
        and response.json()["code"] == "controlled_rng_unavailable"
    )


def test_control_immutable_rows_reject_database_updates(
    api, owner, owner_headers, runtime_pair, migrated_engine
):
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=control_body(runtime_pair, owner.strategy.code, 2),
    )
    assert response.status_code == 202, response.text
    value = response.json()
    for model in (ControlledExperiment, ControlledArm, IndependentEvaluation):
        with Session(migrated_engine) as session:
            row = session.scalar(select(model))
            if model is ControlledExperiment:
                row.protocol_sha256 = "e" * 64
            elif model is ControlledArm:
                row.name = "mutable overwrite forbidden"
            else:
                row.source_snapshot_sha256 = "e" * 64
            with pytest.raises(DBAPIError):
                session.commit()
    assert (
        api.get(
            f"/api/v1/controlled-experiments/{value['id']}", headers=owner_headers
        ).status_code
        == 200
    )


def test_reserved_control_key_blocks_comparison_alias(
    api, owner, owner_headers, runtime_pair
):
    key = "shared-key"
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": key},
        json=control_body(runtime_pair, owner.strategy.code, 2),
    )
    assert response.status_code == 202, response.text
    response = api.post(
        "/api/v1/comparisons",
        headers=owner_headers | {"Idempotency-Key": key},
        json=comparison.request_body(runtime_pair),
    )
    assert response.status_code == 409


def test_one_shot_registration_stop_gate_and_fresh_retry(
    api, owner_headers, published_pair, migrated_engine, settings
):
    from backend.app.independent_services import (
        begin_independent_evaluation,
        complete_independent_evaluation,
        finish_independent_attempt,
    )
    from backend.app.services import claim_attempt, mark_running

    value = create(api, owner_headers, published_pair)
    run_id = UUID(value["id"])
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session, run_id, 1, "independent-boundary-no-engine", settings
        )
        assert mark_running(session, attempt.id, 1, attempt.lease_token)
        first = begin_independent_evaluation(
            session, attempt.id, 1, attempt.lease_token
        )
        again = begin_independent_evaluation(
            session, attempt.id, 1, attempt.lease_token
        )
        assert again.id == first.id
        assert first.candidate_id is None
        assert first.window == {
            "split": value["config"]["split"],
            **value["config"]["reconstruction_window"],
        }
        session.commit()
    outcome = {
        "status": "failed",
        "exit_code": None,
        "error": "Synthetic stop-boundary test, no engine ran",
        "evaluation_id": str(first.id),
    }
    with Session(migrated_engine) as session:
        pending = complete_independent_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            first.id,
            outcome=outcome,
            sandbox_stopped=False,
            settings=settings,
        )
        assert pending.finished_at is None and not pending.sandbox_stopped
        run = finish_independent_attempt(
            session, attempt.id, 1, attempt.lease_token, outcome, False
        )
        assert run.status == "running"
        session.commit()
    retry_path = f"/api/v1/independent-evaluations/{run_id}/retry"
    assert (
        api.post(
            retry_path,
            headers=owner_headers | {"Idempotency-Key": "retry-while-uncontained"},
        ).status_code
        == 409
    )
    with Session(migrated_engine) as session:
        done = complete_independent_evaluation(
            session,
            attempt.id,
            1,
            attempt.lease_token,
            first.id,
            outcome=outcome,
            sandbox_stopped=True,
            settings=settings,
        )
        assert done.status == "failed" and done.sandbox_stopped
        finish_independent_attempt(
            session, attempt.id, 1, attempt.lease_token, outcome, True
        )
        session.commit()
    retry = api.post(
        retry_path,
        headers=owner_headers | {"Idempotency-Key": "retry-after-proven-stop"},
    )
    assert retry.status_code == 202, retry.text
    with Session(migrated_engine) as session:
        second_attempt = claim_attempt(
            session, run_id, 2, "second-synthetic-boundary", settings
        )
        assert mark_running(session, second_attempt.id, 2, second_attempt.lease_token)
        second = begin_independent_evaluation(
            session, second_attempt.id, 2, second_attempt.lease_token
        )
        assert second.id != first.id and second.attempt_id != first.attempt_id
        assert second.window == first.window and second.generation == 2
        run = session.get(ResearchRun, run_id)
        assert run.latest_checkpoint_id is run.resume_checkpoint_id is None
        session.commit()


def test_independent_recovery_requires_both_query_guard_and_docker_stop(
    api, owner_headers, published_pair, migrated_engine, settings
):
    from datetime import timedelta

    from backend.app.comparison_guard import comparison_attempt_guard
    from backend.app.independent_services import begin_independent_evaluation
    from backend.app.models import ExecutionAttempt, utcnow
    from backend.app.services import (
        FenceLost,
        claim_attempt,
        mark_running,
        reconcile_attempt,
    )

    value = create(api, owner_headers, published_pair)
    run_id = UUID(value["id"])
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(
            session, run_id, 1, "synthetic-recovery-boundary", settings
        )
        assert mark_running(session, attempt.id, 1, attempt.lease_token)
        evaluation = begin_independent_evaluation(
            session, attempt.id, 1, attempt.lease_token
        )
        attempt.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    stops = []

    def confirmed_stop(identifier):
        stops.append(identifier)
        return True

    with comparison_attempt_guard(settings.storage_root, run_id, attempt.id):
        with Session(migrated_engine) as session:
            assert not reconcile_attempt(
                session, attempt.id, confirmed_stop, settings=settings
            )
            session.commit()
        assert not stops, "A live host query guard must block recovery before Docker"
    with Session(migrated_engine) as session:
        assert not reconcile_attempt(
            session, attempt.id, lambda _: False, settings=settings
        )
        session.commit()
    with Session(migrated_engine) as session:
        current = session.get(ExecutionAttempt, attempt.id)
        assert not current.sandbox_stopped and current.finished_at is None
        assert reconcile_attempt(session, attempt.id, confirmed_stop, settings=settings)
        session.commit()
    assert str(evaluation.id) in stops and str(attempt.id) in stops
    with Session(migrated_engine) as session, pytest.raises(FenceLost):
        mark_running(session, attempt.id, 1, attempt.lease_token)


def test_retry_readmits_original_result_bytes_before_new_generation(
    api, owner_headers, published_pair, migrated_engine, settings
):
    value = create(api, owner_headers, published_pair)
    path = f"/api/v1/independent-evaluations/{value['id']}"
    assert api.post(path + "/cancel", headers=owner_headers).status_code == 202
    source_artifact = published_pair[0].result.artifact
    original_path = settings.storage_root / source_artifact.storage_key
    original_path.write_bytes(original_path.read_bytes() + b"\n")
    response = api.post(
        path + "/retry", headers=owner_headers | {"Idempotency-Key": uuid4().hex}
    )
    assert response.status_code == 409, response.text
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(value["id"]))
        assert run.generation == 1 and run.status == "cancelled"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == run.id)
            )
            == 1
        )


def test_exact_baseline_is_required_even_with_recorded_rng(
    api, owner, owner_headers, runtime_pair, migrated_engine
):
    body = control_body(runtime_pair, owner.strategy.code, 2)
    body["arms"][0]["code"] += "# This is not the exact baseline\n"
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert (
        response.status_code == 422 and response.json()["code"] == "baseline_required"
    )
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(ControlledExperiment)) == 0
        )
        assert (
            session.scalar(select(func.count()).select_from(IndependentEvaluation)) == 0
        )


def test_new_admission_preflights_frozen_input_bytes_before_queue(
    api, owner_headers, published_pair, migrated_engine, settings
):
    # The synthetic result carries identity/manifest fields, not the persisted
    # storage registration. Corrupt the actual frozen input admitted by the API.
    with Session(migrated_engine) as session:
        dataset = session.get(DatasetVersion, published_pair[0].result.dataset.id)
        assert dataset is not None and dataset.storage_key is not None
        name = next(
            name for name in dataset.manifest["files"] if name.startswith("data/")
        )
        path = settings.storage_root / dataset.storage_key / name
    path.write_bytes(path.read_bytes() + b"\n")
    body = diagnostic_body(published_pair)
    response = api.post(
        f"/api/v1/evaluations/{body['source']['evaluation_id']}/diagnostic-replays",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=body,
    )
    assert response.status_code in {409, 422}, response.text
    with Session(migrated_engine) as session:
        assert (
            session.scalar(select(func.count()).select_from(IndependentEvaluation)) == 0
        )
        assert (
            session.scalar(
                select(ResearchRun.id)
                .where(
                    ResearchRun.config["execution_mode"].astext
                    == "independent_evaluation"
                )
                .limit(1)
            )
            is None
        )
