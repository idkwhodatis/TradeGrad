"""Lossless physical comparison encoding, legacy reads and honest bounds."""

from __future__ import annotations

import hashlib
import json
import math
import random
from uuid import uuid4

import pytest

from packages.research_trace.comparison import ComparisonControl, ComparisonReader
from packages.research_trace.comparison_codec import (
    PAYLOAD_CODEC,
    PROJECTED_FIELDS,
    _scalar_size_sql,
    decode_record,
    encode_record,
    unsafe_numeric_sql,
)
from packages.research_trace.comparison_contracts import (
    ComparisonError,
    ComparisonLimits,
    ComparisonPublication,
    ComparisonRecord,
    EventNamespace,
)
from packages.research_trace.comparison_storage import _OUTPUT_SCHEMA, ComparisonWriter
from packages.research_trace.contracts import canonical_bytes, digest
from tests.research_trace.test_comparison import make_input, run_compare


def record(**updates):
    return ComparisonRecord.model_validate(
        {
            "row_id": "row-1",
            "section": "events",
            "event_pair_id": "event-1",
            "session_date": "2024-01-02",
            "instrument_id": "股票",
            "status": "equal",
            "left_state": "observed",
            "right_state": "observed",
            "left": {},
            "right": {},
            "match_basis": "source_event",
            **updates,
        }
    )


def projection(value):
    return {key: getattr(value, key) for key in PROJECTED_FIELDS}


@pytest.mark.parametrize(
    "left,right",
    [
        (None, None),
        ({}, {"missing": None}),
        ({"flag": False}, {"flag": 0}),
        ({"flag": True}, {"flag": 1}),
        ({"value": 1}, {"value": 1.0}),
        ({"value": -0.0}, {"value": 0.0}),
        ({"score": 1.0}, {"score": 1.0 + 1e-13}),
        ({"nested": {"default": False, "null": None, "array": []}}, {}),
        ({"items": [{"ordinal": 1}, {"ordinal": 1}]}, {"items": []}),
        (
            {"pre_state": {"cash": 100, "open_orders": ["left"]}},
            {"pre_state": {"cash": 90, "open_orders": ["right"]}},
        ),
        ({"codec": PAYLOAD_CODEC, "record": "not an envelope"}, {}),
    ],
)
def test_full_public_canonical_bytes_round_trip(left, right):
    original = record(left=left, right=right)
    encoded, size = encode_record(original)
    decoded, actual = decode_record(encoded, projection(original), size)
    assert size == actual == len(canonical_bytes(original))
    assert canonical_bytes(decoded) == canonical_bytes(original)
    assert json.loads(encoded)[1]["left"] == original.left
    assert json.loads(encoded)[1]["right"] == original.right


def test_ambiguity_states_and_every_order_association_round_trip():
    associations = [
        {
            "side": side,
            "event_pair_id": f"event-{index}",
            "decision_id": f"decision-{side}-{index}",
            "stage_id": "orders",
            "logical_order_id": f"logical-{side}-{index}",
            "instrument_id": "B",
            "link_ordinal": index,
            "source_event_paired": False,
            "child_order_ids": [f"child-{side}-{index}", f"child-{side}-{index}"],
            "fill_ids": [f"fill-{side}-{index}", "shared-fill"],
        }
        for side in ("left", "right")
        for index in range(2)
    ]
    original = record(
        section="orders",
        status="ambiguous",
        ambiguous=True,
        left_state="unpaired",
        right_state="incomplete_coverage",
        missing_reason="Unknown, not false or absent",
        associations=associations,
        left={"order_graph": [{"children": ["a", "b", "a"]}]},
        right={"order_graph": [{"children": ["b", "a", "a"]}]},
    )
    encoded, size = encode_record(original)
    decoded, _ = decode_record(encoded, projection(original), size)
    assert canonical_bytes(decoded) == canonical_bytes(original)
    assert len(decoded.associations) == 4


def test_only_frozen_top_level_model_defaults_are_omitted():
    value = record(
        left={"ambiguous": False, "associations": [], "missing_reason": None}
    )
    encoded, size = encode_record(value)
    body = json.loads(encoded)[1]
    assert {"ambiguous", "associations", "missing_reason"}.isdisjoint(body)
    assert body["left"] == value.left
    explicit = value.model_dump(mode="json")
    decoded, _ = decode_record(encoded, projection(value), size)
    assert decoded.model_dump(mode="json") == explicit


