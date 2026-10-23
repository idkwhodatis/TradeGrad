"""Real final/ZIP byte checks through HTTP with explicit fake persistence only.

The frozen-selection lookup is the shared loader harness's DTO seam. No DB,
Docker, socket, strategy or policy execution occurs, and no integration is claimed.
"""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_final_loader_admission import harness  # noqa: F401

from backend.app import strategy_package_routes as routes
from backend.app import strategy_package_services as services
from backend.app.api.deps import get_current_user, get_session
from backend.app.models import Artifact, DataUsageExposure
from backend.app.services import DomainError
from backend.app.strategy_package_schemas import StrategyPackageCreate
from packages.tradegrad_adapter.strategy_package import (
    canonical,
    deterministic_archive,
    sha256,
)


def register_package(h):
    """Register bounded synthetic ZIP bytes referring to actual typed result bytes."""
    run, evaluation, selection = h.result.run, h.result.evaluation, h.selection
    selection.source_checkpoint_id = uuid4()
    selection.source_checkpoint_sha256 = "c" * 64
    selection.request_sha256 = "d" * 64
    selection.selection_evidence = {"valid": True, "complete": True}
    selection.source_code_sha256 = evaluation.code_sha256
    selection.source_config_sha256 = run.config["source_config_sha256"]
    frozen = {
        "schema_version": "tradegrad.frozen-selection-export.v1",
        "selection_id": str(selection.id),
        "experiment_id": str(run.experiment_id),
        "split_version_id": str(run.split_version_id),
        "split_sha256": run.config["split_sha256"],
        "strategy_version_id": str(run.strategy_version_id),
        "source_run_id": str(selection.source_run_id),
        "source_candidate_id": str(selection.source_candidate_id),
        "source_checkpoint_id": str(selection.source_checkpoint_id),
        "source_checkpoint_sha256": selection.source_checkpoint_sha256,
        "selection_request_sha256": selection.request_sha256,
        "selection_evidence_sha256": sha256(canonical(selection.selection_evidence)),
        "source_code_sha256": selection.source_code_sha256,
        "source_config_sha256": selection.source_config_sha256,
        "final_run_id": str(run.id),
        "final_config_sha256": run.request_sha256,
        "reference_evaluation_id": str(evaluation.id),
        "reference_result_sha256": h.result.artifact.sha256,
        "dataset_sha256": h.result.dataset.content_sha256,
        "universe_sha256": run.config["source_config"]["universe"]["sha256"],
    }
    files = {
        "selection/frozen-selection.json": canonical(frozen),
        "reference/result-bundle.json": h.result.content,
    }
    inventory = {
        name: {"sha256": sha256(value), "bytes": len(value)}
        for name, value in files.items()
    }
    manifest = {
        "schema_version": "tradegrad.strategy-package.v1",
        "profile": "reference_only.v1",
        "content_sha256": sha256(canonical(inventory)),
        "raw_market_data_included": False,
        "files": inventory,
    }
    files["package-manifest.json"] = canonical(manifest)
    content = deterministic_archive(files.items())
    body = StrategyPackageCreate(reference_evaluation_id=evaluation.id)
    request = services.package_request(selection.id, body)
    item = SimpleNamespace(
        id=services.package_id(run.owner_id, request),
        owner_id=run.owner_id,
        final_selection_id=selection.id,
        request=request,
        request_sha256=sha256(canonical(request)),
        content_sha256=manifest["content_sha256"],
        archive_sha256=sha256(content),
        size_bytes=len(content),
        storage_key="immutable-test-package.zip",
        manifest=manifest,
        exposure_snapshot={"classification": "original_immutable_snapshot"},
        exposure_as_of=datetime(2026, 10, 22, tzinfo=UTC),
        created_at=datetime(2026, 10, 22, tzinfo=UTC),
    )
    (h.settings.storage_root / item.storage_key).write_bytes(content)
    h.session.package_row = item
    return item, body, content


@pytest.fixture
def package_http(harness):  # noqa: F811 - shared parametrized pytest fixture
    h = harness
    h.item, h.body, h.archive = register_package(h)
    h.persisted_exposures = []
    h.commit_failure = False

    def commit():
        h.calls.append("commit")
        if h.commit_failure:
            raise RuntimeError("Synthetic commit failure; no exposure was persisted")
        h.persisted_exposures[:] = copy.deepcopy(h.exposures)

    h.session.commit = commit
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.state.settings = h.settings
    h.user = SimpleNamespace(id=h.result.run.owner_id)
    app.dependency_overrides[get_session] = lambda: h.session
    app.dependency_overrides[get_current_user] = lambda: h.user

    @app.exception_handler(DomainError)
    async def error(_request: Request, exc: DomainError):
        return JSONResponse({"code": exc.code}, status_code=exc.status_code)

    with TestClient(app, raise_server_exceptions=False) as client:
        h.client = client
        yield h


def request_package(h, action):
    if action == "replay":
        return h.client.post(
            f"/api/v1/final-selections/{h.selection.id}/packages",
            json=h.body.model_dump(mode="json"),
        )
    return h.client.get(f"/api/v1/strategy-packages/{h.item.id}/download")


