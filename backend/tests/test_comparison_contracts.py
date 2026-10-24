"""Pure comparison DTO/security tests; not database or runtime admission evidence."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.app.comparison_schemas import ComparisonCreate, ComparisonSideCreate
from backend.app.comparison_services import _bounded_response, common_configuration
from backend.app.comparison_views import public_comparison_event, safe_error
from backend.app.services import DomainError
from backend.tests.research_view_fixtures import synthetic_result
from packages.research_trace.contracts import Contract
from packages.tradegrad_adapter.config import RunConfig


def side():
    return dict(  # noqa: C408 -- named fixture fields
        evaluation_id=uuid4(),
        execution_attempt_id=uuid4(),
        result_manifest_sha256="a" * 64,
        trace_manifest_sha256=None,
        source_kind="candidate",
        source_id=uuid4(),
        code_sha256="b" * 64,
    )


def test_trace_absence_requires_explicit_null_not_latest_default():
    value = side()
    assert ComparisonSideCreate.model_validate(value).trace_manifest_sha256 is None
    del value["trace_manifest_sha256"]
    with pytest.raises(ValidationError):
        ComparisonSideCreate.model_validate(value)


@pytest.mark.parametrize(
    "change",
    [
        {"spec_revision": 1},
        {"spec_sha256": "a" * 64},
        {"spec_revision": 0, "spec_sha256": "a" * 64},
        {"source_kind": "seed_fallback"},
        {"trace_manifest_sha256": "not-a-hash"},
        {"sql": "SELECT 1"},
    ],
)
def test_comparison_rejects_ambiguous_or_unbounded_input(change):
    with pytest.raises(ValidationError):
        ComparisonSideCreate.model_validate(side() | change)


@pytest.mark.parametrize("seconds", [0, 3601, True, 1.5, "300"])
def test_comparison_runtime_budget_is_fixed_and_strict(seconds):
    with pytest.raises(ValidationError):
        ComparisonCreate(
            left=side(), right=side(), options={"max_runtime_seconds": seconds}
        )


def test_request_hash_preserves_side_order_spec_absence_and_options():
    left, right = side(), side()
    value = ComparisonCreate(left=left, right=right)
    assert (
        value.sha256
        == ComparisonCreate.model_validate_json(value.model_dump_json()).sha256
    )
    assert value.sha256 != ComparisonCreate(left=right, right=left).sha256
    assert (
        value.sha256
        != ComparisonCreate(
            left=left, right=right, options={"max_runtime_seconds": 2}
        ).sha256
    )
    assert (
        value.sha256
        != ComparisonCreate(
            left=left | {"spec_revision": 1, "spec_sha256": "c" * 64}, right=right
        ).sha256
    )


def event(kind="comparison_artifact_ready", **payload):
    return SimpleNamespace(
        schema_version=1,
        run_id=uuid4(),
        attempt_id=uuid4(),
        seq=5,
        occurred_at=datetime.now(UTC),
        type=kind,
        payload=payload,
    )


def test_sse_strips_outcomes_source_text_arbitrary_errors_and_unknown_types():
    item = event(
        status="succeeded",
        generation=2,
        manifest_sha256="a" * 64,
        comparison_id=str(uuid4()),
        manifest_artifact_id=str(uuid4()),
        summary={"secret": "outcome"},
        source="full source",
        error_code="SQL credentials",
    )
    public = public_comparison_event(item)
    assert set(public.payload) == {
        "status",
        "generation",
        "manifest_sha256",
        "comparison_id",
        "manifest_artifact_id",
    }
    assert (
        public_comparison_event(
            event("untrusted-outcome-type", summary={"profit": 1})
        ).model_dump()["type"]
        == "comparison_status"
    )
    assert public_comparison_event(event("unknown", status="succeeded")).payload == {}
    assert safe_error("SQL credentials") == "comparison_failed"


@pytest.mark.parametrize(
    "payload",
    [
        {"status": {"result": 2}},
        {"generation": True},
        {"manifest_sha256": "x"},
        {"comparison_id": "not-a-uuid"},
        {"error_code": {"secret": 1}},
    ],
)
def test_sse_malformed_payload_is_not_evidence(payload):
    assert public_comparison_event(event(**payload)).payload == {}


def test_fixed_config_preserves_all_groups_and_does_not_fabricate_strategy_rng():
    result = synthetic_result()
    result.run.portable_policy_image_digest = None
    config = RunConfig.model_validate(result.run.config)
    value = common_configuration(
        result, config, SimpleNamespace(frozen_universe_sha256=None)
    )
    assert set(value) == {
        "data",
        "universe",
        "window",
        "warmup",
        "initial_state",
        "state_policy",
        "fees",
        "fill",
        "market",
        "RNG",
        "engine",
    }
    assert value["RNG"]["strategy_rng"] == {"status": "not_recorded", "value": None}
    assert "not_strategy" in value["RNG"]["optimizer_seed"]["scope"]
    assert (
        value["fill"]["resolved_execution_policy"]
        == result.bundle["engine"]["resolved_execution_policy"]
    )
    assert value["engine"]["research_image_digest"] == result.run.research_image_digest
    assert "total_from_fills" not in value["fees"]


def test_response_limit_counts_final_identity_envelope():
    class Response(Contract):
        body: str

    with pytest.raises(DomainError, match="byte budget"):
        _bounded_response(Response(body="x" * 100), 100)
    assert _bounded_response(Response(body="small"), 100).body == "small"


@pytest.mark.parametrize(
    "changed",
    [
        {"capture": None},
        {"trace_state": "historical_absence"},
        {"trace_coverage": "invented_complete"},
        {"trace_missing_reasons": ["invented_reason"]},
    ],
)
def test_publication_reverifies_complete_capture_scope_pins(
    tmp_path, monkeypatch, changed
):
    """Real comparison bytes, synthetic source pins; no DB admission claim."""
    import backend.app.comparison_services as services
    from packages.research_trace.comparison import compare_evaluations
    from packages.research_trace.comparison_contracts import (
        DEFAULT_LIMITS,
        ComparisonPublication,
        EventNamespace,
    )
    from tests.research_trace.test_comparison import make_input

    namespace = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256="a" * 64,
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256="b" * 64,
        instrument_namespace="native_symbol",
    )
    left, right = (
        make_input(tmp_path / "left", namespace),
        make_input(tmp_path / "right", namespace),
    )
    publication = ComparisonPublication(
        comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
    )
    manifest, _ = compare_evaluations(
        left, right, tmp_path / "output", publication=publication
    )
    item = SimpleNamespace(
        id=publication.comparison_id, engine_input_sha256=manifest.input_hash
    )
    run, attempt = (
        SimpleNamespace(id=publication.run_id),
        SimpleNamespace(id=publication.execution_attempt_id),
    )
    monkeypatch.setattr(
        services,
        "comparison_config",
        lambda *a: SimpleNamespace(
            limits=DEFAULT_LIMITS,
            algorithm_version=manifest.algorithm_version,
            tolerance_version=manifest.tolerance_version,
        ),
    )
    sides = (SimpleNamespace(input=left), SimpleNamespace(input=right))
    services._validate_manifest(manifest, item, run, attempt, sides)
    tampered = manifest.model_copy(
        update={"right": manifest.right.model_copy(update=changed)}
    )
    with pytest.raises(DomainError) as caught:
        services._validate_manifest(tampered, item, run, attempt, sides)
    assert caught.value.code == "comparison_integrity"
