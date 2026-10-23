"""Real migrated PostgreSQL-only read-publication/exposure boundaries.

Synthetic persisted bundles, not engine execution. Collected locally, executed only
with an authorized PostgreSQL TEST_DATABASE_URL; no substitute database/session.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.experiment_schemas import ExperimentCreate, SplitVersionCreate
from backend.app.experiment_services import (
    create_experiment,
    create_split_version,
    get_exposure_snapshot,
)
from backend.app.models import (
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
    RunEvent,
    utcnow,
)
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.config import RunConfig
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration import test_strategy_packages as packages

research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = packages.completed_source
frozen_selection = packages.frozen_selection
package_reference = packages.package_reference
pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "state",
    [
        "pending_finish",
        "cancel_requested",
        "cancelled",
        "failed",
        "retry",
        "late_old",
        "wrong_commit",
        "unfinished_attempt",
        "unconfirmed_stop",
    ],
)
def test_final_typed_generic_and_package_reads_reject_uncommitted_or_stale_success(
    api,
    owner_headers,
    migrated_engine,
    package_reference,
    state,
):
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, package_reference.run_id)
        evaluation = session.get(Evaluation, package_reference.evaluation_id)
        artifact_id = evaluation.payload["result_bundle"]["artifact_id"]
        attempt = session.get(ExecutionAttempt, evaluation.attempt_id)
        if state == "pending_finish":
            run.status = attempt.status = "running"
            attempt.finished_at = None
            run.result = None
        elif state in {"cancel_requested", "cancelled", "failed"}:
            run.status = state
        elif state == "retry":
            run.status, run.result, run.active_attempt_id = "queued", None, None
            run.generation += 1
        elif state == "late_old":
            run.generation += 1
        elif state == "wrong_commit":
            run.result = {**run.result, "evaluation_id": str(uuid4())}
        elif state == "unfinished_attempt":
            attempt.finished_at = None
        else:
            attempt.sandbox_stopped = False
        session.commit()
    base = f"/api/v1/final-evaluations/evaluations/{package_reference.evaluation_id}/result"
    for path in [base, base + "/download", f"/api/v1/artifacts/{artifact_id}/download"]:
        response = api.get(path, headers=owner_headers)
        assert response.status_code == 409, (state, path, response.text)
    response = api.post(
        f"/api/v1/final-selections/{package_reference.selection.id}/packages",
        headers=owner_headers,
        json=package_reference.body.model_dump(mode="json"),
    )
    assert response.status_code == 409, response.text
    # Physical diagnostic records remain separately inspectable, not trusted success.
    response = api.get(
        f"/api/v1/final-evaluations/{package_reference.run_id}/evaluations",
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def historical_result(migrated_engine, owner, settings, research_payload):
    """Insert a pre-M5-style run without any admission row, using immutable inputs."""
    with Session(migrated_engine, expire_on_commit=False) as session:
        config = RunConfig.model_validate(research_payload)
        dataset = session.get(DatasetVersion, UUID(config.dataset.id))
        result = synthetic_result(
            config, owner_id=owner.user.id, dataset_manifest=dataset.manifest
        )
        run = result.run
        run.idempotency_key = uuid4().hex
        run.generation, run.status = 1, "succeeded"
        run.active_attempt_id = result.evaluation.attempt_id
        run.result = {
            "execution_mode": "optimization",
            "stop_reason": "max_iterations",
            "combined_score": 50.0,
            "metrics": result.evaluation.payload["metrics"]["metrics"],
            "is_synthetic": True,
        }
        session.add(run)
        session.flush()
        session.add(
            ExecutionAttempt(
                id=result.evaluation.attempt_id,
                run_id=run.id,
                generation=1,
                worker_id="synthetic-persisted-boundary",
                lease_expires_at=utcnow() + timedelta(minutes=5),
                sandbox_id="synthetic-not-executed",
                sandbox_stopped=True,
                status="succeeded",
                exit_code=0,
                finished_at=utcnow(),
            )
        )
        session.flush()
        session.add_all([result.evaluation, result.artifact])
        session.add(
            RunEvent(
                run_id=run.id,
                attempt_id=result.evaluation.attempt_id,
                seq=1,
                type="evaluation_completed",
                payload={"metrics": {"return": 0.1}},
            )
        )
        experiment = create_experiment(
            session, owner.user.id, ExperimentCreate(name="New split after old run")
        )
        definition = {
            "dataset": config.dataset.model_dump(),
            "universe": config.universe.model_dump(),
            "windows": {
                "train": {"start": "2024-01-01", "end": "2024-01-10"},
                "validation": {"start": "2024-01-15", "end": "2024-01-20"},
                "holdout": {"start": "2024-02-01", "end": "2024-02-10"},
            },
        }
        split = create_split_version(
            session,
            owner.user.id,
            SplitVersionCreate(experiment_id=experiment.id, definition=definition),
        )
        assert (
            get_exposure_snapshot(session, owner.user.id, split)["classification"]
            == "no_tracked_overlap"
        )
        session.commit()
    path = settings.storage_root / result.artifact.storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.content)
    return SimpleNamespace(result=result, split=split)


@pytest.mark.parametrize(
    "surface",
    [
        "result",
        "download",
        "report",
        "artifact",
        "evaluation",
        "research-run",
        "research-runs",
        "legacy-run",
        "legacy-runs",
        "events",
    ],
)
def test_historical_result_access_is_committed_idempotent_and_visible_to_new_split(
    api,
    owner_headers,
    other_headers,
    migrated_engine,
    owner,
    historical_result,
    surface,
):
    result, split = historical_result.result, historical_result.split
    path = {
        "result": f"/research-evaluations/{result.evaluation.id}/results",
        "download": f"/research-evaluations/{result.evaluation.id}/results/download",
        "report": f"/research-evaluations/{result.evaluation.id}/report",
        "artifact": f"/artifacts/{result.artifact.id}/download",
        "evaluation": f"/evaluations/{result.evaluation.id}",
        "research-run": f"/research-runs/{result.run.id}",
        "research-runs": "/research-runs",
        "legacy-run": f"/runs/{result.run.id}",
        "legacy-runs": "/runs",
        "events": f"/runs/{result.run.id}/events?follow=false",
    }[surface]
    response = api.get("/api/v1" + path, headers=other_headers)
    assert response.status_code == (
        200 if surface in {"research-runs", "legacy-runs"} else 404
    )
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(DataUsageExposure)) == 0
    for _ in range(2):
        response = api.get("/api/v1" + path, headers=owner_headers)
        assert response.status_code == 200, response.text
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(DataUsageExposure).where(DataUsageExposure.owner_id == owner.user.id)
        ).all()
        assert len(rows) == (
            2
            if surface
            in {"research-run", "research-runs", "legacy-run", "legacy-runs", "events"}
            else 1
        )
        assert all(row.dataset_version_id == result.dataset.id for row in rows)
        assert (
            get_exposure_snapshot(session, owner.user.id, split)["classification"]
            == "previously_exposed"
        )
