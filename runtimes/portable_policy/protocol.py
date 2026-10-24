"""Framing shared by the trusted relay and both isolated runtime images.

These helpers do not implement host deadlines: the relay uses nonblocking FDs.
The blocking helpers are for resource-limited containers only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from uuid import UUID

from packages.strategy_sdk.json import JsonLimits, canonical_json, parse_json

PROTOCOL_VERSION = "tradegrad.portable-wire.v1"
MAX_OBSERVATION_BYTES = 4 * 1024 * 1024
MAX_REPLY_BYTES = 256 * 1024
MAX_STARTUP_BYTES = 32 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_DECISIONS = 100_000
MAX_ENGINE_AUDIT_BYTES = 4 * 1024 * 1024 + 64 * 1024
AUDIT_CHUNK_BYTES = 36 * 1024


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


def policy_startup_envelope(evaluation_id, rng_state, *, restored_from_sha256=None):
    """The reviewed bootstrap emits this before importing candidate code.

    This records only the declared RNG seam, not a general checkpoint or an
    independent proof about randomness used later by the untrusted policy.
    """
    from packages.tradegrad_adapter.runtime_observation import validate_rng_state

    _identity(evaluation_id)
    validate_rng_state(rng_state, include_numpy=False)
    if restored_from_sha256 is not None and (
        type(restored_from_sha256) is not str
        or len(restored_from_sha256) != 64
        or any(c not in "0123456789abcdef" for c in restored_from_sha256)
    ):
        raise ValueError("Invalid portable restored RNG digest")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "policy_startup",
        "evaluation_id": evaluation_id,
        "rng_state": rng_state,
        "restored_from_sha256": restored_from_sha256,
    }


def validate_policy_startup(value, evaluation_id):
    if not isinstance(value, dict) or set(value) != {
        "protocol_version",
        "kind",
        "evaluation_id",
        "rng_state",
        "restored_from_sha256",
    }:
        raise ValueError("Invalid portable policy startup schema")
    expected = policy_startup_envelope(
        evaluation_id,
        value["rng_state"],
        restored_from_sha256=value["restored_from_sha256"],
    )
    if value != expected:
        raise ValueError("Portable policy startup identity mismatch")
    # Also enforce the startup-specific budget for callers not using framing.
    canonical_json(value, limits=limits(MAX_STARTUP_BYTES))
    return value["rng_state"]


def engine_audit_envelope(evaluation_id, sequence, audit):
    """Keep exact native int64 timestamps outside the interoperable JSON ABI."""
    from packages.tradegrad_adapter.runtime_observation import canonical, validate_audit

    _identity(evaluation_id)
    _sequence(sequence)
    validate_audit(audit)
    data = canonical(audit)
    if len(data) > MAX_ENGINE_AUDIT_BYTES:
        raise ValueError("Portable engine audit exceeds its byte budget")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "engine_audit",
        "evaluation_id": evaluation_id,
        "sequence": sequence,
        "encoding": "base64-json.v1",
        "sha256": hashlib.sha256(data).hexdigest(),
        "chunks": [
            base64.b64encode(data[offset : offset + AUDIT_CHUNK_BYTES]).decode("ascii")
            for offset in range(0, len(data), AUDIT_CHUNK_BYTES)
        ],
    }


def validate_engine_audit(value, evaluation_id, sequence):
    from packages.tradegrad_adapter.runtime_observation import canonical, validate_audit

    _identity(evaluation_id)
    _sequence(sequence)
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "protocol_version",
            "kind",
            "evaluation_id",
            "sequence",
            "encoding",
            "sha256",
            "chunks",
        }
        or (
            value["protocol_version"],
            value["kind"],
            value["evaluation_id"],
            value["sequence"],
            value["encoding"],
        )
        != (PROTOCOL_VERSION, "engine_audit", evaluation_id, sequence, "base64-json.v1")
        or type(value["sequence"]) is not int
    ):
        raise ValueError("Invalid portable engine audit envelope")
    chunks = value["chunks"]
    if (
        not isinstance(chunks, list)
        or not 1 <= len(chunks) <= 120
        or any(
            type(chunk) is not str
            or not chunk
            or len(chunk) > 4 * ((AUDIT_CHUNK_BYTES + 2) // 3)
            for chunk in chunks
        )
    ):
        raise ValueError("Invalid portable engine audit chunks")
    data = b"".join(base64.b64decode(chunk, validate=True) for chunk in chunks)
    if (
        len(data) > MAX_ENGINE_AUDIT_BYTES
        or hashlib.sha256(data).hexdigest() != value["sha256"]
    ):
        raise ValueError("Portable engine audit bytes or checksum mismatch")
    try:
        audit = json.loads(data)
        # Canonical equality also rejects duplicate keys, nonfinite numbers,
        # whitespace/trailing bytes and alternate encodings before validation.
        if canonical(audit) != data:
            raise ValueError("Noncanonical portable engine audit")
        validate_audit(audit)
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Invalid portable engine audit complexity") from exc
    return audit


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
