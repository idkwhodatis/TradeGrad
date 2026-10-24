"""Real PostgreSQL version-boundary gates with explicit synthetic saved v1 bytes.

A legacy record is inserted as saved evidence, never produced by running the new
algorithm under an old identity. All normal triggers and source admission remain
on. These tests do not replace the separate cold-backup/restore acceptance gate.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.comparison_schemas import ComparisonConfig, ComparisonCreate
from backend.app.comparison_services import input_snapshot, load_comparison_inputs
from backend.app.core.db import session_factory
from backend.app.models import (
    Artifact,
    Comparison,
    ComparisonSubmission,
    ExecutionAttempt,
    OutboxEvent,
    ResearchRun,
    utcnow,
)
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    LEGACY_ALGORITHM_VERSION,
    ComparisonPublication,
)
from packages.research_trace.contracts import canonical_bytes, digest
from tests.integration import test_comparison_api as boundary
from tests.research_trace.comparison_version_fixtures import (
    write_synthetic_legacy_result,
)
from workers.research.comparison import execute_comparison

base_research_payload = boundary.base_research_payload
research_payload = boundary.research_payload
submit_research = boundary.submit_research
research_attempt = boundary.research_attempt
published_pair = boundary.published_pair
pytestmark = pytest.mark.postgres


def insert_saved_legacy(session, owner_id, values, settings, *, completed):
    body = ComparisonCreate.model_validate(boundary.request_body(values))
    sides = load_comparison_inputs(session, owner_id, body, settings)
    snapshot = input_snapshot(
        owner_id, body, sides, algorithm_version=LEGACY_ALGORITHM_VERSION
    )
    identifier, run_id = uuid4(), uuid4()
    config = ComparisonConfig(
        comparison_id=identifier,
        input_sha256=digest(snapshot),
        engine_input_sha256=snapshot["engine_input_sha256"],
        request=body,
        algorithm_version=LEGACY_ALGORITHM_VERSION,
    )
    source = sides[0].trace.result
    run = ResearchRun(
        id=run_id,
        owner_id=owner_id,
        idempotency_key="saved-legacy-key",
        request_sha256=config.sha256,
        config=config.model_dump(mode="json"),
        dataset_version_id=source.dataset.id,
        strategy_version_id=source.run.strategy_version_id,
        status="queued",
        generation=1,
        next_event_seq=0,
        result=None,
    )
    session.add(run)
    session.flush()
    item = Comparison(
        id=identifier,
        owner_id=owner_id,
        run_id=run.id,
        left_evaluation_id=body.left.evaluation_id,
        right_evaluation_id=body.right.evaluation_id,
        input_sha256=config.input_sha256,
        engine_input_sha256=config.engine_input_sha256,
        input_snapshot=snapshot,
    )
    session.add(item)
    session.flush()
    session.add(
        ComparisonSubmission(
            owner_id=owner_id,
            idempotency_key="saved-legacy-key",
            comparison_id=item.id,
            request_sha256=body.sha256,
        )
    )
    session.add(OutboxEvent(run_id=run.id, generation=1))
    saved_bytes = {}
    if completed:
        attempt = ExecutionAttempt(
            id=uuid4(),
            run_id=run.id,
            generation=1,
            worker_id="synthetic-restored-legacy",
            lease_token=uuid4(),
            lease_expires_at=utcnow(),
            status="succeeded",
            sandbox_id="synthetic-restored-legacy",
            sandbox_stopped=True,
            exit_code=0,
            finished_at=utcnow(),
        )
        session.add(attempt)
        session.flush()
        manifest, artifacts = write_synthetic_legacy_result(
            sides[0].input,
            sides[1].input,
            settings.storage_root / "saved-legacy" / str(run.id),
            ComparisonPublication(
                comparison_id=item.id, run_id=run.id, execution_attempt_id=attempt.id
            ),
            durable_root=settings.storage_root,
        )
        manifest_row = None
        for artifact in artifacts:
            saved_bytes[artifact.path] = artifact.path.read_bytes()
            row = Artifact(
                id=uuid4(),
                run_id=run.id,
                attempt_id=attempt.id,
                name=artifact.name,
                media_type=artifact.media_type,
                storage_key=str(artifact.path.relative_to(settings.storage_root)),
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
            )
            session.add(row)
            if artifact.section is None:
                manifest_row = row
        session.flush()
        assert (
            manifest_row is not None and manifest.input_hash == item.engine_input_sha256
        )
        item.published_attempt_id = attempt.id
        item.manifest_artifact_id = manifest_row.id
        item.manifest_sha256 = manifest_row.sha256
        run.status, run.active_attempt_id = "succeeded", attempt.id
    session.flush()
    return SimpleNamespace(
        id=str(item.id),
        run_id=str(run.id),
        body=body,
        input_sha256=item.input_sha256,
        engine_input_sha256=item.engine_input_sha256,
        config_bytes=canonical_bytes(run.config),
        snapshot_bytes=canonical_bytes(snapshot),
        manifest_sha256=item.manifest_sha256,
        saved_bytes=saved_bytes,
    )


def test_legacy_idempotency_remains_old_but_new_key_and_cache_use_current_algorithm(
    api, owner_headers, owner, published_pair, migrated_engine, settings
):
    with Session(migrated_engine) as session:
        old = insert_saved_legacy(
            session, owner.user.id, published_pair, settings, completed=True
        )
        session.commit()
    historical = boundary.submit(api, owner_headers, published_pair, "saved-legacy-key")
    assert historical["id"] == old.id
    assert historical["algorithm_version"] == LEGACY_ALGORITHM_VERSION
    assert historical["input_sha256"] == old.input_sha256
    changed_body = boundary.request_body(published_pair) | {
        "options": {"max_runtime_seconds": 1}
    }
    conflict = api.post(
        "/api/v1/comparisons",
        headers=owner_headers | {"Idempotency-Key": "saved-legacy-key"},
        json=changed_body,
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "idempotency_conflict"
    )
    current = boundary.submit(api, owner_headers, published_pair, "new-algorithm-key")
    assert current["id"] != old.id
    assert current["algorithm_version"] == ALGORITHM_VERSION
    assert current["input_sha256"] != old.input_sha256
    assert current["engine_input_sha256"] != old.engine_input_sha256
    assert execute_comparison(
        current["run_id"], 1, settings, session_factory(migrated_engine)
    ) == {
        "status": "succeeded",
        "query_stopped": True,
    }
    cached = boundary.submit(
        api, owner_headers, published_pair, "new-current-cache-key"
    )
    assert cached["id"] == current["id"] and cached["status"] == "succeeded"
    repeated = boundary.submit(api, owner_headers, published_pair, "saved-legacy-key")
    assert (
        repeated["id"] == old.id and repeated["manifest_sha256"] == old.manifest_sha256
    )
    result = api.get(f"/api/v1/comparisons/{old.id}/result", headers=owner_headers)
    assert result.status_code == 200, result.text
    assert result.json()["manifest"]["algorithm_version"] == LEGACY_ALGORITHM_VERSION
    section = api.get(
        f"/api/v1/comparisons/{old.id}/sections/events",
        headers=owner_headers,
        params={"manifest_sha256": old.manifest_sha256},
    )
    assert section.status_code == 200, section.text
    assert section.json()["items"][0]["right_state"] == "observed_absence"
    listing = api.get("/api/v1/comparisons", headers=owner_headers).json()
    assert {row["algorithm_version"] for row in listing["data"]} == {
        LEGACY_ALGORITHM_VERSION,
        ALGORITHM_VERSION,
    }
    with Session(migrated_engine) as session:
        item, run = (
            session.get(Comparison, UUID(old.id)),
            session.get(ResearchRun, UUID(old.run_id)),
        )
        assert canonical_bytes(item.input_snapshot) == old.snapshot_bytes
        assert canonical_bytes(run.config) == old.config_bytes
        assert session.scalar(select(func.count()).select_from(Comparison)) == 2
        assert (
            session.scalar(select(func.count()).select_from(ComparisonSubmission)) == 3
        )
    assert {path: path.read_bytes() for path in old.saved_bytes} == old.saved_bytes


def test_queued_legacy_is_terminally_rejected_and_cannot_retry_as_current(
    api, owner_headers, owner, published_pair, migrated_engine, settings, monkeypatch
):
    import packages.research_trace.comparison as engine
    import workers.research.comparison as worker

    with Session(migrated_engine) as session:
        old = insert_saved_legacy(
            session, owner.user.id, published_pair, settings, completed=False
        )
        session.commit()
    monkeypatch.setattr(
        engine,
        "compare_evaluations",
        lambda *a, **k: pytest.fail("Legacy job executed current algorithm"),
    )
    monkeypatch.setattr(
        worker,
        "validate_comparison_inputs",
        lambda *a, **k: pytest.fail("Legacy job loaded source evidence"),
    )
    assert execute_comparison(
        old.run_id, 1, settings, session_factory(migrated_engine)
    ) == {
        "status": "failed",
        "query_stopped": True,
    }
    status = api.get(f"/api/v1/comparisons/{old.id}", headers=owner_headers)
    assert status.status_code == 200, status.text
    assert status.json()["algorithm_version"] == LEGACY_ALGORITHM_VERSION
    assert status.json()["error_code"] == "comparison_algorithm_unsupported"
    assert not status.json()["artifact_ready"]
    retry = api.post(
        f"/api/v1/comparisons/{old.id}/retry",
        headers=owner_headers | {"Idempotency-Key": "legacy-retry-new-key"},
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["code"] == "comparison_algorithm_unsupported"
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, UUID(old.run_id))
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        assert run.status == "failed" and run.generation == 1
        assert attempt.sandbox_stopped and attempt.finished_at is not None
        assert canonical_bytes(run.config) == old.config_bytes
        assert (
            session.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(Artifact.run_id == run.id)
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
