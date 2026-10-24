"""Original/replay truth, with real maintained AKQuant execution when required."""

from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.research_trace.contracts import EvaluationBinding, digest
from packages.research_trace.diagnostic_parity import (
    _targets,
    compare_diagnostic,
)
from packages.research_trace.diagnostic_parity_contracts import (
    REQUIRED_CHECKS,
    DiagnosticParityReport,
    DiagnosticParitySide,
    ParityInput,
)
from packages.research_trace.templates import template_source
from packages.tradegrad_adapter.runtime_observation import digest as runtime_digest
from tests.research_trace.test_independent_native_results import native


def inputs(value, shared, *, name="cross_sectional"):
    bundle = value["bundle"]
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=uuid4(),
        code_sha256=bundle["identity"]["strategy_sha256"],
        execution_config_sha256="a" * 64,
        result_manifest_sha256=digest(bundle),
        **shared,
    )
    return ParityInput(
        pins=DiagnosticParitySide(
            binding=binding,
            source_kind="strategy_version",
            source_id=binding.strategy_version_id,
            spec_revision=None,
            spec_sha256=None,
            trace_manifest_sha256="5" * 64,
            runtime_observation_sha256=runtime_digest(value["observation"]),
        ),
        source_code=template_source(name),
        bundle=bundle,
        runtime_observation=value["observation"],
        fixed_conditions={
            "fixture_conditions": "genuine_engine_synthetic_business_bindings_not_DB_admission"
        },
        decisions=tuple(value["records"]),
        capture={"mode": "research"},
    )


@pytest.fixture(scope="module")
def actual_pair():
    source = native(name="cross_sectional")
    replay = native(name="cross_sectional", rng_restore=source["rng"])
    shared = {
        "dataset_version_id": uuid4(),
        "dataset_sha256": "1" * 64,
        "universe_version_id": uuid4(),
        "universe_sha256": "2" * 64,
        "engine_dependencies_sha256": "3" * 64,
    }
    return inputs(source, shared), inputs(replay, shared)


def repin(value, *, bundle=None, observation=None, **kwargs):
    bundle = copy.deepcopy(bundle if bundle is not None else value.bundle)
    observation = copy.deepcopy(
        observation if observation is not None else value.runtime_observation
    )
    pins = value.pins.model_copy(
        update={
            "binding": value.pins.binding.model_copy(
                update={"result_manifest_sha256": digest(bundle)}
            ),
            "runtime_observation_sha256": runtime_digest(observation),
        }
    )
    return replace(
        value, pins=pins, bundle=bundle, runtime_observation=observation, **kwargs
    )


def checks(report):
    return {c.key: c.status for c in report.checks}


def test_actual_original_start_rng_restore_economics_and_all_decisions_match(
    actual_pair,
):
    a, b = actual_pair
    report = compare_diagnostic(a, b)
    assert report.outcome == "matched", [
        (c.key, c.status, c.reason) for c in report.checks
    ]
    assert set(checks(report)) == set(REQUIRED_CHECKS)
    assert len(report.events) == 8
    assert all(p.mapping == "verified_original_event" for p in report.events)
    assert all(p.original_decision_id != p.replay_decision_id for p in report.events)
    assert report.events[0].pre_state_status == "matched"  # a genuine non-trading day


@pytest.mark.parametrize("field", ["raw_targets", "constrained_plan"])
def test_unknown_targets_never_become_observed_empty(field):
    row = {
        "stages": [
            {
                "stage_id": "allocation",
                "coverage": "complete",
                "raw_targets": [],
                "constrained_plan": [],
            }
        ]
    }
    assert _targets(row, {}) is not None
    row["stages"][0][field] = None
    assert _targets(row, {}) is None


@pytest.mark.parametrize(
    "stages,selected",
    [
        ([], []),
        (
            [
                {
                    "stage_id": "features",
                    "coverage": "complete",
                    "raw_targets": None,
                    "constrained_plan": None,
                }
            ],
            [],
        ),
        (
            [
                {
                    "stage_id": "allocation",
                    "coverage": "complete",
                    "raw_targets": [],
                    "constrained_plan": [],
                }
            ],
            ["missing"],
        ),
    ],
)
def test_absent_target_stages_cannot_certify(stages, selected):
    assert _targets({"stages": stages}, {"stage_ids": selected}) is None


def test_partial_target_missing_reason_not_ignored():
    row = {
        "stages": [
            {
                "stage_id": "allocation",
                "coverage": "partial",
                "missing_reason": "capacity",
                "raw_targets": [],
                "constrained_plan": [],
            }
        ]
    }
    assert _targets(row, {}) is None


def test_actual_missing_original_observation_not_metrics_parity(actual_pair):
    a, b = actual_pair
    a = replace(
        a,
        pins=a.pins.model_copy(update={"runtime_observation_sha256": None}),
        runtime_observation=None,
    )
    report = compare_diagnostic(a, b)
    assert report.outcome == "not_verifiable"
    assert checks(report)["rng_reconstruction"] == "not_verifiable"
    assert checks(report)["audit_coverage"] == "not_verifiable"
    assert not any(p.mapping == "verified_original_event" for p in report.events)


