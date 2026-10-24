"""Actual AKQuant observations survive bounded replay queries without reinterpretation.

These tests execute only the SHA-pinned maintained fixtures, then use real DuckDB
and Parquet. Business UUIDs/result hashes are synthetic query-fixture bindings;
this is not a PostgreSQL/HTTP, ResultBundle-admission, Docker or browser test.
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from test_native_runtime import _native as native_capture
from test_templates import _native as native_template

from packages.research_trace.contracts import (
    CaptureConfig,
    DecisionRecord,
    EvaluationBinding,
)
from packages.research_trace.samples import CONTROLLED_SOURCE_SHA256
from packages.research_trace.storage import TraceReader, write_trace_artifacts
from packages.research_trace.transport import CaptureReceiver, iter_capture_records


def store_actual(tmp_path, template, records, summary):
    # Intentionally fixture-only business identifiers, not invented production rows.
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=uuid4(),
        code_sha256=CONTROLLED_SOURCE_SHA256[template],
        dataset_version_id=uuid4(),
        dataset_sha256="b" * 64,
        universe_version_id=None,
        universe_sha256=None,
        execution_config_sha256="c" * 64,
        engine_dependencies_sha256="d" * 64,
        result_manifest_sha256="e" * 64,
    )
    manifest, artifacts = write_trace_artifacts(
        (DecisionRecord.model_validate(row) for row in records),
        binding,
        summary,
        tmp_path,
    )
    return manifest, TraceReader(manifest, {row.name: row.path for row in artifacts})


@pytest.fixture(scope="module")
def cross(tmp_path_factory):
    observed = native_template("cross_sectional")
    return store_actual(
        tmp_path_factory.mktemp("actual-cross-replay"),
        "cross_sectional",
        observed["records"],
        observed["summary"],
    )


@pytest.fixture(scope="module")
def timing(tmp_path_factory):
    observed = native_template("daily_timing")
    return store_actual(
        tmp_path_factory.mktemp("actual-timing-replay"),
        "daily_timing",
        observed["records"],
        observed["summary"],
    )


def event(reader, when):
    start = date.fromisoformat(when)
    page = reader.query("events", start=start, end=start + timedelta(days=1))
    assert page["total"] == 1
    row = page["items"][0]
    return {
        "decision_id": row["decision_id"],
        "start": start,
        "end": start + timedelta(days=1),
    }


def observations(reader, section, identity, **filters):
    page = reader.query(section, **identity, **filters)
    return [row["payload"] for row in page["items"]], page["total"]


def test_actual_high_score_filtered_and_rank_loser_are_not_empty_fills_inferences(
    cross,
):
    _, reader = cross
    identity = event(reader, "2024-01-04")
    rows, count = observations(reader, "candidates", identity, stage_id="ranking")
    assert count == 4
    candidates = {row["instrument_id"]: row for row in rows}
    assert candidates["B"]["selected"] is True and candidates["B"]["rank"] == 1
    assert candidates["C"]["score"] > candidates["B"]["score"]
    assert candidates["C"]["reason"] == "eligibility_filter"
    assert candidates["C"]["selected"] is False and candidates["C"]["rank"] is None
    assert candidates["A"]["reason"] == "rank_not_selected"
    filtered, count = observations(
        reader,
        "candidates",
        identity,
        stage_id="ranking",
        eligibility="ineligible",
        reason="eligibility_filter",
        score_min=0.49,
    )
    assert count == 1 and filtered[0]["instrument_id"] == "C"
    losers, count = observations(
        reader, "candidates", identity, stage_id="ranking", rank_min=2, rank_max=3
    )
    assert count == 2 and {row["instrument_id"] for row in losers} == {"A", "D"}
    first, total = observations(
        reader, "candidates", identity, stage_id="ranking", limit=1
    )
    second, total2 = observations(
        reader, "candidates", identity, stage_id="ranking", limit=1, offset=1
    )
    assert (
        total == total2 == 4 and first[0]["instrument_id"] != second[0]["instrument_id"]
    )


def test_actual_retention_and_declared_universe_exit_remain_distinct(cross):
    _, reader = cross
    retained, count = observations(
        reader,
        "candidates",
        event(reader, "2024-01-05"),
        stage_id="ranking",
        instrument_id="B",
    )
    assert count == 1 and retained[0]["reason"] == "holding_retained"
    outside, count = observations(
        reader,
        "candidates",
        event(reader, "2024-01-09"),
        stage_id="ranking",
        instrument_id="B",
    )
    assert count == 1 and outside[0]["reason"] == "outside_universe"
    assert outside[0]["universe_member"] is False
    assert outside[0]["selected"] is False
    # Membership and already-computed feature validity are independent.
    assert outside[0]["score"] == pytest.approx(11 / 13 - 1)
    assert outside[0]["score_validity"] == "valid"


def test_actual_false_not_evaluated_and_unavailable_feature_are_not_interchangeable(
    timing,
):
    _, reader = timing
    warm = event(reader, "2024-01-02")
    features, _ = observations(reader, "features", warm, stage_id="features")
    predicates, _ = observations(reader, "predicates", warm, stage_id="signal")
    assert (
        features[0]["value"] is None
        and features[0]["validity"] == "warmup_insufficient"
    )
    assert features[0]["missing_reason"]
    assert predicates[0]["state"] == "not_evaluated" and predicates[0]["missing_reason"]
    missed = event(reader, "2024-01-04")
    predicates, _ = observations(reader, "predicates", missed, stage_id="signal")
    assert predicates[0]["state"] == "false" and predicates[0]["operands"] is not None
    stages, _ = observations(reader, "stages", missed, stage_id="execution")
    assert (
        stages[0]["order_links"] == []
    )  # Observed absence of intent, not inferred from no fills.
    decision, _ = observations(reader, "decisions", missed)
    assert decision[0]["pre_state"]["cash"] == 10000
    assert (
        decision[0]["pre_state"]["positions"] == []
    )  # Observed flat account, not a missing-value default.
    assert decision[0]["market_timezone"] == "America/New_York"
    assert decision[0]["information_cutoff"].startswith("2024-01-04T21:00:00")


def test_actual_summary_omits_detail_without_inventing_false_or_zero(tmp_path):
    observed = native_template("daily_timing", mode="summary")
    manifest, reader = store_actual(
        tmp_path, "daily_timing", observed["records"], observed["summary"]
    )
    identity = event(reader, "2024-01-04")
    features, count = observations(reader, "features", identity, stage_id="features")
    predicates, predicate_count = observations(
        reader, "predicates", identity, stage_id="signal"
    )
    assert features == predicates == [] and count == predicate_count == 0
    assert manifest.capture.mode == "summary" and manifest.coverage == "partial"
    assert manifest.missing_reasons
    # Zero stored detail is a capacity/collection fact, never zero feature values
    # or false predicate outcomes. Actual candidate/non-trade summaries remain.
    candidates, count = observations(reader, "candidates", identity, stage_id="signal")
    assert count == 1 and candidates[0]["reason"] == "timing_not_triggered"


def test_actual_delayed_partial_fills_do_not_overwrite_same_day_decision_prestate(
    tmp_path,
):
    captured = native_capture("cross_sectional", "research", "partial")
    config = CaptureConfig(mode="research")
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    for frame in captured["frames"]:
        receiver.accept(frame)
    envelope = receiver.result()
    records = list(
        iter_capture_records(
            envelope["research_capture_path"],
            envelope["research_capture_sha256"],
            envelope["research_capture_size_bytes"],
            config,
        )
    )
    _, reader = store_actual(
        tmp_path / "parquet", "cross_sectional", records, envelope["research_capture"]
    )
    all_events = reader.query("events", limit=500)["items"]
    by_date = {}
    for row in all_events:
        by_date.setdefault(row["session_date"], []).append(row)
    mixed_days = [
        rows
        for rows in by_date.values()
        if any(
            row["callback_phase"].startswith("native.execution_result") for row in rows
        )
        and any(
            not row["callback_phase"].startswith("native.execution_result")
            for row in rows
        )
    ]
    assert mixed_days
    mixed = [row for rows in mixed_days for row in rows]
    assert len({row["decision_id"] for row in mixed}) == len(mixed)
    found_partial = False
    for row in mixed:
        identity = {
            "decision_id": row["decision_id"],
            "start": row["session_date"],
            "end": row["session_date"] + timedelta(days=1),
        }
        decision, count = observations(reader, "decisions", identity)
        assert count == 1 and decision[0]["decision_id"] == row["decision_id"]
        if row["callback_phase"].startswith("native.execution_result"):
            assert (
                decision[0]["pre_state"] is None
                and decision[0]["pre_state_missing_reason"]
            )
            stages, _ = observations(reader, "stages", identity)
            for stage in stages:
                for link in stage["order_links"]:
                    if (
                        link["status"] == "partially_filled"
                        or len(link["fill_ids"]) > 1
                    ):
                        found_partial = True
                        assert link["child_order_ids"] and link["fill_ids"]
                        assert all(
                            value > link["submitted_at"] for value in link["fill_times"]
                        )
        else:
            assert decision[0]["pre_state"] is not None
    assert found_partial
