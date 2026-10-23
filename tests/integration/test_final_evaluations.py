"""M5 actual migrated PostgreSQL transaction/race evidence, collect-only locally.

Synthetic candidate/evaluation records exercise the DB/application boundary, not
native-engine validity. This suite never substitutes SQLite or mocked sessions.
"""

import copy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.experiment_schemas import FinalSelectionCreate
from backend.app.experiment_services import (
    freeze_final_selection,
    get_exposure_snapshot,
)
from backend.app.final_evaluation_schemas import FinalEvaluationCreate
from backend.app.final_evaluation_services import (
    begin_final_evaluation,
    final_config,
    preflight_final_evaluation,
    retry_final_evaluation,
    submit_final_evaluation,
)
from backend.app.models import (
    Candidate,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    Experiment,
    FinalSelection,
    OutboxEvent,
    ResearchRun,
    RunEvent,
    SplitVersion,
    StrategyVersion,
)
from backend.app.research_services import finish_research_attempt
from backend.app.services import DomainError, claim_attempt, request_cancel
from tests.integration import test_research_lifecycle as lifecycle

base_research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
pytestmark = pytest.mark.postgres


@pytest.fixture
def research_payload(base_research_payload, request):
    payload = copy.deepcopy(base_research_payload)
    if hasattr(request, "param"):
        payload["optimizer"].update(request.param)
    return payload


@pytest.fixture
def completed_source(migrated_engine, research_attempt, complete_candidate):
    publish, state = complete_candidate
    checkpoint, _ = publish(state)
    with Session(migrated_engine, expire_on_commit=False) as session:
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
        assert run.split_version_id is not None
        session.commit()
    return SimpleNamespace(
        run=run,
        checkpoint=checkpoint,
        candidate_id=UUID(checkpoint.summary["candidates"]["candidate-0000"]["id"]),
        body=FinalSelectionCreate(
            split_version_id=run.split_version_id,
            source_run_id=run.id,
            source_candidate_id=UUID(
                checkpoint.summary["candidates"]["candidate-0000"]["id"]
            ),
            source_checkpoint_id=checkpoint.id,
        ),
    )


@pytest.fixture
def frozen_selection(migrated_engine, owner, settings, completed_source):
    with Session(migrated_engine, expire_on_commit=False) as session:
        selection = freeze_final_selection(
            session, owner.user.id, completed_source.body, settings
        )
        session.commit()
    return selection


def submit_final(engine, owner_id, settings, selection_id, key=None):
    with Session(engine, expire_on_commit=False) as session:
        run = submit_final_evaluation(
            session,
            owner_id,
            FinalEvaluationCreate(selection_id=selection_id),
            key or uuid4().hex,
            settings,
        )
        session.commit()
        return run


def count(engine, model):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(model))


def test_m5_migration_creates_immutable_schema(migrated_engine):
    tables = set(inspect(migrated_engine).get_table_names())
    assert {
        "experiments",
        "split_versions",
        "final_selections",
        "data_usage_exposures",
        "strategy_packages",
    } <= tables
    columns = {
        item["name"] for item in inspect(migrated_engine).get_columns("research_runs")
    }
    assert {
        "experiment_id",
        "split_version_id",
        "final_selection_id",
        "portable_policy_image_digest",
    } <= columns
    assert any(
        "holdout" in item["sqltext"] and "backtest" in item["sqltext"]
        for item in inspect(migrated_engine).get_check_constraints(
            "research_evaluations"
        )
    )


def test_new_native_admission_binds_split_without_changing_config(
    submit_research, research_payload, migrated_engine
):
    response = submit_research("bound-original")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["config"] == research_payload
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(body["id"]))
        assert (
            run.experiment_id
            and run.split_version_id
            and run.final_selection_id is None
        )
        assert (
            session.get(SplitVersion, run.split_version_id).definition["windows"]
            == research_payload["windows"]
        )
        exposures = session.scalars(
            select(DataUsageExposure).where(DataUsageExposure.run_id == run.id)
        ).all()
        assert {row.purpose for row in exposures} == {
            "optimization_train_admission",
            "optimization_validation_admission",
        }
        assert all(
            row.end_date.isoformat() <= research_payload["windows"]["holdout"]["start"]
            for row in exposures
        )


