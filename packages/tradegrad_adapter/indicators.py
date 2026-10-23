"""Pure contract for AKQuant 0.3.27's explicitly recorded indicator outputs.

This module never loads strategy code or computes an indicator from prices. The
only producer is the evaluator's live BacktestResult.indicator_outputs object.
An optional hashed ResultBundle section keeps pre-extension artifacts readable.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

VERSION = "akquant.0.3.27.recorded-indicators.v1"
MAX_DEFINITIONS = 64
MAX_INSTANCES = 256
MAX_OBSERVATIONS = 100_000
MAX_BYTES = 2_000_000
DEFINITION_KEYS = {
    "indicator_key",
    "display_name",
    "pane",
    "render_type",
    "unit",
    "precision",
    "color",
    "reference_lines",
    "scale_group",
}
INSTANCE_KEYS = {
    "instance_id",
    "owner_strategy_id",
    "symbol",
    "indicator_key",
    "meta_json",
}
POINT_KEYS = {
    "instance_id",
    "owner_strategy_id",
    "symbol",
    "indicator_key",
    "timestamp",
    "timestamp_ms",
    "value",
    "warmup",
}


def indicator_time(timestamp_ns):
    # Integer arithmetic avoids epoch-nanosecond rounding through a float.
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=timestamp_ns // 1000
    )


def unavailable_indicators(reason, status="not_recorded"):
    return {
        "definition_version": VERSION,
        "status": status,
        "reason": reason,
        "definitions": [],
        "instances": [],
        "points": [],
    }


def validate_indicators(value):
    if not isinstance(value, dict) or set(value) != {
        "definition_version",
        "status",
        "reason",
        "definitions",
        "instances",
        "points",
    }:
        raise ValueError("Invalid recorded indicator section")
    if value["definition_version"] != VERSION or value["status"] not in {
        "available",
        "not_recorded",
        "unsupported",
    }:
        raise ValueError("Invalid recorded indicator version/status")
    if not isinstance(value["reason"], str) or len(value["reason"]) > 500:
        raise ValueError("Invalid recorded indicator reason")
    for key, limit in (
        ("definitions", MAX_DEFINITIONS),
        ("instances", MAX_INSTANCES),
        ("points", MAX_OBSERVATIONS),
    ):
        if not isinstance(value[key], list) or len(value[key]) > limit:
            raise ValueError("Recorded indicator budget exceeded")
    if len(json.dumps(value, allow_nan=False).encode()) > MAX_BYTES:
        raise ValueError("Recorded indicator byte budget exceeded")
    if value["status"] != "available":
        if (
            any(value[key] for key in ("definitions", "instances", "points"))
            or not value["reason"]
        ):
            raise ValueError(
                "Unavailable indicators must have an explanation and no observations"
            )
        return value
    if not value["points"]:
        raise ValueError("Available indicators require actual observations")
    definitions = {}
    for item in value["definitions"]:
        if not isinstance(item, dict) or set(item) != DEFINITION_KEYS:
            raise ValueError("Invalid recorded indicator declaration")
        for key in (
            "indicator_key",
            "display_name",
            "render_type",
            "unit",
            "color",
            "scale_group",
        ):
            if not isinstance(item[key], str) or len(item[key]) > 160:
                raise ValueError("Invalid recorded indicator metadata")
        if not item["indicator_key"] or item["indicator_key"] in definitions:
            raise ValueError("Duplicate or empty recorded indicator key")
        if type(item["pane"]) is not int or not 0 <= item["pane"] <= 16:
            raise ValueError("Unsupported indicator pane")
        if item["precision"] is not None and (
            type(item["precision"]) is not int or not 0 <= item["precision"] <= 16
        ):
            raise ValueError("Invalid indicator precision")
        if (
            not isinstance(item["reference_lines"], list)
            or len(item["reference_lines"]) > 32
        ):
            raise ValueError("Invalid indicator reference lines")
        definitions[item["indicator_key"]] = item
    instances = {}
    for item in value["instances"]:
        if (
            not isinstance(item, dict)
            or set(item) != INSTANCE_KEYS
            or any(not isinstance(v, str) or len(v) > 4096 for v in item.values())
        ):
            raise ValueError("Invalid recorded indicator instance")
        if (
            not item["instance_id"]
            or item["instance_id"] in instances
            or item["indicator_key"] not in definitions
        ):
            raise ValueError("Unbound recorded indicator instance")
        instances[item["instance_id"]] = item
    previous = {}
    for point in value["points"]:
        if not isinstance(point, dict) or set(point) != POINT_KEYS:
            raise ValueError("Invalid recorded indicator observation")
        instance = instances.get(point["instance_id"])
        if instance is None or any(
            point[k] != instance[k]
            for k in ("owner_strategy_id", "symbol", "indicator_key")
        ):
            raise ValueError("Unbound recorded indicator observation")
        if (
            type(point["timestamp"]) is not int
            or type(point["timestamp_ms"]) is not int
            or point["timestamp_ms"] != point["timestamp"] // 1_000_000
        ):
            raise ValueError("Invalid indicator timestamp units")
        indicator_time(point["timestamp"])
        if point["timestamp"] < previous.get(point["instance_id"], point["timestamp"]):
            raise ValueError("Recorded indicator observations are not ordered")
        previous[point["instance_id"]] = point["timestamp"]
        if type(point["warmup"]) is not bool or (
            point["value"] is not None
            and (
                type(point["value"]) not in (float, int)
                or not math.isfinite(point["value"])
            )
        ):
            raise ValueError("Invalid recorded indicator value")
    return value


def export_recorded_indicators(payload):
    """Copy recorded metadata/observations only; nonfinite missing values become null."""
    if not isinstance(payload, dict) or not payload.get("points"):
        return unavailable_indicators(
            "The strategy did not record indicator observations with AKQuant record_indicator; no indicators are inferred from code or OHLCV."
        )
    try:
        if set(payload) != {"definitions", "instances", "points"}:
            raise ValueError("Unsupported indicator payload")
        # Standard JSON primitives only. The engine recorder emits float NaNs for
        # warm-up/missing values; preserve missingness rather than interpolating.
        value = json.loads(json.dumps(payload), parse_constant=lambda _: None)
        value.update(
            definition_version=VERSION,
            status="available",
            reason="Explicit AKQuant record_indicator observations; native strategy and engine share a process.",
        )
        return validate_indicators(value)
    except (ValueError, TypeError, OverflowError, KeyError):
        return unavailable_indicators(
            "Recorded indicator outputs exceed the bounded V1 contract or use unsupported metadata; no replacement series is computed.",
            "unsupported",
        )
