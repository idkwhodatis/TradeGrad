"""Actual PostgreSQL HTTP ownership/exposure tests, with synthetic result bytes.

The optional required flag turns missing PostgreSQL into a failing gate. Neither
these synthetic observations nor local skips claim real engine/browser coverage.
"""

import os
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import (
    Artifact,
    DatasetVersion,
    DataUsageExposure,
    ExecutionAttempt,
    ResearchRun,
)
from backend.app.research_services import begin_evaluation, complete_evaluation
from backend.tests.research_view_fixtures import synthetic_result
from packages.research_trace.binding import make_binding
from packages.research_trace.storage import write_trace_artifacts
from packages.tradegrad_adapter.config import RunConfig, evaluation_windows
from tests.integration import test_research_lifecycle as lifecycle
from tests.integration import test_strategy_packages as packages
from tests.research_trace.test_storage import captured

base_research_payload = lifecycle.research_payload
submit_research = lifecycle.submit_research
research_attempt = lifecycle.research_attempt
publish_initial = lifecycle.publish_initial
complete_candidate = lifecycle.complete_candidate
completed_source = packages.completed_source
frozen_selection = packages.frozen_selection
package_reference = packages.package_reference
pytestmark = pytest.mark.postgres


@pytest.fixture
def research_payload(base_research_payload):
    return {**base_research_payload, "research_capture": {"mode": "research"}}


def test_trace_required_postgres_gate():
    if os.environ.get("TRADEGRAD_TRACE_POSTGRES_REQUIRED") == "1":
        assert os.environ.get("TEST_DATABASE_URL"), (
            "Required trace HTTP acceptance needs real PostgreSQL TEST_DATABASE_URL"
        )


@pytest.fixture
def publish_trace(migrated_engine, settings, owner, research_payload, research_attempt):
    def publish(*, collected=True, binding_update=None):
        config = RunConfig.model_validate(research_payload)
        attempt = research_attempt
        with Session(migrated_engine, expire_on_commit=False) as session:
            item = begin_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                candidate_key="candidate-0000",
                code_sha256=config.seed_strategy.sha256,
                window=evaluation_windows(config)[0],
            )
            dataset = session.get(DatasetVersion, UUID(config.dataset.id))
            run = session.get(ResearchRun, attempt.run_id)
            result = synthetic_result(
                config,
                owner_id=owner.user.id,
                run_id=attempt.run_id,
                attempt_id=attempt.id,
                evaluation_id=item.id,
                dataset_manifest=dataset.manifest,
            )
            session.commit()
        path = settings.storage_root / result.artifact.storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(result.content)
        recorder, manifest, written = None, None, []
        payload = result.evaluation.payload.copy()
        if collected:
            recorder = captured(
                code_hash=config.seed_strategy.sha256, begin=date(2024, 2, 1)
            )
            binding = make_binding(
                run=run,
                evaluation=item,
                config=config,
                bundle=result.bundle,
                result_sha256=result.artifact.sha256,
            )
            if binding_update:
                binding = binding.model_copy(update=binding_update)
            manifest, written = write_trace_artifacts(
                recorder.records, binding, recorder.summary(), path.parent
            )
        with Session(migrated_engine, expire_on_commit=False) as session:
            session.add(result.artifact)
            registrations = []
            for source in written:
                artifact = Artifact(
                    id=uuid4(),
                    run_id=run.id,
                    attempt_id=attempt.id,
                    name=source.name,
                    sha256=source.sha256,
                    size_bytes=source.size_bytes,
                    media_type=source.media_type,
                    storage_key=str(source.path.relative_to(settings.storage_root)),
                )
                session.add(artifact)
                registrations.append(artifact)
            if registrations:
                artifact = registrations[-1]
                payload["trace_manifest"] = {
                    "artifact_id": str(artifact.id),
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
                payload["research_capture"] = recorder.summary()
            session.flush()
            completed = complete_evaluation(
                session,
                attempt.id,
                1,
                attempt.lease_token,
                item.id,
                outcome=payload,
                sandbox_stopped=True,
            )
            assert completed.status == "succeeded"
            session.commit()
        return SimpleNamespace(
            result=result,
            recorder=recorder,
            manifest=manifest,
            artifacts=written,
            registrations=registrations,
        )

    return publish


def _base(value):
    return f"/api/v1/evaluations/{value.result.evaluation.id}"


def test_trace_pages_are_owner_scoped_and_exposure_is_committed(
    api, owner_headers, other_headers, publish_trace, migrated_engine
):
    value = publish_trace()
    decision = value.recorder.records[0].decision_id
    paths = [
        "/trace-manifest",
        "/decisions",
        f"/decisions/{decision}",
        f"/decisions/{decision}/candidates",
    ]
    with Session(migrated_engine) as session:
        before = session.scalar(
            select(func.count())
            .select_from(DataUsageExposure)
            .where(DataUsageExposure.purpose == "result_preview")
        )
    for path in paths:
        foreign = api.get(_base(value) + path, headers=other_headers)
        assert foreign.status_code == 404, foreign.text
    with Session(migrated_engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DataUsageExposure)
                .where(DataUsageExposure.purpose == "result_preview")
            )
            == before
        )
    for artifact in value.registrations:
        path = f"/api/v1/artifacts/{artifact.id}/download"
        assert api.get(path, headers=other_headers).status_code == 404
        response = api.get(path, headers=owner_headers)
        assert response.status_code == 200, response.text
        assert len(response.content) == artifact.size_bytes
    for path in paths:
        response = api.get(_base(value) + path, headers=owner_headers)
        assert response.status_code == 200, response.text
        assert response.json()["context"]["binding"]["evaluation_id"] == str(
            value.result.evaluation.id
        )
    with Session(migrated_engine) as session:
        rows = session.scalars(
            select(DataUsageExposure).where(
                DataUsageExposure.purpose == "result_preview"
            )
        ).all()
        assert len(rows) == before + 1
        assert rows[-1].provenance["evaluation_id"] == str(value.result.evaluation.id)


