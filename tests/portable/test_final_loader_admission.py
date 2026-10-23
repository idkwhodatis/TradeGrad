"""Actual native/portable file verification with explicit fake persistence only.

No PostgreSQL, socket, Docker or strategy execution occurs. Frozen-selection DB
resolution is replaced with an owned fixture; terminal, evaluation, admission,
storage checksum and result-schema validators are the production implementations.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_portable_final_exports import final_evidence

from backend.app import (
    final_evaluation_routes,
    final_evaluation_services,
    final_evaluation_views,
    portable_final_routes,
    portable_final_views,
    result_access,
    strategy_package_services,
)
from backend.app.models import (
    Artifact,
    DatasetVersion,
    DataUsageExposure,
    Evaluation,
    ExecutionAttempt,
    FinalSelection,
    ResearchRun,
    StrategyPackage,
    UniverseVersion,
)
from backend.app.services import DomainError
from backend.app.strategy_package_schemas import StrategyPackageCreate
from backend.tests.final_result_fixtures import (
    synthetic_final_admission,
    synthetic_final_result,
)


@pytest.fixture(params=["native", "portable"])
def harness(request, tmp_path, monkeypatch):
    portable = request.param == "portable"
    result = final_evidence() if portable else synthetic_final_result()
    views = portable_final_views if portable else final_evaluation_views
    routes = portable_final_routes if portable else final_evaluation_routes
    result.run.dataset_version_id = result.dataset.id
    result.evaluation.candidate_id = uuid4()
    result.evaluation.candidate_key = f"final:{result.run.final_selection_id}"
    result.evaluation.sandbox_id = f"tradegrad-portable-{result.evaluation.id}"
    attempt = SimpleNamespace(
        id=result.evaluation.attempt_id,
        run_id=result.run.id,
        generation=result.run.generation,
        status="succeeded",
        finished_at=result.evaluation.finished_at,
        sandbox_stopped=True,
        exit_code=0,
    )
    source = SimpleNamespace(
        id=uuid4(),
        owner_id=result.run.owner_id,
        config=result.run.config["source_config"],
    )
    selection = SimpleNamespace(
        id=result.run.final_selection_id,
        owner_id=result.run.owner_id,
        source_run_id=source.id,
        source_candidate_id=result.evaluation.candidate_id,
    )
    snapshot = {
        "classification": "no_tracked_overlap",
        "matching_exposure_ids": [],
        "matching_purposes": [],
        "matching_count": 0,
        "identities": [],
        "as_of": datetime.now(UTC).isoformat(),
    }
    admission = synthetic_final_admission(result, snapshot)
    universe_ref = source.config["universe"]
    universe = SimpleNamespace(
        id=UUID(universe_ref["id"]),
        owner_id=result.run.owner_id,
        content_sha256=universe_ref["sha256"],
    )
    objects = {
        ResearchRun: result.run,
        Evaluation: result.evaluation,
        ExecutionAttempt: attempt,
        DatasetVersion: result.dataset,
        UniverseVersion: universe,
    }
    calls = []

    class Session:
        admission_row = admission
        package_row = None

        def get(self, model, identifier):
            item = objects[model]
            return item if item.id == identifier else None

        def execute(self, query):
            params = query.compile().params
            owned = (
                result.run.owner_id in params.values()
                and result.evaluation.id in params.values()
            )
            return SimpleNamespace(
                one_or_none=lambda: (result.evaluation, result.run) if owned else None
            )

        def scalar(self, query):
            model = query.column_descriptions[0]["entity"]
            calls.append(model)
            return {
                DataUsageExposure: self.admission_row,
                Artifact: result.artifact,
                FinalSelection: selection,
                StrategyPackage: self.package_row,
                ResearchRun: source,
            }[model]

    session = Session()
    binding_module = portable_final_views if portable else final_evaluation_services
    monkeypatch.setattr(
        binding_module,
        "validate_final_binding",
        lambda *args: (selection, source, result.dataset, None, None),
    )
    monkeypatch.setattr(strategy_package_services, "advisory_lock", lambda *args: None)
    exposures = []
    for module in (result_access, routes, strategy_package_services):
        monkeypatch.setattr(
            module,
            "record_exposure",
            lambda session, **values: exposures.append(values),
        )
    path = tmp_path / result.artifact.storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.content)
    settings = SimpleNamespace(storage_root=tmp_path, max_artifact_bytes=8 * 1_048_576)
    return SimpleNamespace(
        result=result,
        views=views,
        routes=routes,
        session=session,
        settings=settings,
        admission=admission,
        exposures=exposures,
        calls=calls,
        path=path,
        selection=selection,
        source=source,
        attempt=attempt,
    )


def access(h, entrypoint, owner_id=None):
    owner_id = owner_id or h.result.run.owner_id
    if entrypoint == "generic":
        return result_access.load_result_aware_artifact(
            h.session, h.result.artifact, owner_id, h.settings
        )
    if entrypoint == "typed":
        return h.routes._result_access(
            h.session, h.result.evaluation.id, owner_id, h.settings, "result_download"
        )[0].content
    if entrypoint == "package":
        return strategy_package_services.create_strategy_package(
            h.session,
            owner_id,
            h.selection.id,
            StrategyPackageCreate(reference_evaluation_id=h.result.evaluation.id),
            h.settings,
        )
    return h.views.load_owned_final_result(
        h.session, h.result.evaluation.id, owner_id, h.settings
    ).content


@pytest.mark.parametrize("entrypoint", ["loader", "generic", "typed"])
def test_admitted_final_reads_validate_real_persisted_bytes(harness, entrypoint):
    h = harness
    assert access(h, entrypoint) == h.result.content
    assert h.calls.index(DataUsageExposure) < h.calls.index(Artifact)
    assert len(h.exposures) == (0 if entrypoint == "loader" else 1)


@pytest.mark.parametrize("entrypoint", ["loader", "generic", "typed", "package"])
@pytest.mark.parametrize(
    "receipt",
    [
        "missing",
        "wrong_generation",
        "wrong_owner",
        "wrong_run",
        "wrong_dataset",
        "wrong_purpose",
        "no_snapshot",
        "malformed_snapshot",
        "null_snapshot",
        "nonobject_provenance",
        "oversized_snapshot",
        "false_unseen_claim",
    ],
)
def test_missing_or_malformed_admission_rejects_all_final_paths_before_file_access(
    harness, entrypoint, receipt, monkeypatch
):
    h = harness
    # Establish that this is an otherwise valid, checksum-bound result, not a
    # fake loader that would reject unrelated malformed fixture bytes.
    assert access(h, "loader") == h.result.content
    h.calls.clear()
    if receipt == "missing":
        h.session.admission_row = None
    elif receipt == "wrong_generation":
        h.admission.source_key = (
            f"final:{h.result.run.id}:{h.result.evaluation.generation + 1}"
        )
    elif receipt in {"wrong_owner", "wrong_run", "wrong_dataset"}:
        setattr(
            h.admission,
            {
                "wrong_owner": "owner_id",
                "wrong_run": "run_id",
                "wrong_dataset": "dataset_version_id",
            }[receipt],
            uuid4(),
        )
    elif receipt == "wrong_purpose":
        h.admission.purpose = "result_preview"
    elif receipt == "no_snapshot":
        h.admission.provenance = {}
    elif receipt == "malformed_snapshot":
        h.admission.provenance["prior_exposure_snapshot"] = {
            "classification": "no_tracked_overlap"
        }
    elif receipt == "null_snapshot":
        h.admission.provenance["prior_exposure_snapshot"] = None
    elif receipt == "nonobject_provenance":
        h.admission.provenance = []
    elif receipt == "oversized_snapshot":
        h.admission.provenance["prior_exposure_snapshot"]["identities"] = [
            {"symbol": "x" * 256_001}
        ]
    else:
        h.admission.provenance["prior_exposure_snapshot"][
            "independent_unseen_holdout_established"
        ] = True
    monkeypatch.setattr(
        h.views, "read_verified", lambda *args: pytest.fail("Unadmitted bytes opened")
    )
    with pytest.raises(DomainError) as caught:
        access(h, entrypoint)
    assert caught.value.code == "admission_required"
    assert Artifact not in h.calls
    assert not h.exposures


@pytest.mark.parametrize("entrypoint", ["loader", "generic", "typed", "package"])
def test_admission_never_bypasses_real_checksum_verification(harness, entrypoint):
    h = harness
    h.path.write_bytes(h.result.content + b" ")
    with pytest.raises(DomainError) as caught:
        access(h, entrypoint)
    assert caught.value.code == "artifact_integrity"
    assert not h.exposures


@pytest.mark.parametrize("entrypoint", ["loader", "generic", "typed"])
def test_owner_checked_before_admission_or_files(harness, entrypoint, monkeypatch):
    h = harness
    monkeypatch.setattr(
        h.views, "read_verified", lambda *args: pytest.fail("Foreign bytes opened")
    )
    with pytest.raises(DomainError) as caught:
        access(h, entrypoint, uuid4())
    assert caught.value.code == "not_found"
    assert DataUsageExposure not in h.calls and Artifact not in h.calls
    assert not h.exposures


@pytest.mark.parametrize("entrypoint", ["loader", "generic", "typed", "package"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", 999),
        ("generation", True),
        ("selection_id", str(uuid4())),
        ("final_config_sha256", "f" * 64),
        ("requested_window", {"start": "2020-01-01", "end": "2020-02-01"}),
        ("admission_before_dispatch", False),
        ("admission_before_dispatch", 1),
        ("coverage", "holdout_only"),
        ("tracking_coverage", "independently_unseen"),
        ("experiment_id", uuid4()),
        ("split_version_id", uuid4()),
        ("start_date", datetime(2024, 2, 2).date()),
        ("end_date", datetime(2024, 2, 3).date()),
        ("identities", []),
        ("identities", [{"listing_id": None, "market": "OTHER", "symbol": None}]),
    ],
)
def test_admission_receipt_is_bound_to_actual_recorded_inputs_before_files(
    harness, entrypoint, field, value, monkeypatch
):
    h = harness
    assert access(h, "loader") == h.result.content
    h.calls.clear()
    if field in {
        "experiment_id",
        "split_version_id",
        "start_date",
        "end_date",
        "identities",
    }:
        setattr(h.admission, field, value)
    else:
        h.admission.provenance[field] = value
    monkeypatch.setattr(
        h.views,
        "read_verified",
        lambda *args: pytest.fail("Corrupt admission opened bytes"),
    )
    with pytest.raises(DomainError) as caught:
        access(h, entrypoint)
    assert caught.value.code == "admission_required"
    assert Artifact not in h.calls and not h.exposures


def test_gate_accepts_the_real_admission_writers_exact_record(harness, monkeypatch):
    """Exercise the writer and record_exposure, not merely a hand-built receipt."""
    from backend.app import experiment_services, portable_final_services
    from backend.app.final_result_admission import require_final_result_admission

    h = harness
    writer = (
        portable_final_services
        if h.result.run.config["schema_version"]
        == "tradegrad.portable.final-evaluation.v1"
        else final_evaluation_services
    )
    snapshot = h.admission.provenance["prior_exposure_snapshot"]
    monkeypatch.setattr(writer, "get_exposure_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(experiment_services, "advisory_lock", lambda *args: None)

    class Persistence:
        row = None

        def scalar(self, query):
            return self.row

        def add(self, row):
            self.row = row

        def flush(self):
            pass

    session = Persistence()
    writer.record_final_admission(
        session, h.result.run, h.selection, None, h.result.dataset
    )
    for key, value in vars(h.admission).items():
        assert getattr(session.row, key) == value
    accepted = require_final_result_admission(
        session,
        h.result.run,
        h.result.evaluation,
        h.result.run.owner_id,
        dataset=h.result.dataset,
    )
    assert accepted["classification"] == snapshot["classification"]