def test_sql_preflight_matches_escaped_scalars_and_preserves_canonical_numbers():
    rng = random.Random(82)
    control = ComparisonControl()
    with control.connection() as connection:
        for value in (None, "", '\x00\x7f\n\t"\\', "股票é", "😀\u2028\uffff"):
            actual = connection.execute(
                "SELECT "
                + _scalar_size_sql("value")
                + " FROM (SELECT ?::VARCHAR AS value)",
                [value],
            ).fetchone()[0]
            assert actual == len(canonical_bytes(value))
        values = [
            math.ldexp(rng.random(), rng.randint(-1024, 1023)) for _ in range(1000)
        ]
        values += [0, -0.0, 1e-5, 1e-4, 1e15, 1e16, 5e-324, "1e15", '"0.1e+16"']
        assert not connection.execute(
            "SELECT " + unsafe_numeric_sql() + " FROM (SELECT ? AS payload)",
            [canonical_bytes({"values": values}).decode()],
        ).fetchone()[0]
        for number in (
            "1e15",
            "0.1e+16",
            "1e1",
            "1E+16",
            "1e+04",
            "1e-4",
            "1000000000000000128.0",
        ):
            assert connection.execute(
                "SELECT " + unsafe_numeric_sql() + " FROM (SELECT ? AS payload)",
                ['{"value":' + number + "}"],
            ).fetchone()[0]


@pytest.mark.parametrize("value", [0, 1, "false"])
def test_noncanonical_typed_booleans_are_not_coerced(value):
    original = record()
    encoded, size = encode_record(original)
    body = json.loads(encoded)
    body[1]["ambiguous"] = value
    with pytest.raises(ValueError):
        decode_record(canonical_bytes(body), projection(original), size)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: ["unknown-codec", v[1]],
        lambda v: [True, v[1]],
        lambda v: [*v, {}],
        lambda v: [v[0], []],
        lambda v: [v[0], {**v[1], "event_pair_id": "override"}],
        lambda v: [v[0], {k: x for k, x in v[1].items() if k != "left"}],
        lambda v: [v[0], {**v[1], "unknown": False}],
    ],
)
def test_malformed_codec_fails_closed(mutate):
    value = record()
    encoded, size = encode_record(value)
    with pytest.raises(ComparisonError):
        decode_record(
            canonical_bytes(mutate(json.loads(encoded))), projection(value), size
        )


@pytest.mark.parametrize("size", [None, 0, -1, True, 2.5, "500", 500])
def test_invalid_or_forged_expanded_size_fails_closed(size):
    value = record()
    encoded, _ = encode_record(value)
    with pytest.raises(ComparisonError):
        decode_record(encoded, projection(value), size)


def test_duplicate_json_key_and_nonfinite_values_are_not_silently_normalized():
    value = record()
    encoded, size = encode_record(value)
    malformed = encoded.replace(b'"left":{}', b'"left":{},"left":null')
    with pytest.raises(ComparisonError):
        decode_record(malformed, projection(value), size)
    malformed = encoded.replace(b'"left":{}', b'"left":{"value":1e999}')
    with pytest.raises(ComparisonError):
        decode_record(malformed, projection(value), size)
    malformed = encoded.replace(b'"left":{}', b'"left":{"value":NaN}')
    with pytest.raises(ComparisonError):
        decode_record(malformed, projection(value), size)
    with pytest.raises(ValueError):
        encode_record(record(left={"value": float("nan")}))


def test_expanded_record_and_block_guards_remain_strict(tmp_path):
    value = record(left={"large": "x" * 710})
    encoded, expanded = encode_record(value)
    assert len(encoded) < 1024 < expanded
    control = ComparisonControl()
    with control.connection() as connection:
        writer = ComparisonWriter(
            tmp_path,
            ComparisonPublication(
                comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
            ),
            control,
            connection,
            limits=ComparisonLimits(max_record_bytes=1024),
        )
        with pytest.raises(ComparisonError) as caught:
            writer.append(value)
        assert caught.value.code == "comparison_capacity"
        assert writer.total_rows == 0
    assert control.closure_confirmed


def test_block_and_aggregate_accounting_do_not_truncate_or_expand_blocks(tmp_path):
    value = record(left={"large": "x" * 210})
    raw, expanded = encode_record(value)
    control = ComparisonControl()
    with control.connection() as connection:
        writer = ComparisonWriter(
            tmp_path,
            ComparisonPublication(
                comparison_id=uuid4(), run_id=uuid4(), execution_attempt_id=uuid4()
            ),
            control,
            connection,
            limits=ComparisonLimits(block_bytes=1024),
        )
        writer.append(value)
        writer.append(value.model_copy(update={"row_id": "row-2"}))
        assert expanded * 2 > 1024 > len(raw) * 2
        assert len(writer.refs) == 1 and writer.refs[0].rows == 1
        assert writer.total_bytes == 2 * len(raw)
        assert writer.total_expanded_bytes == 2 * expanded
        assert writer.sizes["events"] == expanded
        writer.finish_blocks()
        assert sum(ref.rows for ref in writer.refs) == 2
    assert control.closure_confirmed


