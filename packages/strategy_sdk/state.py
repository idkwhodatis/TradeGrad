"""Version-pinned, checksum-verified state snapshots with detached restoration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Contract, Hash, Name
from .json import STATE_LIMITS, canonical_json, hash_json, parse_json


class StateField(Contract):
    name: Name
    kind: Literal["string", "integer", "number", "boolean", "object", "array", "null"]
    required: bool = True


class StateSchema(Contract):
    name: Name
    version: int = Field(ge=1, le=1_000_000)
    fields: tuple[StateField, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_fields(self):
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("duplicate state schema field")
        return self

    @property
    def sha256(self) -> str:
        return hash_json(self)


class StateSnapshot(Contract):
    codec_version: Literal["state-json.v1"] = "state-json.v1"
    schema_name: Name
    schema_version: int = Field(ge=1, le=1_000_000)
    schema_sha256: Hash
    payload_json: str = Field(max_length=STATE_LIMITS.max_bytes)
    payload_sha256: Hash

    @model_validator(mode="after")
    def valid_payload(self):
        payload = parse_json(self.payload_json, limits=STATE_LIMITS)
        if type(payload) is not dict:
            raise ValueError("state must be a JSON object")
        if canonical_json(payload, limits=STATE_LIMITS).decode() != self.payload_json:
            raise ValueError("state payload must be canonical JSON")
        if hash_json(payload, limits=STATE_LIMITS) != self.payload_sha256:
            raise ValueError("state payload checksum mismatch")
        return self


class StateCodec:
    """Validate this exact schema; never infer or silently migrate old versions.

    Object and array fields allow bounded finite JSON, not arbitrary Python
    objects. Restored dictionaries are detached copies, not mutable snapshots.
    """

    def __init__(self, schema: StateSchema):
        self.schema = StateSchema.model_validate(schema)

    def _validate(self, payload: Any) -> dict:
        canonical_json(payload, limits=STATE_LIMITS)
        if type(payload) is not dict:
            raise ValueError("state must be a JSON object")
        fields = {field.name: field for field in self.schema.fields}
        if set(payload) - set(fields):
            raise ValueError("state contains undeclared fields")
        types = {
            "string": (str,),
            "integer": (int,),
            "number": (int, float),
            "boolean": (bool,),
            "object": (dict,),
            "array": (list,),
            "null": (type(None),),
        }
        for name, field in fields.items():
            if name not in payload:
                if field.required:
                    raise ValueError(f"missing state field: {name}")
            elif type(payload[name]) not in types[field.kind]:
                raise ValueError(f"state field type mismatch: {name}")
        return payload

    def encode(self, payload: dict) -> StateSnapshot:
        self._validate(payload)
        return StateSnapshot(
            schema_name=self.schema.name,
            schema_version=self.schema.version,
            schema_sha256=self.schema.sha256,
            payload_json=canonical_json(payload, limits=STATE_LIMITS).decode(),
            payload_sha256=hash_json(payload, limits=STATE_LIMITS),
        )

    def decode(self, snapshot: StateSnapshot) -> dict:
        snapshot = StateSnapshot.model_validate(snapshot)
        if (snapshot.schema_name, snapshot.schema_version, snapshot.schema_sha256) != (
            self.schema.name,
            self.schema.version,
            self.schema.sha256,
        ):
            raise ValueError(
                "state schema/version mismatch; migration must be explicit"
            )
        return self._validate(parse_json(snapshot.payload_json, limits=STATE_LIMITS))

    def dumps(self, payload: dict) -> bytes:
        return canonical_json(self.encode(payload))

    def loads(self, raw: bytes | str) -> dict:
        value = parse_json(raw)
        return self.decode(StateSnapshot.model_validate_json(canonical_json(value)))
