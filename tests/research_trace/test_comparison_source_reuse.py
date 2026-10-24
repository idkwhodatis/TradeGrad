"""Source reuse keeps exact evidence, admission, work and retained allocations."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from packages.research_trace.comparison import _observe_section
from packages.research_trace.comparison_contracts import DEFAULT_LIMITS, ComparisonError
from packages.research_trace.comparison_semantics import (
    CanonicalObservation,
    SourceObservation,
    SourceReuse,
    TraceObservation,
    _MatchingWork,
    group_record,
    multiset_equal,
    observed_value,
    source_retention_possible,
    source_retention_saving,
    source_semantic_size,
    trace_retention_saving,
)
from packages.research_trace.comparison_storage import TrustedTraceScan
from packages.research_trace.contracts import canonical_bytes


def payload(**updates):
    return {
        "feature_id": "momentum",
        "instrument_id": "B",
        "value": "股票😀",
        "validity": "valid",
        "unit": "ratio",
        "calculation_window": "1 session",
        "information_cutoff": "2024-01-02T16:00:00+00:00",
        "source": None,
        **updates,
    }


class Control:
    def __init__(self):
        self.calls = 0

    def check(self):
        self.calls += 1


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, args):
        self.calls.append((query, args))
        if query.startswith("SELECT count"):
            sizes = [len(row["payload"].encode()) for row in self.rows]
            return SimpleNamespace(
                fetchone=lambda: (len(sizes), sum(sizes), max(sizes, default=0))
            )
        keys = list(self.rows[0]) if self.rows else []
        batches = iter([[tuple(row[key] for key in keys) for row in self.rows], []])
        return SimpleNamespace(
            description=[(key,) for key in keys], fetchmany=lambda _: next(batches)
        )


def scan_rows(raws, *, retain=False, reuse=None, limits=DEFAULT_LIMITS, ordinals=None):
    rows = [
        {
            "decision_id": "decision",
            "session_date": "2024-01-02",
            "stage_id": "ranking",
            "ordinal": ordinal,
            "instrument_id": "B",
            "payload": raw,
        }
        for ordinal, raw in zip(
            range(len(raws)) if ordinals is None else ordinals, raws, strict=True
        )
    ]
    scan = object.__new__(TrustedTraceScan)
    scan.limits, scan.control = limits, Control()
    scan.source = lambda *_: ["pinned-fixture"]
    connection = Connection(rows)
    result = list(
        scan.rows(
            connection,
            "features",
            decision_id="decision",
            semantic_projection=True,
            retain_source=retain,
            reuse=reuse,
        )
    )
    return result, scan.control.calls, connection.calls, rows


def source(value):
    raw = canonical_bytes(value).decode()
    return SourceObservation(raw, value, source_semantic_size(value, len(raw)))


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"source": None},
        {"source": "股票😀", "source_ref": {"z": ["\ud800", False]}},
        payload(),
        payload(value=-0.0),
        payload(value={"z": 1, "a": [None]}),
    ],
)
def test_source_exact_matching_preserves_byte_node_and_control_boundaries(value):
    semantic = observed_value(value)
    wrapped = source(value)
    assert wrapped.semantic_size == len(canonical_bytes(semantic))
    for nodes in (0, 1, 2, 3, 4, 256, 65536):
        for size in (0, 1, 2 * wrapped.semantic_size - 1, 2 * wrapped.semantic_size):
            before_control, after_control = Control(), Control()
            before, after = _MatchingWork(before_control), _MatchingWork(after_control)
            before.nodes = after.nodes = nodes
            before.bytes = after.bytes = size
            expected = multiset_equal([semantic], [semantic], _work=before)
            assert multiset_equal([wrapped], [wrapped], _work=after) is expected
            assert (after.nodes, after.bytes, after_control.calls) == (
                before.nodes,
                before.bytes,
                before_control.calls,
            )


def test_source_nonexact_fallback_preserves_original_insertion_order():
    left, right = {"z": 0, "a": [None] * 65536}, {"z": 1, "a": [None] * 65536}
    for nodes in (5, 65536):
        before, after = _MatchingWork(None), _MatchingWork(None)
        before.nodes = after.nodes = nodes
        assert multiset_equal(
            [source(left)], [source(right)], _work=after
        ) is multiset_equal([left], [right], _work=before)
        assert (after.nodes, after.bytes) == (before.nodes, before.bytes)


def test_only_exact_ascii_canonical_sources_share_and_each_side_is_admitted():
    value = payload()
    canonical = canonical_bytes(value).decode()
    left, checks, calls, _ = scan_rows([canonical], retain=True)
    witness = left[0].semantic
    assert isinstance(witness, SourceObservation) and witness.raw.isascii()
    reuse = SourceReuse("ranking", [TraceObservation(0, left[0].payload, witness)])
    right, right_checks, right_calls, _ = scan_rows([canonical], reuse=reuse)
    assert right[0].payload is left[0].payload and right[0].semantic is witness
    assert checks == right_checks and calls == right_calls
    for other in (
        json.dumps(value, ensure_ascii=False),
        json.dumps(value, ensure_ascii=True),
        canonical_bytes(payload(source="distinct-provenance")).decode(),
    ):
        ordinary, _, _, _ = scan_rows([other], retain=True)
        checked, _, _, _ = scan_rows([other], reuse=reuse)
        assert checked[0].payload is not left[0].payload
        if other != canonical_bytes(json.loads(other)).decode():
            assert not isinstance(ordinary[0].semantic, SourceObservation)
    for options in ({"retain": True}, {"reuse": reuse}, {}):
        _, _, _, rows = scan_rows([canonical], **options)
        exact = 2 + sum(
            len(canonical_bytes({**row, "payload": json.loads(row["payload"])})) + 1
            for row in rows
        )
        assert (
            len(
                scan_rows(
                    [canonical],
                    limits=DEFAULT_LIMITS.model_copy(update={"max_event_bytes": exact}),
                    **options,
                )[0]
            )
            == 1
        )
        with pytest.raises(ComparisonError, match="fixed row/byte budget"):
            scan_rows(
                [canonical],
                limits=DEFAULT_LIMITS.model_copy(update={"max_event_bytes": exact - 1}),
                **options,
            )


def test_source_retention_and_index_are_funded_by_released_containers():
    raw = canonical_bytes(payload()).decode()
    rows, _, _, old_rows = scan_rows([raw] * 20, retain=True)
    items = [TraceObservation(row.ordinal, row.payload, row.semantic) for row in rows]
    for row, old in zip(rows, old_rows, strict=True):
        assert isinstance(row.semantic, SourceObservation)
        semantic = observed_value(row.payload)
        old_semantic = min(
            sys.getsizeof(semantic),
            sys.getsizeof(CanonicalObservation(b"", {}))
            + sys.getsizeof(canonical_bytes(semantic)),
        )
        new_semantic = (
            sys.getsizeof(row.semantic)
            + sys.getsizeof(row.semantic.raw)
            + sys.getsizeof(row.semantic.semantic_size)
        )
        assert sys.getsizeof(row) + new_semantic <= sys.getsizeof(old) + old_semantic
        assert source_retention_saving(row.semantic, semantic) >= 0
    holder = SourceReuse("ranking", items)
    assert sum(trace_retention_saving(item) for item in items) >= sys.getsizeof(
        holder
    ) + sys.getsizeof(items)


def test_large_provenance_is_rejected_before_source_encoding(monkeypatch):
    from packages.research_trace import comparison_storage as storage

    value = {"source": "x" * 100000, "instrument_id": "B"}
    assert not source_retention_possible(
        canonical_bytes(value).decode(), observed_value(value)
    )
    monkeypatch.setattr(
        storage,
        "_SOURCE_ENCODER",
        SimpleNamespace(
            encode=lambda _: pytest.fail("unaffordable source was encoded")
        ),
    )
    rows, _, _, _ = scan_rows([canonical_bytes(value).decode()], retain=True)
    assert not isinstance(rows[0].semantic, SourceObservation)
    assert rows[0].payload == value


def test_group_shared_witness_keeps_ordinal_provenance_and_source_unchanged():
    value = payload()
    witness = source(value)
    before = canonical_bytes(value)
    item = TraceObservation(0, value, witness)
    args = {"left_state": "observed", "right_state": "observed"}
    row = group_record("features", ("ranking", "B", "momentum"), [item], [item], **args)
    assert row.source is witness and row["left"] is row["right"]
    other = TraceObservation(1, value, witness)
    different_ordinal = group_record(
        "features", ("ranking", "B", "momentum"), [item], [other], **args
    )
    assert (
        type(different_ordinal) is dict
        and different_ordinal["right"]["items"][0]["ordinal"] == 1
    )
    assert canonical_bytes(value) == before


@pytest.mark.parametrize("ordinals", [[1], [-1], [0, 0]])
def test_reuse_index_is_not_created_for_invalid_ordinals(ordinals):
    value = payload()
    rows, _, _, _ = scan_rows(
        [canonical_bytes(value).decode()] * len(ordinals),
        retain=True,
        ordinals=ordinals,
    )
    scan = SimpleNamespace(rows=lambda *a, **kw: iter(rows))
    with pytest.raises(ComparisonError):
        _observe_section(
            None,
            scan,
            {"decision_id": "decision", "session_date": "2024-01-02"},
            {"ranking": {"features_saved": len(rows)}},
            "features",
            make_reuse=True,
        )


def test_unfavorable_interpreter_container_sizes_fall_back_without_rejecting(
    monkeypatch,
):
    from packages.research_trace import comparison_semantics as semantics

    value = payload()
    monkeypatch.setattr(semantics, "_TRACE_INPUT_SIZE", 10000)
    rows, _, _, _ = scan_rows([canonical_bytes(value).decode()], retain=True)
    assert rows[0].payload == value
    assert not isinstance(rows[0].semantic, SourceObservation)


def test_duplicate_groups_release_all_raw_sources_before_reuse_or_counter_matching():
    values = [payload(value=index) for index in range(40)]
    inputs, _, _, _ = scan_rows(
        [canonical_bytes(value).decode() for value in values], retain=True
    )
    assert all(isinstance(row.semantic, SourceObservation) for row in inputs)
    scan = SimpleNamespace(rows=lambda *a, **kw: iter(inputs))
    groups, reuse = _observe_section(
        None,
        scan,
        {"decision_id": "decision", "session_date": "2024-01-02"},
        {"ranking": {"features_saved": len(inputs)}},
        "features",
        make_reuse=True,
    )
    items = groups[("ranking", "B", "momentum")]
    assert len(items) == len(values)
    assert all(not isinstance(item.semantic, SourceObservation) for item in items)
    if reuse is not None:
        assert all(
            not isinstance(item.semantic, SourceObservation)
            for item in reuse.observations
        )
        assert all(reuse.observations[item.ordinal] is item for item in items)
    before, after = _MatchingWork(None), _MatchingWork(None)
    expected = [observed_value(value) for value in values]
    actual = [item.semantic for item in items]
    assert multiset_equal(actual, actual, _work=after) is multiset_equal(
        expected, expected, _work=before
    )
    assert (after.nodes, after.bytes) == (before.nodes, before.bytes)


def test_right_multiplicity_mismatch_keeps_exact_short_circuit_work():
    values = [payload(feature_id=f"f{index:02d}") for index in range(40)]
    inputs, _, _, _ = scan_rows(
        [canonical_bytes(value).decode() for value in values], retain=True
    )
    event = {"decision_id": "decision", "session_date": "2024-01-02"}
    left, reuse = _observe_section(
        None,
        SimpleNamespace(rows=lambda *a, **kw: iter(inputs)),
        event,
        {"ranking": {"features_saved": len(inputs)}},
        "features",
        make_reuse=True,
    )
    assert reuse is not None
    right_values = [values[0], *values]
    right_rows, _, _, _ = scan_rows(
        [canonical_bytes(value).decode() for value in right_values], reuse=reuse
    )
    right = _observe_section(
        None,
        SimpleNamespace(rows=lambda *a, **kw: iter(right_rows)),
        event,
        {"ranking": {"features_saved": len(right_rows)}},
        "features",
    )
    key = ("ranking", "B", "f00")
    assert len(left[key]) == 1 and len(right[key]) == 2
    before, after = _MatchingWork(None), _MatchingWork(None)
    assert (
        multiset_equal(
            [item.semantic for item in left[key]],
            [item.semantic for item in right[key]],
            _work=after,
        )
        is False
    )
    assert (
        multiset_equal(
            [observed_value(values[0])], [observed_value(values[0])] * 2, _work=before
        )
        is False
    )
    assert (after.nodes, after.bytes) == (before.nodes, before.bytes)
