"""Real PG + publisher/download boundary; synthetic outcome, not engine execution."""

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Artifact
from backend.app.services import finish_attempt, mark_running
from workers.research.executor import publish_artifacts

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize("mutation", [None, "new_generation", "wrong_image"])
def test_legacy_seed_provenance_matches_actual_publisher_and_committed_attempt(
    api,
    owner_headers,
    other_headers,
    migrated_engine,
    settings,
    claimed_attempt,
    mutation,
):
    attempt = claimed_attempt
    outcome = {
        "status": "succeeded",
        "exit_code": 0,
        "metrics": {"metrics": {"total_return": 0.1}, "combined_score": 0.1},
        "image_digest": "sha256:" + "a" * 64,
        "logs": {},
    }
    target = (
        settings.storage_root
        / "runs"
        / str(attempt.run_id)
        / str(attempt.id)
        / "artifacts"
    )
    target.parent.mkdir(parents=True)
    artifacts = publish_artifacts(
        settings.storage_root, target, outcome, settings.max_artifact_bytes
    )
    with Session(migrated_engine) as session:
        assert mark_running(
            session, attempt.id, attempt.generation, attempt.lease_token
        )
        run = finish_attempt(
            session,
            attempt.id,
            attempt.generation,
            attempt.lease_token,
            outcome,
            sandbox_stopped=True,
            artifacts=artifacts,
        )
        assert run.research_image_digest is None
        assert run.result["image_digest"] == outcome["image_digest"]
        session.commit()
        artifact = session.scalar(
            select(Artifact).where(
                Artifact.attempt_id == attempt.id, Artifact.name == "provenance.json"
            )
        )
        artifact_id, digest = artifact.id, artifact.sha256
        if mutation == "new_generation":
            run.generation += 1
        elif mutation == "wrong_image":
            run.result = {**run.result, "image_digest": "sha256:" + "b" * 64}
        session.commit()
    path = f"/api/v1/artifacts/{artifact_id}/download"
    assert api.get(path, headers=other_headers).status_code == 404
    response = api.get(path, headers=owner_headers)
    if mutation is None:
        assert response.status_code == 200
        assert hashlib.sha256(response.content).hexdigest() == digest
    else:
        assert response.status_code == 409
        assert response.json()["code"] == "artifact_metadata_invalid"
