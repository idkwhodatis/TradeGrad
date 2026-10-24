"""Performance changes must preserve exact bytes, bounds and matching work."""

from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

import pytest

from packages.research_trace.comparison import (
    ComparisonControl,
    _details,
    _observe_section,
)
from packages.research_trace.comparison_contracts import (
    DEFAULT_LIMITS,
    ComparisonError,
    ComparisonLimits,
)
from packages.research_trace.comparison_semantics import (
    CanonicalObservation,
    _MatchingWork,
    equal_value,
    multiset_equal,
    observation_payload_size,
    observed_value,
)
from packages.research_trace.comparison_storage import TrustedTraceScan, _json_row_size
from packages.research_trace.contracts import canonical_bytes
from script.research_trace_benchmark_fixture import frozen_namespace, trace_shaped_input
from tests.research_trace.test_comparison import feature, make_input, run_compare


def _legacy_bytes(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _values():
    rng = random.Random(427)
    values = [
        None,
        {},
        [],
        False,
        0,
        0.0,
        -0.0,
        True,
        1,
        1.0,
        5e-324,
        {"z": '股票😀\ud800\x00\x7f"\\', "a": [None, False, -0.0, 1e15]},
    ]
    for _ in range(200):
        values.append(
            {
                "z": [rng.random(), rng.randrange(-100000, 100000), None],
                "source": rng.choice([None, "strategy_emitted", "股票😀"]),
                "nested": {"z": rng.choice(values[:12]), "a": "x" * rng.randrange(30)},
            }
        )
    return values


def test_shared_encoder_preserves_canonical_bytes_and_concurrent_error_recovery():
    values = _values()
    assert [canonical_bytes(v) for v in values] == [_legacy_bytes(v) for v in values]
    cycle = []
    cycle.append(cycle)
    invalid = [math.nan, math.inf, -math.inf, object(), cycle, {"x": object()}]

    def check(index):
        value = values[index % len(values)]
        bad = invalid[index % len(invalid)]
        with pytest.raises((TypeError, ValueError)) as old:
            _legacy_bytes(bad)
        with pytest.raises(type(old.value)):
            canonical_bytes(bad)
        return canonical_bytes(value) == _legacy_bytes(value)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(check, range(800)))


def test_row_and_provenance_sizes_are_exact_for_nested_unicode_and_empty_objects():
    payloads = [
        {},
        {"source": None},
        {"source_ref": None},
        {"source": "股票😀", "source_ref": {"z": ["\ud800", 1e15], "a": False}},
        *_values(),
    ]
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        semantic = observed_value(payload)
        size = observation_payload_size(payload, len(canonical_bytes(semantic)))
        assert size == len(_legacy_bytes(payload))
        row = {
            "decision_id": "id",
            "session_date": "2024-01-01",
            "ordinal": 1,
            "payload": payload,
            "instrument_id": "股票😀",
        }
        assert _json_row_size(row) == len(_legacy_bytes(row))
        assert _json_row_size({**row, "payload": None}) - 4 + size == len(
            _legacy_bytes(row)
        )


def _legacy_singleton(left, right, work):
    if not work.spend():
        return None
    counters = []
    for value in (left, right):
        raw = _legacy_bytes(value)
        if not work.spend(size=len(raw)):
            return None
        counters.append(Counter({raw: 1}))
    if counters[0] == counters[1]:
        return True
    return equal_value(left, right, _work=work)


@pytest.mark.parametrize(
    "left,right",
    [
        ({"flag": False}, {"flag": 0}),
        ({"flag": True}, {"flag": 1}),
        ({"value": 0}, {"value": -0.0}),
        ({"score": 1.0}, {"score": 1.0 + 1e-13}),
        ({"z": 0, "a": [None]}, {"z": 1, "a": [None]}),
        ({"a": [1, 2]}, {"a": [2, 1]}),
        ({"same": "股票😀"}, {"same": "股票😀"}),
    ],
)
def test_singleton_encoded_and_ordinary_values_preserve_every_work_charge(left, right):
    raw_size = len(_legacy_bytes(left)) + len(_legacy_bytes(right))
    for nodes in (0, 1, 2, 3, 4, 5, 8, 256, 65536):
        for byte_budget in (0, 1, raw_size - 1, raw_size, raw_size + 1):
            expected = _MatchingWork(None)
            expected.nodes = nodes
            expected.bytes = byte_budget
            outcome = _legacy_singleton(left, right, expected)
            for encoded in (False, True):
                actual = _MatchingWork(None)
                actual.nodes = nodes
                actual.bytes = byte_budget
                values = [
                    CanonicalObservation(_legacy_bytes(v), v) if encoded else v
                    for v in (left, right)
                ]
                assert multiset_equal([values[0]], [values[1]], _work=actual) is outcome
                assert (actual.nodes, actual.bytes) == (expected.nodes, expected.bytes)


def test_encoded_noncanonical_source_keeps_original_default_budget_traversal_order():
    left = {"z": 0, "a": [None] * 65536}
    right = {"z": 1, "a": [None] * 65536}
    assert multiset_equal([left], [right]) is False
    assert (
        multiset_equal(
            [CanonicalObservation(_legacy_bytes(left), left)],
            [CanonicalObservation(_legacy_bytes(right), right)],
        )
        is False
    )