def test_freeze_is_exact_idempotent_and_promotes_exact_code(
    migrated_engine, settings, owner, completed_source, frozen_selection
):
    before = count(migrated_engine, StrategyVersion)
    with Session(migrated_engine) as session:
        same = freeze_final_selection(
            session, owner.user.id, completed_source.body, settings
        )
        assert same.id == frozen_selection.id
        strategy = session.get(StrategyVersion, same.strategy_version_id)
        candidate = session.get(Candidate, completed_source.candidate_id)
        assert (
            strategy.code == candidate.code
            and strategy.content_sha256 == candidate.code_sha256
        )
        assert same.source_config_sha256 == completed_source.run.request_sha256
        assert same.exposure_snapshot["classification"] == "no_tracked_overlap"
        assert not same.exposure_snapshot["independent_unseen_holdout_established"]
        session.commit()
    assert count(migrated_engine, StrategyVersion) == before
    changed = completed_source.body.model_copy(update={"source_candidate_id": uuid4()})
    with (
        Session(migrated_engine) as session,
        pytest.raises(DomainError, match="different final selection"),
    ):
        freeze_final_selection(session, owner.user.id, changed, settings)


def test_freeze_refuses_foreign_owner_or_unbound_source(
    migrated_engine, settings, other, owner, completed_source
):
    with Session(migrated_engine) as session, pytest.raises(DomainError) as error:
        freeze_final_selection(session, other.user.id, completed_source.body, settings)
    assert error.value.status_code == 404
    # A new family/split does not permit reusing source run evidence from another split.
    with Session(migrated_engine) as session:
        split = session.get(SplitVersion, completed_source.run.split_version_id)
        new_split = SplitVersion(
            id=uuid4(),
            owner_id=owner.user.id,
            experiment_id=split.experiment_id,
            dataset_version_id=split.dataset_version_id,
            universe_version_id=split.universe_version_id,
            definition=copy.deepcopy(split.definition),
            content_sha256=split.content_sha256,
        )
        session.add(new_split)
        session.commit()
        with pytest.raises(DomainError, match="exact split"):
            freeze_final_selection(
                session,
                owner.user.id,
                completed_source.body.model_copy(
                    update={"split_version_id": new_split.id}
                ),
                settings,
            )


def test_concurrent_freeze_has_one_exact_selection(
    migrated_engine, settings, owner, completed_source
):
    def freeze():
        with Session(migrated_engine, expire_on_commit=False) as session:
            row = freeze_final_selection(
                session, owner.user.id, completed_source.body, settings
            )
            session.commit()
            return row.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: freeze(), range(2)))
    assert values[0] == values[1]
    assert count(migrated_engine, FinalSelection) == 1


def test_concurrent_conflicting_freeze_cannot_replace_winner(
    migrated_engine, settings, owner, completed_source, frozen_selection
):
    bodies = [
        completed_source.body,
        completed_source.body.model_copy(update={"source_checkpoint_id": uuid4()}),
    ]

    def freeze(body):
        with Session(migrated_engine) as session:
            try:
                row = freeze_final_selection(session, owner.user.id, body, settings)
                session.commit()
                return str(row.id)
            except DomainError as error:
                session.rollback()
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(freeze, bodies))
    assert outcomes == [str(frozen_selection.id), "selection_frozen"]


def test_preflight_has_no_writes_or_quota_reservation(
    migrated_engine, owner, settings, frozen_selection
):
    models = [ResearchRun, ExecutionAttempt, RunEvent, OutboxEvent, DataUsageExposure]
    before = [count(migrated_engine, model) for model in models]
    with Session(migrated_engine) as session:
        preview = preflight_final_evaluation(
            session,
            owner.user.id,
            FinalEvaluationCreate(selection_id=frozen_selection.id),
            settings,
        )
        assert (
            preview.eligible
            and preview.exposure_snapshot.classification == "no_tracked_overlap"
        )
        session.commit()
    assert [count(migrated_engine, model) for model in models] == before


