"""Pure native/portable final lifecycle projections using inert result fixtures.

No database, socket, policy execution, native engine, or Docker acceptance claim.
"""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_portable_final_exports import final_evidence

from backend.app import final_evaluation_routes, portable_final_routes
from backend.app.final_evaluation_views import validate_final_persisted_result
from backend.app.final_summary_access import accepted_result_access
from backend.app.models import (
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    ResearchRun,
)
from backend.app.portable_final_views import validate_portable_final_result
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import (
    synthetic_final_admission,
    synthetic_final_result,
)


@pytest.fixture(params=["native", "portable"])
def harness(request, monkeypatch):
    portable = request.param == "portable"
    f = final_evidence() if portable else synthetic_final_result()
    routes = portable_final_routes if portable else final_evaluation_routes
    validator = (
        validate_portable_final_result if portable else validate_final_persisted_result
    )
    now = datetime.now(UTC)
    f.run.experiment_id = uuid4()
    f.run.dataset_version_id = f.dataset.id
    f.run.created_at = f.run.updated_at = now
    f.run.next_event_seq = 2
    f.run.error = None
    # The public projection must not trust this unverified denormalized summary.
    f.run.result = {"evaluation_id": str(f.evaluation.id), "metrics": {"wrong": 999}}
    f.evaluation.candidate_id = None
    f.evaluation.candidate_key = f"final:{f.run.final_selection_id}"
    f.evaluation.sandbox_id = "synthetic-not-executed"
    f.evaluation.error = None
    f.evaluation.created_at = now
    attempt = SimpleNamespace(
        id=f.evaluation.attempt_id,
        run_id=f.run.id,
        generation=f.run.generation,
        status="succeeded",
        sandbox_stopped=True,
        exit_code=0,
        error=None,
        heartbeat_at=now,
        started_at=now,
        finished_at=now,
    )
    snapshot = {
        "classification": "previously_exposed",
        "matching_exposure_ids": [str(uuid4())],
        "matching_purposes": ["final_evaluation_admission"],
        "matching_count": 1,
        "identities": [],
        "as_of": now.isoformat(),
    }
    admission = synthetic_final_admission(f, snapshot)
    calls = []

    class Session:
        def __init__(self):
            self.evaluation_rows = [f.evaluation]
            self.run_rows = [f.run]
            self.admission_row = admission

        def get(self, model, identifier):
            calls.append(("get", model, identifier))
            if model is ExecutionAttempt:
                return attempt if identifier == attempt.id else None
            if model is Evaluation:
                return next(
                    (row for row in self.evaluation_rows if row.id == identifier), None
                )
            pytest.fail(f"Unexpected lookup: {model}")

        def scalars(self, query):
            model = query.column_descriptions[0]["entity"]
            rows = {
                Evaluation: self.evaluation_rows,
                ResearchRun: self.run_rows,
                ExecutionAttempt: [attempt],
            }[model]
            return SimpleNamespace(all=lambda: rows)

        def scalar(self, query):
            if query.column_descriptions[0]["entity"] is DataUsageExposure:
                calls.append(("admission",))
                return self.admission_row
            return len(self.evaluation_rows)

        def commit(self):
            calls.append(("commit",))

    def load(session, evaluation_id, owner_id, settings):
        calls.append(("load", evaluation_id, owner_id))
        assert owner_id == f.run.owner_id and evaluation_id == f.evaluation.id
        validator(f.evaluation, f.run, f.dataset, f.artifact, f.content)
        return f

    def record(session, **kwargs):
        calls.append(("exposure", kwargs))

    monkeypatch.setattr(routes, "load_owned_final_result", load)
    monkeypatch.setattr(routes, "record_exposure", record)
    for name in (
        "owned_final_run",
        "submit_final_evaluation",
        "retry_final_evaluation",
        "request_cancel",
    ):
        monkeypatch.setattr(routes, name, lambda *args, **kwargs: f.run)
    session = Session()
    return SimpleNamespace(
        f=f,
        routes=routes,
        session=session,
        attempt=attempt,
        calls=calls,
        user=SimpleNamespace(id=f.run.owner_id),
        request=SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=None))
        ),
    )


