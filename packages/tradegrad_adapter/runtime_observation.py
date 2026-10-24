"""Bounded data-only RNG and public engine audit observations, never ledger proof.

This module stays standard-library-only in the isolated portable policy image.
NumPy is imported only by the explicit native capture/restore branch. No pickle,
callbacks replaced, candidate edits, expression evaluation, or implicit seeding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
from datetime import date
from pathlib import Path

RNG_VERSION = "tradegrad.rng-state.v1"
VERSION = "tradegrad.runtime-observation.v1"
AUDIT_VERSION = "tradegrad.akquant-audit.v1"
PREFIX = "TRADEGRAD_OBSERVATION_V1:"
MAX_RNG_BYTES = 32 * 1024
MAX_AUDIT_BYTES = 4 * 1024 * 1024
MAX_EVENTS = 50_000
MAX_OBSERVATION_BYTES = MAX_AUDIT_BYTES + 256 * 1024
UNCOVERED = ["numpy.default_rng", "os_entropy", "strategy_self_reseeding"]


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"Invalid {label} schema")


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _uint(value, high):
    return type(value) is int and 0 <= value <= high


def _plain_tree(value, depth=0):
    if depth > 32:
        raise ValueError("Observation JSON nesting exceeds limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _plain_tree(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _plain_tree(item, depth + 1)
        return
    raise ValueError("Observation must contain only finite plain JSON")


def _strict(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate observation JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite observation JSON")
            ),
        )
        _plain_tree(value)
        return value
    except (RecursionError, OverflowError) as exc:
        raise ValueError("Observation JSON complexity exceeds limit") from exc


def validate_rng_state(value, *, include_numpy=None):
    _plain_tree(value)
    _keys(
        value,
        {"schema_version", "seam", "python", "numpy_legacy", "scope", "uncovered"},
        "RNG state",
    )
    if (
        len(canonical(value)) > MAX_RNG_BYTES
        or value["schema_version"] != RNG_VERSION
        or value["seam"] != "before_candidate_import.v1"
    ):
        raise ValueError("Unsupported or oversized RNG state")
    py = value["python"]
    _keys(py, {"version", "state", "gauss_next"}, "Python RNG")
    if (
        type(py["version"]) is not int
        or py["version"] != 3
        or not isinstance(py["state"], list)
        or len(py["state"]) != 625
        or any(not _uint(x, 2**32 - 1) for x in py["state"][:624])
        or not _uint(py["state"][-1], 624)
        or (py["gauss_next"] is not None and not _finite(py["gauss_next"]))
    ):
        raise ValueError("Invalid Python RNG state")
    np = value["numpy_legacy"]
    if include_numpy is not None and (np is not None) != include_numpy:
        raise ValueError("RNG state scope does not match runtime")
    if np is not None:
        _keys(
            np,
            {"algorithm", "keys", "position", "has_gauss", "cached_gaussian"},
            "NumPy RNG",
        )
        if (
            np["algorithm"] != "MT19937"
            or not isinstance(np["keys"], list)
            or len(np["keys"]) != 624
            or any(not _uint(x, 2**32 - 1) for x in np["keys"])
            or not _uint(np["position"], 624)
            or not _uint(np["has_gauss"], 1)
            or not _finite(np["cached_gaussian"])
        ):
            raise ValueError("Invalid NumPy legacy RNG state")
    expected = ["python.random"] + (["numpy.random.legacy"] if np is not None else [])
    if value["scope"] != expected or value["uncovered"] != UNCOVERED:
        raise ValueError("Invalid RNG scope declaration")
    return value


def capture_rng_state(*, include_numpy=False):
    py = random.getstate()
    np = None
    if include_numpy:
        import numpy

        state = numpy.random.get_state()
        np = {
            "algorithm": state[0],
            "keys": state[1].tolist(),
            "position": state[2],
            "has_gauss": state[3],
            "cached_gaussian": state[4],
        }
    value = {
        "schema_version": RNG_VERSION,
        "seam": "before_candidate_import.v1",
        "python": {"version": py[0], "state": list(py[1]), "gauss_next": py[2]},
        "numpy_legacy": np,
        "scope": ["python.random"] + (["numpy.random.legacy"] if include_numpy else []),
        "uncovered": list(UNCOVERED),
    }
    return validate_rng_state(value, include_numpy=include_numpy)


def restore_rng_state(value, *, include_numpy=False):
    validate_rng_state(value, include_numpy=include_numpy)
    py = value["python"]
    # Validate all fields before changing either generator.
    if include_numpy:
        import numpy

        np = value["numpy_legacy"]
        numpy.random.set_state(
            (
                np["algorithm"],
                numpy.array(np["keys"], dtype="uint32"),
                np["position"],
                np["has_gauss"],
                np["cached_gaussian"],
            )
        )
    random.setstate((py["version"], tuple(py["state"]), py["gauss_next"]))


def _event(value):
    _keys(
        value,
        {"run_id", "seq", "ts", "event_type", "symbol", "level", "payload"},
        "audit event",
    )
    if (
        not isinstance(value["run_id"], str)
        or not value["run_id"]
        or not _uint(value["seq"], 2**63 - 1)
        or type(value["ts"]) is not int
        or not all(isinstance(value[k], str) for k in ("event_type", "level"))
        or (value["symbol"] is not None and not isinstance(value["symbol"], str))
        or not isinstance(value["payload"], dict)
    ):
        raise ValueError("Invalid audit event fields")
    if len(canonical(value)) > 64 * 1024:
        raise ValueError("Oversized audit event")
    if any(
        not isinstance(k, str)
        or v is not None
        and type(v) not in (str, bool, int, float)
        for k, v in value["payload"].items()
    ):
        raise ValueError("Audit payload must contain plain scalar values")
    required = {
        "order": ("order_id", "status", "symbol"),
        "trade": ("order_id", "trade_id", "price", "quantity", "side"),
    }.get(value["event_type"], ())
    if any(
        not isinstance(value["payload"].get(k), str) or not value["payload"][k]
        for k in required
    ):
        raise ValueError("Missing explicit audit order/trade association")
    return value


def _completed(events, counts, loss):
    if (
        loss
        or counts.get("started") != 1
        or counts.get("finished") != 1
        or not events
        or events[0]["event_type"] != "started"
        or events[-1]["event_type"] != "finished"
    ):
        return False
    finished = [event for event in events if event["event_type"] == "finished"]
    if len(finished) != 1:
        return False
    value = finished[0]["payload"]
    # AKQuant 0.3.27 public stream payload values are strings, not JSON numbers.
    return all(
        str(value.get(k)).lower() == v
        for k, v in {
            "status": "completed",
            "stream_mode": "audit",
            "backpressure_policy": "block",
            "sampling_enabled": "false",
            "dropped_event_count": "0",
            "callback_error_count": "0",
        }.items()
    ) and value.get("dropped_event_count_by_type") in ("", "{}")


class NativeAuditObserver:
    """Copy supplied public values only; observer errors never alter strategy flow."""

    def __init__(self, *, max_events=MAX_EVENTS, max_bytes=MAX_AUDIT_BYTES):
        if (
            not _uint(max_events, MAX_EVENTS)
            or max_events < 2
            or not _uint(max_bytes, MAX_AUDIT_BYTES)
            or max_bytes < 1024
        ):
            raise ValueError("Invalid native audit limits")
        self.max_events, self.max_bytes = max_events, max_bytes
        self.events, self.counts, self.loss = [], {}, []
        self.observed = self.bytes = 0
        self.previous = None
        self.run_id = None

    def _loss(self, reason):
        if reason not in self.loss:
            self.loss.append(reason)

    def __call__(self, value):
        self.observed += 1
        try:
            value = _event(value)
            kind = value["event_type"]
            if self.observed == 1 and kind != "started":
                self._loss("event_before_started")
            if self.counts.get("finished", 0):
                self._loss("event_after_finished")
            if kind not in self.counts and len(self.counts) >= 64:
                self._loss("event_type_capacity_exceeded")
                return
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if self.previous is not None and value["seq"] <= self.previous:
                self._loss("non_increasing_event_sequence")
            if self.run_id is not None and value["run_id"] != self.run_id:
                self._loss("mixed_stream_run_identity")
            self.previous, self.run_id = value["seq"], value["run_id"]
            if kind not in ("started", "order", "trade", "equity", "finished"):
                return
            data = canonical(value)
            if (
                len(self.events) >= self.max_events
                or self.bytes + len(data) > self.max_bytes
            ):
                self._loss("observer_capacity_exceeded")
                return
            self.events.append(json.loads(data))
            self.bytes += len(data)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self._loss("observer_copy_or_schema_error")

    def finish(self):
        for event in self.events:
            if event["event_type"] != "finished":
                continue
            receipt = event["payload"]
            for key, expected, reason in (
                ("status", "completed", "engine_not_completed"),
                ("stream_mode", "audit", "engine_audit_mode_mismatch"),
                ("backpressure_policy", "block", "engine_backpressure_policy_mismatch"),
                ("sampling_enabled", "false", "engine_sampling_or_missing_receipt"),
                (
                    "dropped_event_count",
                    "0",
                    "engine_dropped_events_or_missing_receipt",
                ),
                (
                    "callback_error_count",
                    "0",
                    "engine_callback_errors_or_missing_receipt",
                ),
            ):
                if str(receipt.get(key)).lower() != expected:
                    self._loss(reason)
            if receipt.get("dropped_event_count_by_type") not in ("", "{}"):
                self._loss("engine_dropped_event_types_or_missing_receipt")
        if self.counts.get("started") != 1:
            self._loss("missing_or_duplicate_started")
        if self.counts.get("finished") != 1:
            self._loss("missing_or_duplicate_finished")
        result = {
            "schema_version": AUDIT_VERSION,
            "events": self.events,
            "event_counts": self.counts,
            "observed_events": self.observed,
            "retained_events": len(self.events),
            "limits": {"max_events": self.max_events, "max_bytes": self.max_bytes},
            "loss_reasons": self.loss,
            "completed": _completed(self.events, self.counts, self.loss),
        }
        return validate_audit(result)


def validate_audit(value):
    _keys(
        value,
        {
            "schema_version",
            "events",
            "event_counts",
            "observed_events",
            "retained_events",
            "limits",
            "loss_reasons",
            "completed",
        },
        "native audit",
    )
    _keys(value["limits"], {"max_events", "max_bytes"}, "audit limits")
    limits = value["limits"]
    if (
        value["schema_version"] != AUDIT_VERSION
        or not _uint(limits["max_events"], MAX_EVENTS)
        or not 2 <= limits["max_events"]
        or not _uint(limits["max_bytes"], MAX_AUDIT_BYTES)
        or limits["max_bytes"] < 1024
    ):
        raise ValueError("Unsupported audit limits or schema")
    events, counts, loss = value["events"], value["event_counts"], value["loss_reasons"]
    if (
        not isinstance(events, list)
        or len(events) > limits["max_events"]
        or type(value["retained_events"]) is not int
        or len(events) != value["retained_events"]
        or not isinstance(counts, dict)
        or len(counts) > 64
        or any(
            not isinstance(k, str) or len(k) > 100 or not _uint(v, 2**63 - 1)
            for k, v in counts.items()
        )
        or not _uint(value["observed_events"], 2**63 - 1)
        or sum(counts.values()) > value["observed_events"]
    ):
        raise ValueError("Invalid audit record counts")
    if (
        not isinstance(loss, list)
        or len(loss) > 16
        or any(not isinstance(s, str) or len(s) > 100 for s in loss)
        or len(set(loss)) != len(loss)
    ):
        raise ValueError("Invalid audit loss reasons")
    previous = None
    run_id = None
    retained = {}
    for event in events:
        _event(event)
        kind = event["event_type"]
        if kind not in ("started", "order", "trade", "equity", "finished"):
            raise ValueError("Unexpected retained audit event")
        retained[kind] = retained.get(kind, 0) + 1
        if (
            previous is not None
            and event["seq"] <= previous
            and "non_increasing_event_sequence" not in loss
        ):
            raise ValueError("Audit ordering inconsistency")
        if (
            run_id is not None
            and event["run_id"] != run_id
            and "mixed_stream_run_identity" not in loss
        ):
            raise ValueError("Audit run identity inconsistency")
        previous, run_id = event["seq"], event["run_id"]
    if sum(len(canonical(e)) for e in events) > limits["max_bytes"] or any(
        counts.get(k, 0) < v for k, v in retained.items()
    ):
        raise ValueError("Audit retained event budget/count mismatch")
    if not loss and (
        sum(counts.values()) != value["observed_events"]
        or any(
            counts.get(k, 0) != retained.get(k, 0)
            for k in ("started", "order", "trade", "equity", "finished")
        )
    ):
        raise ValueError("Audit claims complete retention despite missing events")
    if type(value["completed"]) is not bool or value["completed"] != _completed(
        events, counts, loss
    ):
        raise ValueError("Audit completion receipt mismatch")
    return value


def make_runtime_observation(
    *, runtime_kind, identity, initialization, rng_state, restored_from=None, audit=None
):
    value = {
        "schema_version": VERSION,
        "runtime_kind": runtime_kind,
        "identity": identity,
        "initialization": initialization,
        "rng": {
            "state": rng_state,
            "state_sha256": digest(rng_state),
            "restored_from_sha256": None
            if restored_from is None
            else digest(restored_from),
        },
        "audit": audit,
        "trust": "native_strategy_and_engine_share_process"
        if runtime_kind == "native"
        else "isolated_policy_rng_self_report_not_ledger_proof",
    }
    return validate_runtime_observation(value)


def validate_runtime_observation(
    value,
    *,
    expected_identity=None,
    expected_initialization=None,
    expected_runtime_kind=None,
):
    _plain_tree(value)
    _keys(
        value,
        {
            "schema_version",
            "runtime_kind",
            "identity",
            "initialization",
            "rng",
            "audit",
            "trust",
        },
        "runtime observation",
    )
    if (
        len(canonical(value)) > MAX_OBSERVATION_BYTES
        or value["schema_version"] != VERSION
        or value["runtime_kind"] not in ("native", "portable")
    ):
        raise ValueError("Unsupported runtime observation")
    if (
        not isinstance(value["identity"], dict)
        or any(
            not isinstance(value["identity"].get(k), str)
            or len(value["identity"][k]) != 64
            or any(c not in "0123456789abcdef" for c in value["identity"][k])
            for k in ("strategy_sha256", "run_config_sha256")
        )
        or expected_identity is not None
        and value["identity"] != expected_identity
    ):
        raise ValueError("Runtime observation identity mismatch")
    if (
        expected_runtime_kind is not None
        and value["runtime_kind"] != expected_runtime_kind
    ):
        raise ValueError("Runtime observation mode differs from trusted evaluator")
    _keys(value["initialization"], {"policy", "window"}, "initialization")
    if value["initialization"][
        "policy"
    ] != "reconstruct_from_original_start.v1" or not isinstance(
        value["initialization"]["window"], dict
    ):
        raise ValueError("Unsupported initialization observation")
    window = value["initialization"]["window"]
    try:
        if date.fromisoformat(window["end"]) <= date.fromisoformat(window["start"]):
            raise ValueError("Nonpositive initialization window")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid initialization window") from exc
    if (
        expected_initialization is not None
        and value["initialization"] != expected_initialization
    ):
        raise ValueError("Runtime initialization differs from trusted reconstruction")
    rng = value["rng"]
    _keys(rng, {"state", "state_sha256", "restored_from_sha256"}, "RNG evidence")
    validate_rng_state(rng["state"], include_numpy=value["runtime_kind"] == "native")
    if rng["state_sha256"] != digest(rng["state"]) or rng[
        "restored_from_sha256"
    ] not in (None, rng["state_sha256"]):
        raise ValueError("RNG state or restored state hash mismatch")
    if value["runtime_kind"] == "native":
        validate_audit(value["audit"])
        if value["trust"] != "native_strategy_and_engine_share_process":
            raise ValueError("Native runtime cannot claim independent proof")
    else:
        if value["audit"] is not None:
            validate_audit(value["audit"])
        if value["trust"] != "isolated_policy_rng_self_report_not_ledger_proof":
            raise ValueError("Portable policy RNG cannot claim engine audit proof")
    return value


def runtime_wire_record(value):
    validate_runtime_observation(value)
    return PREFIX + base64.b64encode(
        canonical({"observation": value, "sha256": digest(value)})
    ).decode("ascii")


def decode_runtime_protocol(
    stdout,
    *,
    expected_identity=None,
    expected_initialization=None,
    expected_runtime_kind=None,
    required=True,
):
    records = [
        line[len(PREFIX) :]
        for line in stdout.decode("utf-8", errors="strict").splitlines()
        if line.startswith(PREFIX)
    ]
    if not records and not required:
        return None
    if len(records) != 1 or len(records[0]) > 4 * (
        (MAX_OBSERVATION_BYTES + 256 + 2) // 3
    ):
        raise ValueError("Missing, duplicated or oversized runtime observation")
    data = base64.b64decode(records[0], validate=True)
    if len(data) > MAX_OBSERVATION_BYTES + 256:
        raise ValueError("Oversized runtime observation")
    envelope = _strict(data)
    _keys(envelope, {"observation", "sha256"}, "runtime observation envelope")
    value = validate_runtime_observation(
        envelope["observation"],
        expected_identity=expected_identity,
        expected_initialization=expected_initialization,
        expected_runtime_kind=expected_runtime_kind,
    )
    if envelope["sha256"] != digest(value):
        raise ValueError("Runtime observation envelope hash mismatch")
    return value


def write_runtime_observation(output_dir, value):
    validate_runtime_observation(value)
    path = Path(output_dir) / "runtime-observation.json"
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Runtime observation path cannot traverse symlinks")
    data = canonical(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "runtime_observation": {
            "path": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "schema_version": VERSION,
        }
    }
