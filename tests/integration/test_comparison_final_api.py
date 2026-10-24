"""Real PostgreSQL bilateral native-final release fences; source bytes synthetic.

This exercises genuine freeze/admission/completion loaders, not a substituted
final loader. It does not claim real engine execution or local PostgreSQL proof.
"""

import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import (
    Artifact,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
)
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration import test_strategy_packages as packages
from workers.research.comparison import execute_comparison

research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = packages.completed_source
frozen_selection = packages.frozen_selection
pytestmark = pytest.mark.postgres


@pytest.fixture
def native_final_pair(migrated_engine, settings, owner, frozen_selection):
    refs = [
        packages.package_reference.__wrapped__(
            migrated_engine, settings, owner, frozen_selection
        )
        for _ in range(2)
    ]
    sides = []
    with Session(migrated_engine) as session:
        for reference in refs:
            evaluation = session.get(Evaluation, reference.evaluation_id)
            run = session.get(ResearchRun, reference.run_id)
            artifact = session.get(
                Artifact, UUID(evaluation.payload["result_bundle"]["artifact_id"])
            )
            sides.append(
                {
                    "evaluation_id": str(evaluation.id),
                    "execution_attempt_id": str(evaluation.attempt_id),
                    "result_manifest_sha256": artifact.sha256,
                    "trace_manifest_sha256": None,
                    "source_kind": "strategy_version",
                    "source_id": str(run.strategy_version_id),
                    "code_sha256": evaluation.code_sha256,
                }
            )
    return SimpleNamespace(
        refs=refs, body=dict(zip(("left", "right"), sides, strict=True))
    )


def create(api, headers, body, key):
    return api.post(
        "/api/v1/comparisons", headers=headers | {"Idempotency-Key": key}, json=body
    )


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    "fault",
    ["pending", "retry", "unfinished_attempt", "wrong_publication", "selected_source"],
)
def test_both_final_sources_are_readmitted_before_cache_summary_page_and_download(
    api, owner_headers, migrated_engine, settings, native_final_pair, side, fault
):
    pair = native_final_pair
    response = create(api, owner_headers, pair.body, "final-comparison")
    assert response.status_code == 202, response.text
    comparison = response.json()
    assert (
        execute_comparison(
            comparison["run_id"],
            1,
            settings,
            sessionmaker(migrated_engine, expire_on_commit=False),
        )["status"]
        == "succeeded"
    )
    status = api.get(
        f"/api/v1/comparisons/{comparison['id']}", headers=owner_headers
    ).json()
    result = api.get(
        f"/api/v1/comparisons/{comparison['id']}/result", headers=owner_headers
    )
    assert result.status_code == 200, result.text
    assert result.json()["manifest"][side]["trace_state"] == "historical_absence"
    with Session(migrated_engine) as session:
        exposed = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.purpose == "result_preview"
            )
        ).all()
        assert {str(ref.evaluation_id) for ref in pair.refs} <= {
            row.provenance.get("evaluation_id") for row in exposed
        }
    if fault == "selected_source":
        wrong = copy.deepcopy(pair.body)
        wrong[side]["source_id"] = str(uuid4())
        response = create(api, owner_headers, wrong, "wrong-final-source")
        assert response.status_code in {404, 409}
        return
    reference = pair.refs[0 if side == "left" else 1]
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, reference.run_id)
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if fault == "pending":
            run.status, run.result = "running", None
        elif fault == "retry":
            run.generation += 1
        elif fault == "unfinished_attempt":
            attempt.finished_at = None
        else:
            run.result = {"evaluation_id": str(uuid4())}
        session.commit()
    base = f"/api/v1/comparisons/{comparison['id']}"
    # Status remains pollable and does not itself release any outcome/source bytes.
    status_response = api.get(base, headers=owner_headers)
    assert (
        status_response.status_code == 200 and "manifest" not in status_response.json()
    )
    for path, params in (
        (base + "/result", {}),
        (base + "/sections/events", {"manifest_sha256": status["manifest_sha256"]}),
        (f"/api/v1/artifacts/{status['manifest_artifact_id']}/download", {}),
    ):
        response = api.get(path, headers=owner_headers, params=params)
        assert response.status_code == 409, (side, fault, path, response.text)
    for key in ("final-comparison", "new-key-final-cache"):
        response = create(api, owner_headers, pair.body, key)
        assert response.status_code == 409, response.text