def test_final_admission_is_atomic_idempotent_and_separate_schema(
    migrated_engine, owner, settings, frozen_selection
):
    first = submit_final(
        migrated_engine, owner.user.id, settings, frozen_selection.id, "final-once"
    )
    repeated = submit_final(
        migrated_engine, owner.user.id, settings, frozen_selection.id, "final-once"
    )
    assert first.id == repeated.id
    assert (
        final_config(first).source_config_sha256
        == frozen_selection.source_config_sha256
    )
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == first.id)
            )
            == 1
        )
        admissions = session.scalars(
            select(DataUsageExposure).where(DataUsageExposure.run_id == first.id)
        ).all()
        assert (
            len(admissions) == 1
            and admissions[0].provenance["admission_before_dispatch"]
        )
        assert (
            admissions[0].provenance["prior_exposure_snapshot"]["classification"]
            == "no_tracked_overlap"
        )
        with pytest.raises(DomainError) as error:
            submit_final_evaluation(
                session,
                owner.user.id,
                FinalEvaluationCreate(
                    selection_id=frozen_selection.id, max_runtime_seconds=301
                ),
                "final-once",
                settings,
            )
        assert error.value.code == "idempotency_conflict"


def test_final_transaction_rollback_leaves_no_admission_job_or_outbox(
    migrated_engine, owner, settings, frozen_selection
):
    models = [ResearchRun, DataUsageExposure, RunEvent, OutboxEvent]
    before = [count(migrated_engine, model) for model in models]
    with Session(migrated_engine) as session:
        submit_final_evaluation(
            session,
            owner.user.id,
            FinalEvaluationCreate(selection_id=frozen_selection.id),
            "rollback-final",
            settings,
        )
        session.rollback()
    assert [count(migrated_engine, model) for model in models] == before


def test_two_first_admissions_are_serialized_and_second_sees_prior_exposure(
    migrated_engine, owner, settings, frozen_selection
):
    def submit(_):
        return submit_final(
            migrated_engine, owner.user.id, settings, frozen_selection.id
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(DataUsageExposure)
            .where(DataUsageExposure.run_id.in_(ids))
            .order_by(DataUsageExposure.created_at)
        ).all()
        assert [
            row.provenance["prior_exposure_snapshot"]["classification"] for row in rows
        ] == ["no_tracked_overlap", "previously_exposed"]


def test_freeze_and_first_admission_share_owner_transaction_lock(
    migrated_engine, owner, settings, completed_source
):
    def freeze_and_admit(_):
        with Session(migrated_engine, expire_on_commit=False) as session:
            selection = freeze_final_selection(
                session, owner.user.id, completed_source.body, settings
            )
            run = submit_final_evaluation(
                session,
                owner.user.id,
                FinalEvaluationCreate(selection_id=selection.id),
                uuid4().hex,
                settings,
            )
            session.commit()
            return selection.id, run.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(freeze_and_admit, range(2)))
    assert rows[0][0] == rows[1][0] and rows[0][1] != rows[1][1]
    assert count(migrated_engine, FinalSelection) == 1


def test_final_quota_race_is_atomic(migrated_engine, owner, settings, frozen_selection):
    settings.max_active_runs_per_user = 1

    def submit(_):
        try:
            return str(
                submit_final(
                    migrated_engine, owner.user.id, settings, frozen_selection.id
                ).id
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(submit, range(2)))
    assert values.count("run_quota") == 1
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataUsageExposure)
                .where(DataUsageExposure.purpose == "final_evaluation_admission")
            )
            == 1
        )


def test_owner_cannot_admit_foreign_final_selection(
    migrated_engine, other, settings, frozen_selection
):
    with pytest.raises(DomainError) as error:
        submit_final(migrated_engine, other.user.id, settings, frozen_selection.id)
    assert error.value.status_code == 404


