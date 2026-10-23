from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from packages.research_trace.contracts import (
    CandidateObservation,
    CaptureConfig,
    DecisionRecord,
    EvidenceRef,
    Feature,
    PortfolioState,
    Predicate,
    SourceRef,
    StrategySpec,
    TraceManifest,
    legacy_coverage,
)
from packages.research_trace.recorder import Recorder, current_recorder, recording

HASH = "a" * 64
NOW = "2024-01-04T21:00:00+00:00"


def feature(**updates):
    return {
        "feature_id": "momentum",
        "instrument_id": "B",
        "value": 0.2,
        "validity": "valid",
        "unit": "fraction",
        "calculation_window": "3 completed sessions",
        "information_cutoff": NOW,
        "source": "strategy_emitted",
        **updates,
    }


def predicate(**updates):
    return {
        "condition_id": "liquid",
        "operator": "gte",
        "operands": [1200],
        "threshold": 1000,
        "state": "true",
        "source": "strategy_emitted",
        **updates,
    }


def start(recorder, **updates):
    return recorder.begin_decision(
        data_event_id="snapshot:2024-01-04:close",
        event_seq=0,
        scope="portfolio",
        callback_phase="after_close",
        decision_time=NOW,
        information_cutoff=NOW,
        market_timezone="America/New_York",
        **updates,
    )


@pytest.mark.parametrize(
    "state", ["true", "false", "not_evaluated", "unknown", "not_sampled"]
)
def test_five_predicate_states_remain_distinct(state):
    body = predicate(state=state)
    if state not in {"true", "false"}:
        body.update(operands=None, missing_reason=f"explicit {state}")
    assert Predicate.model_validate(body).state == state


@pytest.mark.parametrize(
    "state",
    [
        "warmup_insufficient",
        "input_missing",
        "calculation_error",
        "not_collected",
        "not_sampled",
    ],
)
def test_missing_feature_is_not_a_zero(state):
    body = feature(value=None, validity=state, missing_reason="specific cause")
    assert Feature.model_validate(body).value is None
    with pytest.raises(ValidationError):
        Feature.model_validate({**body, "value": 0})


def test_false_and_zero_are_real_observations_not_missing():
    assert Feature.model_validate(feature(value=0)).value == 0
    assert Predicate.model_validate(predicate(state="false")).operands == [1200]
    with pytest.raises(ValidationError):
        Predicate.model_validate(predicate(state="unknown"))


def test_missing_portfolio_does_not_default_empty():
    with pytest.raises(ValidationError):
        PortfolioState(
            cash=None,
            positions=None,
            open_orders=None,
            coverage="complete",
            source="engine_observed",
        )
    value = PortfolioState(
        cash=None,
        positions=None,
        open_orders=None,
        coverage="not_collected",
        missing_reason="old schema",
        source="adapter_observed",
    )
    assert value.positions is None
    assert legacy_coverage()["candidates"] is None


def test_candidate_availability_and_rank_contracts():
    row = {
        "instrument_id": "B",
        "universe_member": True,
        "eligibility": "eligible",
        "reason": "selected",
        "score": 0.2,
        "score_validity": "valid",
        "selected": True,
        "prior_holding": False,
        "source": "strategy_emitted",
        "rank": 1,
    }
    with pytest.raises(ValidationError):
        CandidateObservation(**row)
    row.update(ranking_direction="descending", tie_break="instrument_id_ascending")
    assert CandidateObservation(**row).rank == 1
    with pytest.raises(ValidationError):
        CandidateObservation(**{**row, "score": None})


def test_source_reference_cannot_escape_or_change_code():
    with pytest.raises(ValidationError):
        SourceRef(
            code_sha256=HASH,
            file="../strategy.py",
            symbol="on_bar",
            line_start=1,
            line_end=1,
        )
    assert SourceRef(
        code_sha256=HASH, file="strategy.py", symbol="on_bar", line_start=1, line_end=2
    )
    with pytest.raises(ValidationError):
        EvidenceRef(
            kind="code",
            strategy_version_id=uuid4(),
            code_sha256=HASH,
            source_ref={
                "code_sha256": "b" * 64,
                "file": "strategy.py",
                "symbol": "x",
                "line_start": 1,
                "line_end": 1,
            },
        )


