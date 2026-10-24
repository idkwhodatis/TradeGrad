"""Data-only protocol, RNG seam, loss and immutable observation safety checks."""

import json
import random
from types import SimpleNamespace

import pytest

from packages.tradegrad_adapter.runtime_observation import (
    NativeAuditObserver,
    canonical,
    capture_rng_state,
    decode_runtime_protocol,
    digest,
    make_runtime_observation,
    restore_rng_state,
    runtime_wire_record,
    validate_audit,
    validate_rng_state,
    validate_runtime_observation,
    write_runtime_observation,
)


def event(kind, seq, **payload):
    return {
        "run_id": "actual-engine-run",
        "seq": seq,
        "ts": 1704402000000000000 + seq,
        "event_type": kind,
        "symbol": None,
        "level": "info",
        "payload": payload,
    }


def finished(seq=3):
    return event(
        "finished",
        seq,
        status="completed",
        stream_mode="audit",
        backpressure_policy="block",
        sampling_enabled="false",
        dropped_event_count="0",
        callback_error_count="0",
        dropped_event_count_by_type="",
        processed_events="7",
    )


def audit():
    value = NativeAuditObserver()
    value(event("started", 1, total_events="8"))
    value(event("bar", 2, symbol="A"))
    value(finished())
    return value.finish()


def observation():
    return make_runtime_observation(
        runtime_kind="portable",
        identity={"strategy_sha256": "a" * 64, "run_config_sha256": "b" * 64},
        initialization={
            "policy": "reconstruct_from_original_start.v1",
            "window": {
                "start": "2024-01-02",
                "end": "2024-01-12",
                "warmup_start": "2023-12-01",
            },
        },
        rng_state=capture_rng_state(),
        audit=audit(),
    )


def test_stdlib_capture_is_read_only_and_restore_matches_future_draws():
    before = random.getstate()
    state = capture_rng_state()
    assert random.getstate() == before
    actual = [random.random() for _ in range(5)]
    restore_rng_state(state)
    assert [random.random() for _ in range(5)] == actual
    assert state["scope"] == ["python.random"]
    assert state["uncovered"] == [
        "numpy.default_rng",
        "os_entropy",
        "strategy_self_reseeding",
    ]
    random.setstate(before)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["python"]["state"].pop(),
        lambda s: s["python"]["state"].__setitem__(0, True),
        lambda s: s["python"]["state"].__setitem__(-1, 625),
        lambda s: s["python"].__setitem__("gauss_next", float("nan")),
        lambda s: s["python"].__setitem__("version", 2),
        lambda s: s["uncovered"].clear(),
        lambda s: s.__setitem__("scope", ["all_randomness"]),
        lambda s: s.__setitem__("payload", "pickle-not-accepted"),
    ],
)
def test_rng_malformed_scope_state_never_changes_generator(mutation):
    before = random.getstate()
    state = capture_rng_state()
    mutation(state)
    with pytest.raises((ValueError, TypeError)):
        restore_rng_state(state)
    assert before == random.getstate()


def test_portable_cannot_claim_native_numpy_state():
    with pytest.raises(ValueError, match="scope"):
        validate_rng_state(capture_rng_state(), include_numpy=True)


def test_audit_real_counts_are_not_processed_events_or_bar_count():
    value = audit()
    assert value["completed"] is True
    assert value["observed_events"] == 3
    assert value["retained_events"] == 2
    assert value["events"][-1]["payload"]["processed_events"] == "7"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e["payload"].__setitem__("callback_error_count", "1"),
        lambda e: e["payload"].__setitem__("dropped_event_count", "1"),
        lambda e: e["payload"].__setitem__("status", "failed"),
        lambda e: e["payload"].__setitem__("stream_mode", "observability"),
        lambda e: e["payload"].__setitem__("sampling_enabled", "true"),
        lambda e: e["payload"].__setitem__("backpressure_policy", "drop"),
    ],
)
def test_failed_lossy_or_sampled_receipt_never_certifies(mutation):
    observer = NativeAuditObserver()
    observer(event("started", 1))
    last = finished(2)
    mutation(last)
    observer(last)
    assert observer.finish()["completed"] is False


@pytest.mark.parametrize(
    "events",
    [
        [finished(1), event("started", 2)],
        [event("started", 1), finished(2), event("bar", 3)],
        [event("started", 1), event("started", 2), finished(3)],
        [event("started", 1), finished(1)],
        [event("started", 1)],
    ],
)
def test_invalid_stream_framing_is_loss_not_complete(events):
    observer = NativeAuditObserver()
    for value in events:
        observer(value)
    value = observer.finish()
    assert value["completed"] is False and value["loss_reasons"]
    value["completed"] = True
    with pytest.raises(ValueError):
        validate_audit(value)


