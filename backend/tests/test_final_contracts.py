"""Offline M5 contracts only. No mocked-session or SQLite database substitutes."""

import copy
import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.app.experiment_schemas import (
    ExperimentCreate,
    FinalSelectionCreate,
    SplitDefinition,
)
from backend.app.experiment_services import (
    classify_exposures,
    dataset_identities,
    identities_overlap,
    split_definition,
)
from backend.app.final_evaluation_schemas import FinalEvaluationCreate
from backend.app.final_evaluation_services import final_config
from backend.app.models import DataUsageExposure, ResearchRun, StrategyPackage
from backend.app.research_services import _validated_window
from backend.app.services import DomainError
from backend.tests.test_research_contracts import sample_config
from packages.tradegrad_adapter.config import DateWindow, RunConfig, canonical_bytes
from packages.tradegrad_adapter.final_config import FinalEvaluationConfig


def source_config():
    value = sample_config().model_dump(mode="json")
    for index, name in enumerate(("dataset", "seed_strategy", "universe"), 1):
        value[name]["id"] = str(UUID(int=index))
    return RunConfig.model_validate(value)


def final_configuration():
    source = source_config()
    split = SplitDefinition(
        dataset=source.dataset, universe=source.universe, windows=source.windows
    )
    return FinalEvaluationConfig(
        selection_id=UUID(int=5),
        split_version_id=UUID(int=6),
        split_sha256=split.sha256,
        selected_strategy={"id": str(UUID(int=7)), "sha256": "d" * 64},
        source_config=source,
        source_config_sha256=source.sha256,
        holdout=source.windows.holdout,
        environment_digest=source.environment_digest,
        max_runtime_seconds=300,
    )


def test_old_run_config_bytes_and_hash_are_exactly_unchanged():
    config = source_config()
    assert (
        config.sha256
        == "ea9c54fadc8ea319d9eec6ea69c1657836f971fb39b72690b32a4db7edc44c7c"
    )
    assert config.canonical_bytes() == canonical_bytes(config.model_dump(mode="json"))
    assert (
        not {"experiment_id", "split_version_id", "final_selection_id"}
        & RunConfig.model_fields.keys()
    )
    assert (
        RunConfig.from_verified_json(config.canonical_bytes(), config.sha256) == config
    )