def call_route(h, name):
    args = (h.f.run.id, h.user, h.session, h.request)
    if name == "evidence":
        return h.routes.evidence(*args, offset=0, limit=100)["data"][0]
    if name == "retry":
        return h.routes.retry(*args, idempotency_key="same-retry")
    if name == "create":
        return h.routes.create(None, h.user, h.session, h.request, "same-create")
    return getattr(h.routes, name)(*args)


def test_listing_is_metadata_only_without_opening_any_result_artifacts(harness):
    h = harness
    h.session.run_rows = [h.f.run] * 100
    response = h.routes.listing(h.user, h.session, offset=0, limit=100)
    assert len(response["data"]) == 100
    assert all(
        row.result is None and row.status == "succeeded" for row in response["data"]
    )
    assert h.calls == []
    assert h.f.run.result["metrics"] == {"wrong": 999}


@pytest.mark.parametrize("route", ["detail", "evidence", "create", "cancel", "retry"])
def test_successful_metric_responses_validate_bytes_and_commit_exposure(harness, route):
    h = harness
    original_payload = deepcopy(h.f.evaluation.payload)
    original_result = deepcopy(h.f.run.result)
    response = call_route(h, route)
    if route == "evidence":
        assert response.payload["metrics"] == original_payload["metrics"]
        assert "result_data_redacted" not in response.payload
        assert response.payload["payload_projection"] == "verified_final_summary"
    else:
        assert response.result is not None
        assert response.result.evaluation_id == h.f.evaluation.id
        assert response.result.metrics and "wrong" not in response.result.metrics
        assert response.result.holdout_executed is True
        if route == "detail":
            assert response.attempts[0].id == h.attempt.id
    actions = [row[0] for row in h.calls]
    assert actions[-4:] == ["load", "admission", "exposure", "commit"]
    exposure = h.calls[-2][1]
    assert exposure["purpose"] == "result_preview"
    assert exposure["source_key"] == f"final-result:{h.f.evaluation.id}:result_preview"
    assert exposure["provenance"]["artifact_sha256"] == h.f.artifact.sha256
    assert (
        h.f.evaluation.payload == original_payload and h.f.run.result == original_result
    )


@pytest.mark.parametrize("route", ["detail", "evidence", "create", "cancel", "retry"])
@pytest.mark.parametrize(
    "status",
    ["running", "finalizing", "cancel_requested", "cancelled", "failed", "interrupted"],
)
def test_nonterminal_or_rejected_result_is_diagnostic_only(harness, route, status):
    h = harness
    h.f.run.status = status
    response = call_route(h, route)
    if route == "evidence":
        assert response.status == "succeeded"  # Physical history is not rewritten.
        assert response.payload["status"] == "succeeded"
        assert response.payload["exit_code"] == 0
        assert response.payload["result_data_redacted"] is True
        assert response.payload["payload_projection"] == "diagnostic_only"
        assert (
            "metrics" not in response.payload
            and "combined_score" not in response.payload
        )
        assert "result_trust" not in response.payload
        assert response.payload_sha256 == h.f.evaluation.payload_sha256
    else:
        assert response.status == status and response.result is None
    assert h.calls == [("commit",)]


