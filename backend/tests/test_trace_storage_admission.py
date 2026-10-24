"""HTTP storage regressions with real files and synthetic session/result admission.

These local checks do not replace PostgreSQL ownership or migration acceptance.
"""

import secrets
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import research_views, result_access, storage, trace_routes
from backend.app.api.deps import get_current_user, get_session
from backend.app.core.config import Settings
from backend.app.main import create_app
from backend.app.models import Artifact, Evaluation, ResearchRun
from backend.tests.research_view_fixtures import synthetic_result
from packages.research_trace.binding import make_binding
from packages.research_trace.storage import write_trace_artifacts
from packages.tradegrad_adapter.config import RunConfig
from tests.research_trace.test_storage import captured


class TraceAPI(SimpleNamespace):
    def __repr__(self):
        return "TraceAPI(<synthetic session and private temporary storage>)"


@pytest.fixture
def trace_api(tmp_path, monkeypatch):
    config = RunConfig.model_validate(
        {**synthetic_result().run.config, "research_capture": {"mode": "research"}}
    )
    result = synthetic_result(config)
    settings = Settings(
        database_url="postgresql+psycopg://localhost/unused",
        secret_key=secrets.token_urlsafe(48),
        storage_root=tmp_path / "storage",
    )
    directory = settings.storage_root / "trace"
    directory.mkdir(parents=True)
    recorder = captured(code_hash=config.seed_strategy.sha256)
    _, written = write_trace_artifacts(
        recorder.records,
        make_binding(
            run=result.run,
            evaluation=result.evaluation,
            config=config,
            bundle=result.bundle,
            result_sha256=result.artifact.sha256,
        ),
        recorder.summary(),
        directory,
    )
    artifacts = [
        Artifact(
            id=uuid4(),
            run_id=result.run.id,
            attempt_id=result.evaluation.attempt_id,
            name=item.name,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            media_type=item.media_type,
            storage_key=str(item.path.relative_to(settings.storage_root)),
        )
        for item in written
    ]
    manifest = artifacts[-1]
    result.evaluation.payload.update(
        trace_manifest={
            "artifact_id": str(manifest.id),
            "sha256": manifest.sha256,
            "size_bytes": manifest.size_bytes,
        },
        research_capture=recorder.summary(),
    )
    commits, exposures, opened = [], [], []
    owner = SimpleNamespace(id=result.run.owner_id)

    def scalar(query):
        params = query.compile().params
        if "name_1" in params:
            return manifest
        if params["owner_id_1"] != owner.id:
            return None
        return next((row for row in artifacts if row.id == params["id_1"]), None)

    def execute(query):
        params = query.compile().params
        pair = (
            (result.evaluation, result.run)
            if params["owner_id_1"] == owner.id
            else None
        )
        return SimpleNamespace(one_or_none=lambda: pair)

    objects = {ResearchRun: result.run, Evaluation: result.evaluation}
    session = SimpleNamespace(
        execute=execute,
        scalar=scalar,
        scalars=lambda query: SimpleNamespace(all=lambda: artifacts[:-1]),
        get=lambda model, identifier: objects[model],
        commit=lambda: commits.append(True),
    )
    monkeypatch.setattr(research_views, "load_owned_result", lambda *args: result)
    for module in (trace_routes, result_access):
        monkeypatch.setattr(
            module,
            "record_verified_result_access",
            lambda *args, **kwargs: exposures.append(True),
        )
    original_open = storage.os.open

    def open_file(path, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", open_file)
    app = create_app(settings=settings, engine=object())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: owner
    with TestClient(app) as api:
        yield TraceAPI(
            api=api,
            app=app,
            settings=settings,
            directory=directory,
            artifacts=artifacts,
            manifest=manifest,
            candidates=next(
                row for row in artifacts if "-trace-candidates-" in row.name
            ),
            decision=recorder.records[0].decision_id,
            base=f"/api/v1/evaluations/{result.evaluation.id}",
            commits=commits,
            exposures=exposures,
            opened=opened,
        )


@pytest.mark.parametrize("artifact_kind", ["manifest", "candidates"])
@pytest.mark.parametrize(
    "fault", ["symlink", "parent_symlink", "absolute", "traversal"]
)
def test_unsafe_registered_trace_is_sanitized_conflict_on_both_http_boundaries(
    trace_api, tmp_path, artifact_kind, fault
):
    h = trace_api
    artifact = getattr(h, artifact_kind)
    path = h.settings.storage_root / artifact.storage_key
    outside = tmp_path / "private-target"
    if fault == "parent_symlink":
        h.directory.rename(outside)
        h.directory.symlink_to(outside, target_is_directory=True)
    else:
        outside.write_bytes(path.read_bytes())
        if fault == "symlink":
            path.unlink()
            path.symlink_to(outside)
        else:
            artifact.storage_key = (
                str(outside) if fault == "absolute" else "../private-target"
            )
    for url in (
        h.base + f"/decisions/{h.decision}/candidates",
        f"/api/v1/artifacts/{artifact.id}/download",
    ):
        response = h.api.get(url)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "trace_integrity"
        assert str(tmp_path) not in response.text
        assert "private-target" not in response.text
    assert not h.commits and not h.exposures
    assert not any(item == outside or item.is_relative_to(outside) for item in h.opened)
    assert path not in h.opened


def test_intact_trace_remains_readable_on_both_http_boundaries(trace_api):
    h = trace_api
    response = h.api.get(h.base + f"/decisions/{h.decision}/candidates")
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 2
    response = h.api.get(f"/api/v1/artifacts/{h.candidates.id}/download")
    assert response.status_code == 200, response.text
    assert len(response.content) == h.candidates.size_bytes
    assert len(h.commits) == len(h.exposures) == 2


def test_foreign_owner_never_probes_corrupt_trace_storage(trace_api, tmp_path):
    h = trace_api
    path = h.settings.storage_root / h.manifest.storage_key
    path.unlink()
    path.symlink_to(tmp_path / "never-open")
    h.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())
    for url in (
        h.base + "/trace-manifest",
        f"/api/v1/artifacts/{h.candidates.id}/download",
    ):
        response = h.api.get(url)
        assert response.status_code == 404, response.text
    assert not h.opened and not h.commits and not h.exposures


