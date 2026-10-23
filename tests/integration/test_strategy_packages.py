"""Actual PostgreSQL package ownership/race/immutability/rollback boundaries.

Result bytes and image identities are explicitly synthetic fixtures. These tests
never execute a candidate or establish Docker isolation/native engine evidence.
Missing TEST_DATABASE_URL is a skip, never SQLite or fake-session acceptance.
"""

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from backend.app.core.atomic_storage import publish_bytes
from backend.app.final_evaluation_schemas import FinalEvaluationCreate
from backend.app.final_evaluation_services import (
    begin_final_evaluation,
    complete_final_evaluation,
    finish_final_attempt,
    submit_final_evaluation,
)
from backend.app.models import (
    Artifact,
    DatasetVersion,
    DataUsageExposure,
    ExecutionAttempt,
    ResearchRun,
    StrategyPackage,
)
from backend.app.services import claim_attempt
from backend.app.strategy_package_schemas import StrategyPackageCreate
from backend.app.strategy_package_services import create_strategy_package, read_package
from backend.tests.final_result_fixtures import synthetic_final_result
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig
from packages.tradegrad_adapter.strategy_package import (
    canonical,
    capture_environment_evidence,
    sha256,
)
from tests.integration import test_final_evaluations as final_tests
from tests.integration import test_research_lifecycle as lifecycle

research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = final_tests.completed_source
frozen_selection = final_tests.frozen_selection
pytestmark = pytest.mark.postgres


@pytest.fixture
def package_reference(migrated_engine, settings, owner, frozen_selection):
    """Real admission/fencing/SQL + explicitly synthetic holdout bundle bytes."""
    with Session(migrated_engine, expire_on_commit=False) as session:
        run = submit_final_evaluation(
            session,
            owner.user.id,
            FinalEvaluationCreate(selection_id=frozen_selection.id),
            uuid4().hex,
            settings,
        )
        session.commit()
        attempt = claim_attempt(
            session, run.id, run.generation, "package-pg-fixture", settings
        )
        assert attempt is not None
        session.commit()
        evaluation = begin_final_evaluation(
            session, attempt.id, attempt.generation, attempt.lease_token
        )
        session.commit()
        dataset = session.get(DatasetVersion, run.dataset_version_id)
        config = FinalEvaluationConfig.model_validate(run.config)
        result = synthetic_final_result(
            config,
            owner_id=owner.user.id,
            run_id=run.id,
            attempt_id=attempt.id,
            evaluation_id=evaluation.id,
            dataset_manifest=dataset.manifest,
        )
        environment = capture_environment_evidence(
            config.environment_digest, run.research_image_digest
        )
        environment_bytes = canonical(environment)
        environment_key = f"package-fixture/{attempt.id}/execution-environment.json"
        environment_artifact = Artifact(
            run_id=run.id,
            attempt_id=attempt.id,
            name="execution-environment.json",
            media_type="application/json",
            storage_key=environment_key,
            sha256=sha256(environment_bytes),
            size_bytes=len(environment_bytes),
        )
        publish_bytes(
            settings.storage_root / environment_key,
            environment_bytes,
            sha256=sha256(environment_bytes),
        )
        publish_bytes(
            settings.storage_root / result.artifact.storage_key,
            result.content,
            sha256=result.artifact.sha256,
        )
        session.add_all([environment_artifact, result.artifact])
        session.flush()
        complete_final_evaluation(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            evaluation.id,
            outcome=result.evaluation.payload,
            sandbox_stopped=True,
        )
        finished = finish_final_attempt(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            {
                "status": "succeeded",
                "exit_code": 0,
                "evaluation_id": str(evaluation.id),
            },
            True,
        )
        assert finished.status == "succeeded"
        session.commit()
    return SimpleNamespace(
        selection=frozen_selection,
        evaluation_id=evaluation.id,
        run_id=run.id,
        body=StrategyPackageCreate(reference_evaluation_id=evaluation.id),
        result_content=result.content,
    )


def _counts(engine):
    with Session(engine) as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ResearchRun, ExecutionAttempt)
        )


def _post(api, headers, reference):
    return api.post(
        f"/api/v1/final-selections/{reference.selection.id}/packages",
        headers=headers,
        json=reference.body.model_dump(mode="json"),
    )


def test_owned_http_create_exact_replay_download_and_no_invented_attempt(
    api, owner_headers, other_headers, package_reference, migrated_engine
):
    before = _counts(migrated_engine)
    assert _post(api, other_headers, package_reference).status_code == 404
    first = _post(api, owner_headers, package_reference)
    assert first.status_code == 201, first.text
    replay = _post(api, owner_headers, package_reference)
    assert replay.status_code == 200 and replay.json() == first.json()
    identity = first.json()["id"]
    assert _counts(migrated_engine) == before
    assert (
        api.get(
            f"/api/v1/strategy-packages/{identity}", headers=other_headers
        ).status_code
        == 404
    )
    assert (
        api.get(
            f"/api/v1/strategy-packages/{identity}/download", headers=other_headers
        ).status_code
        == 404
    )
    response = api.get(
        f"/api/v1/strategy-packages/{identity}/download", headers=owner_headers
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="strategy-package-{identity}.zip"'
    )
    assert sha256(response.content) == first.json()["archive_sha256"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert (
            archive.read("reference/result-bundle.json")
            == package_reference.result_content
        )
        assert not any(name.endswith(".csv") for name in archive.namelist())
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(StrategyPackage)) == 1
        exports = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.source_key == f"strategy-package:{identity}:export"
            )
        ).all()
        assert len(exports) == 1
        assert exports[0].purpose == "strategy_package_export"
        assert exports[0].run_id == package_reference.run_id
        assert exports[0].provenance["evaluation_id"] == str(
            package_reference.evaluation_id
        )
        assert exports[0].provenance["archive_sha256"] == first.json()["archive_sha256"]