def _write_rows(path, rows, connection, *, legacy=False):
    source = path.with_suffix(".jsonl")
    source.write_bytes(b"\n".join(canonical_bytes(row) for row in rows))
    columns = {
        k: v
        for k, v in _OUTPUT_SCHEMA.items()
        if not legacy or k != "expanded_payload_bytes"
    }
    output = path.with_name("new-" + path.name)
    connection.execute(
        f"COPY (SELECT * FROM read_json($source, format='newline_delimited', columns={canonical_bytes(columns).decode()})) TO $output (FORMAT PARQUET)",
        {"source": str(source), "output": str(output)},
    )
    output.replace(path)
    source.unlink()


def _fixture(tmp_path):
    ns = EventNamespace(
        dataset_version_id=uuid4(),
        dataset_sha256="a" * 64,
        universe_version_id=None,
        universe_sha256=None,
        frozen_universe_sha256="b" * 64,
        instrument_namespace="native_symbol",
    )
    left = make_input(tmp_path / "left", ns, event_ids=("frozen:one", "frozen:two"))
    right = make_input(
        tmp_path / "right", ns, suffix="b", event_ids=("frozen:one", "frozen:two")
    )
    return run_compare(
        tmp_path / "out", left, right, limits=ComparisonLimits(block_rows=1)
    )


