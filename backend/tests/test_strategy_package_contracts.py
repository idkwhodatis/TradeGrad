"""HTTP/pure package boundary tests with explicit fakes, not PostgreSQL evidence."""

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app import strategy_package_routes as routes
from backend.app import strategy_package_services as services
from backend.app.api.deps import get_current_user, get_session
from backend.app.services import DomainError
from backend.app.strategy_package_schemas import StrategyPackageCreate
from packages.tradegrad_adapter.strategy_package import (
    MAX_TOTAL_BYTES,
    PackageError,
    canonical,
    sha256,
)


@pytest.mark.parametrize(
    "field",
    [
        "include_raw_data",
        "storage_key",
        "code",
        "environment",
        "logs",
        "owner_id",
        "parameters",
        "include_upstream",
    ],
)
def test_package_request_has_no_path_data_or_sensitive_payload_options(field):
    with pytest.raises(ValidationError):
        StrategyPackageCreate(reference_evaluation_id=uuid4(), **{field: "forbidden"})
    with pytest.raises(ValidationError):
        StrategyPackageCreate(reference_evaluation_id=uuid4(), profile="self_contained")


def test_exact_request_and_owner_identity_changes():
    owner, selection = uuid4(), uuid4()
    body = StrategyPackageCreate(reference_evaluation_id=uuid4())
    request = services.package_request(selection, body)
    assert services.package_id(owner, request) == services.package_id(
        owner, copy.deepcopy(request)
    )
    assert services.package_id(owner, request) != services.package_id(uuid4(), request)
    assert services.package_id(owner, request) != services.package_id(
        owner, services.package_request(uuid4(), body)
    )
    assert services.package_id(owner, request) != services.package_id(
        owner, {**request, "reference_evaluation_id": str(uuid4())}
    )


def test_ownership_is_checked_before_any_file_lookup(monkeypatch):
    session = MagicMock()
    session.scalar.return_value = None
    filesystem = MagicMock(side_effect=AssertionError("Unauthorized file access"))
    monkeypatch.setattr(services, "read_verified", filesystem)
    for call in (
        lambda: services.get_owned_package(session, uuid4(), uuid4()),
        lambda: services.create_strategy_package(
            session,
            uuid4(),
            uuid4(),
            StrategyPackageCreate(reference_evaluation_id=uuid4()),
            SimpleNamespace(),
        ),
    ):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.status_code == 404
    filesystem.assert_not_called()


def test_private_download_reverifies_hash_and_size_and_paths(tmp_path):
    content = b"not an engine artifact, synthetic download fixture"
    target = tmp_path / "package.zip"
    target.write_bytes(content)
    item = SimpleNamespace(
        storage_key="package.zip",
        archive_sha256=sha256(content),
        size_bytes=len(content),
    )
    settings = SimpleNamespace(storage_root=tmp_path)
    assert services.read_package(item, settings) == content
    item.size_bytes += 1
    with pytest.raises(DomainError, match="size"):
        services.read_package(item, settings)
    item.size_bytes = len(content)
    target.write_bytes(b"tampered fixture")
    with pytest.raises(DomainError, match="checksum"):
        services.read_package(item, settings)
    for name in ("../package.zip", "C:/escape", "a\\b"):
        item.storage_key = name
        with pytest.raises(DomainError):
            services.read_package(item, settings)
    item.size_bytes = MAX_TOTAL_BYTES + 1
    item.storage_key = "package.zip"
    with pytest.raises(DomainError):
        services.read_package(item, settings)


def test_idempotent_replay_uses_immutable_existing_snapshot(package_service_fixture):
    f = package_service_fixture
    item, _ = services.create_strategy_package(
        f.session, f.owner_id, f.selection.id, f.body, f.settings
    )
    original_bytes = services.read_package(item, f.settings)
    original_snapshot = copy.deepcopy(item.exposure_snapshot)
    f.session.reset_mock()
    f.session.scalar.side_effect = [f.selection, item, f.source]
    result, created = services.create_strategy_package(
        f.session, f.owner_id, f.selection.id, f.body, f.settings
    )
    assert result is item and created is False
    assert result.exposure_snapshot == original_snapshot
    assert services.read_package(item, f.settings) == original_bytes
    f.session.add.assert_not_called()
    f.session.flush.assert_not_called()
    assert len(f.exposures) == 2
    assert f.exposures[0] == f.exposures[1]