def test_actual_unknown_target_both_sides_is_not_verified(actual_pair):
    a, b = actual_pair

    def without(value):
        rows = copy.deepcopy(value.decisions)
        for row in rows:
            for stage in row["stages"]:
                stage["raw_targets"] = stage["constrained_plan"] = None
        return replace(value, decisions=rows)

    report = compare_diagnostic(without(a), without(b))
    assert report.outcome == "not_verifiable"
    assert checks(report)["observed_targets"] == "not_verifiable"


def test_actual_wrong_rng_restore_not_silently_same_results(actual_pair):
    a, b = actual_pair
    obs = copy.deepcopy(b.runtime_observation)
    obs["rng"]["restored_from_sha256"] = None
    report = compare_diagnostic(a, repin(b, observation=obs))
    assert (
        report.outcome == "mismatch"
        and checks(report)["rng_reconstruction"] == "mismatch"
    )


@pytest.mark.parametrize("field", ["cash", "equity"])
def test_actual_report_uses_published_tolerance_for_audit_decimal_amounts(
    actual_pair, field
):
    a, b = actual_pair
    observation = copy.deepcopy(b.runtime_observation)
    event = next(
        e for e in observation["audit"]["events"] if e["event_type"] == "equity"
    )
    event["payload"][field] = str(
        Decimal(event["payload"][field]) + Decimal("0.00000001")
    )
    report = compare_diagnostic(a, repin(b, observation=observation))
    assert report.outcome == "matched"
    assert checks(report)["account_trajectory"] == "matched"
    assert checks(report)["audit_coverage"] == "matched"


@pytest.mark.parametrize("field", ["cash", "equity"])
@pytest.mark.parametrize("amount", [None, "NaN", "Infinity", "1_0"])
def test_actual_report_never_certifies_malformed_intermediate_account_amounts(
    actual_pair, field, amount
):
    a, b = actual_pair
    observation = copy.deepcopy(b.runtime_observation)
    event = next(
        e for e in observation["audit"]["events"] if e["event_type"] == "equity"
    )
    event["payload"][field] = amount
    report = compare_diagnostic(a, repin(b, observation=observation))
    assert report.outcome == "not_verifiable"
    assert checks(report)["account_trajectory"] == "not_verifiable"
    assert checks(report)["audit_coverage"] == "not_verifiable"
    assert not any(p.mapping == "verified_original_event" for p in report.events)


def test_actual_changed_order_status_is_mismatch_not_return_only(actual_pair):
    a, b = actual_pair
    obs = copy.deepcopy(b.runtime_observation)
    event = next(
        e
        for e in obs["audit"]["events"]
        if e["event_type"] == "order" and e["payload"]["status"] == "New"
    )
    event["payload"]["status"] = "Accepted"
    report = compare_diagnostic(a, repin(b, observation=obs))
    assert (
        report.outcome == "mismatch"
        and checks(report)["ordered_status_history"] == "mismatch"
    )


def test_actual_bogus_trade_id_cannot_match_by_dropping_id(actual_pair):
    a, b = actual_pair
    obs = copy.deepcopy(b.runtime_observation)
    next(e for e in obs["audit"]["events"] if e["event_type"] == "trade")["payload"][
        "trade_id"
    ] = "unregistered-fill"
    report = compare_diagnostic(a, repin(b, observation=obs))
    assert report.outcome == "not_verifiable"
    assert checks(report)["fills"] == "not_verifiable"


def test_actual_missing_prestate_does_not_become_zero_state(actual_pair):
    a, b = actual_pair
    rows = copy.deepcopy(b.decisions)
    rows[0]["pre_state"] = None
    report = compare_diagnostic(a, replace(b, decisions=rows))
    assert (
        report.outcome == "not_verifiable"
        and checks(report)["decision_pre_states"] == "not_verifiable"
    )


def test_actual_same_date_not_source_event_pairing(actual_pair):
    a, b = actual_pair
    rows = copy.deepcopy(b.decisions)
    rows[0]["data_event_id"] = "different-frozen-event"
    report = compare_diagnostic(a, replace(b, decisions=rows))
    assert (
        report.outcome == "mismatch"
        and checks(report)["original_event_mapping"] == "mismatch"
    )
    assert any(p.mapping == "unpaired" for p in report.events)


def test_actual_observation_window_does_not_shrink_economic_checks(actual_pair):
    a, b = actual_pair
    capture = {
        "mode": "research",
        "observation_window": {"start": "2024-01-08", "end": "2024-01-12"},
    }
    report = compare_diagnostic(a, replace(b, capture=capture))
    assert report.outcome == "matched" and len(report.events) == 4
    assert checks(report)["position_trajectory"] == "matched"
    assert (
        report.observation_scope["execution_scope"]
        == "full_original_evaluation_and_warmup"
    )


def test_report_cannot_omit_named_checks_or_promote_unknown_mapping(actual_pair):
    report = compare_diagnostic(*actual_pair).model_dump(mode="json")
    report["checks"].pop()
    with pytest.raises(ValidationError):
        DiagnosticParityReport.model_validate(report)
    report = compare_diagnostic(*actual_pair).model_dump(mode="json")
    report["checks"][0]["status"] = "not_verifiable"
    report["outcome"] = "not_verifiable"
    with pytest.raises(ValidationError):
        DiagnosticParityReport.model_validate(report)


def test_mutated_pins_fail_integrity_not_false_parity(actual_pair):
    a, b = actual_pair
    with pytest.raises(ValueError, match="immutable pins"):
        compare_diagnostic(a, replace(b, source_code=b.source_code + "\n"))