@pytest.mark.parametrize("mode", ["legacy", "mixed"])
def test_legacy_and_mixed_blocks_preserve_all_public_queries(tmp_path, mode):
    manifest, reader, artifacts = _fixture(tmp_path)
    expected = {}
    events = reader.page("events").items
    for section in manifest.sections:
        for options in (
            {},
            {"offset": 1, "limit": 1},
            {"instrument_id": "B"},
            {"event_pair_id": events[0].event_pair_id},
            {"event_pair_id": events[1].event_pair_id},
            {"event_pair_id": events[0].event_pair_id, "instrument_id": "B"},
        ):
            expected[section, tuple(options.items())] = canonical_bytes(
                reader.page(section, **options)
            )
    refs = []
    control = ComparisonControl()
    with control.connection() as connection:
        for index, ref in enumerate(manifest.artifacts):
            path = reader.paths[ref.name]
            if mode == "legacy" or index % 2 == 0:
                cursor = connection.execute(
                    "SELECT * FROM read_parquet(?)", [str(path)]
                )
                keys = [column[0] for column in cursor.description]
                rows = [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
                for row in rows:
                    projected = {
                        key: ref.section if key == "section" else row[key]
                        for key in PROJECTED_FIELDS
                    }
                    value, _ = decode_record(
                        row["payload"], projected, row.pop("expanded_payload_bytes")
                    )
                    row["payload"] = canonical_bytes(value).decode()
                _write_rows(path, rows, connection, legacy=True)
            refs.append(
                ref.model_copy(
                    update={
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size_bytes": path.stat().st_size,
                    }
                )
            )
    legacy_manifest = manifest.model_copy(update={"artifacts": refs})
    manifest_bytes = canonical_bytes(legacy_manifest)
    loaded = type(manifest).model_validate_json(manifest_bytes)
    before = {
        ref.name: hashlib.sha256(reader.paths[ref.name].read_bytes()).hexdigest()
        for ref in refs
    }
    legacy_reader = ComparisonReader(loaded, reader.paths)
    for (section, options), value in expected.items():
        assert canonical_bytes(legacy_reader.page(section, **dict(options))) == value
    assert digest(loaded) == hashlib.sha256(manifest_bytes).hexdigest()
    assert {
        ref.name: hashlib.sha256(reader.paths[ref.name].read_bytes()).hexdigest()
        for ref in refs
    } == before
    assert all(item.path.exists() for item in artifacts)


def test_expanded_page_guard_runs_before_payload_decoder(tmp_path, monkeypatch):
    import packages.research_trace.comparison_storage as storage

    manifest, reader, _ = _fixture(tmp_path)
    ref = next(item for item in manifest.artifacts if item.section == "events")
    path = reader.paths[ref.name]
    control = ComparisonControl()
    with control.connection() as connection:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        keys = [column[0] for column in cursor.description]
        rows = [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
        rows[0]["expanded_payload_bytes"] = storage.MAX_RESPONSE_BYTES + 1
        _write_rows(path, rows, connection)
    ref = ref.model_copy(
        update={
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    )
    manifest = manifest.model_copy(update={"artifacts": [ref]})
    monkeypatch.setattr(
        storage,
        "decode_record",
        lambda *a: pytest.fail("materialized oversized expanded payload"),
    )
    with pytest.raises(ComparisonError) as caught:
        ComparisonReader(manifest, reader.paths).page("events")
    assert caught.value.code == "comparison_integrity"


@pytest.mark.parametrize(
    "text",
    ["\x00" * 800_000, "股票" * 400_000, '"' * 2_200_000, "😀" * 400_000],
    ids=["nul-800k", "cjk-400k", "quotes-2200k", "astral-400k"],
)
@pytest.mark.parametrize("forged", [False, True])
def test_projected_escape_expansion_is_rejected_before_fetch(
    tmp_path, monkeypatch, text, forged
):
    import packages.research_trace.comparison_storage as storage

    manifest, reader, _ = _fixture(tmp_path)
    ref = next(item for item in manifest.artifacts if item.section == "events")
    path = reader.paths[ref.name]
    control = ComparisonControl()
    with control.connection() as connection:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        keys = [column[0] for column in cursor.description]
        rows = [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
        projected = {
            key: ref.section if key == "section" else rows[0][key]
            for key in PROJECTED_FIELDS
        }
        value, _ = decode_record(
            rows[0]["payload"], projected, rows[0]["expanded_payload_bytes"]
        )
        value = value.model_copy(update={"instrument_id": text})
        raw, size = encode_record(value)
        assert len(raw) < storage.MAX_RESPONSE_BYTES < size
        rows[0].update(
            payload=raw.decode(),
            instrument_id=text,
            expanded_payload_bytes=1 if forged else size,
        )
        _write_rows(path, rows, connection)
    ref = ref.model_copy(
        update={
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    )
    manifest = manifest.model_copy(update={"artifacts": [ref]})
    monkeypatch.setattr(
        storage,
        "decode_record",
        lambda *a: pytest.fail("materialized oversized projected evidence"),
    )
    with pytest.raises(ComparisonError) as caught:
        ComparisonReader(manifest, reader.paths).page("events")
    assert caught.value.code == (
        "comparison_integrity" if forged else "comparison_detail_too_large"
    )


@pytest.mark.parametrize(
    "attack",
    [
        "literal_unicode_core",
        "numeric_core",
        "numeric_fixed_core",
        "legacy_large_projection",
    ],
)
def test_noncanonical_core_and_corrupt_legacy_projection_fail_before_decoder(
    tmp_path, monkeypatch, attack
):
    import packages.research_trace.comparison_storage as storage

    manifest, reader, _ = _fixture(tmp_path)
    ref = next(item for item in manifest.artifacts if item.section == "events")
    path = reader.paths[ref.name]
    control = ComparisonControl()
    with control.connection() as connection:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        keys = [column[0] for column in cursor.description]
        rows = [dict(zip(keys, row, strict=True)) for row in cursor.fetchall()]
        projected = {
            key: ref.section if key == "section" else rows[0][key]
            for key in PROJECTED_FIELDS
        }
        value, _ = decode_record(
            rows[0]["payload"], projected, rows[0]["expanded_payload_bytes"]
        )
        if attack in {"literal_unicode_core", "numeric_core", "numeric_fixed_core"}:
            value = value.model_copy(update={"left": {"large": "股" * 800_000}})
            if attack == "numeric_core":
                value = value.model_copy(update={"left": {"values": [1e15] * 240_000}})
            elif attack == "numeric_fixed_core":
                value = value.model_copy(
                    update={"left": {"values": [1.0000000000000001e18] * 185_000}}
                )
            raw, size = encode_record(value)
            malformed = (
                raw.decode().replace("1000000000000000.0", "1e15")
                if attack == "numeric_core"
                else raw.decode().replace(
                    "1.0000000000000001e+18", "1000000000000000128.0"
                )
                if attack == "numeric_fixed_core"
                else json.dumps(
                    json.loads(raw), ensure_ascii=False, separators=(",", ":")
                )
            )
            rows[0].update(
                payload=malformed,
                expanded_payload_bytes=size - len(raw) + len(malformed.encode()),
            )
        else:
            rows[0].update(
                payload=canonical_bytes(value).decode(),
                expanded_payload_bytes=None,
                instrument_id="x" * (storage.MAX_RESPONSE_BYTES + 1),
            )
        _write_rows(path, rows, connection)
    ref = ref.model_copy(
        update={
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }
    )
    manifest = manifest.model_copy(update={"artifacts": [ref]})
    monkeypatch.setattr(
        storage,
        "decode_record",
        lambda *a: pytest.fail("materialized malformed amplified evidence"),
    )
    with pytest.raises(ComparisonError):
        ComparisonReader(manifest, reader.paths).page("events")
