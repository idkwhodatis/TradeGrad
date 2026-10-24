"""Pure cold-drill fixture checks, NOT PostgreSQL or completed-result evidence."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.comparison_schemas import ComparisonConfig, ComparisonCreate
from backend.app.comparison_services import comparison_config
from backend.app.services import DomainError
from backend.tests.test_comparison_contracts import side
from packages.research_trace.comparison_contracts import (
    ALGORITHM_VERSION,
    DEFAULT_LIMITS,
    LEGACY_ALGORITHM_VERSION,
    TOLERANCE_VERSION,
)
from packages.research_trace.contracts import canonical_bytes, digest
from tests.integration.test_backup_restore import _comparison_boundary_records


@pytest.fixture
def boundary():
    source = SimpleNamespace(
        owner_id=uuid4(), dataset_version_id=uuid4(), strategy_version_id=uuid4()
    )
    body = ComparisonCreate(left=side(), right=side())
    return _comparison_boundary_records(
        source, body, "a" * 64, datetime(2024, 4, 2, tzinfo=UTC)
    )


def test_cancelled_drill_fixture_is_canonical_and_remains_explicitly_legacy(boundary):
    config, run, item = boundary
    before = canonical_bytes(run.config), canonical_bytes(item.input_snapshot)
    assert comparison_config(run, item) == config
    assert before == (canonical_bytes(run.config), canonical_bytes(item.input_snapshot))
    assert config.algorithm_version == LEGACY_ALGORITHM_VERSION != ALGORITHM_VERSION
    assert item.input_snapshot["schema_version"] == "tradegrad.comparison-input.v1"
    assert item.input_snapshot["algorithm_version"] == LEGACY_ALGORITHM_VERSION
    assert item.input_snapshot["tolerance_version"] == TOLERANCE_VERSION
    assert item.input_snapshot["limits"] == DEFAULT_LIMITS.model_dump(mode="json")
    assert item.input_snapshot["synthetic"] is True
    assert item.input_snapshot["engine_executed"] is False
    assert item.input_snapshot["result_available"] is False
    assert run.status == "cancelled" and run.result is None
    assert item.published_attempt_id is None
    assert item.manifest_artifact_id is None and item.manifest_sha256 is None


def _rehash(run, item):
    """Keep outer hashes consistent so failures exercise the cross-field pins."""
    item.input_sha256 = digest(item.input_snapshot)
    run.config = run.config | {"input_sha256": item.input_sha256}
    run.request_sha256 = ComparisonConfig.model_validate(run.config).sha256


@pytest.mark.parametrize("target", ["config", "snapshot"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("algorithm_version", ALGORITHM_VERSION),
        ("limits", DEFAULT_LIMITS.model_dump(mode="json") | {"max_events": 10}),
    ],
)
def test_legacy_fixture_rejects_version_or_limit_mismatch_after_rehash(
    boundary, target, field, value
):
    _, run, item = boundary
    if target == "config":
        run.config = run.config | {field: value}
    else:
        item.input_snapshot = item.input_snapshot | {field: value}
    _rehash(run, item)
    with pytest.raises(DomainError) as caught:
        comparison_config(run, item)
    assert caught.value.code == "comparison_integrity"


@pytest.mark.parametrize("field", ["algorithm_version", "tolerance_version", "limits"])
def test_legacy_fixture_rejects_missing_snapshot_pins_after_rehash(boundary, field):
    _, run, item = boundary
    item.input_snapshot = dict(item.input_snapshot)
    del item.input_snapshot[field]
    _rehash(run, item)
    with pytest.raises(DomainError) as caught:
        comparison_config(run, item)
    assert caught.value.code == "comparison_integrity"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "tradegrad.synthetic-comparison-backup-boundary.v1"),
        ("owner_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_synthetic_marker_does_not_bypass_snapshot_identity_guards(
    boundary, field, value
):
    _, run, item = boundary
    item.input_snapshot = item.input_snapshot | {field: value}
    _rehash(run, item)
    with pytest.raises(DomainError) as caught:
        comparison_config(run, item)
    assert caught.value.code == "comparison_integrity"