@pytest.mark.parametrize("artifact_kind", ["manifest", "candidates"])
def test_generic_trace_download_rechecks_path_after_manifest_admission(
    trace_api, tmp_path, monkeypatch, artifact_kind
):
    from backend.app import trace_services

    h = trace_api
    artifact = getattr(h, artifact_kind)
    path = h.settings.storage_root / artifact.storage_key
    outside = tmp_path / "replacement"
    original_load = trace_services.load_owned_trace

    def admit_then_replace(*args, **kwargs):
        trace = original_load(*args, **kwargs)
        path.rename(outside)
        path.symlink_to(outside)
        return trace

    monkeypatch.setattr(trace_services, "load_owned_trace", admit_then_replace)
    response = h.api.get(f"/api/v1/artifacts/{artifact.id}/download")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "trace_integrity"
    assert str(tmp_path) not in response.text
    assert outside not in h.opened
    assert not h.commits and not h.exposures


@pytest.mark.parametrize("artifact_kind", ["manifest", "candidates"])
def test_nofollow_rejects_actual_leaf_swap_after_path_check(
    trace_api, tmp_path, monkeypatch, artifact_kind
):
    h = trace_api
    artifact = getattr(h, artifact_kind)
    path = h.settings.storage_root / artifact.storage_key
    outside = tmp_path / "race-target"
    original_open = storage.os.open
    replaced = []

    def replace_then_open(candidate, flags, *args, **kwargs):
        if Path(candidate) == path and not replaced:
            assert flags & storage.os.O_NOFOLLOW
            path.rename(outside)
            path.symlink_to(outside)
            replaced.append(True)
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(storage.os, "open", replace_then_open)
    response = h.api.get(f"/api/v1/artifacts/{artifact.id}/download")
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "trace_integrity"
    assert str(tmp_path) not in response.text
    assert replaced and path.is_symlink()
    assert not h.commits and not h.exposures


def test_trace_admission_does_not_hide_unrelated_domain_or_operational_errors():
    import errno

    from backend.app.services import DomainError
    from backend.app.trace_services import trace_storage_admission

    for original in (
        DomainError("trace_pin_mismatch", "Evidence pins differ", 409),
        DomainError("storage_unavailable", "Storage unavailable", 503),
        OSError(errno.EIO, "Synthetic I/O failure"),
    ):
        with pytest.raises(type(original)) as error, trace_storage_admission():
            raise original
        assert error.value is original


@pytest.mark.parametrize("artifact_kind", ["manifest", "candidates"])
@pytest.mark.parametrize("fault", ["missing", "changed"])
def test_missing_or_changed_trace_is_never_released(trace_api, artifact_kind, fault):
    h = trace_api
    artifact = getattr(h, artifact_kind)
    path = h.settings.storage_root / artifact.storage_key
    if fault == "missing":
        path.unlink()
    else:
        original = path.read_bytes()
        path.unlink()
        path.write_bytes(original + b"tampered")
    for url in (
        h.base + f"/decisions/{h.decision}/candidates",
        f"/api/v1/artifacts/{artifact.id}/download",
    ):
        response = h.api.get(url)
        assert response.status_code == 409, response.text
        assert str(h.settings.storage_root) not in response.text
        assert "tampered" not in response.text
    assert not h.commits and not h.exposures