def test_cancel_retains_exposure_retry_new_generation_and_exact_replay(
    migrated_engine, owner, settings, frozen_selection
):
    run = submit_final(migrated_engine, owner.user.id, settings, frozen_selection.id)
    # Carry immutable scalar identity across transactions, not an expired ORM row.
    run_id = run.id
    with Session(migrated_engine) as session:
        request_cancel(session, run_id, owner.user.id)
        session.commit()
    with Session(migrated_engine) as session:
        run = retry_final_evaluation(
            session, run_id, owner.user.id, settings, idempotency_key="retry-once"
        )
        assert run.generation == 2
        session.commit()
        request_cancel(session, run_id, owner.user.id)
        session.commit()
    with Session(migrated_engine) as session:
        replay = retry_final_evaluation(
            session, run_id, owner.user.id, settings, idempotency_key="retry-once"
        )
        assert replay.generation == 2 and replay.status == "cancelled"
        session.commit()
        rows = session.scalars(
            select(DataUsageExposure).where(DataUsageExposure.run_id == run_id)
        ).all()
        assert len(rows) == 2
        assert sorted(
            row.provenance["prior_exposure_snapshot"]["classification"] for row in rows
        ) == ["no_tracked_overlap", "previously_exposed"]


def test_retry_cannot_overrun_unconfirmed_child_sandbox(
    migrated_engine, owner, settings, frozen_selection
):
    run = submit_final(migrated_engine, owner.user.id, settings, frozen_selection.id)
    with Session(migrated_engine, expire_on_commit=False) as session:
        attempt = claim_attempt(session, run.id, 1, "final-transaction-test", settings)
        evaluation = begin_final_evaluation(session, attempt.id, 1, attempt.lease_token)
        session.commit()
        persisted = session.get(ResearchRun, run.id)
        persisted.status = "interrupted"
        session.commit()
    with (
        Session(migrated_engine) as session,
        pytest.raises(DomainError, match="confirmed termination"),
    ):
        retry_final_evaluation(
            session, run.id, owner.user.id, settings, idempotency_key="unsafe-retry"
        )
    with Session(migrated_engine) as session:
        assert session.get(Evaluation, evaluation.id).sandbox_stopped is False
        assert session.get(ResearchRun, run.id).generation == 1


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("final_selections", "source_code_sha256", "'changed'"),
        ("split_versions", "content_sha256", "'changed'"),
        ("data_usage_exposures", "purpose", "'erased'"),
        ("strategy_versions", "code", "'replacement'"),
    ],
)
def test_database_rejects_mutation_of_published_m5_evidence(
    migrated_engine, frozen_selection, table, column, value
):
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(text(f"UPDATE {table} SET {column} = {value}"))


@pytest.mark.parametrize(
    "table",
    ["final_selections", "data_usage_exposures", "split_versions", "experiments"],
)
def test_database_rejects_deleting_exposure_or_frozen_family(
    migrated_engine, frozen_selection, table
):
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(text(f"DELETE FROM {table}"))


def test_database_rejects_rebinding_an_existing_run(migrated_engine, completed_source):
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "UPDATE research_runs SET split_version_id = NULL, experiment_id = NULL WHERE id = :id"
            ),
            {"id": completed_source.run.id},
        )