def test_final_schema_is_distinct_and_contains_exact_source_bytes():
    config = final_configuration()
    assert config.source_config.canonical_bytes() == source_config().canonical_bytes()
    assert config.source_config_sha256 == source_config().sha256
    assert config.sha256 == hashlib.sha256(config.canonical_bytes()).hexdigest()
    assert FinalEvaluationConfig.model_validate_json(config.canonical_bytes()) == config
    with pytest.raises(ValidationError):
        RunConfig.model_validate(config.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        FinalEvaluationConfig.model_validate(source_config().model_dump(mode="json"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_config_sha256", "a" * 64),
        ("environment_digest", "b" * 64),
        ("holdout", {"start": "2024-03-02", "end": "2024-03-10"}),
        ("execution_mode", "optimization"),
        ("max_runtime_seconds", 0),
        ("max_runtime_seconds", 3601),
        ("schema_version", "tradegrad.run.v1"),
        ("optimizer", {}),
    ],
)
def test_final_rejects_changed_source_plan_or_unbounded_runtime(field, value):
    payload = final_configuration().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        FinalEvaluationConfig.model_validate(payload)


def test_final_config_revalidation_catches_nested_copy_mutation():
    config = final_configuration()
    raw = copy.deepcopy(config.model_dump(mode="json"))
    raw["source_config"]["optimizer"]["max_iterations"] = 2
    run = ResearchRun(config=raw, request_sha256=config.sha256)
    with pytest.raises(DomainError, match="exact frozen"):
        final_config(run)


def test_final_run_hash_and_frozen_config_enforced():
    config = final_configuration()
    assert (
        final_config(
            ResearchRun(
                config=config.model_dump(mode="json"), request_sha256=config.sha256
            )
        )
        == config
    )
    with pytest.raises(DomainError):
        final_config(
            ResearchRun(config=config.model_dump(mode="json"), request_sha256="0" * 64)
        )
    with pytest.raises(ValidationError):
        config.max_runtime_seconds = 10


def test_optimizer_still_rejects_holdout_and_final_schema():
    source = source_config()
    window = {"split": "holdout", **source.windows.holdout.model_dump(mode="json")}
    for config in [source, final_configuration()]:
        with pytest.raises(DomainError):
            _validated_window(
                ResearchRun(config=config.model_dump(mode="json")), window
            )


def test_split_hash_canonical_and_reference_validation():
    config = source_config()
    definition = SplitDefinition(
        dataset=config.dataset, universe=config.universe, windows=config.windows
    )
    row = SimpleNamespace(
        definition=definition.model_dump(mode="json"),
        content_sha256=definition.sha256,
        dataset_version_id=UUID(config.dataset.id),
        universe_version_id=UUID(config.universe.id),
    )
    assert split_definition(row) == definition
    row.content_sha256 = "0" * 64
    with pytest.raises(DomainError):
        split_definition(row)
    row.content_sha256 = definition.sha256
    row.dataset_version_id = uuid4()
    with pytest.raises(DomainError):
        split_definition(row)


def test_selection_body_hash_is_exact_for_lost_response_and_changed_candidate():
    body = FinalSelectionCreate(
        split_version_id=uuid4(),
        source_run_id=uuid4(),
        source_candidate_id=uuid4(),
        source_checkpoint_id=uuid4(),
    )
    assert (
        FinalSelectionCreate.model_validate(body.model_dump(mode="json")).sha256
        == body.sha256
    )
    assert (
        body.model_copy(update={"source_candidate_id": uuid4()}).sha256 != body.sha256
    )
    with pytest.raises(ValidationError):
        FinalSelectionCreate.model_validate(
            {**body.model_dump(mode="json"), "code": "override"}
        )


@pytest.mark.parametrize(
    "body",
    [
        {"selection_id": str(uuid4()), "max_runtime_seconds": 3601},
        {"selection_id": str(uuid4()), "allow_holdout": True},
        {"selection_id": str(uuid4()), "strategy_code": "override"},
    ],
)
def test_final_submission_cannot_override_code_plan_or_budget(body):
    with pytest.raises(ValidationError):
        FinalEvaluationCreate.model_validate(body)


def identity(lid=None, symbol="INDEX", market="CN"):
    return {"listing_id": str(lid) if lid else None, "market": market, "symbol": symbol}


def exposure(
    identities,
    start="2024-03-01",
    end="2024-03-10",
    purpose="final_evaluation_admission",
    **kwargs,
):
    return SimpleNamespace(
        id=uuid4(),
        identities=identities,
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
        purpose=purpose,
        **kwargs,
    )


@pytest.mark.parametrize(
    "purpose",
    [
        "optimization_train_admission",
        "optimization_validation_admission",
        "final_evaluation_admission",
        "dataset_preview",
        "dataset_download",
        "result_preview",
        "result_download",
        "seed_evaluation_admission",
    ],
)
def test_all_admitted_or_viewed_data_is_conservative_exposure(purpose):
    ids = [identity(uuid4())]
    snapshot = classify_exposures(
        ids,
        DateWindow(start="2024-03-01", end="2024-03-10"),
        [exposure(ids, purpose=purpose)],
    )
    assert snapshot["classification"] == "previously_exposed"
    assert snapshot["matching_purposes"] == [purpose]
    assert snapshot["independent_unseen_holdout_established"] is False


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2024-02-01", "2024-03-01", False),
        ("2024-03-10", "2024-03-11", False),
        ("2024-03-01", "2024-03-02", True),
        ("2024-03-09", "2024-03-11", True),
        ("2024-02-01", "2024-04-01", True),
    ],
)
def test_exposure_intervals_are_half_open(start, end, expected):
    ids = [identity(uuid4())]
    snapshot = classify_exposures(
        ids,
        DateWindow(start="2024-03-01", end="2024-03-10"),
        [exposure(ids, start, end)],
    )
    assert (snapshot["classification"] == "previously_exposed") is expected


def test_refresh_new_family_changed_universe_and_reset_do_not_hide_overlap():
    listing_id = uuid4()
    target = [identity(listing_id, "RENAMED"), identity(uuid4(), "OTHER")]
    # Matching is not partitioned by experiment/split/dataset or run status.
    histories = [
        exposure(
            [identity(listing_id, "OLD")],
            experiment_id=uuid4(),
            dataset_id=uuid4(),
            status="cancelled",
        ),
        exposure(
            [identity(uuid4(), "RENAMED")],
            experiment_id=uuid4(),
            dataset_id=uuid4(),
            status="failed",
        ),
    ]
    snapshot = classify_exposures(
        target, DateWindow(start="2024-03-01", end="2024-03-10"), histories
    )
    assert snapshot["matching_count"] == 2


def test_market_symbol_fallback_and_stable_listing_matching():
    listing_id = uuid4()
    assert identities_overlap(
        [identity(listing_id, "OLD")], [identity(listing_id, "NEW")]
    )
    assert identities_overlap([identity(uuid4(), "abc")], [identity(uuid4(), "ABC")])
    assert not identities_overlap(
        [identity(uuid4(), "ABC", "CN")], [identity(uuid4(), "ABC", "US")]
    )
    assert not identities_overlap(
        [identity(uuid4(), "ABC")], [identity(uuid4(), "OTHER")]
    )


def test_unknown_legacy_identity_is_conservative_market_wildcard():
    assert identities_overlap([identity(uuid4())], [identity(symbol=None)])
    assert not identities_overlap(
        [identity(uuid4(), market="US")], [identity(symbol=None)]
    )
    snapshot = classify_exposures(
        [identity(symbol=None)], DateWindow(start="2024-03-01", end="2024-03-10"), []
    )
    assert snapshot["classification"] == "unknown_identity"
    assert (
        snapshot["tracking_coverage"]
        == "historical tracking incomplete/external use unknown"
    )


