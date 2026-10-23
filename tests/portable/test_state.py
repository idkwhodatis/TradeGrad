"""Offline finite JSON and versioned state boundary tests, never policy replay."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from packages.strategy_sdk import (
    JsonLimits,
    StateCodec,
    StateField,
    StateSchema,
    StateSnapshot,
    canonical_json,
    hash_json,
    parse_json,
)


def schema(**changes):
    fields = {
        "name": "reference",
        "version": 1,
        "fields": (
            StateField(name="count", kind="integer"),
            StateField(name="meta", kind="object"),
        ),
    }
    fields.update(changes)
    return StateSchema(**fields)


def test_state_is_canonical_immutable_detached_and_roundtrippable():
    codec = StateCodec(schema())
    original = {"meta": {"scores": [1.0, 2.0], "valid": True}, "count": 1}
    state = codec.encode(original)
    original["meta"]["scores"].append(99.0)
    assert codec.decode(state) == {
        "count": 1,
        "meta": {"scores": [1.0, 2.0], "valid": True},
    }
    restored = codec.decode(state)
    restored["meta"]["scores"].append(99.0)
    assert codec.decode(state)["meta"]["scores"] == [1.0, 2.0]
    assert codec.loads(codec.dumps(codec.decode(state))) == codec.decode(state)
    assert (
        state.payload_sha256 == hashlib.sha256(state.payload_json.encode()).hexdigest()
    )
    assert state.schema_sha256 == schema().sha256
    with pytest.raises(ValidationError):
        state.payload_json = "{}"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"count": True, "meta": {}},
        {"count": "1", "meta": {}},
        {"count": 1.0, "meta": {}},
        {"count": 1, "meta": []},
        {"count": 1, "meta": {}, "other": 42},
        [],
        None,
        {"count": 1, "meta": {"bad": float("nan")}},
        {"count": 1, "meta": {"bad": float("inf")}},
        {"count": 2**53, "meta": {}},
    ],
)
def test_state_rejects_undeclared_mistyped_or_nonfinite_data(payload):
    with pytest.raises(ValueError):
        StateCodec(schema()).encode(payload)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("string", "hello"),
        ("integer", 1),
        ("number", 1),
        ("number", 1.5),
        ("boolean", True),
        ("object", {"nested": [True, None, 2]}),
        ("array", [1, 2]),
        ("null", None),
    ],
)
def test_schema_supported_json_types(kind, value):
    codec = StateCodec(
        StateSchema(name="all", version=1, fields=(StateField(name="key", kind=kind),))
    )
    assert codec.decode(codec.encode({"key": value})) == {"key": value}


def test_optional_fields_and_empty_state():
    codec = StateCodec(
        StateSchema(
            name="optional",
            version=1,
            fields=(StateField(name="key", kind="string", required=False),),
        )
    )
    assert codec.decode(codec.encode({})) == {}
    with pytest.raises(ValueError):
        codec.encode({"key": None})
    assert (
        StateCodec(StateSchema(name="empty", version=1, fields=()))
        .encode({})
        .payload_json
        == "{}"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_name": "other"},
        {"schema_version": 2},
        {"schema_sha256": "b" * 64},
        {"payload_sha256": "b" * 64},
        {"codec_version": "state-json.v2"},
        {"payload_json": '{"count": 1,"meta":{}}'},
    ],
)
def test_recovery_rejects_wrong_version_schema_digest_or_noncanonical_payload(changes):
    codec = StateCodec(schema())
    payload = codec.encode({"count": 1, "meta": {}}).model_dump(mode="json")
    payload.update(changes)
    with pytest.raises(ValueError):
        codec.loads(canonical_json(payload))


def test_shape_change_cannot_reuse_schema_name_and_version():
    original = StateCodec(schema()).encode({"count": 1, "meta": {}})
    changed = StateSchema(
        name="reference",
        version=1,
        fields=(
            StateField(name="count", kind="number"),
            StateField(name="meta", kind="object"),
        ),
    )
    with pytest.raises(ValueError, match="schema/version mismatch"):
        StateCodec(changed).decode(original)
    with pytest.raises(ValueError):
        StateSchema(
            name="duplicate",
            version=1,
            fields=(
                StateField(name="x", kind="integer"),
                StateField(name="x", kind="integer"),
            ),
        )


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1,"x":2}',
        '{"nested":{"x":1,"x":2}}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":-Infinity}',
        '{"x":1e999}',
        '{"x":9007199254740992}',
        '{"x":"\\ud800"}',
        b"\xff",
        "[1,]",
        "{} trailing",
    ],
)
def test_untrusted_json_rejects_duplicates_nonfinite_unicode_and_ambiguous_numbers(raw):
    with pytest.raises(ValueError):
        parse_json(raw)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        2**53,
        {1: "nonstring key"},
        {"value": set()},
        (1, 2),
        b"bytes",
        "\ud800",
    ],
)
def test_canonical_json_has_no_object_hooks_or_lossy_coercion(value):
    with pytest.raises(ValueError):
        canonical_json(value)


def test_canonical_sorted_unicode_json_stable_and_exact():
    a = {"z": [True, False, None, 1.25], "a": "股票"}
    b = {"a": "股票", "z": [True, False, None, 1.25]}
    assert canonical_json(a) == canonical_json(b)
    assert hash_json(a) == hash_json(b)
    assert parse_json(canonical_json(a)) == a
    assert canonical_json(a).decode().startswith('{"a":"股票"')


@pytest.mark.parametrize(
    "value,limits",
    [
        ("x" * 11, JsonLimits(max_string=10)),
        ([1, 2, 3], JsonLimits(max_items=2)),
        ({"a": 1, "b": 2}, JsonLimits(max_items=1)),
        ([[[[1]]]], JsonLimits(max_depth=3)),
        ([1, 2, 3], JsonLimits(max_nodes=3)),
        ("é" * 5, JsonLimits(max_bytes=10)),
    ],
)
def test_json_resource_limits_apply_on_encode_and_decode(value, limits):
    with pytest.raises(ValueError):
        canonical_json(value, limits=limits)
    with pytest.raises(ValueError):
        parse_json(json.dumps(value), limits=limits)


def test_deep_and_oversized_raw_json_rejects_without_recursion_escape():
    with pytest.raises(ValueError):
        parse_json("[" * 2000 + "0" + "]" * 2000)
    with pytest.raises(ValueError):
        parse_json(b" " * 101 + b"{}", limits=JsonLimits(max_bytes=100))
    with pytest.raises(ValueError):
        StateCodec(schema()).encode({"count": 1, "meta": {"data": "x" * 8193}})
    with pytest.raises(ValueError):
        StateCodec(schema()).encode({"count": 1, "meta": {"data": [0] * 1025}})


def test_state_model_revalidates_corrupt_copied_instances():
    codec = StateCodec(schema())
    valid = codec.encode({"count": 1, "meta": {}})
    corrupt = valid.model_copy(update={"payload_json": "{}"})
    with pytest.raises(ValueError):
        codec.decode(corrupt)
    with pytest.raises(ValueError):
        StateSnapshot.model_validate(corrupt)