@pytest.mark.parametrize("action", ["download", "replay"])
def test_valid_package_read_checks_current_bytes_commits_exposure_and_is_immutable(
    package_http, action
):
    h = package_http
    original = copy.deepcopy(h.item.__dict__)
    for _ in range(2):
        response = request_package(h, action)
        assert response.status_code == 200, response.text
        if action == "download":
            assert response.content == h.archive
            assert response.headers["etag"] == f'"{h.item.archive_sha256}"'
        else:
            assert response.json()["archive_sha256"] == h.item.archive_sha256
        assert h.calls[-1] == "commit"
        assert h.persisted_exposures[-1]["purpose"] == "strategy_package_export"
        assert h.persisted_exposures[-1]["provenance"]["evaluation_id"] == str(
            h.result.evaluation.id
        )
    assert h.calls.index(DataUsageExposure) < h.calls.index(Artifact)
    assert h.item.__dict__ == original
    assert (h.settings.storage_root / h.item.storage_key).read_bytes() == h.archive
    assert h.exposures[0] == h.exposures[1]


@pytest.mark.parametrize("action", ["download", "replay"])
@pytest.mark.parametrize(
    "failure",
    [
        "missing_admission",
        "misbound_admission",
        "current_receipt",
        "current_attempt",
        "generation",
        "cancelled",
        "owner",
    ],
)
def test_package_read_rechecks_current_final_gates_before_any_files(
    package_http, action, failure, monkeypatch
):
    h = package_http
    if failure == "missing_admission":
        h.session.admission_row = None
    elif failure == "misbound_admission":
        h.admission.provenance["final_config_sha256"] = "a" * 64
    elif failure == "current_receipt":
        h.result.run.result = {"evaluation_id": str(uuid4())}
    elif failure == "current_attempt":
        h.result.run.active_attempt_id = uuid4()
    elif failure == "generation":
        h.result.run.generation += 1
    elif failure == "cancelled":
        h.result.run.status = "cancelled"
    else:
        h.user.id = uuid4()
    for module in (h.views, services):
        monkeypatch.setattr(
            module,
            "read_verified",
            lambda *args: pytest.fail("Rejected export opened bytes"),
        )
    response = request_package(h, action)
    assert response.status_code == (404 if failure == "owner" else 409), response.text
    assert not h.exposures and not h.persisted_exposures
    assert "commit" not in h.calls
    if failure == "owner":
        assert DataUsageExposure not in h.calls and Artifact not in h.calls


@pytest.mark.parametrize("action", ["download", "replay"])
def test_package_commit_failure_releases_no_zip_or_success_body(package_http, action):
    h = package_http
    h.commit_failure = True
    response = request_package(h, action)
    assert response.status_code == 500
    assert h.calls[-1] == "commit" and h.exposures
    assert not h.persisted_exposures
    assert h.archive not in response.content
    assert "application/zip" not in response.headers.get("content-type", "")
    assert (h.settings.storage_root / h.item.storage_key).read_bytes() == h.archive


@pytest.mark.parametrize("action", ["download", "replay"])
@pytest.mark.parametrize(
    "field",
    [
        "request_sha256",
        "content_sha256",
        "manifest",
        "frozen_selection",
        "reference_bytes",
    ],
)
def test_immutable_package_registration_and_reference_must_match_current_result(
    package_http, action, field
):
    import io
    import zipfile

    h = package_http
    if field in {"request_sha256", "content_sha256"}:
        setattr(h.item, field, "f" * 64)
    elif field == "manifest":
        h.item.manifest = {**h.item.manifest, "raw_market_data_included": True}
    else:
        # Even a self-consistent registration for a different frozen/reference
        # payload cannot bypass the currently verified result binding.
        with zipfile.ZipFile(io.BytesIO(h.archive)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        if field == "reference_bytes":
            files["reference/result-bundle.json"] = b'{"unrelated":"result"}'
        else:
            import json

            frozen = json.loads(files["selection/frozen-selection.json"])
            frozen["reference_evaluation_id"] = str(uuid4())
            files["selection/frozen-selection.json"] = canonical(frozen)
        inventory = {
            name: {"sha256": sha256(value), "bytes": len(value)}
            for name, value in files.items()
            if name != "package-manifest.json"
        }
        h.item.content_sha256 = sha256(canonical(inventory))
        h.item.manifest = {
            **h.item.manifest,
            "files": inventory,
            "content_sha256": h.item.content_sha256,
        }
        files["package-manifest.json"] = canonical(h.item.manifest)
        content = deterministic_archive(files.items())
        h.item.archive_sha256, h.item.size_bytes = sha256(content), len(content)
        (h.settings.storage_root / h.item.storage_key).write_bytes(content)
    response = request_package(h, action)
    assert response.status_code == 409, response.text
    assert not h.exposures and "commit" not in h.calls


def test_package_metadata_can_remain_readable_without_current_admission(package_http):
    h = package_http
    h.session.admission_row = None
    response = h.client.get(f"/api/v1/strategy-packages/{h.item.id}")
    assert response.status_code == 200
    assert not h.exposures and "commit" not in h.calls


@pytest.mark.parametrize("action", ["download", "replay"])
def test_stored_request_cannot_substitute_an_unrelated_evaluation(
    package_http, action, monkeypatch
):
    h = package_http
    h.body = StrategyPackageCreate(reference_evaluation_id=uuid4())
    h.item.request = services.package_request(h.selection.id, h.body)
    h.item.request_sha256 = sha256(canonical(h.item.request))
    h.item.id = services.package_id(h.item.owner_id, h.item.request)
    for module in (h.views, services):
        monkeypatch.setattr(
            module,
            "read_verified",
            lambda *args: pytest.fail("Unrelated reference opened bytes"),
        )
    response = request_package(h, action)
    assert response.status_code == 404, response.text
    assert Artifact not in h.calls and not h.exposures and "commit" not in h.calls