def test_no_tracked_overlap_does_not_claim_clean_independence():
    snapshot = classify_exposures(
        [identity(uuid4())],
        DateWindow(start="2024-03-01", end="2024-03-10"),
        [],
        as_of=datetime(2026, 10, 22, tzinfo=UTC),
    )
    assert snapshot["classification"] == "no_tracked_overlap"
    assert snapshot["independent_unseen_holdout_established"] is False
    assert "external use unknown" in snapshot["tracking_coverage"]


def test_exposure_snapshot_bounds_evidence_without_losing_count():
    ids = [identity(uuid4())]
    snapshot = classify_exposures(
        ids,
        DateWindow(start="2024-03-01", end="2024-03-10"),
        [exposure(ids) for _ in range(1001)],
    )
    assert snapshot["matching_count"] == 1001
    assert len(snapshot["matching_exposure_ids"]) == 1000
    assert snapshot["evidence_truncated"]


def test_dataset_identity_capture_retains_refresh_fallback():
    listing_id = str(uuid4())
    dataset = SimpleNamespace(
        market="CN",
        manifest={
            "listings": [{"listing_id": listing_id}],
            "evaluator_symbol_mapping": {listing_id: "index"},
        },
    )
    assert dataset_identities(dataset) == [identity(listing_id)]
    dataset.manifest = {}
    assert dataset_identities(dataset) == [identity(symbol=None)]


def test_metadata_has_standalone_package_and_no_fake_attempt_binding():
    assert "attempt_id" not in StrategyPackage.__table__.columns
    assert "run_id" not in StrategyPackage.__table__.columns
    assert {"experiment_id", "split_version_id", "final_selection_id"} <= set(
        ResearchRun.__table__.columns.keys()
    )
    assert {"source_key", "identities", "start_date", "end_date", "purpose"} <= set(
        DataUsageExposure.__table__.columns.keys()
    )
    assert ExperimentCreate(name="new family").parent_experiment_id is None


def test_m5_migration_preserves_old_inputs_and_guards_immutable_evidence():
    root = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    migration = (root / "004_m5.py").read_text()
    sql = (root / "004_m5.sql").read_text()
    for name in (
        "experiments",
        "split_versions",
        "final_selections",
        "data_usage_exposures",
        "strategy_packages",
    ):
        assert f"CREATE TABLE {name}" in sql
    for term in (
        "BEFORE UPDATE OR DELETE",
        "append_only_data_exposure",
        "immutable_m5_run_binding",
        "protect_m5_owner_reference",
        "historical unbound rows",
        "portable_policy_image_digest",
        "immutable_universe_definition",
    ):
        assert term in migration
    assert "UPDATE research_runs SET config" not in migration


def test_final_routes_authentication_and_required_retry_replay_key():
    import secrets

    from fastapi.testclient import TestClient

    from backend.app.core.config import Settings
    from backend.app.main import create_app

    app = create_app(
        Settings(
            database_url="postgresql://localhost/not_used",
            secret_key=secrets.token_urlsafe(48),
        )
    )
    schema = app.openapi()
    paths = [
        "/api/v1/experiments",
        "/api/v1/split-versions",
        "/api/v1/final-selections",
        "/api/v1/final-evaluations",
        "/api/v1/data-usage-exposures",
    ]
    with TestClient(app) as client:
        for path in paths:
            assert path in schema["paths"]
            assert client.get(path).status_code == 401
        assert client.post("/api/v1/final-evaluations/preflight").status_code == 401
    retry = schema["paths"]["/api/v1/final-evaluations/{run_id}/retry"]["post"]
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in retry["parameters"]
    )
    query = schema["paths"][
        "/api/v1/final-evaluations/evaluations/{evaluation_id}/result"
    ]["get"]
    assert (
        next(
            parameter["schema"]["maximum"]
            for parameter in query["parameters"]
            if parameter["name"] == "max_points"
        )
        == 5000
    )


def test_exposure_public_dates_serialize_without_erasing_half_open_bounds():
    from backend.app.experiment_schemas import DataUsageExposurePublic

    item = DataUsageExposure(
        id=uuid4(),
        owner_id=uuid4(),
        experiment_id=None,
        split_version_id=None,
        run_id=None,
        dataset_version_id=None,
        source_key="synthetic-contract-only",
        identities=[identity(uuid4())],
        start_date=date(2024, 3, 1),
        end_date=date(2024, 3, 10),
        purpose="dataset_preview",
        provenance={},
        created_at=datetime.now(UTC),
    )
    value = DataUsageExposurePublic.model_validate(item).model_dump(mode="json")
    assert value["start_date"] == "2024-03-01" and value["end_date"] == "2024-03-10"