def test_trace_filters_exact_counts_times_and_separate_detail(
    api, owner_headers, publish_trace
):
    value = publish_trace()
    decision = value.recorder.records[0].decision_id
    base = _base(value)
    response = api.get(
        base + "/decisions",
        headers=owner_headers,
        params={"start": "2024-02-01", "end": "2024-02-02", "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["event_seq"] == 0
    detail = api.get(
        base + f"/decisions/{decision}",
        headers=owner_headers,
        params={"instrument_id": "B", "stage_id": "selection", "limit": 2},
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["decision"]["pre_state"] is None
    assert body["decision"]["pre_state_missing_reason"]
    assert body["predicates"]["total"] == 5
    assert len(body["predicates"]["items"]) == 2
    assert "candidates" not in body["stages"][0]
    candidates = api.get(
        base + f"/decisions/{decision}/candidates",
        headers=owner_headers,
        params={"eligibility": "ineligible"},
    )
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["total"] == 1
    assert candidates.json()["items"][0]["observation"]["instrument_id"] == "C"
    scores = api.get(
        base + f"/decisions/{decision}/candidates",
        headers=owner_headers,
        params={"score_min": 0.5, "score_max": 1.0},
    )
    assert scores.status_code == 200, scores.text
    assert scores.json()["total"] == 1
    assert scores.json()["items"][0]["observation"]["instrument_id"] == "C"
    assert (
        api.get(
            base + f"/decisions/{decision}/candidates",
            headers=owner_headers,
            params={"score_min": "nan"},
        ).status_code
        == 422
    )

    empty = api.get(
        base + f"/decisions/{decision}/candidates",
        headers=owner_headers,
        params={"offset": 5},
    )
    assert empty.json()["total"] == 2 and empty.json()["items"] == []
    assert (
        api.get(
            base + "/decisions", headers=owner_headers, params={"limit": 501}
        ).status_code
        == 422
    )
    assert (
        api.get(
            base + "/decisions", headers=owner_headers, params={"offset": 100001}
        ).status_code
        == 422
    )
    assert (
        api.get(base + f"/decisions/{uuid4()}", headers=owner_headers).status_code
        == 404
    )


@pytest.mark.parametrize(
    "pin", ["execution_attempt_id", "result_manifest_sha256", "trace_manifest_sha256"]
)
def test_evidence_pin_mismatch_never_retargets(api, owner_headers, publish_trace, pin):
    value = publish_trace()
    response = api.get(
        _base(value) + "/trace-manifest",
        headers=owner_headers,
        params={pin: str(uuid4()) if pin == "execution_attempt_id" else "f" * 64},
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "trace_pin_mismatch"


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink"])
def test_tampered_selected_parquet_is_not_served(
    api, owner_headers, publish_trace, fault, tmp_path
):
    value = publish_trace()
    candidate = next(
        item for item in value.artifacts if "-trace-candidates-" in item.name
    )
    original = candidate.path.read_bytes()
    candidate.path.unlink()
    if fault == "changed":
        candidate.path.write_bytes(original + b"tampered")
    elif fault == "symlink":
        replacement = tmp_path / "foreign.parquet"
        replacement.write_bytes(original)
        candidate.path.symlink_to(replacement)
    response = api.get(
        _base(value) + f"/decisions/{value.recorder.records[0].decision_id}/candidates",
        headers=owner_headers,
    )
    assert response.status_code == 409, response.text
    assert str(tmp_path) not in response.text
    registration = next(
        row for row in value.registrations if row.name == candidate.name
    )
    generic = api.get(
        f"/api/v1/artifacts/{registration.id}/download", headers=owner_headers
    )
    assert generic.status_code == 409, generic.text
    assert str(tmp_path) not in generic.text


@pytest.mark.parametrize(
    "field",
    [
        "code_sha256",
        "execution_config_sha256",
        "engine_dependencies_sha256",
        "result_manifest_sha256",
    ],
)
def test_bound_but_wrong_manifest_is_rejected(api, owner_headers, publish_trace, field):
    value = publish_trace(binding_update={field: "f" * 64})
    response = api.get(_base(value) + "/trace-manifest", headers=owner_headers)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "trace_integrity"


def test_legacy_result_absence_is_null_not_fabricated_empty_candidates(
    api, owner_headers, publish_trace
):
    value = publish_trace(collected=False)
    for suffix, field in [
        ("/trace-manifest", "manifest"),
        ("/decisions", "items"),
        (f"/decisions/{uuid4()}", "decision"),
        (f"/decisions/{uuid4()}/candidates", "items"),
    ]:
        response = api.get(_base(value) + suffix, headers=owner_headers)
        assert response.status_code == 200, response.text
        assert response.json()["context"]["status"] == "not_collected"
        assert response.json()[field] is None
    assert (
        api.get(
            f"/api/v1/evaluations/{uuid4()}/trace-manifest", headers=owner_headers
        ).status_code
        == 404
    )


@pytest.mark.parametrize("fault", ["pending", "retry", "unfinished_attempt"])
def test_final_admission_fence_applies_even_when_trace_is_absent(
    api, owner_headers, package_reference, migrated_engine, fault
):
    with Session(migrated_engine) as session:
        run = session.get(ResearchRun, package_reference.run_id)
        attempt = session.get(ExecutionAttempt, run.active_attempt_id)
        if fault == "pending":
            run.status, run.result = "running", None
        elif fault == "retry":
            run.generation += 1
        else:
            attempt.finished_at = None
        session.commit()
    response = api.get(
        f"/api/v1/evaluations/{package_reference.evaluation_id}/trace-manifest",
        headers=owner_headers,
    )
    assert response.status_code == 409, response.text
