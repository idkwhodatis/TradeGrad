"""Pure fake-session routing/access contracts, not PostgreSQL acceptance."""

from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app import result_access
from backend.app.experiment_services import classify_exposures, dataset_identities
from backend.app.models import DatasetVersion, Evaluation, ResearchRun, UniverseVersion
from backend.app.services import DomainError
from backend.tests.final_result_fixtures import synthetic_final_result
from backend.tests.research_view_fixtures import synthetic_result
from packages.tradegrad_adapter.config import DateWindow


@pytest.fixture
def access_context(monkeypatch):
    result = synthetic_result()
    result.dataset.start_date = date(2024, 1, 1)
    result.dataset.end_date = date(2024, 12, 31)
    result.dataset.market = "CN"
    result.run.generation = 1
    config = result.run.config
    universe = SimpleNamespace(
        id=UUID(config["universe"]["id"]),
        owner_id=result.run.owner_id,
        content_sha256=config["universe"]["sha256"],
        members=[],
    )
    objects = {
        DatasetVersion: result.dataset,
        UniverseVersion: universe,
        ResearchRun: result.run,
        Evaluation: result.evaluation,
    }
    calls = []
    session = SimpleNamespace(get=lambda model, identifier: objects[model])
    monkeypatch.setattr(
        result_access, "record_exposure", lambda session, **kwargs: calls.append(kwargs)
    )
    return result, universe, session, calls


def test_exact_window_dataset_universe_and_repeated_identity(access_context):
    result, universe, session, calls = access_context
    for _ in range(2):
        result_access.record_verified_result_access(
            session, result, result.run.owner_id
        )
    assert calls[0] == calls[1]
    row = calls[0]
    assert row["dataset"] is result.dataset and row["universe"] is universe
    assert row["window"].model_dump(mode="json") == {
        "start": result.evaluation.window["start"],
        "end": result.evaluation.window["end"],
    }
    assert row["provenance"]["universe"] == result.run.config["universe"]
    assert row["purpose"] == "result_preview"


def test_unknown_legacy_window_never_invents_clean_exposure(access_context):
    result, _, session, calls = access_context
    result.run.config = {}
    result_access.record_result_access(session, result.run, result.run.owner_id)
    row = calls[0]
    assert row["window"] == DateWindow(
        start=result.dataset.start_date, end=result.dataset.end_date
    )
    assert "unknown_legacy" in row["provenance"]["coverage"]
    identities = dataset_identities(result.dataset)
    exposure = SimpleNamespace(
        id=uuid4(),
        start_date=row["window"].start,
        end_date=row["window"].end,
        identities=identities,
        purpose=row["purpose"],
    )
    snapshot = classify_exposures(
        [{"listing_id": str(uuid4()), "market": "CN", "symbol": "SOME_SYMBOL"}],
        DateWindow(start="2024-02-01", end="2024-03-01"),
        [exposure],
    )
    assert snapshot["classification"] == "previously_exposed"


def test_legacy_known_window_and_optimization_summary_do_not_include_metadata_holdout(
    access_context,
):
    result, _, session, calls = access_context
    result.run.config["windows"]["holdout"] = {
        "start": "2024-04-01",
        "end": "2024-05-01",
    }
    result_access.record_result_access(session, result.run, result.run.owner_id)
    assert len(calls) == 2
    assert all(row["window"].end < date(2024, 4, 1) for row in calls)
    calls.clear()
    result.run.config = {"start_date": "2024-02-01", "end_date": "2024-03-01"}
    result_access.record_result_access(session, result.run, result.run.owner_id)
    assert calls[0]["window"] == DateWindow(start="2024-02-01", end="2024-03-01")


@pytest.mark.parametrize(
    "mutation",
    ["owner", "dataset_owner", "dataset_hash", "universe_owner", "universe_hash"],
)
def test_unauthorized_or_corrupt_context_records_nothing(access_context, mutation):
    result, universe, session, calls = access_context
    owner = result.run.owner_id
    if mutation == "owner":
        owner = uuid4()
    elif mutation == "dataset_owner":
        result.dataset.owner_id = uuid4()
    elif mutation == "dataset_hash":
        result.dataset.content_sha256 = "f" * 64
    elif mutation == "universe_owner":
        universe.owner_id = uuid4()
    else:
        universe.content_sha256 = "f" * 64
    with pytest.raises(DomainError):
        result_access.record_verified_result_access(session, result, owner)
    assert not calls


