"""Actual bounded Parquet scans for parity, not engine/database certification."""

from dataclasses import replace

import pytest

from packages.research_trace.comparison_storage import ComparisonControl
from tests.research_trace import test_comparison as fixture

make_input = fixture.make_input
namespace = fixture.namespace
from workers.research.diagnostic_parity_publication import read_parity_decisions


def test_parity_reader_uses_actual_prestate_and_targets_not_candidate_reexecution(
    tmp_path, namespace
):
    value = make_input(tmp_path, namespace, stage_id="allocation")
    with ComparisonControl().connection() as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    control = ComparisonControl()
    rows, missing = read_parity_decisions(value, control)
    assert not missing and len(rows) == 1
    assert rows[0]["pre_state"]["cash"] == 100
    assert rows[0]["stages"][0]["raw_targets"] == [
        {"instrument_id": "B", "weight": 0.2}
    ]
    assert "candidates" not in rows[0]["stages"][0]
    assert control.closure_confirmed


def test_missing_disabled_and_capacity_are_explicit_not_empty_equality(
    tmp_path, namespace
):
    value = make_input(tmp_path, namespace)
    for updated in [
        replace(value, trace_manifest=None),
        replace(
            value,
            trace_manifest=value.trace_manifest.model_copy(
                update={"decisions_saved": 10001}
            ),
        ),
    ]:
        rows, missing = read_parity_decisions(updated, ComparisonControl())
        assert not rows and missing


def test_bad_selected_artifact_hash_never_becomes_not_collected(tmp_path, namespace):
    value = make_input(tmp_path, namespace)
    ref = next(
        ref for ref in value.trace_manifest.artifacts if "-trace-decisions-" in ref.name
    )
    bad = tmp_path / "corrupt-copy.parquet"
    bad.write_bytes(b"not the original evidence")
    value = replace(value, trace_paths={**value.trace_paths, ref.name: bad})
    with pytest.raises(ValueError):
        read_parity_decisions(value, ComparisonControl())


def test_cancelled_parity_query_closes_owned_connections(tmp_path, namespace):
    value = make_input(tmp_path, namespace)
    control = ComparisonControl()
    control.cancel()
    with pytest.raises(ValueError):
        read_parity_decisions(value, control)
    assert control.closure_confirmed
