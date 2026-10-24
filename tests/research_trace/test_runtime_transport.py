"""Hostile data-only wire cases. No Docker execution is claimed by these tests."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import random

import pytest

from packages.research_trace.contracts import (
    CaptureConfig,
    DecisionRecord,
    canonical_bytes,
)
from packages.research_trace.recorder import Recorder
from packages.research_trace.transport import (
    CAPTURE_FILENAME,
    MAX_COMPRESSED_BYTES,
    TRACE_PREFIX,
    WIRE_VERSION,
    CaptureReceiver,
    CaptureWriter,
    NativeCaptureDemultiplexer,
    iter_capture_records,
)


def record(index=0, value=1):
    return DecisionRecord.model_validate(
        {
            "decision_id": f"decision:{index}",
            "data_event_id": "daily_bar:2024-01-04:ABC",
            "event_seq": index,
            "scope": "instrument",
            "instrument_id": "ABC",
            "callback_phase": "on_bar",
            "information_cutoff": "2024-01-04T21:00:00Z",
            "decision_time": "2024-01-04T21:00:00Z",
            "market_timezone": "America/New_York",
            "pre_state": None,
            "pre_state_missing_reason": "not observed",
            "stages": [
                {
                    "stage_id": "features",
                    "coverage": "complete",
                    "features": [
                        {
                            "feature_id": "observed",
                            "value": value,
                            "validity": "valid",
                            "unit": "native",
                            "calculation_window": "actual observation",
                            "information_cutoff": "2024-01-04T21:00:00Z",
                            "source": "strategy_emitted",
                        }
                    ],
                }
            ],
        }
    )


def summary(config, records=1, details=1):
    return {
        "capture": config.model_dump(mode="json"),
        "coverage": "complete",
        "missing_reasons": [],
        "decisions_seen": records,
        "decisions_saved": records,
        "detail_rows_seen": details,
        "detail_rows_saved": details,
        "stage_coverage": {"features": "complete"},
        "source": "strategy_emitted",
        "trust_boundary": "Native strategy and engine share a process. Emitted observations are not an independently trusted ledger or no-lookahead proof.",
    }


def frames(config=None):
    config = config or CaptureConfig(mode="research")
    values = []
    writer = CaptureWriter(config, values.append)
    writer.append(record())
    writer.finish(summary(config))
    return config, values


def test_roundtrip_no_opaque_objects_and_exclusive_owned_output(tmp_path):
    config, values = frames()
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    for value in values:
        receiver.accept(value)
    result = receiver.result()
    assert result["research_capture"] == summary(config)
    observed = list(
        iter_capture_records(
            result["research_capture_path"],
            result["research_capture_sha256"],
            result["research_capture_size_bytes"],
            config,
        )
    )
    assert observed == [record()]
    assert {p.name for p in tmp_path.iterdir()} == {CAPTURE_FILENAME}
    second = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    with pytest.raises(FileExistsError):
        second.accept(values[0])


def test_trace_larger_than_original_eight_megabyte_wire_streams_without_buffering(
    tmp_path,
):
    config = CaptureConfig(mode="research")
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    writer = CaptureWriter(config, receiver.accept)
    rng = random.Random(42)
    for i in range(180):
        # Reproducible, deliberately incompressible observed strings. Not market data.
        writer.append(
            record(i, base64.b85encode(rng.randbytes(64 * 1024)).decode("ascii"))
        )
        assert len(writer.pending) < 48 * 1024
        assert len(receiver.decoder.pending) <= 16 * 1024 * 1024
    writer.finish(summary(config, 180, 180))
    result = receiver.result()
    assert (
        8 * 1024 * 1024 < result["research_capture_size_bytes"] < MAX_COMPRESSED_BYTES
    )
    assert (
        sum(
            1
            for _ in iter_capture_records(
                result["research_capture_path"],
                result["research_capture_sha256"],
                result["research_capture_size_bytes"],
                config,
            )
        )
        == 180
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "sequence",
        "unknown",
        "path",
        "hash",
        "count",
        "config",
        "source",
        "trust",
        "duplicate_start",
        "trailing",
    ],
)
def test_malicious_frame_rejected(tmp_path, mutation):
    config, values = frames()
    values = copy.deepcopy(values)
    if mutation == "sequence":
        values[1]["sequence"] = 0
    if mutation == "unknown":
        values[1]["kind"] = "extract_archive"
    if mutation == "path":
        values[1]["path"] = "../../outside"
    if mutation == "hash":
        values[-1]["sha256"] = "0" * 64
    if mutation == "count":
        values[-1]["summary"]["decisions_saved"] = 0
    if mutation == "config":
        values[0]["capture"]["mode"] = "summary"
    if mutation == "source":
        values[-1]["summary"]["source"] = "engine_observed"
    if mutation == "trust":
        values[-1]["summary"]["trust_boundary"] = "independently certified"
    if mutation == "duplicate_start":
        values[1] = {**values[0], "sequence": 1}
    if mutation == "trailing":
        values.append({**values[1], "sequence": len(values)})
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    with pytest.raises(ValueError):
        for value in values:
            receiver.accept(value)
    receiver.abort()
    assert not (tmp_path / CAPTURE_FILENAME).exists()


@pytest.mark.parametrize(
    "payload",
    [
        b'{"decision_id":"a","decision_id":"b"}\n',
        b'{"x":NaN}\n',
        b"[" * 2000,
        b"not json\n",
    ],
)
def test_hostile_json_lines_fail_closed(tmp_path, payload):
    config, values = frames()
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    receiver.accept(values[0])
    compressed = gzip.compress(payload)
    with pytest.raises(ValueError):
        receiver.accept(
            {
                "protocol_version": WIRE_VERSION,
                "kind": "research_chunk",
                "sequence": 1,
                "data": base64.b64encode(compressed).decode(),
            }
        )
        receiver.accept({**values[-1], "sequence": 2})
    receiver.abort()


@pytest.mark.parametrize(
    "payload",
    [
        gzip.compress(b"x" * 100_000),
        gzip.compress(canonical_bytes(record()) + b"\n") + gzip.compress(b"\n"),
    ],
)
def test_decompression_bomb_and_concatenated_gzip_rejected(tmp_path, payload):
    config = CaptureConfig(mode="research", max_uncompressed_bytes=1024)
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    receiver.accept(
        {
            "protocol_version": WIRE_VERSION,
            "kind": "research_start",
            "sequence": 0,
            "capture": config.model_dump(mode="json"),
        }
    )
    with pytest.raises(ValueError):
        receiver.accept(
            {
                "protocol_version": WIRE_VERSION,
                "kind": "research_chunk",
                "sequence": 1,
                "data": base64.b64encode(payload).decode(),
            }
        )
    receiver.abort()


def test_duplicate_source_event_and_decision_ids_rejected(tmp_path):
    config, values = frames()
    payload = gzip.compress((canonical_bytes(record()) + b"\n") * 2)
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    receiver.accept(values[0])
    with pytest.raises(ValueError, match="Duplicate"):
        receiver.accept(
            {
                "protocol_version": WIRE_VERSION,
                "kind": "research_chunk",
                "sequence": 1,
                "data": base64.b64encode(payload).decode(),
            }
        )
    receiver.abort()


def test_declared_budget_rejection_leaves_valid_partial_stream_and_execution_continues(
    tmp_path,
):
    config = CaptureConfig(mode="research", max_uncompressed_bytes=2048)
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    writer = CaptureWriter(config, receiver.accept)
    recorder = Recorder(code_sha256="a" * 64, config=config, on_record=writer.append)
    for i in range(10):
        recorder.begin_decision(
            data_event_id=f"actual:{i}",
            event_seq=i,
            scope="portfolio",
            callback_phase="session",
            decision_time="2024-01-04T21:00:00Z",
            information_cutoff="2024-01-04T21:00:00Z",
            market_timezone="UTC",
        )
        recorder.emit("orders", {"coverage": "complete"})
        recorder.end_decision()
    writer.finish(recorder.summary())
    observed = receiver.result()["research_capture"]
    assert observed["decisions_seen"] == 10
    assert 0 < observed["decisions_saved"] < 10
    assert observed["coverage"] == "partial" and observed["missing_reasons"]


def test_native_demux_never_repeats_capture_in_logs_and_handles_byte_splits(tmp_path):
    config, values = frames()
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    mux = NativeCaptureDemultiplexer(receiver, other_limit=1024)
    wire = (
        b"ordinary log\n"
        + b"".join(TRACE_PREFIX.encode() + canonical_bytes(v) + b"\n" for v in values)
        + b"TRADEGRAD_RESULT_V1:e30=\n"
    )
    for index in range(0, len(wire), 7):
        mux.feed(wire[index : index + 7])
    assert mux.finish() == b"ordinary log\nTRADEGRAD_RESULT_V1:e30=\n"
    assert receiver.result()["research_capture"]["decisions_saved"] == 1


def test_reader_rechecks_tamper_and_symlinks(tmp_path):
    config, values = frames()
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    for value in values:
        receiver.accept(value)
    path = tmp_path / CAPTURE_FILENAME
    with pytest.raises(ValueError, match="checksum"):
        list(iter_capture_records(path, "0" * 64, path.stat().st_size, config))
    link = tmp_path / "alias.gz"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="non-symlink"):
        list(
            iter_capture_records(
                link,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_size,
                config,
            )
        )


@pytest.mark.parametrize(
    "config",
    [
        CaptureConfig(mode="off"),
        CaptureConfig(mode="summary"),
        CaptureConfig(mode="research", stage_ids=["other"]),
        CaptureConfig(mode="research", instrument_ids=["other"]),
        CaptureConfig(
            mode="research",
            observation_window={"start": "2024-02-01", "end": "2024-02-02"},
        ),
    ],
)
def test_writer_and_hostile_wire_cannot_bypass_declared_collection_scope(
    tmp_path, config
):
    item = record().model_copy(deep=True)
    # Instrument-scoped predicates/features must explicitly match the scope.
    item = item.model_copy(
        update={
            "stages": [
                item.stages[0].model_copy(
                    update={
                        "features": [
                            item.stages[0]
                            .features[0]
                            .model_copy(update={"instrument_id": "ABC"})
                        ]
                    }
                )
            ]
        }
    )
    writer = CaptureWriter(config, lambda value: None)
    with pytest.raises(ValueError):
        writer.append(item)
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    receiver.accept(
        {
            "protocol_version": WIRE_VERSION,
            "kind": "research_start",
            "sequence": 0,
            "capture": config.model_dump(mode="json"),
        }
    )
    compressed = gzip.compress(canonical_bytes(item) + b"\n")
    with pytest.raises(ValueError):
        receiver.accept(
            {
                "protocol_version": WIRE_VERSION,
                "kind": "research_chunk",
                "sequence": 1,
                "data": base64.b64encode(compressed).decode(),
            }
        )
    receiver.abort()


def test_off_summary_rejects_claimed_saved_observations():
    from packages.research_trace.transport import CaptureSummary

    with pytest.raises(ValueError, match="Disabled"):
        CaptureSummary.model_validate(summary(CaptureConfig(mode="off")))


def test_sql_integer_boundary_is_admitted_and_excess_observer_event_is_partial():
    assert (
        DecisionRecord.model_validate(
            {**record().model_dump(mode="json"), "event_seq": 2**63 - 1}
        ).event_seq
        == 2**63 - 1
    )
    value = record().model_dump(mode="json")
    value["event_seq"] = 2**63
    with pytest.raises(ValueError):
        DecisionRecord.model_validate(value)
    observed = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observed.begin_decision(
        data_event_id="event",
        event_seq=2**63,
        scope="portfolio",
        callback_phase="actual",
        decision_time="2024-01-04T21:00:00Z",
        information_cutoff="2024-01-04T21:00:00Z",
        market_timezone="UTC",
    )
    observed.end_decision()
    assert observed.summary()["decisions_saved"] == 0
    assert observed.summary()["coverage"] == "not_collected"
    assert observed.summary()["missing_reasons"]


def test_excess_observed_rank_is_partial_not_complete_or_execution_failure():
    observed = Recorder(code_sha256="a" * 64, config=CaptureConfig(mode="research"))
    observed.begin_decision(
        data_event_id="event",
        event_seq=1,
        scope="portfolio",
        callback_phase="actual",
        decision_time="2024-01-04T21:00:00Z",
        information_cutoff="2024-01-04T21:00:00Z",
        market_timezone="UTC",
    )
    observed.emit(
        "ranking",
        {
            "candidates": [
                {
                    "instrument_id": "B",
                    "universe_member": True,
                    "eligibility": "eligible",
                    "reason": "rank_not_selected",
                    "score": 1,
                    "score_validity": "valid",
                    "rank": 2**63,
                    "ranking_direction": "descending",
                    "tie_break": "listing_id",
                    "selected": False,
                    "prior_holding": False,
                }
            ]
        },
    )
    observed.end_decision()
    assert len(observed.records) == 1 and not observed.records[0].stages
    assert (
        observed.summary()["detail_rows_seen"] == 1
        and observed.summary()["detail_rows_saved"] == 0
    )
    assert observed.summary()["coverage"] == "partial"


@pytest.mark.parametrize("mode", ["research", "off"])
def test_seen_counters_are_not_saved_budgets_and_survive_many_skipped_callbacks(
    tmp_path, mode
):
    config = CaptureConfig(mode=mode, max_decisions=1, max_detail_rows=1)
    receiver = CaptureReceiver(tmp_path, config, source="strategy_emitted")
    writer = CaptureWriter(config, receiver.accept)
    recorder = Recorder(code_sha256="a" * 64, config=config, on_record=writer.append)
    # Exercise the terminal counter boundary without an artificial2Mcallback loop.
    recorder.decisions_seen = 2_000_001
    recorder.detail_rows_seen = 30_000_000
    recorder._failed(
        "Further observations were not saved after declared capacity or disabled capture."
    )
    writer.finish(recorder.summary())
    observed = receiver.result()["research_capture"]
    assert observed["decisions_seen"] == 2_000_001
    assert observed["detail_rows_seen"] == 30_000_000
    assert observed["decisions_saved"] == observed["detail_rows_saved"] == 0