def test_bounded_observer_copies_payload_without_mutating_economics():
    observer = NativeAuditObserver(max_events=2, max_bytes=1024)
    observer(event("started", 1))
    item = event("order", 2, order_id="o", status="Rejected", symbol="A")
    observer(item)
    item["payload"]["status"] = "forged"
    observer(finished())
    value = observer.finish()
    assert value["events"][1]["payload"]["status"] == "Rejected"
    assert value["completed"] is False
    assert "observer_capacity_exceeded" in value["loss_reasons"]


@pytest.mark.parametrize(
    "status", ["New", "PartiallyFilled", "Filled", "Rejected", "Cancelled"]
)
def test_order_status_is_copied_not_guessed_from_terminal_snapshot(status):
    observer = NativeAuditObserver()
    observer(event("started", 1))
    observer(event("order", 2, order_id="child", status=status, symbol="A"))
    observer(finished())
    assert observer.finish()["events"][1]["payload"]["status"] == status


def test_unexpected_callback_shape_is_explicit_loss_not_exception():
    observer = NativeAuditObserver()
    observer({"bad": "not an engine event"})
    assert "observer_copy_or_schema_error" in observer.finish()["loss_reasons"]


def test_runtime_wire_hash_identity_window_and_exclusive_publication(tmp_path):
    value = observation()
    wire = runtime_wire_record(value).encode()
    assert (
        decode_runtime_protocol(
            wire,
            expected_identity=value["identity"],
            expected_initialization=value["initialization"],
        )
        == value
    )
    with pytest.raises(ValueError):
        decode_runtime_protocol(wire + b"\n" + wire)
    with pytest.raises(ValueError):
        decode_runtime_protocol(wire, expected_identity={})
    with pytest.raises(ValueError):
        decode_runtime_protocol(wire, expected_initialization={})
    metadata = write_runtime_observation(tmp_path, value)["runtime_observation"]
    assert metadata["sha256"] == digest(value)
    assert (tmp_path / metadata["path"]).read_bytes() == canonical(value)
    with pytest.raises(FileExistsError):
        write_runtime_observation(tmp_path, value)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.__setitem__("identity", {}),
        lambda v: v["initialization"].__setitem__("window", {}),
        lambda v: v["rng"].__setitem__("state_sha256", "c" * 64),
        lambda v: v["rng"].__setitem__("restored_from_sha256", "d" * 64),
        lambda v: v.__setitem__("trust", "independent_ledger_proof"),
    ],
)
def test_observation_missing_or_forged_binding_fails(change):
    value = observation()
    change(value)
    with pytest.raises(ValueError):
        validate_runtime_observation(value)


def test_publication_is_hash_bound_to_economic_artifact_and_config(tmp_path):
    from workers.research.runtime_publication import prepare_runtime_artifacts

    root = tmp_path / "run" / "evaluation"
    (root / "output").mkdir(parents=True)
    (root / "artifacts").mkdir()
    value = observation()
    result = {
        "status": "succeeded",
        "bundle": {
            "identity": value["identity"],
            "window": {"start": "2024-01-02", "end": "2024-01-12"},
        },
        **write_runtime_observation(root / "output", value),
    }
    records = [
        {
            "name": "portable-result.json",
            "storage_key": "run/evaluation/artifacts/portable-result.json",
        }
    ]
    config = SimpleNamespace(schema_version="tradegrad.portable.run.v1")
    saved = prepare_runtime_artifacts(
        config=config,
        result=result,
        records=records,
        root=root,
        settings=SimpleNamespace(storage_root=tmp_path),
    )
    assert saved[0]["sha256"] == digest(value)
    (root / "output" / "runtime-observation.json").write_text(
        json.dumps({"forged": True})
    )
    with pytest.raises(ValueError):
        prepare_runtime_artifacts(
            config=config,
            result=result,
            records=records,
            root=root,
            settings=SimpleNamespace(storage_root=tmp_path),
        )


def test_runtime_kind_cannot_be_selected_by_child_observation():
    value = observation()
    with pytest.raises(ValueError, match="mode differs"):
        validate_runtime_observation(value, expected_runtime_kind="native")


def test_independent_validator_import_is_standard_library_only():
    import subprocess
    import sys

    program = "import sys; import packages.tradegrad_adapter.independent_results; import packages.tradegrad_adapter.runtime_observation; assert not {'numpy','pandas','akquant','pickle'} & set(sys.modules)"
    result = subprocess.run(
        [sys.executable, "-S", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_independent_economic_limit_and_json_complexity_stay_strict():
    from packages.tradegrad_adapter.independent_results import validate_envelope
    from packages.tradegrad_adapter.results import MAX_WIRE_BYTES
    from packages.tradegrad_adapter.runtime_observation import _strict

    with pytest.raises(ValueError, match="budget"):
        validate_envelope(b" " * (MAX_WIRE_BYTES + 1))
    for parse in (validate_envelope, _strict):
        with pytest.raises(ValueError):
            parse(b"[" * 2000 + b"0" + b"]" * 2000)
        with pytest.raises(ValueError):
            parse(b'{"a":1,"a":2}')
