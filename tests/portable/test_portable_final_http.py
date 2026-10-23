"""Production router with explicit signature-checked service/session fakes only."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_portable_final_exports import final_evidence

from backend.app import portable_final_routes as routes
from backend.app import portable_final_views as views
from backend.app.api.deps import get_current_user, get_session
from backend.app.services import DomainError
from backend.app.strategy_package_services import load_environment_evidence


@pytest.fixture
def http_harness(monkeypatch):
    f = final_evidence()
    snapshot = {
        "classification": "previously_exposed",
        "matching_exposure_ids": [str(uuid4())],
        "matching_purposes": ["final_evaluation_admission"],
        "matching_count": 1,
        "identities": [],
        "as_of": datetime.now(UTC).isoformat(),
    }
    f.run.experiment_id, f.run.dataset_version_id = uuid4(), f.dataset.id
    f.run.created_at = f.run.updated_at = datetime.now(UTC)
    f.run.next_event_seq, f.run.error, f.run.result = 2, None, None
    session = create_autospec(Session, instance=True)
    user = SimpleNamespace(id=f.run.owner_id)
    app = FastAPI()
    app.state.settings = SimpleNamespace()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_session] = lambda: session
    app.include_router(routes.router, prefix="/api/v1")

    @app.exception_handler(DomainError)
    async def domain(request, error):
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "detail": error.message},
        )

    mocks = {}
    for name in (
        "submit_final_evaluation",
        "retry_final_evaluation",
        "owned_final_run",
        "request_cancel",
    ):
        mock = create_autospec(getattr(routes, name), return_value=f.run)
        monkeypatch.setattr(routes, name, mock)
        mocks[name] = mock
    preflight = create_autospec(
        routes.preflight_final_evaluation,
        return_value={
            "eligible": True,
            "selection_id": f.config.selection_id,
            "config": f.config,
            "exposure_snapshot": snapshot,
            "warnings": ["External history is unknown"],
            "quota_remaining": 1,
        },
    )
    monkeypatch.setattr(routes, "preflight_final_evaluation", preflight)
    verified = views.VerifiedPortableFinal(
        f.evaluation, f.run, f.dataset, f.artifact, f.bundle, f.content
    )
    access = create_autospec(routes._result_access, return_value=(verified, snapshot))
    monkeypatch.setattr(routes, "_result_access", access)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            f=f,
            session=session,
            user=user,
            mocks=mocks,
            access=access,
            preflight=preflight,
        )


def test_create_retry_preflight_owner_idempotency_and_strict_input(http_harness):
    h = http_harness
    prefix = "/api/v1/portable-final-evaluations"
    body = {"selection_id": str(h.f.config.selection_id), "max_runtime_seconds": 300}
    response = h.client.post(prefix + "/preflight", json=body)
    assert (
        response.status_code == 200
        and response.json()["config"]["schema_version"]
        == "tradegrad.portable.final-evaluation.v1"
    )
    assert h.preflight.call_args.args[1] == h.user.id
    response = h.client.post(
        prefix, json=body, headers={"Idempotency-Key": "fixed-request"}
    )
    assert (
        response.status_code == 202
        and response.json()["portable_policy_image_digest"]
        == h.f.run.portable_policy_image_digest
    )
    assert h.mocks["submit_final_evaluation"].call_args.args[1] == h.user.id
    assert h.mocks["submit_final_evaluation"].call_args.args[3] == "fixed-request"
    path = prefix + f"/{h.f.run.id}/retry"
    assert h.client.post(path).status_code == 422
    assert (
        h.client.post(path, headers={"Idempotency-Key": "fixed-retry"}).status_code
        == 202
    )
    assert (
        h.mocks["retry_final_evaluation"].call_args.kwargs["idempotency_key"]
        == "fixed-retry"
    )
    assert (
        h.client.post(
            prefix,
            json={**body, "allow_holdout": True},
            headers={"Idempotency-Key": "invalid"},
        ).status_code
        == 422
    )
    assert (
        h.client.post(
            prefix,
            json={**body, "source_config": {}},
            headers={"Idempotency-Key": "invalid"},
        ).status_code
        == 422
    )
    assert h.client.post(prefix + f"/{h.f.run.id}/pause").status_code == 409
    h.session.commit.assert_called()


def test_result_query_download_bounds_and_owner_use_same_exact_bytes(http_harness):
    h = http_harness
    path = f"/api/v1/portable-final-evaluations/evaluations/{h.f.evaluation.id}/result"
    response = h.client.get(path, params={"section": "decision_trace", "limit": 1})
    assert response.status_code == 200
    result = response.json()
    assert (
        result["selection_feedback"] is False and result["runtime_certified"] is False
    )
    assert len(result["rows"]) == 1 and result["selection_id"] == str(
        h.f.config.selection_id
    )
    assert h.access.call_args.args[2] == h.user.id
    assert h.client.get(path, params={"limit": 501}).status_code == 422
    assert h.client.get(path, params={"section": "prompts"}).status_code == 422
    response = h.client.get(path + "/download")
    assert response.status_code == 200 and response.content == h.f.content
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment;" in response.headers["content-disposition"]


def test_portable_result_owner_schema_checked_before_files(monkeypatch):
    session = create_autospec(Session, instance=True)
    session.execute.return_value.one_or_none.return_value = None
    read = create_autospec(
        views.read_verified, side_effect=AssertionError("Unauthorized filesystem read")
    )
    monkeypatch.setattr(views, "read_verified", read)
    with pytest.raises(DomainError) as error:
        views.load_owned_final_result(session, uuid4(), uuid4(), SimpleNamespace())
    assert error.value.status_code == 404
    query = str(session.execute.call_args.args[0])
    assert "owner_id" in query and "schema_version" in str(
        session.execute.call_args.args[0].compile().params.values()
    )
    read.assert_not_called()


def test_portable_package_never_reconstructs_current_environment():
    session = create_autospec(Session, instance=True)
    session.scalar.return_value = None
    f = final_evidence()
    verified = SimpleNamespace(run=f.run, evaluation=f.evaluation)
    with pytest.raises(DomainError, match="captured dual-image"):
        load_environment_evidence(session, verified, SimpleNamespace())
