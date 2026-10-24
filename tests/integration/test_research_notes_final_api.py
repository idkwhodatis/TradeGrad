"""Real native/portable final-admission and inherited-note outcome release gates."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from backend.app.models import Artifact, Evaluation, ExecutionAttempt, ResearchRun
from tests.integration import test_comparison_final_api as bilateral
from tests.integration import test_independent_final_api as final
from tests.integration.test_research_notes_api import PATH, edit, exact_url, submit

research_payload = final.research_payload
submit_research = final.submit_research
research_attempt = final.research_attempt
publish_initial = final.publish_initial
complete_candidate = final.complete_candidate
completed_source = final.completed_source
frozen_selection = final.frozen_selection
package_reference = final.package_reference
portable_final_pair = final.portable_final_pair
final_source = final.final_source
native_final_pair = bilateral.native_final_pair
pytestmark = pytest.mark.postgres


def denied_surfaces(api, headers, value, original, key):
    urls = [
        f"{PATH}/{value['note_id']}",
        exact_url(value),
        f"{PATH}/{value['note_id']}/revisions",
        f"{PATH}/{value['note_id']}/revisions?offset=100000",
        exact_url(value).replace("?", "/export?"),
        PATH,
        PATH + "?q=SecretOutcome&offset=100000",
        PATH + "?q=not-matching&offset=100000",
        PATH + "?status=supported_under_current_tests&limit=1",
        PATH + "?tag=final",
    ]
    for url in urls:
        response = api.get(url, headers=headers)
        assert response.status_code == 409, (url, response.text)
        assert "SecretOutcome" not in response.text
    assert submit(api, headers, original, key=key).status_code == 409
    assert (
        edit(api, headers, value, changes={"title": "Do not release"}).status_code
        == 409
    )


@pytest.mark.parametrize(
    "fault", ["generation", "pending", "unfinished_attempt", "missing_evaluation"]
)
def test_final_note_all_content_surfaces_readmit_exact_original_generation(
    api, owner_headers, migrated_engine, final_source, fault
):
    content = {
        "title": "SecretOutcome",
        "body": "Previously studied final outcome",
        "tags": ["final"],
        "status": "supported_under_current_tests",
        "evidence": [{"kind": "evaluation", "leaf": final_source.pin}],
    }
    key = uuid4().hex
    response = submit(api, owner_headers, content, key=key)
    assert response.status_code == 201, response.text
    value = response.json()
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, final_source.run_id)
        if fault == "generation":
            run.generation += 1
        elif fault == "pending":
            run.status = "finalizing"
        elif fault == "missing_evaluation":
            session.delete(
                session.get(Evaluation, UUID(final_source.pin["evaluation_id"]))
            )
        else:
            attempt = session.get(ExecutionAttempt, run.active_attempt_id)
            attempt.finished_at = None
        session.commit()
    denied_surfaces(api, owner_headers, value, content, key)


@pytest.mark.parametrize("fault", ["generation", "missing_evaluation"])
def test_removing_explicit_final_reference_does_not_launder_inherited_outcome_text(
    api, owner_headers, migrated_engine, final_source, fault
):
    content = {
        "title": "SecretOutcome",
        "evidence": [{"kind": "evaluation", "leaf": final_source.pin}],
    }
    key = uuid4().hex
    response = submit(api, owner_headers, content, key=key)
    assert response.status_code == 201, response.text
    first = response.json()
    response = edit(api, owner_headers, first, changes={"evidence": []})
    assert response.status_code == 201, response.text
    second = response.json()
    assert second["content"]["evidence"] == []
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, final_source.run_id)
        if fault == "generation":
            run.generation += 1
        else:
            session.delete(
                session.get(Evaluation, UUID(final_source.pin["evaluation_id"]))
            )
        session.commit()
    denied_surfaces(api, owner_headers, second, content, key)


def test_missing_first_side_cannot_hide_other_final_admission_failure(
    api, owner_headers, migrated_engine, settings, native_final_pair
):
    pair = native_final_pair
    content = {
        "title": "SecretOutcome",
        "evidence": [{"kind": "evaluation", "leaf": pin} for pin in pair.body.values()],
    }
    key = uuid4().hex
    response = submit(api, owner_headers, content, key=key)
    assert response.status_code == 201, response.text
    saved = response.json()
    with Session(migrated_engine) as session:
        left = session.get(Evaluation, pair.refs[0].evaluation_id)
        artifact = session.get(
            Artifact, UUID(left.payload["result_bundle"]["artifact_id"])
        )
        (settings.storage_root / artifact.storage_key).unlink()
        right = session.get(ResearchRun, pair.refs[1].run_id)
        right.generation += 1
        session.commit()
    denied_surfaces(api, owner_headers, saved, content, key)