def test_environment_missing_record_does_not_substitute_current_locks(monkeypatch):
    session = MagicMock()
    session.scalar.return_value = None
    verified = SimpleNamespace(
        run=SimpleNamespace(id=uuid4(), config={"environment_digest": "a" * 64}),
        evaluation=SimpleNamespace(
            attempt_id=uuid4(), image_digest="sha256:" + "b" * 64
        ),
    )
    monkeypatch.setattr(
        services,
        "capture_environment_evidence",
        MagicMock(
            side_effect=PackageError(
                "Historical execution environment no longer matches current reviewed inputs"
            )
        ),
    )
    with pytest.raises(PackageError, match="Historical"):
        services.load_environment_evidence(session, verified, SimpleNamespace())
    services.capture_environment_evidence.assert_called_once_with(
        "a" * 64, "sha256:" + "b" * 64
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.state.settings = SimpleNamespace(storage_root=tmp_path)
    app.dependency_overrides[get_session] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid4())

    @app.exception_handler(DomainError)
    async def error(_request: Request, exc: DomainError):
        return JSONResponse(
            {"code": exc.code, "message": exc.message}, status_code=exc.status_code
        )

    with TestClient(app) as value:
        yield value


def test_download_has_fixed_uuid_filename_and_private_security_headers(
    client, monkeypatch
):
    identifier = uuid4()
    content = b"synthetic archive fixture"
    monkeypatch.setattr(
        routes,
        "get_owned_package",
        lambda *args: SimpleNamespace(id=identifier, archive_sha256=sha256(content)),
    )
    monkeypatch.setattr(routes, "read_owned_package", lambda *args: content)
    response = client.get(f"/api/v1/strategy-packages/{identifier}/download")
    assert response.status_code == 200 and response.content == content
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="strategy-package-{identifier}.zip"'
    )
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["etag"] == f'"{sha256(content)}"'


def test_download_rejects_auth_before_resource_or_bytes(client, monkeypatch):
    def unauthenticated():
        raise DomainError("unauthorized", "Authentication required", 401)

    client.app.dependency_overrides[get_current_user] = unauthenticated
    lookup = MagicMock(side_effect=AssertionError("No lookup allowed"))
    monkeypatch.setattr(routes, "get_owned_package", lookup)
    assert (
        client.get(f"/api/v1/strategy-packages/{uuid4()}/download").status_code == 401
    )
    lookup.assert_not_called()


def test_legacy_universe_projection_does_not_claim_original_serialization():
    from datetime import UTC, datetime

    universe = SimpleNamespace(
        id=uuid4(),
        name="Fixture universe",
        state="user_fixed",
        as_of=datetime(2026, 10, 22, tzinfo=UTC),
        members=[],
        evidence=[],
        content_sha256="d" * 64,
        definition=None,
    )
    exported = services._universe_manifest(universe)
    assert exported["reference_sha256"] == universe.content_sha256
    assert exported["manifest_sha256"] != universe.content_sha256
    assert exported["serialization"] == "serialization_not_original"
    definition = {
        "name": universe.name,
        "state": universe.state,
        "as_of": "2026-10-22T08:00:00+08:00",
        "members": [],
        "evidence": [],
    }
    universe.definition = definition
    universe.content_sha256 = sha256(canonical(definition))
    exported = services._universe_manifest(universe)
    assert exported["serialization"] == "original_canonical_json"
    assert exported["manifest"] == definition
    assert exported["reference_sha256"] == exported["manifest_sha256"]
    universe.definition = {**definition, "name": "tampered"}
    with pytest.raises(DomainError, match="checksum"):
        services._universe_manifest(universe)