def test_refresh_same_owner_and_new_family_still_sees_prior_admission(
    migrated_engine, owner, settings, frozen_selection
):
    submit_final(migrated_engine, owner.user.id, settings, frozen_selection.id)
    with Session(migrated_engine) as session:
        original = session.get(SplitVersion, frozen_selection.split_version_id)
        dataset = session.get(DatasetVersion, original.dataset_version_id)
        refresh = DatasetVersion(
            id=uuid4(),
            owner_id=owner.user.id,
            name="Refresh",
            market=dataset.market,
            start_date=dataset.start_date,
            end_date=dataset.end_date,
            source=dataset.source,
            is_synthetic=dataset.is_synthetic,
            content_sha256=dataset.content_sha256,
            manifest=copy.deepcopy(dataset.manifest),
            storage_key=dataset.storage_key,
        )
        family = Experiment(
            id=uuid4(), owner_id=owner.user.id, name="Reset is not clean"
        )
        session.add_all([refresh, family])
        session.flush()
        definition = copy.deepcopy(original.definition)
        definition["dataset"]["id"] = str(refresh.id)
        from backend.app.experiment_schemas import SplitDefinition

        new = SplitVersion(
            id=uuid4(),
            owner_id=owner.user.id,
            experiment_id=family.id,
            dataset_version_id=refresh.id,
            universe_version_id=original.universe_version_id,
            definition=definition,
            content_sha256=SplitDefinition.model_validate(definition).sha256,
        )
        session.add(new)
        session.flush()
        snapshot = get_exposure_snapshot(session, owner.user.id, new)
        assert snapshot["classification"] == "previously_exposed"
        session.commit()