@pytest.mark.parametrize("changed", ["current_receipt", "current_attempt", "cancelled"])
def test_existing_package_replay_and_download_recheck_current_final_state(
    api, owner_headers, package_reference, migrated_engine, changed
):
    first = _post(api, owner_headers, package_reference)
    assert first.status_code == 201, first.text
    identifier = first.json()["id"]
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, package_reference.run_id)
        if changed == "current_receipt":
            run.result = {"evaluation_id": str(uuid4())}
        elif changed == "current_attempt":
            run.active_attempt_id = None
        else:
            run.status = "cancelled"
        session.commit()
    assert _post(api, owner_headers, package_reference).status_code == 409
    assert (
        api.get(
            f"/api/v1/strategy-packages/{identifier}/download", headers=owner_headers
        ).status_code
        == 409
    )
    assert (
        api.get(
            f"/api/v1/strategy-packages/{identifier}", headers=owner_headers
        ).status_code
        == 200
    )


def test_postgres_concurrent_exact_requests_publish_one_immutable_package(
    migrated_engine, settings, owner, package_reference
):
    barrier = Barrier(2)

    def create():
        with Session(migrated_engine, expire_on_commit=False) as session:
            barrier.wait(timeout=10)
            item, created = create_strategy_package(
                session,
                owner.user.id,
                package_reference.selection.id,
                package_reference.body,
                settings,
            )
            session.commit()
            return item.id, item.archive_sha256, created

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0][:2] == results[1][:2]
    assert sorted(item[2] for item in results) == [False, True]
    with Session(migrated_engine) as session:
        assert session.scalar(select(func.count()).select_from(StrategyPackage)) == 1
    assert len(list(settings.storage_root.glob("strategy-packages/*/*/*.zip"))) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE strategy_packages SET manifest = '{}'::jsonb WHERE id = :id",
        "UPDATE strategy_packages SET archive_sha256 = repeat('0', 64) WHERE id = :id",
        "DELETE FROM strategy_packages WHERE id = :id",
    ],
)
def test_postgres_rejects_package_mutation_or_delete(
    migrated_engine, settings, owner, package_reference, statement
):
    with Session(migrated_engine, expire_on_commit=False) as session:
        item, _ = create_strategy_package(
            session,
            owner.user.id,
            package_reference.selection.id,
            package_reference.body,
            settings,
        )
        session.commit()
        identity, digest = item.id, item.archive_sha256
    with Session(migrated_engine) as session:
        with pytest.raises(DBAPIError):
            session.execute(text(statement), {"id": identity})
            session.commit()
        session.rollback()
        retained = session.get(StrategyPackage, identity)
        assert retained.archive_sha256 == digest
        assert sha256(read_package(retained, settings)) == digest


def test_database_rollback_exposes_no_row_and_retry_commits_complete_archive(
    migrated_engine, settings, owner, package_reference
):
    with Session(migrated_engine, expire_on_commit=False) as session:
        item, created = create_strategy_package(
            session,
            owner.user.id,
            package_reference.selection.id,
            package_reference.body,
            settings,
        )
        assert created
        identifier = item.id
        session.rollback()
    with Session(migrated_engine) as session:
        assert session.get(StrategyPackage, identifier) is None
        item, created = create_strategy_package(
            session,
            owner.user.id,
            package_reference.selection.id,
            package_reference.body,
            settings,
        )
        assert created and item.id == identifier
        session.commit()
        assert sha256(read_package(item, settings)) == item.archive_sha256
    assert not list(settings.storage_root.rglob("*.tmp"))
    for path in settings.storage_root.glob("strategy-packages/*/*/*.zip"):
        assert path.stem == sha256(path.read_bytes())


def test_tampered_package_cannot_download_and_foreign_owner_stays_404(
    api, owner_headers, other_headers, package_reference, migrated_engine, settings
):
    response = _post(api, owner_headers, package_reference)
    assert response.status_code == 201, response.text
    identifier = UUID(response.json()["id"])
    with Session(migrated_engine) as session:
        item = session.get(StrategyPackage, identifier)
        path = settings.storage_root / item.storage_key
        path.chmod(0o600)
        path.write_bytes(b"tampered synthetic package fixture")
    endpoint = f"/api/v1/strategy-packages/{identifier}/download"
    assert api.get(endpoint, headers=other_headers).status_code == 404
    assert api.get(endpoint, headers=owner_headers).status_code == 409
    assert _post(api, owner_headers, package_reference).status_code == 409
