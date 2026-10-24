"""Streamed synthetic native-trace-shaped storage/comparison workload.

The full frozen 1,000 listings × 750 sessions, baseline + two variants is exposed
as a reusable fixture factory. This test runs a labelled small session smoke,
not the full representative measurement and not an actual market evaluation.
The real AKQuant integration is tested separately in test_comparison_native_truth.
"""

from __future__ import annotations

from test_comparison import run_compare

from script.research_trace_benchmark_fixture import (
    FROZEN_LISTINGS,
    FROZEN_SESSIONS,
    frozen_namespace,
    session_dates,
    trace_shaped_input,
)

__all__ = [
    "FROZEN_LISTINGS",
    "FROZEN_SESSIONS",
    "frozen_namespace",
    "session_dates",
    "trace_shaped_input",
]


def test_thousand_listing_batch_uses_all_rows_and_exact_event_index(tmp_path):
    namespace = frozen_namespace()
    left = trace_shaped_input(tmp_path / "left", namespace, sessions=3)
    right = trace_shaped_input(tmp_path / "right", namespace, sessions=3, variant=2)
    manifest, reader, artifacts = run_compare(tmp_path / "out", left, right)
    assert (
        left.trace_manifest.detail_rows_saved
        == right.trace_manifest.detail_rows_saved
        == 9000
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

    assert manifest.sections["features"].rows == 3000
    assert manifest.sections["predicates"].rows == 3000
    assert manifest.sections["features"].changed == 0
    assert manifest.sections["predicates"].changed == 0
