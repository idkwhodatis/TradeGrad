"""Actual paired native backtests, persisted traces and production comparison.

Only the maintained template and one fixed comment-only derivative run. Each
side is a fresh AKQuant execution with its own engine IDs and full path state.
Synthetic business identities are query fixtures, not database/HTTP admission.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from native_comparison_fixture import run_native

from packages.research_trace.comparison_contracts import (
    ComparisonInput,
    ComparisonPublication,
    EventNamespace,
)
from packages.research_trace.contracts import (
    CaptureConfig,
    DateRange,
    DecisionRecord,
    EvaluationBinding,
    digest,
)
from packages.research_trace.samples import CROSS_SECTION_TAPE
from packages.research_trace.storage import write_trace_artifacts
from packages.research_trace.templates import native_contract, template_spec
from packages.research_trace.transport import CaptureReceiver, iter_capture_records


def stored_input(path, observed, namespace, *, parameters=None, capture=None):
    config = CaptureConfig.model_validate(capture or {"mode": "research"})
    path.mkdir()
    receiver = CaptureReceiver(path, config, source="strategy_emitted")
    for frame in observed["frames"]:
        receiver.accept(frame)
    envelope = receiver.result()
    records = [
        DecisionRecord.model_validate(row)
        for row in iter_capture_records(
            envelope["research_capture_path"],
            envelope["research_capture_sha256"],
            envelope["research_capture_size_bytes"],
            config,
        )
    ]
    engine = observed["bundle"]["engine"]
    common = {
        "data": {"snapshot_sha256": namespace.dataset_sha256},
        "universe": {"effective_membership_sha256": namespace.frozen_universe_sha256},
        "window": {"start": "2024-01-02", "end": "2024-01-12"},
        "warmup": {"engine_history_depth": 252, "strategy_lookback": 3},
        "initial_state": {"cash": 10000.0, "positions": "fresh_engine"},
        "state_policy": "fresh_full_window_reconstruction",
        "fees": {"commission_rate": 0, "stamp_tax_rate": 0},
        "fill": engine["resolved_execution_policy"],
        "market": {"timezone": "America/New_York", "currency": "USD", "lot_size": 1},
        "RNG": {
            "status": "not_recorded",
            "reason": "No historical strategy RNG declaration",
        },
        "engine": {
            "dependencies": engine["dependencies"],
            "resolved_config": engine["resolved_config"],
        },
    }
    source_id = uuid4()
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=source_id,
        code_sha256=observed["code_sha256"],
        dataset_version_id=namespace.dataset_version_id,
        dataset_sha256=namespace.dataset_sha256,
        universe_version_id=None,
        universe_sha256=None,
        execution_config_sha256=digest(
            {"common": common, "parameters": parameters or {}}
        ),
        engine_dependencies_sha256=digest(engine["dependencies"]),
        result_manifest_sha256=digest(observed["bundle"]),
    )
    manifest, artifacts = write_trace_artifacts(
        records, binding, envelope["research_capture"], path / "parquet"
    )
    manifest_ref = next(row for row in artifacts if row.name.endswith(".json"))
    spec = template_spec(
        "cross_sectional",
        source=observed["source"],
        parameters=native_contract(
            "cross_sectional", parameters=parameters
        ).parameter_values(),
    )
    return ComparisonInput(
        binding=binding,
        source_kind="strategy_version",
        source_id=source_id,
        source=observed["source"],
        window=DateRange(start="2024-01-02", end="2024-01-12"),
        spec=spec,
        spec_sha256=digest(spec),
        trace_manifest=manifest,
        trace_manifest_sha256=manifest_ref.sha256,
        trace_paths={row.name: row.path for row in artifacts},
        common_config=common,
        economic_bundle=observed["bundle"],
        namespace=namespace,
    )


@pytest.fixture(scope="module")
def actual_pairs(tmp_path_factory):
    path = tmp_path_factory.mktemp("actual-native-comparisons")
    namespace = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256=digest(
            {
                "sample": CROSS_SECTION_TAPE.sample_id,
                "dates": CROSS_SECTION_TAPE.dates,
                "bars": [
                    (item.instrument_id, item.closes, item.volumes)
                    for item in CROSS_SECTION_TAPE.instruments
                ],
            }
        ),
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256=digest(CROSS_SECTION_TAPE.universe),
        instrument_namespace="native_symbol",
    )
    rows = {}
    for name, parameters, capture, comment in (
        ("baseline", {}, {}, False),
        ("repeated", {}, {}, False),
        ("no_liquidity", {"liquidity_enabled": False}, {}, False),
        ("comment", {}, {}, True),
        ("narrow", {}, {"mode": "research", "instrument_ids": ["B"]}, False),
    ):
        observed = run_native(
            parameters=parameters, capture=capture, comment_only=comment
        )
        rows[name] = stored_input(
            path / name, observed, namespace, parameters=parameters, capture=capture
        )
    return rows


def compare(tmp_path, left, right):
    from packages.research_trace.comparison import compare_evaluations
    from packages.research_trace.comparison_storage import (
        ComparisonControl,
        ComparisonReader,
    )

    publication = ComparisonPublication(
        comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
    )
    control = ComparisonControl()
    try:
        manifest, artifacts = compare_evaluations(
            left,
            right,
            tmp_path,
            publication=publication,
            control=control,
        )
    finally:
        control.close()
    return manifest, ComparisonReader(
        manifest, {row.name: row.path for row in artifacts}
    )


def test_actual_repeated_runs_keep_source_events_not_random_record_ids(
    actual_pairs, tmp_path
):
    left, right = actual_pairs["baseline"], actual_pairs["repeated"]
    assert left.binding.evaluation_id != right.binding.evaluation_id
    assert (
        left.economic_bundle["orders"]["rows"]
        != right.economic_bundle["orders"]["rows"]
    )
    manifest, reader = compare(tmp_path, left, right)
    assert manifest.namespace_status == "common"
    events = reader.page("events").items
    decisions = [
        row
        for row in events
        if row.left and row.right and row.left["callback_phase"] == "on_cross_section"
    ]
    assert len(decisions) == 8
    assert all(row.status == "equal" for row in decisions)
    unpaired = [
        row
        for row in events
        if row.left_state == "unpaired" or row.right_state == "unpaired"
    ]
    # Only require generic order identity to stay unpaired when the engine
    # actually emitted it. This fixture can retain explicit origins for all
    # six final execution snapshots despite other capture coverage warnings.
    assert all(
        (row.left or row.right)["data_event_id"].startswith("native_order:")
        for row in unpaired
    )
    assert all(row.left["decision_id"] != row.right["decision_id"] for row in decisions)
    assert manifest.sections["candidates"].changed == 0
    assert manifest.sections["fills"].changed == 0
    assert manifest.sections["orders"].changed == 0
    assert manifest.diagnostic_parity_certified is False
    assert reader.page("events").total > 8


def test_actual_one_mechanism_change_preserves_path_different_prestates(
    actual_pairs, tmp_path
):
    manifest, reader = compare(
        tmp_path, actual_pairs["baseline"], actual_pairs["no_liquidity"]
    )
    assert manifest.observed_behavior == "changed"
    assert manifest.sections["candidates"].changed > 0
    assert manifest.sections["positions"].observed_equality == "different"
    assert manifest.causal_claim is manifest.local_counterfactual is False
    events = reader.page("events").items
    assert any(
        row.left
        and row.right
        and row.left.get("pre_state") != row.right.get("pre_state")
        for row in events
    )
    candidates = reader.page("candidates", instrument_id="C").items
    assert any(row.status == "changed" for row in candidates)


def test_actual_comment_only_change_has_explicit_observed_behavior_equality(
    actual_pairs, tmp_path
):
    manifest, _ = compare(tmp_path, actual_pairs["baseline"], actual_pairs["comment"])
    assert manifest.code_changed is True
    # Known engine-only events remain unpaired; the global result must not
    # overstate the exact unchanged strategy decisions as full replay parity.
    assert manifest.observed_behavior in {"unchanged_observed", "insufficient_coverage"}
    assert {"candidates", "targets"} <= set(manifest.observed_equal_sections)
    assert manifest.diagnostic_parity_certified is False
    assert all(
        row.mapping != "controlled"
        for row in manifest.mechanism_changes
        if row.status != "equal"
    )


def test_actual_narrow_capture_never_claims_whole_universe_unchanged(
    actual_pairs, tmp_path
):
    manifest, _ = compare(tmp_path, actual_pairs["baseline"], actual_pairs["narrow"])
    assert manifest.sections["candidates"].right_availability == "partial"
    assert manifest.sections["candidates"].observed_equality == "unknown"
    assert "candidates" not in manifest.observed_equal_sections
    assert manifest.diagnostic_parity_certified is False