@pytest.mark.parametrize(
    "endpoint",
    ["query", "compare", "download", "report", "portable-query", "portable-download"],
)
def test_every_typed_result_response_records_and_commits_before_return(
    monkeypatch, endpoint
):
    from backend.app import portable_routes, research_routes

    result = synthetic_result()
    other = synthetic_result(owner_id=result.run.owner_id)
    seen = []
    session = SimpleNamespace(commit=lambda: seen.append("commit"))
    user = SimpleNamespace(id=result.run.owner_id)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))

    def loaded(session, identifier, owner, settings):
        seen.append("load")
        return result if identifier == result.evaluation.id else other

    def recorded(session, value, owner, **kwargs):
        seen.append(
            ("access", value.evaluation.id, kwargs.get("purpose", "result_preview"))
        )

    monkeypatch.setattr(research_routes, "load_owned_result", loaded)
    monkeypatch.setattr(portable_routes, "load_owned_portable_result", loaded)
    for module in (research_routes, portable_routes):
        monkeypatch.setattr(module, "record_verified_result_access", recorded)
    monkeypatch.setattr(research_routes, "result_view", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        portable_routes, "portable_result_view", lambda *a, **k: {"ok": True}
    )
    if endpoint == "query":
        research_routes.read_research_results(
            result.evaluation.id, user, session, request
        )
    elif endpoint == "compare":
        research_routes.compare_research_results(
            SimpleNamespace(evaluation_ids=[result.evaluation.id, other.evaluation.id]),
            user,
            session,
            request,
        )
    elif endpoint == "download":
        research_routes.download_research_results(
            result.evaluation.id, user, session, request
        )
    elif endpoint == "report":
        research_routes.download_research_report(
            result.evaluation.id, user, session, request
        )
    elif endpoint == "portable-query":
        portable_routes.results(result.evaluation.id, user, session, request)
    else:
        portable_routes.download(result.evaluation.id, user, session, request)
    assert seen[-1] == "commit"
    records = [entry for entry in seen if isinstance(entry, tuple)]
    assert len(records) == (2 if endpoint == "compare" else 1)
    assert {entry[1] for entry in records} == (
        {result.evaluation.id, other.evaluation.id}
        if endpoint == "compare"
        else {result.evaluation.id}
    )


def test_failed_exposure_commit_prevents_result_return(monkeypatch):
    from backend.app import research_routes

    result = synthetic_result()
    monkeypatch.setattr(research_routes, "load_owned_result", lambda *args: result)
    monkeypatch.setattr(
        research_routes, "record_verified_result_access", lambda *args, **kwargs: None
    )

    def fail_commit():
        raise DomainError("storage_unavailable", "Exposure commit failed", 503)

    session = SimpleNamespace(commit=fail_commit)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))
    with pytest.raises(DomainError, match="Exposure commit failed"):
        research_routes.download_research_results(
            result.evaluation.id,
            SimpleNamespace(id=result.run.owner_id),
            session,
            request,
        )


def test_generic_final_artifact_uses_terminal_fence_not_raw_file(monkeypatch):
    from backend.app import final_evaluation_views, storage

    result = synthetic_final_result()
    result.run.status = "cancelled"
    session = SimpleNamespace(
        get=lambda model, identifier: (
            result.run if model is ResearchRun else result.evaluation
        )
    )

    def checked(*args):
        final_evaluation_views.validate_final_persisted_result(
            result.evaluation,
            result.run,
            result.dataset,
            result.artifact,
            result.content,
        )
        return result

    monkeypatch.setattr(final_evaluation_views, "load_owned_final_result", checked)
    monkeypatch.setattr(
        storage,
        "read_verified",
        lambda *args: pytest.fail("Generic raw final download bypass"),
    )
    with pytest.raises(DomainError, match="durably committed"):
        result_access.load_result_aware_artifact(
            session, result.artifact, result.run.owner_id, None
        )


def test_generic_final_metrics_artifact_fails_closed_before_raw_file(monkeypatch):
    from backend.app import storage

    result = synthetic_final_result()
    result.artifact.name = f"evaluation-{result.evaluation.id}-metrics.json"
    session = SimpleNamespace(
        get=lambda model, identifier: (
            result.run if model is ResearchRun else result.evaluation
        )
    )
    monkeypatch.setattr(
        storage,
        "read_verified",
        lambda *args: pytest.fail("Alternate final metrics exposed"),
    )
    with pytest.raises(DomainError, match="verified final result"):
        result_access.load_result_aware_artifact(
            session, result.artifact, result.run.owner_id, None
        )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("logs.txt", 1),
        ("metrics.json", 1),
    ],
)
def test_generic_artifact_diagnostic_and_metric_disclosure(
    access_context, monkeypatch, name, expected
):
    from backend.app import storage

    result, _, session, calls = access_context
    result.artifact.name = name
    monkeypatch.setattr(storage, "read_verified", lambda *args: b"checked bytes")
    settings = SimpleNamespace(storage_root=None, max_artifact_bytes=10000)
    assert (
        result_access.load_result_aware_artifact(
            session, result.artifact, result.run.owner_id, settings
        )
        == b"checked bytes"
    )
    assert len(calls) == (2 if name == "metrics.json" else expected)
    if name == "logs.txt":
        assert "diagnostic" in calls[0]["provenance"]["coverage"]


def test_pure_status_is_not_outcome_exposure():
    assert not result_access.contains_result_data({"status": "running", "result": None})
    assert not result_access.contains_result_data(
        {"metrics": {}, "combined_score": None}
    )
    assert result_access.contains_result_data({"outcome": {"metrics": {"return": 0}}})


def test_generic_unauthorized_or_corrupt_artifact_does_not_record_access(
    access_context, monkeypatch
):
    from backend.app import storage

    result, _, session, calls = access_context
    result.artifact.name = "metrics.json"
    settings = SimpleNamespace(storage_root=None, max_artifact_bytes=10000)

    def reject_read(*args):
        raise DomainError("artifact_integrity", "Bytes failed verification", 409)

    monkeypatch.setattr(storage, "read_verified", reject_read)
    with pytest.raises(DomainError, match="Artifact not found"):
        result_access.load_result_aware_artifact(
            session, result.artifact, uuid4(), settings
        )
    with pytest.raises(DomainError, match="Bytes failed verification"):
        result_access.load_result_aware_artifact(
            session, result.artifact, result.run.owner_id, settings
        )
    assert not calls