def test_leaf_evidence_requires_attempt_and_manifest():
    base = {
        "kind": "decision",
        "strategy_version_id": uuid4(),
        "code_sha256": HASH,
        "evaluation_id": uuid4(),
        "decision_id": "d",
    }
    with pytest.raises(ValidationError):
        EvidenceRef(**base)
    ref = EvidenceRef(
        **base,
        execution_attempt_id=uuid4(),
        result_manifest_sha256=HASH,
        trace_manifest_sha256=HASH,
    )
    assert ref.evaluation_id == base["evaluation_id"]


def test_recorder_copies_without_recomputing_and_restores_context():
    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    observed = {"features": [feature()], "predicates": [predicate()]}
    before = deepcopy(observed)
    with recording(recorder):
        assert current_recorder() is recorder
        start(recorder)
        recorder.emit("rank", observed)
        observed["features"][0]["value"] = 999
    assert current_recorder() is None
    assert (
        recorder.records[0].stages[0].features[0].value
        == before["features"][0]["value"]
    )
    assert recorder.summary()["coverage"] == "complete"
    assert recorder.summary()["detail_rows_saved"] == 2


def test_capture_rejects_callable_without_invoking_it_or_raising_to_strategy():
    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    invoked = []
    start(recorder)
    recorder.emit("bad", {"features": [feature(value=lambda: invoked.append(1))]})
    recorder.end_decision()
    assert not invoked
    assert recorder.summary()["coverage"] == "partial"
    assert recorder.records[0].stages == []


def test_summary_does_not_claim_detailed_feature_observations():
    recorder = Recorder(code_sha256=HASH)
    start(recorder)
    recorder.emit("rank", {"features": [feature()], "predicates": [predicate()]})
    recorder.end_decision()
    stage = recorder.records[0].stages[0]
    assert stage.features == stage.predicates == []
    assert stage.coverage == "partial" and "not sampled" in stage.missing_reason
    assert recorder.summary()["detail_rows_seen"] == 2
    assert recorder.summary()["detail_rows_saved"] == 0


def test_capacity_is_visible_and_does_not_change_input_pool():
    recorder = Recorder(
        code_sha256=HASH, config=CaptureConfig(mode="research", max_detail_rows=1)
    )
    start(recorder)
    rows = {"features": [feature(), feature(feature_id="volume")]}
    recorder.emit("features", rows)
    recorder.end_decision()
    assert len(rows["features"]) == 2
    assert recorder.summary()["coverage"] == "partial"
    assert "capacity" in " ".join(recorder.summary()["missing_reasons"])
    assert recorder.summary()["detail_rows_saved"] == 0


def test_no_capture_points_are_not_complete_empty_evidence():
    summary = Recorder(code_sha256=HASH).summary()
    assert summary["coverage"] == "not_collected"
    assert summary["missing_reasons"]


def test_pairing_uses_event_phase_scope_and_sequence_not_record_id():
    a = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    b = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(a)
    a.end_decision()
    start(b)
    b.end_decision()
    assert a.records[0].decision_id != b.records[0].decision_id
    assert a.records[0].pairing_key() == b.records[0].pairing_key()
    raw = a.records[0].model_dump(mode="json")
    other = DecisionRecord.model_validate({**raw, "event_seq": 1})
    assert other.pairing_key() != a.records[0].pairing_key()


def test_future_feature_cutoff_and_per_stock_portfolio_identity_are_invalid():
    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(recorder)
    recorder.emit(
        "features",
        {"features": [feature(information_cutoff="2024-01-05T21:00:00+00:00")]},
    )
    recorder.end_decision()
    assert recorder.records == []
    assert recorder.summary()["detail_rows_saved"] == 0
    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(recorder, instrument_id="B")
    recorder.end_decision()
    assert recorder.records == []


def test_spec_unknown_graph_is_not_a_second_execution_language():
    with pytest.raises(ValidationError):
        StrategySpec(
            code_sha256=HASH,
            template_id=None,
            rules=[],
            edges=[("a", "b")],
            coverage="complete",
            missing_reasons=[],
            revision=1,
        )