def test_encoded_duplicates_keep_tolerant_multiset_outcomes_and_work():
    left = [{"score": 1.0}, {"score": 1.0 + 1e-10}]
    right = [{"score": 1.0 + 2e-10}, {"score": 1.0}]
    for nodes in (1, 3, 8, 15, 100, 65536):
        before = _MatchingWork(None)
        before.nodes = nodes
        after = _MatchingWork(None)
        after.nodes = nodes
        expected = multiset_equal(left, right, _work=before)
        wrapped = [
            [CanonicalObservation(_legacy_bytes(v), v) for v in side]
            for side in (left, right)
        ]
        assert multiset_equal(*wrapped, _work=after) is expected
        assert (after.nodes, after.bytes) == (before.nodes, before.bytes)


def test_cancelled_encoded_exact_match_does_not_skip_control():
    control = ComparisonControl()
    control.cancel()
    value = {"value": 1}
    wrapped = CanonicalObservation(_legacy_bytes(value), value)
    with pytest.raises(ComparisonError) as error:
        multiset_equal([wrapped], [wrapped], control=control)
    assert error.value.code == "comparison_cancelled"


def test_projection_reuses_bytes_without_retained_memory_growth_and_keeps_large_values(
    tmp_path,
):
    namespace = frozen_namespace()
    small = trace_shaped_input(tmp_path / "small", namespace, sessions=1)
    large = make_input(tmp_path / "large", namespace, features=[feature("x" * 5000)])
    seen_encoded = seen_shared = False
    for value, table in (
        (small, "candidates"),
        (small, "features"),
        (small, "predicates"),
        (large, "features"),
    ):
        control = ComparisonControl()
        with ExitStack() as stack, control.connection() as connection:
            scan = TrustedTraceScan(value, stack, control, DEFAULT_LIMITS)
            event = next(scan.rows(connection, "events"))
            _, stages = _details(connection, scan, event)
            groups = _observe_section(connection, scan, event, stages, table)
            for group in groups.values():
                for item in group:
                    expected = observed_value(item["record"])
                    retained = item["semantic"]
                    if isinstance(retained, CanonicalObservation):
                        seen_encoded = True
                        assert retained.raw == _legacy_bytes(expected)
                        assert retained.record is item["record"]
                        assert sys.getsizeof(retained) + sys.getsizeof(
                            retained.raw
                        ) <= sys.getsizeof(expected)
                    else:
                        seen_shared = True
                        assert retained == expected
                        assert all(retained[k] is item["record"][k] for k in retained)
    assert seen_encoded and seen_shared


def test_projection_preserves_exact_input_byte_boundary(tmp_path):
    value = trace_shaped_input(tmp_path / "source", frozen_namespace(), sessions=1)
    control = ComparisonControl()
    with ExitStack() as stack, control.connection() as connection:
        scan = TrustedTraceScan(value, stack, control, DEFAULT_LIMITS)
        event = next(scan.rows(connection, "events"))
        rows = list(
            scan.rows(
                connection,
                "features",
                decision_id=event["decision_id"],
                day=event["session_date"],
            )
        )
        budget = 2 + sum(len(_legacy_bytes(row)) + 1 for row in rows)
        for allowed in (budget - 1, budget):
            scan.limits = ComparisonLimits(max_event_bytes=allowed)
            for projected in (False, True):
                operation = lambda projected=projected: list(
                    scan.rows(
                        connection,
                        "features",
                        decision_id=event["decision_id"],
                        day=event["session_date"],
                        semantic_projection=projected,
                    )
                )
                if allowed < budget:
                    with pytest.raises(ComparisonError) as error:
                        operation()
                    assert error.value.code == "comparison_capacity"
                else:
                    assert len(operation()) == 1000


def test_all_public_comparison_rows_match_without_projection_optimization(
    tmp_path, monkeypatch
):
    namespace = frozen_namespace()
    left = trace_shaped_input(tmp_path / "left", namespace, sessions=2)
    right = trace_shaped_input(tmp_path / "right", namespace, sessions=2, variant=2)
    manifest, reader, _ = run_compare(tmp_path / "optimized", left, right)
    expected = {}
    for section in manifest.sections:
        pages = [
            reader.page(section, offset=offset, limit=500).items
            for offset in range(0, manifest.sections[section].rows, 500)
        ]
        expected[section] = [_legacy_bytes(row) for page in pages for row in page]
    original = TrustedTraceScan.rows

    def ordinary(self, connection, table, **kwargs):
        projected = kwargs.pop("semantic_projection", False)
        for row in original(self, connection, table, **kwargs):
            if projected:
                row["_semantic"] = observed_value(row["payload"])
            yield row

    monkeypatch.setattr(TrustedTraceScan, "rows", ordinary)
    baseline, reader, _ = run_compare(tmp_path / "ordinary", left, right)
    assert baseline.sections == manifest.sections
    for section in baseline.sections:
        pages = [
            reader.page(section, offset=offset, limit=500).items
            for offset in range(0, baseline.sections[section].rows, 500)
        ]
        assert [_legacy_bytes(row) for page in pages for row in page] == expected[
            section
        ]