def test_final_endpoints_owner_scope_and_pause_unsupported(
    api, owner_headers, other_headers, frozen_selection
):
    preview = api.post(
        "/api/v1/final-evaluations/preflight",
        headers=owner_headers,
        json={"selection_id": str(frozen_selection.id)},
    )
    assert preview.status_code == 200, preview.text
    response = api.post(
        "/api/v1/final-evaluations",
        headers={**owner_headers, "Idempotency-Key": "http-final"},
        json={"selection_id": str(frozen_selection.id)},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert (
        api.get(
            f"/api/v1/final-evaluations/{run_id}", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        api.post(
            f"/api/v1/final-evaluations/{run_id}/pause", headers=owner_headers
        ).status_code
        == 409
    )
    assert (
        api.post(
            f"/api/v1/final-evaluations/{run_id}/cancel", headers=owner_headers
        ).status_code
        == 202
    )
    assert (
        api.post(
            f"/api/v1/final-evaluations/{run_id}/retry", headers=owner_headers
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/api/v1/final-evaluations/{run_id}/retry",
            headers={**owner_headers, "Idempotency-Key": "http-retry"},
        ).status_code
        == 202
    )


@pytest.mark.parametrize("research_payload", [{"max_iterations": 1}], indirect=True)
def test_user_can_select_valid_non_automatic_best_candidate(
    migrated_engine,
    settings,
    owner,
    research_attempt,
    complete_candidate,
):
    import hashlib

    from backend.app.models import Artifact
    from backend.app.research_services import begin_evaluation, complete_evaluation

    publish, state = complete_candidate
    chosen = copy.deepcopy(state["candidates"][0])
    chosen.update(
        candidate_id="candidate-0001",
        parent_id="candidate-0000",
        kind="generated",
        iteration=1,
        code=chosen["code"] + "# User-selected valid alternative.\n",
    )
    chosen["code_sha256"] = hashlib.sha256(chosen["code"].encode()).hexdigest()
    outcomes = []
    for old in state["candidates"][0]["window_results"]:
        outcome = {
            key: copy.deepcopy(value)
            for key, value in old.items()
            if key not in {"evaluation_id", "window", "split", "valid", "result_bundle"}
        }
        outcome["combined_score"] = 9.0
        outcome["metrics"]["combined_score"] = 9.0
        with Session(migrated_engine, expire_on_commit=False) as session:
            item = begin_evaluation(
                session,
                research_attempt.id,
                1,
                research_attempt.lease_token,
                candidate_key=chosen["candidate_id"],
                code_sha256=chosen["code_sha256"],
                window=old["window"],
            )
            artifact = Artifact(
                id=uuid4(),
                run_id=research_attempt.run_id,
                attempt_id=research_attempt.id,
                name=f"evaluation-{item.id}-result-bundle.json",
                media_type="application/json",
                storage_key=f"synthetic-db-boundary/{item.id}.json",
                sha256=hashlib.sha256(b"{}").hexdigest(),
                size_bytes=2,
            )
            session.add(artifact)
            outcome["result_bundle"] = {
                "artifact_id": str(artifact.id),
                "sha256": artifact.sha256,
                "size_bytes": 2,
            }
            completed = complete_evaluation(
                session,
                research_attempt.id,
                1,
                research_attempt.lease_token,
                item.id,
                outcome=outcome,
                sandbox_stopped=True,
            )
            assert completed.status == "succeeded"
            session.commit()
        outcomes.append(
            {
                **outcome,
                "evaluation_id": str(item.id),
                "window": old["window"],
                "split": old["split"],
                "valid": True,
            }
        )
    chosen["window_results"] = outcomes
    chosen["evaluation"] = {
        "combined_score": 9.0,
        "windows": outcomes,
        "validation_valid": True,
        "validation_score": 9.0,
    }
    state["candidates"].append(chosen)
    state["current_id"] = chosen["candidate_id"]
    state["iteration"] = 1
    state["optimizer"]["archive"].append(chosen["candidate_id"])
    checkpoint, _ = publish(state)
    with Session(migrated_engine, expire_on_commit=False) as session:
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
        session.commit()
        selected_id = UUID(checkpoint.summary["candidates"]["candidate-0001"]["id"])
        selected = freeze_final_selection(
            session,
            owner.user.id,
            FinalSelectionCreate(
                split_version_id=run.split_version_id,
                source_run_id=run.id,
                source_candidate_id=selected_id,
                source_checkpoint_id=checkpoint.id,
            ),
            settings,
        )
        assert str(selected_id) != run.result["best_candidate_id"]
        assert selected.source_candidate_id == selected_id
        assert selected.selection_evidence["combined_score"] == 9.0
        assert (
            session.get(StrategyVersion, selected.strategy_version_id).code
            == chosen["code"]
        )
        session.commit()


def test_database_rejects_cross_owner_split_reference(
    migrated_engine, owner, other, completed_source
):
    with Session(migrated_engine) as session:
        source = session.get(SplitVersion, completed_source.run.split_version_id)
        foreign = Experiment(
            id=uuid4(), owner_id=other.user.id, name="Other owner family"
        )
        session.add(foreign)
        session.commit()
        bad = SplitVersion(
            id=uuid4(),
            owner_id=other.user.id,
            experiment_id=foreign.id,
            dataset_version_id=source.dataset_version_id,
            universe_version_id=source.universe_version_id,
            definition=source.definition,
            content_sha256=source.content_sha256,
        )
        session.add(bad)
        with pytest.raises(DBAPIError):
            session.flush()


def test_database_rejects_submission_binding_rewrite(migrated_engine, completed_source):
    with migrated_engine.begin() as connection, pytest.raises(DBAPIError):
        connection.execute(
            text(
                "UPDATE research_runs SET submission_binding = '{}'::jsonb WHERE id = :id"
            ),
            {"id": completed_source.run.id},
        )


def test_final_outbox_and_real_queue_dispatch_rollback_together(
    migrated_engine, owner, settings, frozen_selection
):
    from workers.research.dispatcher import dispatch_outbox

    run = submit_final(migrated_engine, owner.user.id, settings, frozen_selection.id)
    with Session(migrated_engine) as session:
        assert dispatch_outbox(session) >= 1
        session.rollback()
    with Session(migrated_engine) as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run.id))
        assert event.job_id is None and event.dispatched_at is None
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM procrastinate_jobs WHERE args->>'run_id' = :run_id"
                ),
                {"run_id": str(run.id)},
            )
            == 0
        )
        assert dispatch_outbox(session) >= 1
        session.commit()
    with Session(migrated_engine) as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run.id))
        job = (
            session.execute(
                text(
                    "SELECT id, args FROM procrastinate_jobs WHERE args->>'run_id' = :run_id"
                ),
                {"run_id": str(run.id)},
            )
            .mappings()
            .one()
        )
        assert event.job_id == job["id"] and job["args"]["generation"] == 1
        assert (
            session.scalar(
                select(DataUsageExposure.id).where(
                    DataUsageExposure.source_key == f"final:{run.id}:1"
                )
            )
            is not None
        )
        assert dispatch_outbox(session) == 0
