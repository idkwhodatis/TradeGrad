"""Pure algorithm pin checks and a stubbed-persistence worker seam, not a PG gate."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.comparison_schemas import ComparisonConfig, ComparisonCreate
from backend.app.comparison_services import comparison_config, input_snapshot
from backend.app.services import DomainError
from backend.tests import test_comparison_worker as worker_seam
from backend.tests.test_comparison_contracts import side
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    LEGACY_ALGORITHM_VERSION,
    EventNamespace,
)
from packages.research_trace.contracts import digest
from tests.research_trace.test_comparison import make_input
from workers.research import comparison as worker

harness = worker_seam.harness


@pytest.fixture
def identities(tmp_path):
    namespace = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256="a" * 64,
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256="b" * 64,
        instrument_namespace="native_symbol",
    )
    sides = tuple(
        SimpleNamespace(input=make_input(tmp_path / name, namespace, no_trace=True))
        for name in ("left", "right")
    )
    return uuid4(), ComparisonCreate(left=side(), right=side()), sides


def objects(identities, algorithm_version):
    owner_id, body, sides = identities
    snapshot = input_snapshot(
        owner_id, body, sides, algorithm_version=algorithm_version
    )
    identifier, run_id = uuid4(), uuid4()
    config = ComparisonConfig(
        comparison_id=identifier,
        input_sha256=digest(snapshot),
        engine_input_sha256=snapshot["engine_input_sha256"],
        request=body,
        algorithm_version=algorithm_version,
    )
    run = SimpleNamespace(
        id=run_id,
        owner_id=owner_id,
        config=config.model_dump(mode="json"),
        request_sha256=config.sha256,
        result=None,
    )
    item = SimpleNamespace(
        id=identifier,
        owner_id=owner_id,
        run_id=run_id,
        input_sha256=digest(snapshot),
        engine_input_sha256=config.engine_input_sha256,
        input_snapshot=snapshot,
        left_evaluation_id=body.left.evaluation_id,
        right_evaluation_id=body.right.evaluation_id,
    )
    return config, run, item


def test_new_and_saved_snapshot_hashes_remain_version_separated(identities):
    owner_id, body, sides = identities
    old = input_snapshot(
        owner_id, body, sides, algorithm_version=LEGACY_ALGORITHM_VERSION
    )
    new = input_snapshot(owner_id, body, sides)
    assert new["algorithm_version"] == ALGORITHM_VERSION
    assert old["request_sha256"] == new["request_sha256"] == body.sha256
    assert old["engine_input_sha256"] != new["engine_input_sha256"]
    assert digest(old) != digest(new)
    assert old == input_snapshot(
        owner_id, body, sides, algorithm_version=LEGACY_ALGORITHM_VERSION
    )


@pytest.mark.parametrize("version", [LEGACY_ALGORITHM_VERSION, ALGORITHM_VERSION])
def test_saved_config_validates_without_rewriting(identities, version):
    config, run, item = objects(identities, version)
    before = digest(run.config), digest(item.input_snapshot)
    assert comparison_config(run, item) == config
    assert before == (digest(run.config), digest(item.input_snapshot))


@pytest.mark.parametrize(
    "field,value",
    [
        ("algorithm_version", LEGACY_ALGORITHM_VERSION),
        ("tolerance_version", "unknown"),
        ("engine_input_sha256", "0" * 64),
        ("request_sha256", "0" * 64),
        ("limits", {}),
    ],
)
def test_snapshot_declaration_cannot_disagree_with_hashed_config(
    identities, field, value
):
    config, run, item = objects(identities, ALGORITHM_VERSION)
    item.input_snapshot = item.input_snapshot | {field: value}
    item.input_sha256 = digest(item.input_snapshot)
    config = config.model_copy(update={"input_sha256": item.input_sha256})
    run.config, run.request_sha256 = config.model_dump(mode="json"), config.sha256
    with pytest.raises(DomainError) as caught:
        comparison_config(run, item)
    assert caught.value.code == "comparison_integrity"


def test_unknown_config_algorithm_is_not_supported(identities):
    config, _, _ = objects(identities, ALGORITHM_VERSION)
    with pytest.raises(ValidationError):
        ComparisonConfig.model_validate(
            config.model_dump() | {"algorithm_version": "v999"}
        )


def test_queued_legacy_worker_fails_before_source_reads_or_queries(
    harness, monkeypatch
):
    get_config = worker.comparison_config
    monkeypatch.setattr(
        worker,
        "comparison_config",
        lambda *a: get_config(*a).model_copy(
            update={"algorithm_version": LEGACY_ALGORITHM_VERSION}
        ),
    )
    finish = worker.finish_comparison_attempt
    errors = []

    def capture(*args, **kwargs):
        errors.append(kwargs["error_code"])
        return finish(*args, **kwargs)

    monkeypatch.setattr(worker, "finish_comparison_attempt", capture)
    assert harness.invoke() == {"status": "failed", "query_stopped": True}
    assert errors == ["comparison_algorithm_unsupported"]
    assert "source_reads" not in harness.record and "query_closed" not in harness.record
    assert harness.state.control is None
    assert harness.record[-2:] == ["commit", "lease_close"]


@pytest.mark.parametrize("field", ["algorithm_version", "tolerance_version"])
def test_publication_and_result_reads_reject_config_manifest_version_disagreement(
    identities, tmp_path, monkeypatch, field
):
    import backend.app.comparison_services as services
    from packages.research_trace.comparison import compare_evaluations
    from packages.research_trace.comparison_contracts import ComparisonPublication

    _, _, sides = identities
    publication = ComparisonPublication(
        comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
    )
    manifest, _ = compare_evaluations(
        sides[0].input, sides[1].input, tmp_path / "result", publication=publication
    )
    expected = SimpleNamespace(
        limits=manifest.limits,
        algorithm_version=manifest.algorithm_version,
        tolerance_version=manifest.tolerance_version,
    )
    monkeypatch.setattr(services, "comparison_config", lambda *a: expected)
    item = SimpleNamespace(
        id=publication.comparison_id, engine_input_sha256=manifest.input_hash
    )
    run = SimpleNamespace(id=publication.run_id)
    attempt = SimpleNamespace(id=publication.execution_attempt_id)
    services._validate_manifest(manifest, item, run, attempt, sides)
    setattr(
        expected,
        field,
        LEGACY_ALGORITHM_VERSION if field == "algorithm_version" else "unknown",
    )
    with pytest.raises(DomainError) as caught:
        services._validate_manifest(manifest, item, run, attempt, sides)
    assert caught.value.code == "comparison_integrity"
