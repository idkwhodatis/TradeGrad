"""Exact shared-side encoding is opt-in, current-value based and lossless."""

from __future__ import annotations

import json
import random
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, model_serializer

from packages.research_trace.comparison_codec import (
    PROJECTED_FIELDS,
    decode_record,
    encode_record,
)
from packages.research_trace.comparison_contracts import ComparisonRecord
from packages.research_trace.comparison_semantics import (
    SourceObservation,
    source_semantic_size,
)
from packages.research_trace.contracts import canonical_bytes


def record(side, **updates):
    return ComparisonRecord.model_validate(
        {
            "row_id": 'row-"left":null,"right":null',
            "section": "candidates",
            "event_pair_id": "event-1",
            "session_date": "2024-01-02",
            "instrument_id": "股票😀",
            "stage_id": "ranking",
            "status": "equal",
            "left_state": "observed",
            "right_state": "observed",
            "left": side,
            "right": side,
            "match_basis": "semantic_multiset",
            **updates,
        }
    )


def assert_shared_parity(value):
    expected = encode_record(value)
    actual = encode_record(value, shared_json_sides=True)
    assert actual == expected
    assert actual[1] == len(canonical_bytes(value))
    decoded, size = decode_record(
        actual[0],
        {key: getattr(value, key) for key in PROJECTED_FIELDS},
        actual[1],
    )
    assert size == actual[1]
    assert canonical_bytes(decoded) == canonical_bytes(value)


@pytest.mark.parametrize(
    "side",
    [
        {},
        {"items": [{"ordinal": 0, "record": {}}], "count": 1},
        {"values": [None, False, True, 0, 1, 1.0, -0.0, 1e15, 1e16, 5e-324]},
        {
            "z": {"right": None, "left": None},
            "a": '"left":null,"right":null,\\"right":null',
            "m": '\x00\x7f\r\n\t\\"股票😀\u2028\uffff',
            "ambiguous": False,
            "associations": [],
            "missing_reason": None,
        },
        {
            "items": [
                {"ordinal": 7, "record": {"x": [1, 2, 1]}},
                {"ordinal": 7, "record": {"x": [1, 2, 1]}},
            ],
            "count": 2,
        },
    ],
)
def test_shared_json_side_matches_all_canonical_bytes(side):
    assert_shared_parity(record(side))


def test_fixed_core_markers_and_nondefault_fields_remain_exact():
    text = '"left":null,"right":null'
    value = record(
        {"items": [{"ordinal": 0, "record": {text: text}}], "count": 1},
        status="ambiguous",
        ambiguous=True,
        right_state="unpaired",
        missing_reason=text,
        associations=[
            {
                "side": "right",
                "event_pair_id": text,
                "decision_id": text,
                "stage_id": text,
                "logical_order_id": text,
                "instrument_id": text,
                "link_ordinal": 0,
                "source_event_paired": False,
                "child_order_ids": [text, text],
                "fill_ids": [text],
            }
        ],
    )
    assert_shared_parity(value)


def test_shared_path_normalizes_one_side_and_uses_its_current_value(monkeypatch):
    side = {"items": [{"ordinal": 0, "record": {"value": 1}}], "count": 1}
    value = record(side)
    assert value.left is not value.right
    assert value.left["items"] is value.right["items"]
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    before = encode_record(value, shared_json_sides=True)
    assert calls == [{"mode": "json", "exclude": {"right"}}]
    side["items"][0]["record"]["value"] = "current, not cached"
    after = encode_record(value, shared_json_sides=True)
    assert before != after
    assert after == encode_record(value)


@pytest.mark.parametrize(
    "left,right",
    [
        ({"items": []}, {"items": []}),
        ({"value": True}, {"value": 1}),
        ({"value": 1}, {"value": 1.0}),
        ({"value": -0.0}, {"value": 0.0}),
        ({"value": None}, {}),
        (None, None),
        ({}, None),
    ],
)
def test_nonidentical_or_missing_sides_keep_generic_normalization(
    monkeypatch, left, right
):
    value = record(left, right=right)
    expected = encode_record(value)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert encode_record(value, shared_json_sides=True) == expected
    assert calls == [{"mode": "json"}]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_shared_side_still_rejects_nonfinite_json(value):
    item = record({"items": [{"value": value}], "count": 1})
    for kwargs in ({}, {"shared_json_sides": True}):
        with pytest.raises(ValueError, match="Out of range float values"):
            encode_record(item, **kwargs)


def test_surrogate_payload_retains_the_same_ascii_escape():
    value = record({"value": "\ud800"})
    assert_shared_parity(value)
    assert b"\\ud800" in encode_record(value, shared_json_sides=True)[0]


