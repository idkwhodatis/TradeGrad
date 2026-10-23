"""Bounded finite JSON. This is portable-json.v1, not an RFC 8785 claim."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class JsonLimits:
    max_bytes: int = 4_194_304
    max_depth: int = 24
    max_nodes: int = 200_000
    max_items: int = 30_000
    max_string: int = 65_536


DEFAULT_LIMITS = JsonLimits()
STATE_LIMITS = JsonLimits(
    max_bytes=65_536, max_depth=12, max_nodes=8192, max_items=1024, max_string=8192
)
REPLY_LIMITS = JsonLimits(max_bytes=262_144, max_nodes=30_000, max_items=1024)


def _check(value: Any, limits: JsonLimits) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise ValueError("JSON complexity limit exceeded")
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if abs(item) > 2**53 - 1:
                raise ValueError("integer exceeds exact interoperable JSON range")
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("non-finite JSON number")
            return
        if type(item) is str:
            if len(item) > limits.max_string:
                raise ValueError("JSON string limit exceeded")
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise ValueError("invalid Unicode in JSON") from exc
            return
        if type(item) is list:
            if len(item) > limits.max_items:
                raise ValueError("JSON array limit exceeded")
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            if len(item) > limits.max_items:
                raise ValueError("JSON object limit exceeded")
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise ValueError("only plain finite JSON values are permitted")

    visit(value, 0)


def canonical_json(value: Any, *, limits: JsonLimits = DEFAULT_LIMITS) -> bytes:
    """Encode deterministic, UTF-8, sorted-key JSON, never object hooks/pickle."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    _check(value, limits)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > limits.max_bytes:
        raise ValueError("JSON byte limit exceeded")
    return encoded


def parse_json(raw: bytes | str, *, limits: JsonLimits = DEFAULT_LIMITS) -> Any:
    if type(raw) not in {str, bytes}:
        raise ValueError("JSON transport must be bytes or text")
    try:
        encoded = raw.encode("utf-8", errors="strict") if isinstance(raw, str) else raw
    except UnicodeError as exc:
        raise ValueError("invalid Unicode in JSON") from exc
    if len(encoded) > limits.max_bytes:
        raise ValueError("JSON byte limit exceeded")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("non-finite JSON number")

    try:
        result = json.loads(
            encoded.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite
        )
        _check(result, limits)
        return result
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid or excessively nested JSON") from exc


def hash_json(value: Any, *, limits: JsonLimits = DEFAULT_LIMITS) -> str:
    return hashlib.sha256(canonical_json(value, limits=limits)).hexdigest()
