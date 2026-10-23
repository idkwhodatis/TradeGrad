"""Framing shared by the trusted relay and both isolated runtime images.

These helpers do not implement host deadlines: the relay uses nonblocking FDs.
The blocking helpers are for resource-limited containers only.
"""

from __future__ import annotations

import struct
from uuid import UUID

from packages.strategy_sdk.json import JsonLimits, canonical_json, parse_json

PROTOCOL_VERSION = "tradegrad.portable-wire.v1"
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 256 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_DECISIONS = 100_000


def limits(max_bytes):
    return JsonLimits(max_bytes=max_bytes, max_nodes=500_000, max_items=100_000)


def frame(value, max_bytes=MAX_OBSERVATION_BYTES):
    data = canonical_json(value, limits=limits(max_bytes))
    return struct.pack("!I", len(data)) + data


def decode(data, max_bytes=MAX_OBSERVATION_BYTES):
    if not data or len(data) > max_bytes:
        raise ValueError("Empty or oversized portable frame")
    value = parse_json(data, limits=limits(max_bytes))
    if not isinstance(value, dict):
        raise ValueError("Portable frame must be an object")  # noqa: TRY004 - wire validation
    return value


def read_frame(stream, max_bytes=MAX_OBSERVATION_BYTES):
    def exact(count):
        data = bytearray()
        while len(data) < count:
            chunk = stream.read(count - len(data))
            if not chunk:
                raise EOFError("Portable channel ended mid-frame")
            data.extend(chunk)
        return bytes(data)

    size = struct.unpack("!I", exact(4))[0]
    if not 1 <= size <= max_bytes:
        raise ValueError("Portable frame length exceeds budget")
    return decode(exact(size), max_bytes)


def write_frame(stream, value, max_bytes=MAX_OBSERVATION_BYTES):
    data = frame(value, max_bytes)
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if written is None or written <= 0:
            raise EOFError("Portable channel refused write")
        offset += written
    stream.flush()


def _identity(evaluation_id):
    if type(evaluation_id) is not str or str(UUID(evaluation_id)) != evaluation_id:
        raise ValueError("Invalid physical evaluation identity")
    return evaluation_id


def _sequence(sequence):
    if type(sequence) is not int or not 0 <= sequence <= MAX_DECISIONS:
        raise ValueError("Invalid portable decision sequence")


def observation_envelope(evaluation_id, sequence, observation):
    _identity(evaluation_id)
    _sequence(sequence)
    if hasattr(observation, "model_dump"):
        observation = observation.model_dump(mode="json")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "observation",
        "evaluation_id": evaluation_id,
        "sequence": sequence,
        "request_id": f"{evaluation_id}:{sequence}",
        "expected_state_sha256": observation["state"]["payload_sha256"],
        "observation": observation,
    }


def validate_request(value, evaluation_id, sequence):
    if not isinstance(value, dict) or set(value) != {
        "protocol_version",
        "kind",
        "evaluation_id",
        "sequence",
        "request_id",
        "expected_state_sha256",
        "observation",
    }:
        raise ValueError("Invalid observation envelope schema")
    _identity(evaluation_id)
    _sequence(sequence)
    if (
        value["protocol_version"],
        value["kind"],
        value["evaluation_id"],
        value["sequence"],
        value["request_id"],
    ) != (
        PROTOCOL_VERSION,
        "observation",
        evaluation_id,
        sequence,
        f"{evaluation_id}:{sequence}",
    ) or type(value["sequence"]) is not int:
        raise ValueError("Observation identity or sequence mismatch")
    if not isinstance(value["observation"], dict) or value[
        "expected_state_sha256"
    ] != value["observation"].get("state", {}).get("payload_sha256"):
        raise ValueError("Observation state digest mismatch")
    return value


def decision_envelope(request, reply):
    if hasattr(reply, "model_dump"):
        reply = reply.model_dump(mode="json")
    return {
        **{
            key: request[key]
            for key in (
                "protocol_version",
                "evaluation_id",
                "sequence",
                "request_id",
                "expected_state_sha256",
            )
        },
        "kind": "decision",
        "reply": reply,
    }


def validate_response(value, request):
    expected = decision_envelope(
        request, value.get("reply") if isinstance(value, dict) else None
    )
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("Invalid decision envelope schema")
    if type(value["sequence"]) is not int or any(
        value[key] != expected[key] for key in expected if key != "reply"
    ):
        raise ValueError("Decision identity, sequence or state mismatch")
    if not isinstance(value["reply"], dict):
        raise ValueError("Decision reply must be an object")  # noqa: TRY004 - wire validation
    return value


def result_envelope(evaluation_id, sequence, result):
    _identity(evaluation_id)
    _sequence(sequence)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "result",
        "evaluation_id": evaluation_id,
        "sequence": sequence,
        "result": result,
    }


def validate_result_frame(value, evaluation_id, sequence):
    if not isinstance(value, dict) or set(value) != {
        "protocol_version",
        "kind",
        "evaluation_id",
        "sequence",
        "result",
    }:
        raise ValueError("Invalid trusted result envelope")
    if type(value["sequence"]) is not int or value != result_envelope(
        evaluation_id, sequence, value["result"]
    ):
        raise ValueError("Trusted result identity or sequence mismatch")
    return value["result"]
