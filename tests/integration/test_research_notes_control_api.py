"""Real PG stopped-arm note history; synthetic lifecycle, not engine execution."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from backend.app.independent_services import (
    begin_independent_evaluation,
    complete_independent_evaluation,
    finish_independent_attempt,
)
from backend.app.models import ResearchRun
from backend.app.services import claim_attempt, mark_running
from tests.integration import test_independent_api as independent
from tests.integration.test_research_notes_api import exact_url, submit

base_research_payload = independent.base_research_payload
research_payload = independent.research_payload
submit_research = independent.submit_research
research_attempt = independent.research_attempt
published_pair = independent.published_pair
runtime_pair = independent.runtime_pair
pytestmark = pytest.mark.postgres


def test_failed_control_note_keeps_original_stopped_attempt_after_retry(
    api, owner, owner_headers, other_headers, runtime_pair, migrated_engine, settings
):
    response = api.post(
        "/api/v1/controlled-experiments",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
        json=independent.control_body(runtime_pair, owner.strategy.code, 2),
    )
    assert response.status_code == 202, response.text
    control = response.json()
    arms = []
    for index, arm in enumerate(control["arms"]):
        run_id = UUID(arm["run_id"])
        with Session(migrated_engine) as session:
            attempt = claim_attempt(
                session, run_id, 1, "note-synthetic-stop-no-engine", settings
            )
            assert mark_running(session, attempt.id, 1, attempt.lease_token)
            evaluation = begin_independent_evaluation(
                session, attempt.id, 1, attempt.lease_token
            )
            outcome = {
                "status": "failed",
                "exit_code": None,
                "error": "Synthetic stopped boundary; no engine ran",
                "evaluation_id": str(evaluation.id),
            }
            complete_independent_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                evaluation.id,
                outcome=outcome,
                sandbox_stopped=True,
                settings=settings,
            )
            finish_independent_attempt(
                session, attempt.id, 1, attempt.lease_token, outcome, True
            )
            arms.append(
                {
                    "ordinal": index,
                    "run_id": str(run_id),
                    "status": "failed",
                    "generation": 1,
                    "execution_attempt_id": str(attempt.id),
                    "result": None,
                }
            )
            session.commit()
    content = {
        "title": "Failure is not support",
        "status": "mixed",
        "evidence": [
            {
                "kind": "controlled",
                "controlled_experiment_id": control["id"],
                "request_sha256": control["request_sha256"],
                "protocol_sha256": control["protocol_sha256"],
                "arms": arms,
                "selected_arm": 0,
            }
        ],
    }
    assert submit(api, other_headers, content).status_code == 404
    response = submit(api, owner_headers, content)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved["evidence"][0]["state"] == "available"
    assert saved["evidence"][0]["href"] is None
    response = api.post(
        f"/api/v1/independent-evaluations/{arms[0]['run_id']}/retry",
        headers=owner_headers | {"Idempotency-Key": uuid4().hex},
    )
    assert response.status_code == 202, response.text
    with Session(migrated_engine) as session:
        actual = session.get(ResearchRun, UUID(arms[0]["run_id"]))
        before = (actual.generation, actual.status, actual.active_attempt_id)
        assert before[0] == 2
    response = api.get(exact_url(saved), headers=owner_headers)
    assert response.status_code == 200, response.text
    reopened = response.json()
    assert reopened["content"] == saved["content"]
    assert reopened["evidence"][0]["state"] == "available"
    assert reopened["evidence"][0]["href"] is None
    with Session(migrated_engine) as session:
        actual = session.get(ResearchRun, UUID(arms[0]["run_id"]))
        assert (actual.generation, actual.status, actual.active_attempt_id) == before