@pytest.mark.parametrize(
    "kwargs", [{}, {"shared_json_sides": False}, {"shared_json_sides": 1}]
)
def test_generic_shared_any_values_keep_both_stateful_normalizations(kwargs):
    calls = []

    class Stateful(BaseModel):
        @model_serializer
        def serialize(self):
            calls.append(None)
            return {"call": len(calls)}

    side = {
        "items": [Stateful()],
        "date": date(2026, 1, 2),
        "decimal": Decimal("1.20"),
        "tuple": (1, 2),
    }
    value = record(side)
    assert all(value.left[key] is value.right[key] for key in value.left)
    raw, _ = encode_record(value, **kwargs)
    body = json.loads(raw)[1]
    assert calls == [None, None]
    assert body["left"]["items"] == [{"call": 1}]
    assert body["right"]["items"] == [{"call": 2}]
    for name in ("left", "right"):
        assert body[name]["date"] == "2026-01-02"
        assert body[name]["decimal"] == "1.20"
        assert body[name]["tuple"] == [1, 2]


def test_subclass_keeps_generic_path_even_with_explicit_opt_in(monkeypatch):
    class Extended(ComparisonRecord):
        extra_evidence: dict

    body = record({"items": [], "count": 0}).model_dump()
    body["right"] = body["left"]
    value = Extended.model_validate(
        {**body, "extra_evidence": {"left": None, "right": None}}
    )
    assert all(value.left[key] is value.right[key] for key in value.left)
    expected = encode_record(value)
    calls = []
    original = Extended.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Extended, "model_dump", tracked)
    assert encode_record(value, shared_json_sides=True) == expected
    assert calls == [{"mode": "json"}]


def test_random_json_side_shapes_preserve_exact_bytes():
    rng = random.Random(713)
    atoms = [None, True, False, 1, 1.0, -0.0, 1e-5, "股票😀", '"right":null']
    for _ in range(100):
        keys = ["z", "a", "left", "right", "ambiguous", "associations"]
        rng.shuffle(keys)
        side = {key: [rng.choice(atoms), {"nested": rng.choice(atoms)}] for key in keys}
        assert_shared_parity(record(side))


def source_record(payload, ordinal=0):
    raw = canonical_bytes(payload).decode("ascii")
    source = SourceObservation(raw, payload, source_semantic_size(payload, len(raw)))
    return record(
        {"items": [{"ordinal": ordinal, "record": payload}], "count": 1}
    ), source


@pytest.mark.parametrize(
    "payload,ordinal",
    [
        ({}, 0),
        ({"x": [None, True, False, 1, 1.0, -0.0, 1e15, 1e16, 5e-324]}, 1),
        ({"x": '股票😀\ud800\u2028\x00\x7f\r\n\\"'}, 10**100),
        (
            {
                "right": None,
                "left": None,
                "source_ref": {"right": None, "left": None},
                "source": '"left":null,"right":null',
            },
            -1,
        ),
    ],
)
def test_canonical_source_side_is_identical_to_fully_normalized_output(
    payload, ordinal
):
    value, source = source_record(payload, ordinal)
    expected = encode_record(value)
    actual = encode_record(value, shared_json_sides=True, shared_source=source)
    assert actual == expected
    assert actual[1] == len(canonical_bytes(value))
    decoded, size = decode_record(
        actual[0],
        {key: getattr(value, key) for key in PROJECTED_FIELDS},
        actual[1],
    )
    assert size == actual[1]
    assert canonical_bytes(decoded) == canonical_bytes(value)


def test_canonical_source_avoids_both_side_normalizations(monkeypatch):
    value, source = source_record({"value": 1})
    expected = encode_record(value)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert (
        encode_record(value, shared_json_sides=True, shared_source=source) == expected
    )
    assert calls == [{"mode": "json", "exclude": {"left", "right"}}]


@pytest.mark.parametrize(
    "mutation",
    [
        "extra_side_key",
        "missing_count",
        "boolean_count",
        "wrong_count",
        "tuple_items",
        "empty_items",
        "extra_item",
        "extra_item_key",
        "missing_ordinal",
        "boolean_ordinal",
        "float_ordinal",
        "copied_source_record",
        "different_right_record",
    ],
)
def test_source_shape_or_identity_mismatch_uses_normalized_evidence(
    monkeypatch, mutation
):
    value, source = source_record({"value": 1})
    side = value.left
    item = side["items"][0]
    if mutation == "extra_side_key":
        side["extra"] = "preserve me"
    elif mutation == "missing_count":
        del side["count"]
    elif mutation == "boolean_count":
        side["count"] = True
    elif mutation == "wrong_count":
        side["count"] = 2
    elif mutation == "tuple_items":
        side["items"] = tuple(side["items"])
    elif mutation == "empty_items":
        side["items"] = []
    elif mutation == "extra_item":
        side["items"].append({"ordinal": 1, "record": {"other": 2}})
    elif mutation == "extra_item_key":
        item["extra"] = "preserve me"
    elif mutation == "missing_ordinal":
        del item["ordinal"]
    elif mutation == "boolean_ordinal":
        item["ordinal"] = True
    elif mutation == "float_ordinal":
        item["ordinal"] = 0.0
    elif mutation == "copied_source_record":
        item["record"] = dict(source.record)
    elif mutation == "different_right_record":
        value.right["items"] = [{"ordinal": 0, "record": {"value": 2}}]
    expected = encode_record(value)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert (
        encode_record(value, shared_json_sides=True, shared_source=source) == expected
    )
    assert len(calls) == 1
    assert "left" not in calls[0].get("exclude", set())