def test_manifest_counts_cannot_hide_truncation():
    binding = {
        "run_id": uuid4(),
        "execution_attempt_id": uuid4(),
        "evaluation_id": uuid4(),
        "candidate_id": None,
        "strategy_version_id": uuid4(),
        "code_sha256": HASH,
        "dataset_version_id": uuid4(),
        "dataset_sha256": HASH,
        "universe_version_id": None,
        "universe_sha256": None,
        "execution_config_sha256": HASH,
        "engine_dependencies_sha256": HASH,
        "result_manifest_sha256": HASH,
    }
    with pytest.raises(ValidationError):
        TraceManifest(
            binding=binding,
            capture=CaptureConfig(),
            coverage="complete",
            missing_reasons=[],
            decisions_seen=2,
            decisions_saved=1,
            detail_rows_seen=0,
            detail_rows_saved=0,
            artifacts=[],
            stage_coverage={},
            source="strategy_emitted",
            trust_boundary="shared process",
        )


def test_observer_never_converts_untrusted_stage_identity():
    class BadStage:
        def __str__(self):
            raise RuntimeError("must never run")

    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(recorder)
    recorder.emit(BadStage(), {})
    assert recorder.summary()["coverage"] == "partial"


def test_envelope_and_prestate_count_against_capacity():
    recorder = Recorder(
        code_sha256=HASH,
        config=CaptureConfig(mode="research", max_uncompressed_bytes=1024),
    )
    start(
        recorder,
        pre_state={
            "cash": 1,
            "positions": [{"payload": "x" * 100000}],
            "open_orders": [],
            "coverage": "complete",
            "source": "strategy_emitted",
        },
    )
    assert recorder.summary()["decisions_saved"] == 0
    assert "capacity" in " ".join(recorder.summary()["missing_reasons"])


def test_rule_evidence_requires_exact_explanation_revision():
    fields = {"kind": "rule", "strategy_version_id": uuid4(), "code_sha256": HASH}
    with pytest.raises(ValidationError):
        EvidenceRef(**fields)
    one = EvidenceRef(**fields, rule_id="liquidity", spec_revision=1, spec_sha256=HASH)
    two = EvidenceRef(**fields, rule_id="rank", spec_revision=1, spec_sha256=HASH)
    assert one != two


def test_datetime_copy_does_not_call_custom_timezone():
    from datetime import datetime, timedelta, tzinfo

    calls = []

    class UserTimezone(tzinfo):
        def utcoffset(self, _):
            calls.append(1)
            return timedelta(0)

    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    recorder.begin_decision(
        data_event_id="event",
        event_seq=0,
        scope="portfolio",
        callback_phase="close",
        decision_time=datetime(2024, 1, 1, tzinfo=UserTimezone()),
        information_cutoff=NOW,
        market_timezone="America/New_York",
    )
    assert calls == []
    assert recorder.summary()["coverage"] == "not_collected"


@pytest.mark.parametrize(
    "coverage", ["partial", "not_collected", "not_sampled", "unsupported"]
)
def test_explicit_stage_gaps_propagate_to_manifest_summary(coverage):
    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(recorder)
    recorder.emit(
        "execution",
        {"coverage": coverage, "missing_reason": "Child fills unavailable."},
    )
    summary = recorder.summary()
    assert summary["coverage"] == "partial"
    assert "Child fills unavailable." in summary["missing_reasons"]


def test_stage_metadata_remains_bounded_after_budget_exhaustion():
    recorder = Recorder(
        code_sha256=HASH,
        config=CaptureConfig(mode="research", max_uncompressed_bytes=1024),
    )
    start(recorder)
    for index in range(1000):
        recorder.emit(str(index), {})
    assert len(recorder.summary()["stage_coverage"]) <= 100


def test_no_pre_copy_dictionary_or_reason_hooks():
    calls = []

    class SneakyKey:
        def __hash__(self):
            return hash("features")

        def __eq__(self, _):
            calls.append("eq")
            return True

    class SneakyReason:
        def __bool__(self):
            calls.append("bool")
            return True

    recorder = Recorder(code_sha256=HASH, config=CaptureConfig(mode="research"))
    start(recorder)
    recorder.emit("bad", {SneakyKey(): []})
    start(recorder, pre_state_missing_reason=SneakyReason())
    assert calls == []


def test_repeated_rejected_stage_reasons_are_bounded():
    recorder = Recorder(
        code_sha256=HASH,
        config=CaptureConfig(mode="research", max_uncompressed_bytes=1024),
    )
    start(recorder)
    for index in range(1000):
        recorder.emit("same", {"coverage": "partial", "missing_reason": f"gap {index}"})
    assert len(recorder.summary()["missing_reasons"]) <= 100