@pytest.fixture
def package_service_fixture(tmp_path, monkeypatch):
    """Explicit fake repository boundary; real hashing/storage/ZIP code is exercised."""
    from datetime import UTC, datetime

    from backend.app import experiment_services, final_evaluation_views
    from tests.tradegrad.test_strategy_package import package_input

    value = package_input.__wrapped__()
    (
        owner_id,
        selection_id,
        source_id,
        strategy_id,
        experiment_id,
        split_id,
        universe_id,
    ) = [uuid4() for _ in range(7)]
    source = SimpleNamespace(
        id=source_id,
        owner_id=owner_id,
        config=value.source_config,
        request_sha256=value.selection["source_config_sha256"],
    )
    strategy = SimpleNamespace(
        id=strategy_id,
        code=value.strategy_code.decode(),
        content_sha256=value.strategy_sha256,
        mode=value.strategy_mode,
    )
    split = SimpleNamespace(
        id=split_id,
        experiment_id=experiment_id,
        universe_version_id=universe_id,
        definition=value.split_version,
        content_sha256=sha256(canonical(value.split_version)),
    )
    selection = SimpleNamespace(
        id=selection_id,
        owner_id=owner_id,
        source_run_id=source_id,
        strategy_version_id=strategy_id,
        split_version_id=split_id,
        experiment_id=experiment_id,
        source_code_sha256=value.strategy_sha256,
        source_config_sha256=value.selection["source_config_sha256"],
        source_checkpoint_id=uuid4(),
        source_checkpoint_sha256="f" * 64,
        source_candidate_id=uuid4(),
        created_at=datetime(2026, 10, 22, tzinfo=UTC),
        request_sha256="1" * 64,
        selection_evidence={
            "candidate_key": "fixture-candidate",
            "valid": True,
            "complete": True,
        },
        exposure_snapshot=value.exposure_snapshot,
    )
    universe = SimpleNamespace(
        id=universe_id,
        name="Synthetic fixture",
        state="user_fixed",
        as_of=selection.created_at,
        members=[],
        evidence=[],
        content_sha256=value.source_config["universe"]["sha256"],
        definition=None,
    )
    config = {
        **value.final_config,
        "selection_id": str(selection_id),
        "split_sha256": split.content_sha256,
    }
    run = SimpleNamespace(
        id=uuid4(),
        owner_id=owner_id,
        final_selection_id=selection_id,
        split_version_id=split_id,
        strategy_version_id=strategy_id,
        experiment_id=experiment_id,
        config=config,
        request_sha256=sha256(canonical(config)),
    )
    evaluation = SimpleNamespace(
        id=uuid4(),
        attempt_id=uuid4(),
        split="holdout",
        code_sha256=value.strategy_sha256,
        image_digest=value.environment_evidence["image_id"],
    )
    dataset = SimpleNamespace(
        storage_key="datasets/fixture",
        content_sha256=sha256(canonical(value.dataset_manifest)),
        manifest=value.dataset_manifest,
    )
    dataset_root = tmp_path / dataset.storage_key
    dataset_root.mkdir(parents=True)
    (dataset_root / "manifest.json").write_bytes(canonical(dataset.manifest))
    environment_content = canonical(value.environment_evidence)
    (tmp_path / "execution-environment.json").write_bytes(environment_content)
    environment_artifact = SimpleNamespace(
        media_type="application/json",
        size_bytes=len(environment_content),
        sha256=sha256(environment_content),
        storage_key="execution-environment.json",
    )
    verified = SimpleNamespace(
        run=run,
        evaluation=evaluation,
        dataset=dataset,
        artifact=SimpleNamespace(sha256=value.result_sha256),
        content=value.result_content,
    )
    monkeypatch.setattr(
        final_evaluation_views, "load_owned_final_result", lambda *args: verified
    )
    monkeypatch.setattr(
        experiment_services,
        "get_exposure_snapshot",
        lambda *args: {**value.exposure_snapshot, "as_of": "2026-10-22T00:00:00Z"},
    )
    monkeypatch.setattr(services, "advisory_lock", MagicMock())
    exposures = []
    monkeypatch.setattr(
        services, "record_exposure", lambda session, **values: exposures.append(values)
    )
    session = MagicMock()
    session.scalar.side_effect = [
        selection,
        None,
        source,
        strategy,
        split,
        universe,
        environment_artifact,
    ]
    return SimpleNamespace(
        session=session,
        owner_id=owner_id,
        selection=selection,
        settings=SimpleNamespace(storage_root=tmp_path),
        body=StrategyPackageCreate(reference_evaluation_id=evaluation.id),
        code=value.strategy_code,
        source=source,
        exposures=exposures,
    )


def test_builder_service_publishes_only_immutable_package_not_an_attempt(
    package_service_fixture,
):
    from backend.app.models import StrategyPackage

    fixture = package_service_fixture
    item, created = services.create_strategy_package(
        fixture.session,
        fixture.owner_id,
        fixture.selection.id,
        fixture.body,
        fixture.settings,
    )
    assert created is True and isinstance(item, StrategyPackage)
    assert fixture.exposures[0]["purpose"] == "strategy_package_export"
    assert fixture.exposures[0]["provenance"]["archive_sha256"] == item.archive_sha256
    fixture.session.add.assert_called_once_with(item)
    assert item.archive_sha256 == sha256(services.read_package(item, fixture.settings))
    assert item.exposure_snapshot["as_of"] == "2026-10-22T00:00:00Z"
    assert (
        item.manifest["capabilities"]["universe_serialization"]
        == "serialization_not_original"
    )
    path = fixture.settings.storage_root / item.storage_key
    assert path.stat().st_mode & 0o222 == 0
    assert not list(path.parent.glob("*.tmp"))


def test_database_flush_failure_never_publishes_a_partial_archive(
    package_service_fixture,
):
    fixture = package_service_fixture
    fixture.session.flush.side_effect = RuntimeError("Synthetic transaction rejection")
    with pytest.raises(RuntimeError):
        services.create_strategy_package(
            fixture.session,
            fixture.owner_id,
            fixture.selection.id,
            fixture.body,
            fixture.settings,
        )
    # A DB rollback can orphan completed immutable content, never advertise a row.
    paths = list(fixture.settings.storage_root.glob("strategy-packages/*/*/*.zip"))
    assert len(paths) == 1
    assert paths[0].stem == sha256(paths[0].read_bytes())
    assert not list(fixture.settings.storage_root.rglob("*.tmp"))
