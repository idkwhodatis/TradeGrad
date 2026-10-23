"""M6 fixed-byte wire regressions; no engine, candidate, Docker or process runs."""

import base64
import io
import json
import struct
from pathlib import Path

import pytest

from packages.tradegrad_adapter.results import MAX_WIRE_BYTES, RESULT_PREFIX_V2
from runtimes.evaluator.runner import RESULT_PREFIX, decode_result_protocol
from runtimes.portable_policy.protocol import (
    MAX_OBSERVATION_BYTES,
    MAX_REPLY_BYTES,
    MAX_RESULT_BYTES,
    read_frame,
)


def metrics_wire():
    metrics = json.loads(
        (Path(__file__).parents[1] / "baseline/expected.json").read_text()
    )["cn_time"]
    return metrics, RESULT_PREFIX.encode() + base64.b64encode(
        json.dumps(metrics).encode()
    )


def test_warning_and_frozen_valid_metrics_parse_without_engine_execution(tmp_path):
    metrics, wire = metrics_wire()
    result, bundle = decode_result_protocol(
        b"warning: harmless diagnostic\n" + wire + b"\n", tmp_path
    )
    assert result == metrics and bundle is None
    assert json.loads((tmp_path / "metrics.json").read_bytes()) == metrics


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "invalid_base64",
        "duplicate",
        "mixed",
        "invalid_schema",
        "oversize_encoded",
        "oversize_decoded",
    ],
)
def test_invalid_result_wire_never_publishes_artifacts(tmp_path, case):
    _, wire = metrics_wire()
    variants = {
        "absent": b"warning only\n",
        "invalid_base64": RESULT_PREFIX_V2.encode() + b"not-base64?",
        "duplicate": wire + b"\n" + wire,
        "mixed": wire + b"\n" + RESULT_PREFIX_V2.encode() + b"e30=",
        "invalid_schema": RESULT_PREFIX.encode() + base64.b64encode(b"{}"),
    }
    if case == "oversize_encoded":
        value = RESULT_PREFIX_V2.encode() + b"A" * (4 * ((MAX_WIRE_BYTES + 2) // 3) + 4)
    elif case == "oversize_decoded":
        value = RESULT_PREFIX_V2.encode() + base64.b64encode(
            b" " * (MAX_WIRE_BYTES + 1)
        )
    else:
        value = variants[case]
    with pytest.raises(ValueError):
        decode_result_protocol(value, tmp_path)
    assert not list(tmp_path.iterdir())


def test_legacy_metric_record_cannot_replace_configured_result_bundle(tmp_path):
    _, wire = metrics_wire()
    with pytest.raises(ValueError, match="complete V2"):
        decode_result_protocol(wire, tmp_path, expected_config_sha256="a" * 64)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "maximum", [MAX_OBSERVATION_BYTES, MAX_REPLY_BYTES, MAX_RESULT_BYTES]
)
@pytest.mark.parametrize("length", ["empty", "over", "uint32_max"])
def test_each_portable_frame_ceiling_rejects_before_body_read(maximum, length):
    size = {"empty": 0, "over": maximum + 1, "uint32_max": 2**32 - 1}[length]

    class HeaderOnly:
        def __init__(self):
            self.reads = []

        def read(self, count):
            self.reads.append(count)
            assert len(self.reads) == 1, "Rejected frame attempted to read its body"
            assert count == 4
            return struct.pack("!I", size)

    stream = HeaderOnly()
    with pytest.raises(ValueError, match="budget"):
        read_frame(stream, maximum)
    assert stream.reads == [4]


def test_exact_portable_byte_ceiling_is_inclusive_and_truncation_is_not_success():
    value = b'{"ok":true}'
    framed = struct.pack("!I", len(value)) + value
    assert read_frame(io.BytesIO(framed), len(value)) == {"ok": True}
    with pytest.raises(EOFError, match="mid-frame"):
        read_frame(io.BytesIO(framed[:-1]), len(value))
