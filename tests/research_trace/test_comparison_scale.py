"""Streamed synthetic native-trace-shaped storage/comparison workload.

The full frozen 1,000 listings × 750 sessions, baseline + two variants is exposed
as a reusable fixture factory. This test runs a labelled small session smoke,
not the full representative measurement and not an actual market evaluation.
The real AKQuant integration is tested separately in test_comparison_native_truth.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

from test_comparison import run_compare

from packages.research_trace.comparison_contracts import ComparisonInput, EventNamespace
from packages.research_trace.contracts import (
    CandidateObservation,
    CaptureConfig,
    DateRange,
    DecisionRecord,
    EvaluationBinding,
    PortfolioState,
    StageObservation,
    digest,
)
from packages.research_trace.storage import write_trace_artifacts

FROZEN_LISTINGS = 1000
FROZEN_SESSIONS = 750


def session_dates(count):
    current, days = date(2021, 1, 4), []
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def trace_shaped_input(path, namespace, *, sessions=FROZEN_SESSIONS, variant=0):
    """Generate one row at a time; never retain the full workload in Recorder.

    Every session retains the full 1,000-candidate pool. Fields represent a
    deliberately simple synthetic native trace, not simulated trading metrics.
    Variant 1 changes only source commentary; variant 2 changes selected rank.
    """
    source = (
        "# Synthetic native trace-shaped workload, not executed strategy\n"
        + f"# variant {variant}\n"
    )
    code_hash = hashlib.sha256(source.encode()).hexdigest()
    days = session_dates(sessions)
    window = DateRange(start=days[0], end=days[-1] + timedelta(days=1))
    binding = EvaluationBinding(
        run_id=uuid4(),
        execution_attempt_id=uuid4(),
        evaluation_id=uuid4(),
        candidate_id=None,
        strategy_version_id=uuid4(),
        code_sha256=code_hash,
        dataset_version_id=namespace.dataset_version_id,
        dataset_sha256=namespace.dataset_sha256,
        universe_version_id=namespace.universe_version_id,
        universe_sha256=namespace.universe_sha256,
        execution_config_sha256=digest(
            {"window": window.model_dump(mode="json"), "variant": variant}
        ),
        engine_dependencies_sha256="d" * 64,
        result_manifest_sha256="e" * 64,
    )
    capture = CaptureConfig(
        mode="research",
        max_decisions=10000,
        max_detail_rows=20_000_000,
        max_uncompressed_bytes=1024**3,
    )
    candidates = [
        CandidateObservation(
            instrument_id=f"S{index:04d}",
            universe_member=True,
            eligibility="eligible",
            reason="selected"
            if index == (1 if variant == 2 else 0)
            else "rank_not_selected",
            score=float(FROZEN_LISTINGS - index),
            score_validity="valid",
            rank=index + 1,
            ranking_direction="descending",
            tie_break="instrument_id",
            selected=index == (1 if variant == 2 else 0),
            prior_holding=False,
            source="strategy_emitted",
        )
        for index in range(FROZEN_LISTINGS)
    ]

    def records():
        for index, day in enumerate(days):
            moment = datetime.combine(day, time(16), tzinfo=UTC)
            yield DecisionRecord(
                decision_id=str(uuid4()),
                data_event_id=f"frozen_daily_slice:{day.isoformat()}",
                event_seq=index,
                scope="portfolio",
                callback_phase="on_cross_section",
                information_cutoff=moment,
                decision_time=moment,
                market_timezone="UTC",
                pre_state=PortfolioState(
                    cash=100000.0,
                    equity=100000.0,
                    positions=[],
                    open_orders=[],
                    coverage="complete",
                    source="strategy_emitted",
                ),
                stages=[
                    StageObservation(
                        stage_id="ranking",
                        coverage="complete",
                        candidates=candidates,
                        raw_targets=[
                            {
                                "instrument_id": f"S{1 if variant == 2 else 0:04d}",
                                "weight": 0.1,
                            }
                        ],
                        constrained_plan=[
                            {
                                "instrument_id": f"S{1 if variant == 2 else 0:04d}",
                                "weight": 0.1,
                            }
                        ],
                    )
                ],
            )

    summary = {
        "capture": capture.model_dump(mode="json"),
        "coverage": "complete",
        "missing_reasons": [],
        "decisions_seen": sessions,
        "decisions_saved": sessions,
        "detail_rows_seen": sessions * FROZEN_LISTINGS,
        "detail_rows_saved": sessions * FROZEN_LISTINGS,
        "stage_coverage": {"ranking": "complete"},
        "source": "strategy_emitted",
        "trust_boundary": "Explicitly synthetic native trace-shaped capacity fixture, not real market or engine output.",
    }
    manifest, artifacts = write_trace_artifacts(records(), binding, summary, path)
    # Deliberately absent economics remains absent. This capacity fixture never
    # fabricates zero-return or no-trade engine results to fill its unknowns.
    return ComparisonInput(
        binding=binding,
        source_kind="strategy_version",
        source_id=binding.strategy_version_id,
        source=source,
        window=window,
        spec=None,
        spec_sha256=None,
        trace_manifest=manifest,
        trace_manifest_sha256=digest(manifest),
        trace_paths={item.name: item.path for item in artifacts},
        common_config={
            "data": namespace.dataset_sha256,
            "universe": namespace.frozen_universe_sha256,
            "window": window.model_dump(mode="json"),
        },
        economic_bundle={},
        namespace=namespace,
    )


def frozen_namespace():
    return EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256=digest(
            {
                "kind": "synthetic_native_trace",
                "listings": FROZEN_LISTINGS,
                "sessions": FROZEN_SESSIONS,
            }
        ),
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256=digest(
            [f"S{index:04d}" for index in range(FROZEN_LISTINGS)]
        ),
        instrument_namespace="native_symbol",
    )


def test_thousand_listing_batch_uses_all_rows_and_exact_event_index(tmp_path):
    namespace = frozen_namespace()
    left = trace_shaped_input(tmp_path / "left", namespace, sessions=3)
    right = trace_shaped_input(tmp_path / "right", namespace, sessions=3, variant=2)
    manifest, reader, artifacts = run_compare(tmp_path / "out", left, right)
    assert (
        left.trace_manifest.detail_rows_saved
        == right.trace_manifest.detail_rows_saved
        == 3000
    )
    assert manifest.sections["candidates"].rows == 3000
    assert manifest.sections["candidates"].changed == 6
    assert reader.page("candidates", offset=2500, limit=500).total == 3000
    event = reader.page("events").items[0]
    page = reader.page("candidates", event_pair_id=event.event_pair_id, limit=500)
    assert page.total == 1000 and len(page.items) == 500
    assert len([item for item in artifacts if item.section == "candidates"]) < 3
    assert manifest.sections["orders"].left_availability == "not_collected"
    assert manifest.sections["turnover"].observed_equality == "unknown"