@pytest.mark.parametrize("kind", ["lookalike", "unicode_raw", "bytes_raw", "subclass"])
def test_only_exact_ascii_source_witness_can_skip_normalization(monkeypatch, kind):
    value, source = source_record({"value": "股"})
    if kind == "lookalike":
        source = SimpleNamespace(raw=source.raw, record=source.record)
    elif kind == "unicode_raw":
        source = SourceObservation(
            '{"value":"股"}', source.record, source.semantic_size
        )
    elif kind == "bytes_raw":
        source = SourceObservation(
            source.raw.encode(), source.record, source.semantic_size
        )
    elif kind == "subclass":

        class Derived(SourceObservation):
            pass

        source = Derived(source.raw, source.record, source.semantic_size)
    expected = encode_record(value)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert (
        encode_record(value, shared_json_sides=True, shared_source=source) == expected
    )
    assert calls == [{"mode": "json", "exclude": {"right"}}]


def test_source_parameter_without_opt_in_never_skips_any_serializer():
    calls = []

    class Stateful(BaseModel):
        @model_serializer
        def serialize(self):
            calls.append(None)
            return {"call": len(calls)}

    _value, source = source_record({"value": 1})
    # Even an unrelated genuine internal witness has no power without the
    # producer's explicit shared-JSON opt-in. Both arbitrary Any sides serialize.
    item = record({"items": [Stateful()], "count": 1})
    raw, _ = encode_record(item, shared_source=source)
    assert calls == [None, None]
    body = json.loads(raw)[1]
    assert body["left"]["items"] == [{"call": 1}]
    assert body["right"]["items"] == [{"call": 2}]


@pytest.mark.parametrize(
    "depth",
    [0, 1, 2, 7, 8, 9, 247, 248, 249, 250, 251, 252, 253, 254, 255, 256, 260, 300],
)
def test_source_reuse_preserves_pydantic_depth_boundary_exactly(monkeypatch, depth):
    nested = 0
    for _ in range(depth):
        nested = [nested]
    # At depth 255 this source is only 1,016 bytes, and its many flat members
    # make retained-allocation admission possible. Size alone is not a depth
    # proof: the old codec rejects it during Pydantic mode=json normalization.
    payload = {f"k{index:02}": 0 for index in range(60)}
    payload.update(nested=nested, source=None)
    value, source = source_record(payload)
    if depth == 255:
        assert len(source.raw) == 1016
        with pytest.raises(ValueError, match="Circular reference detected") as baseline:
            encode_record(value)
        with pytest.raises(type(baseline.value), match="Circular reference detected"):
            encode_record(value, shared_json_sides=True, shared_source=source)
    else:
        try:
            expected = encode_record(value)
        except ValueError as error:
            with pytest.raises(type(error)) as actual:
                encode_record(value, shared_json_sides=True, shared_source=source)
            assert str(actual.value) == str(error)
        else:
            assert (
                encode_record(value, shared_json_sides=True, shared_source=source)
                == expected
            )

    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    try:
        encode_record(value, shared_json_sides=True, shared_source=source)
    except ValueError:
        pass
    assert calls == [
        {"mode": "json", "exclude": {"left", "right"} if depth <= 7 else {"right"}}
    ]


@pytest.mark.parametrize(
    "value",
    [
        {"nested": {"deeper": [{} for _ in range(8)]}},
        [[] for _ in range(8)],
        [{"value": 1} for _ in range(8)],
        {"nested": [{} for _ in range(8)]},
    ],
)
def test_many_json_containers_keep_pydantic_normalization(monkeypatch, value):
    item, source = source_record({"value": value})
    assert source.raw.count("{") + source.raw.count("[") > 8
    expected = encode_record(item)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert encode_record(item, shared_json_sides=True, shared_source=source) == expected
    assert calls == [{"mode": "json", "exclude": {"right"}}]


@pytest.mark.parametrize("text", ["{" * 8, "[" * 8, '\\"{[' * 4])
def test_structural_opens_inside_strings_only_decline_optimization(monkeypatch, text):
    item, source = source_record({"value": text})
    assert source.raw.count("{") + source.raw.count("[") > 8
    expected = encode_record(item)
    calls = []
    original = ComparisonRecord.model_dump

    def tracked(self, *args, **kwargs):
        calls.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ComparisonRecord, "model_dump", tracked)
    assert encode_record(item, shared_json_sides=True, shared_source=source) == expected
    assert calls == [{"mode": "json", "exclude": {"right"}}]


def test_cyclic_any_evidence_keeps_original_normalization_error():
    _item, source = source_record({"value": 1})
    cyclic = []
    cyclic.append(cyclic)
    value = record({"items": [{"ordinal": 0, "record": {"value": cyclic}}], "count": 1})
    with pytest.raises(ValueError, match="Circular reference detected") as baseline:
        encode_record(value)
    for kwargs in (
        {"shared_json_sides": True},
        {"shared_json_sides": True, "shared_source": source},
    ):
        with pytest.raises(type(baseline.value)) as actual:
            encode_record(value, **kwargs)
        assert str(actual.value) == str(baseline.value)