@pytest.mark.parametrize(
    "mutation",
    [
        "run_generation",
        "attempt_id",
        "evaluation_run",
        "evaluation_status",
        "evaluation_unfinished",
        "evaluation_not_stopped",
        "attempt_generation",
        "attempt_run",
        "attempt_status",
        "attempt_unfinished",
        "attempt_not_stopped",
        "attempt_exit",
        "missing_attempt",
        "missing_result",
        "wrong_result",
        "invalid_result_id",
    ],
)
def test_terminal_binding_rejects_late_or_uncommitted_metrics_before_artifact_read(
    harness, mutation
):
    h = harness
    if mutation == "run_generation":
        h.f.run.generation += 1
    elif mutation == "attempt_id":
        h.f.run.active_attempt_id = uuid4()
    elif mutation == "evaluation_run":
        h.f.evaluation.run_id = uuid4()
    elif mutation == "evaluation_status":
        h.f.evaluation.status = "failed"
    elif mutation == "evaluation_unfinished":
        h.f.evaluation.finished_at = None
    elif mutation == "evaluation_not_stopped":
        h.f.evaluation.sandbox_stopped = False
    elif mutation == "attempt_generation":
        h.attempt.generation += 1
    elif mutation == "attempt_run":
        h.attempt.run_id = uuid4()
    elif mutation == "attempt_status":
        h.attempt.status = "running"
    elif mutation == "attempt_unfinished":
        h.attempt.finished_at = None
    elif mutation == "attempt_not_stopped":
        h.attempt.sandbox_stopped = False
    elif mutation == "attempt_exit":
        h.attempt.exit_code = 1
    elif mutation == "missing_attempt":
        h.attempt.id = uuid4()
    elif mutation == "missing_result":
        h.f.run.result = None
    elif mutation == "wrong_result":
        h.f.run.result = {"evaluation_id": str(uuid4())}
    else:
        h.f.run.result = {"evaluation_id": "not-a-uuid"}
    for route in ("detail", "evidence"):
        response = call_route(h, route)
        assert (
            response.result is None
            if route == "detail"
            else "metrics" not in response.payload
        )
    assert not any(row[0] in {"load", "exposure"} for row in h.calls)


def test_historical_evidence_is_preserved_but_only_current_result_is_read(harness):
    h = harness
    current = h.f.evaluation
    old = SimpleNamespace(
        **{
            name: getattr(current, name)
            for name in (
                "id",
                "run_id",
                "attempt_id",
                "generation",
                "candidate_id",
                "candidate_key",
                "code_sha256",
                "split",
                "window",
                "window_sha256",
                "image_digest",
                "sandbox_id",
                "sandbox_stopped",
                "status",
                "payload",
                "payload_sha256",
                "error",
                "created_at",
                "finished_at",
            )
        }
    )
    old.id, old.attempt_id, old.generation = uuid4(), uuid4(), 0
    old.error = "Previous execution diagnostic"
    h.session.evaluation_rows = [old, current]
    response = h.routes.evidence(
        h.f.run.id, h.user, h.session, h.request, offset=0, limit=100
    )
    first, second = response["data"]
    assert first.id == old.id and first.generation == 0 and first.error == old.error
    assert "metrics" not in first.payload and second.payload["metrics"]
    assert len([row for row in h.calls if row[0] == "load"]) == 1
    assert len([row for row in h.calls if row[0] == "exposure"]) == 1


@pytest.mark.parametrize("route", ["detail", "evidence", "create", "cancel", "retry"])
@pytest.mark.parametrize(
    "failure", ["integrity", "missing_admission", "missing_snapshot"]
)
def test_unverified_result_does_not_hide_lifecycle_or_publish_metrics(
    harness, monkeypatch, route, failure
):
    h = harness
    if failure == "integrity":

        def invalid(*args):
            raise DomainError("artifact_integrity", "Synthetic corrupt result", 409)

        monkeypatch.setattr(h.routes, "load_owned_final_result", invalid)
    elif failure == "missing_admission":
        h.session.admission_row = None
    else:
        h.session.admission_row.provenance = {}
    response = call_route(h, route)
    assert (
        response.result is None
        if route != "evidence"
        else "metrics" not in response.payload
    )
    assert not any(row[0] == "exposure" for row in h.calls)
    assert h.calls[-1] == ("commit",)


def test_owner_checked_before_any_result_lookup(harness):
    h = harness
    with pytest.raises(DomainError) as error:
        accepted_result_access(
            h.session, h.f.run, uuid4(), None, h.routes._result_access
        )
    assert error.value.status_code == 404 and h.calls == []


def test_exposure_commit_failure_never_returns_metric_response(harness, monkeypatch):
    h = harness

    def fail_commit():
        raise RuntimeError("Synthetic persistence failure")

    monkeypatch.setattr(h.session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="persistence"):
        call_route(h, "detail")
    assert h.calls[-1][0] == "exposure"
